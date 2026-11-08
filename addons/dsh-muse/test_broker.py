"""Offline broker contract tests. No QQ, model, or live bridge connections."""

import copy
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("dsh_muse_broker_tested", Path(__file__).with_name("broker.py"))
broker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(broker)
OWNER = "10001"
DSH_TOKEN = "synthetic-dsh-test-credential-000000000000"
MUSE_TOKEN = "synthetic-muse-test-credential-00000000000"


def handoff(seed="one"):
    return {
        "version": 1, "id": "dshm:" + hashlib.sha256(seed.encode()).hexdigest(),
        "from": OWNER, "conversation": "private:" + OWNER,
        "message": {"id": "synthetic-" + seed, "text": "Discuss the selected topic.", "time": 1700000000},
        "snapshot": {
            "history": [{"role": "user", "text": "Previous question."},
                        {"role": "assistant", "text": "Previous answer."}],
            "memories": [{"id": "synthetic-memory", "scope": "chat:private:" + OWNER,
                          "text": "Prefers concise answers.", "kind": "preference"}],
            "persona": "Use clear prose.",
        },
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.calls = []
        self.store = broker.Store(self.tmp.name, OWNER, self.send)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def send(self, recipient, text):
        self.calls.append((recipient, text))
        return "synthetic-receipt"

    def rejected(self, status, code, fn, *args):
        with self.assertRaises(broker.Rejected) as caught:
            fn(*args)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))

    def reply(self, job, text="Synthetic reply."):
        return {"user_id": OWNER, "text": text, "ids": [job["id"]]}

    def test_canonical_enqueue_is_idempotent_and_conflict_preserves_original(self):
        job = handoff()
        self.store.enqueue(job)
        self.store.enqueue(dict(reversed(list(job.items()))))
        self.assertEqual(self.store.status()["pending"], 1)
        changed = copy.deepcopy(job)
        changed["message"]["text"] = "A different request."
        self.rejected(409, "id_conflict", self.store.enqueue, changed)
        self.assertEqual(self.store.context(job["id"]), job)
        job["snapshot"]["history"].clear()
        self.assertEqual(len(self.store.context(job["id"])["snapshot"]["history"]), 2)

    def test_scope_and_history_reject_unrelated_or_privileged_data(self):
        for scope in ("", "global", "chat:private:99999", "chat:group:10", "user:99999"):
            with self.subTest(scope=scope):
                job = handoff()
                job["snapshot"]["memories"][0]["scope"] = scope
                self.rejected(403, "invalid_memory_scope", self.store.enqueue, job)
        for role in ("system", "tool", "developer"):
            job = handoff()
            job["snapshot"]["history"][0]["role"] = role
            self.rejected(400, "invalid_history", self.store.enqueue, job)
        job = handoff()
        job["snapshot"]["tools"] = []
        self.rejected(400, "invalid_snapshot", self.store.enqueue, job)
        job = handoff()
        job["snapshot"]["memories"][0]["scope"] = "user:" + OWNER
        self.store.enqueue(job)

    def test_owner_and_conversation_must_both_match(self):
        for field, value in (("from", "99999"), ("conversation", "group:10001")):
            job = handoff()
            job[field] = value
            self.rejected(403, "owner_only", self.store.enqueue, job)
        self.assertEqual(self.store.status()["pending"], 0)

    def test_oversized_context_is_rejected_without_truncation_or_enqueue(self):
        job = handoff()
        job["snapshot"]["history"][0]["text"] = "x" * broker.MAX_BODY
        self.rejected(413, "context_too_large", self.store.enqueue, job)
        self.assertEqual(self.store.inbox(), {"messages": []})

    def test_inbox_context_reference_is_complete_and_read_only(self):
        job = handoff()
        self.store.enqueue(job)
        row = self.store.inbox()["messages"][0]
        self.assertEqual(set(row), {"id", "from", "text", "time", "handoff", "phase"})
        self.assertEqual(row["from"], OWNER)
        packed = broker.canonical(job).encode()
        self.assertEqual(row["handoff"]["context_bytes"], len(packed))
        self.assertEqual(row["handoff"]["context_sha256"], hashlib.sha256(packed).hexdigest())
        self.assertEqual(self.store.context(job["id"]), job)
        self.assertEqual(self.store.job(job["id"])["phase"], "queued")

    def test_send_receipt_required_before_ack_and_duplicate_never_resends(self):
        job = handoff()
        self.store.enqueue(job)
        self.rejected(409, "receipt_required", self.store.ack, {"ids": [job["id"]]})
        response = self.store.reply(self.reply(job))
        self.assertEqual(response["message_id"], "synthetic-receipt")
        self.assertEqual(self.store.status()["pending"], 1)
        self.assertTrue(self.store.reply(self.reply(job))["duplicate"])
        self.store.ack({"ids": [job["id"]]})
        self.store.ack({"ids": [job["id"]]})
        self.assertTrue(self.store.reply(self.reply(job))["duplicate"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.status()["pending"], 0)
        self.rejected(409, "job_not_sendable", self.store.reply, self.reply(job, "Different reply."))

    def test_unknown_outcome_blocks_resend_and_ack(self):
        job = handoff()
        self.store.enqueue(job)
        def unconfirmed(_recipient, _text):
            self.calls.append("attempt")
            raise TimeoutError("synthetic timeout")
        self.store.send = unconfirmed
        self.rejected(502, "send_unknown", self.store.reply, self.reply(job))
        self.rejected(409, "send_unknown", self.store.reply, self.reply(job))
        self.rejected(409, "job_not_sendable", self.store.reply, self.reply(job, "Changed text."))
        self.rejected(409, "receipt_required", self.store.ack, {"ids": [job["id"]]})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.store.status(), {"ok": True, "pending": 1, "unknown": 1})

    def test_invalid_receipts_are_unknown(self):
        for index, receipt in enumerate((None, True, "", "   ", {}, [])):
            job = handoff(str(index))
            self.store.enqueue(job)
            self.store.send = lambda _recipient, _text, value=receipt: value
            self.rejected(502, "send_unknown", self.store.reply, self.reply(job))
            self.assertEqual(self.store.job(job["id"])["phase"], "unknown")

    def test_crash_reservation_becomes_unknown_on_restart(self):
        job = handoff()
        self.store.enqueue(job)
        with self.store.db:
            self.store.db.execute("UPDATE jobs SET phase='sending',reservation='synthetic'")
            self.store.db.execute("INSERT INTO sends VALUES ('synthetic','[]','Synthetic reply.','sending',NULL)")
        self.store.close()
        self.store = broker.Store(self.tmp.name, OWNER, self.send)
        self.assertEqual(self.store.job(job["id"])["phase"], "unknown")
        self.assertEqual(self.store.db.execute("SELECT phase FROM sends").fetchone()[0], "unknown")
        self.rejected(409, "job_not_sendable", self.store.reply, self.reply(job))
        self.assertEqual(self.calls, [])

    def test_same_state_directory_cannot_have_second_writer(self):
        with self.assertRaisesRegex(ValueError, "another broker"):
            broker.Store(self.tmp.name, OWNER, self.send)
        self.store.enqueue(handoff())
        self.assertEqual(self.store.status()["pending"], 1)

    def test_concurrent_reply_reserves_before_network_io(self):
        job = handoff()
        self.store.enqueue(job)
        started, release = threading.Event(), threading.Event()
        result = []
        def blocking_send(recipient, text):
            self.calls.append((recipient, text))
            started.set()
            self.assertTrue(release.wait(5))
            return "synthetic-receipt"
        self.store.send = blocking_send
        worker = threading.Thread(target=lambda: result.append(self.store.reply(self.reply(job))))
        worker.start()
        try:
            self.assertTrue(started.wait(5))
            self.rejected(409, "send_sending", self.store.reply, self.reply(job))
            self.rejected(409, "job_not_sendable", self.store.reply, self.reply(job, "Changed reply."))
            self.rejected(409, "receipt_required", self.store.ack, {"ids": [job["id"]]})
        finally:
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(result[0]["message_id"], "synthetic-receipt")

    def test_ack_batch_is_atomic_and_send_is_owner_only(self):
        first, second = handoff("one"), handoff("two")
        self.store.enqueue(first)
        self.store.enqueue(second)
        self.store.reply(self.reply(first))
        self.rejected(409, "receipt_required", self.store.ack, {"ids": [first["id"], second["id"]]})
        self.assertEqual(self.store.job(first["id"])["phase"], "sent")
        invalid = self.reply(second)
        invalid["user_id"] = "99999"
        self.rejected(403, "owner_only", self.store.reply, invalid)
        for ids in ([], [first["id"], first["id"]], ["invalid"]):
            self.rejected(400, "invalid_ids", self.store.ack, {"ids": ids})


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = broker.Store(self.tmp.name, OWNER, lambda _recipient, _text: "receipt")
        self.server = broker.make_server("127.0.0.1", 0, self.store, DSH_TOKEN, MUSE_TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.job = handoff()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.store.close()
        self.tmp.cleanup()

    def request(self, method, path, token=None, body=None, headers=None):
        request_headers = dict(headers or {})
        if token:
            request_headers["Authorization"] = "Bearer " + token
        encoded = broker.canonical(body).encode() if body is not None else None
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            connection.request(method, path, body=encoded, headers=request_headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read()), dict(response.getheaders())
        finally:
            connection.close()

    def test_distinct_tokens_enforce_route_roles_and_auth_precedes_body_read(self):
        for token in (None, MUSE_TOKEN):
            self.assertEqual(self.request("POST", "/dsh/handoff", token, self.job)[0], 401)
        self.assertEqual(self.request("POST", "/dsh/handoff", DSH_TOKEN, self.job)[0], 200)
        for path in ("/inbox", "/dsh/context/" + self.job["id"]):
            for token in (None, DSH_TOKEN):
                self.assertEqual(self.request("GET", path, token)[0], 401)
            self.assertEqual(self.request("GET", path, MUSE_TOKEN)[0], 200)
        for path in ("/dsh/status", "/dsh/jobs/" + self.job["id"]):
            self.assertEqual(self.request("GET", path, MUSE_TOKEN)[0], 401)
            self.assertEqual(self.request("GET", path, DSH_TOKEN)[0], 200)
        for path in ("/send", "/ack"):
            self.assertEqual(self.request("POST", path, DSH_TOKEN, {})[0], 401)
        self.assertEqual(self.request("POST", "/dsh/handoff", headers={"Content-Length": str(broker.MAX_BODY + 1)})[0], 401)

    def test_health_has_no_message_contents_and_responses_disable_cache(self):
        self.store.enqueue(self.job)
        status, body, headers = self.request("GET", "/health")
        self.assertEqual((status, body), (200, {"ok": True, "pending": 1, "unknown": 0}))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn(self.job["message"]["text"], json.dumps(body))

    def test_loopback_tailnet_read_only_routes_and_percent_encoded_id(self):
        self.store.enqueue(self.job)
        self.assertEqual(self.request("GET", "/inbox-tailnet")[0], 200)
        encoded_id = self.job["id"].replace(":", "%3A")
        self.assertEqual(self.request("GET", "/dsh/context-tailnet/" + encoded_id)[1], self.job)
        self.assertEqual(self.request("POST", "/inbox-tailnet", body={})[0], 404)
        self.assertEqual(self.request("POST", "/dsh/context-tailnet/" + encoded_id, body={})[0], 404)
        self.assertEqual(self.store.job(self.job["id"])["phase"], "queued")

    def test_tailnet_gate_uses_real_peer_and_rejects_before_reading_store(self):
        self.store.enqueue(self.job)
        forged = {"X-Forwarded-For": "100.100.100.100", "Forwarded": "for=100.100.100.100", "X-Real-IP": "100.100.100.100"}
        with patch.object(broker, "private_peer", return_value=False) as peer, \
                patch.object(self.store, "inbox", wraps=self.store.inbox) as inbox, \
                patch.object(self.store, "context", wraps=self.store.context) as context:
            for path in ("/inbox-tailnet", "/dsh/context-tailnet/" + self.job["id"]):
                self.assertEqual(self.request("GET", path, headers=forged)[:2],
                                 (403, {"ok": False, "error": "tailnet_only"}))
            self.assertEqual([call.args for call in peer.call_args_list], [("127.0.0.1",), ("127.0.0.1",)])
            inbox.assert_not_called()
            context.assert_not_called()

    def test_request_framing_rejects_chunked_duplicate_or_oversized_length(self):
        cases = [([("Transfer-Encoding", "chunked")], "0\r\n\r\n", 400),
                 ([("Content-Length", "2"), ("Content-Length", "2")], "{}", 400),
                 ([("Content-Length", str(broker.MAX_BODY + 1))], "", 413),
                 ([("Content-Length", "no")], "", 400)]
        for headers, raw, expected in cases:
            with self.subTest(headers=headers):
                connection = http.client.HTTPConnection(*self.server.server_address, timeout=3)
                try:
                    connection.putrequest("POST", "/dsh/handoff")
                    connection.putheader("Authorization", "Bearer " + DSH_TOKEN)
                    for name, value in headers:
                        connection.putheader(name, value)
                    connection.endheaders(raw.encode())
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                finally:
                    connection.close()
        self.assertEqual(self.store.status()["pending"], 0)


class CredentialAndPeerTests(unittest.TestCase):
    def test_private_peer_exact_ranges(self):
        for address in ("127.0.0.1", "::1", "100.64.0.0", "100.127.255.255", "::ffff:100.100.1.1"):
            self.assertTrue(broker.private_peer(address), address)
        for address in ("100.63.255.255", "100.128.0.0", "192.168.1.1", "10.0.0.1", "8.8.8.8", "localhost", "100.100.1.1:80"):
            self.assertFalse(broker.private_peer(address), address)

    def test_credentials_require_private_regular_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "credential"
            path.write_text("synthetic-credential-00000000000000000")
            path.chmod(0o600)
            self.assertTrue(broker.private_file(path).startswith("synthetic-"))
            path.chmod(0o640)
            with self.assertRaises(ValueError):
                broker.private_file(path)
            path.chmod(0o600)
            link = Path(directory) / "symlink"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                broker.private_file(link)
            for value in ("short", "x" * 4097, "x" * 32 + "\n" + "y" * 32):
                path.write_text(value)
                with self.assertRaises(ValueError):
                    broker.private_file(path)

    def test_state_directory_rejects_shared_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state"
            path.mkdir(mode=0o755)
            path.chmod(0o755)
            with self.assertRaises(ValueError):
                broker.Store(path, OWNER, lambda *_args: "receipt")
            path.chmod(0o700)
            link = Path(directory) / "linked"
            link.symlink_to(path, target_is_directory=True)
            with self.assertRaises(ValueError):
                broker.Store(link, OWNER, lambda *_args: "receipt")


class OneBotTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.response = {"status": "ok", "retcode": 0, "data": {"message_id": "synthetic-receipt"}}
        self.extra_headers = {}
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                outer.requests.append((self.path, dict(self.headers), body))
                raw = outer.response if isinstance(outer.response, bytes) else json.dumps(outer.response).encode()
                self.send_response(outer.status)
                for name, value in outer.extra_headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.url = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def test_valid_send_uses_one_request_and_owner_only_body(self):
        self.assertEqual(broker.onebot_send(self.url, MUSE_TOKEN, OWNER, "Synthetic reply."), "synthetic-receipt")
        self.assertEqual(len(self.requests), 1)
        path, headers, body = self.requests[0]
        self.assertEqual(path, "/send_private_msg")
        self.assertEqual(headers["Authorization"], "Bearer " + MUSE_TOKEN)
        self.assertEqual(json.loads(body), {"user_id": int(OWNER), "message": "Synthetic reply.", "auto_escape": True})

    def test_invalid_receipt_or_http_response_never_retries(self):
        cases = [(200, {"status": "ok", "retcode": "0", "data": {"message_id": 1}}),
                 (200, {"status": "ok", "retcode": False, "data": {"message_id": 1}}),
                 (200, {"status": "failed", "retcode": 0, "data": {"message_id": 1}}),
                 (200, {"status": "ok", "retcode": 0, "data": {"message_id": True}}),
                 (200, {"status": "ok", "retcode": 0, "data": {}}),
                 (200, b"not-json"), (200, b"x" * 65537), (503, b"unavailable")]
        for status, body in cases:
            with self.subTest(status=status, body=str(body)[:70]):
                self.status, self.response = status, body
                before = len(self.requests)
                with self.assertRaises(Exception):
                    broker.onebot_send(self.url, MUSE_TOKEN, OWNER, "Synthetic reply.")
                self.assertEqual(len(self.requests), before + 1)

    def test_redirect_is_not_followed(self):
        self.status, self.response = 302, b"redirect"
        self.extra_headers = {"Location": self.url + "/redirect-target"}
        with self.assertRaises(Exception):
            broker.onebot_send(self.url, MUSE_TOKEN, OWNER, "Synthetic reply.")
        self.assertEqual([row[0] for row in self.requests], ["/send_private_msg"])

    def test_inherited_proxy_is_not_used(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:1", "http_proxy": "http://127.0.0.1:1",
                                     "ALL_PROXY": "http://127.0.0.1:1", "NO_PROXY": "", "no_proxy": ""}):
            self.assertEqual(broker.onebot_send(self.url, MUSE_TOKEN, OWNER, "Synthetic reply."), "synthetic-receipt")
        self.assertEqual(len(self.requests), 1)

    def test_nonliteral_or_nonloopback_targets_are_rejected_before_io(self):
        for url in ("http://localhost:3000", "http://8.8.8.8:3000", "https://127.0.0.1:3000",
                    self.url + "/prefix", self.url + "?q=1", self.url + "#fragment",
                    self.url.replace("http://", "http://user:password@")):
            with self.subTest(url=url), self.assertRaises(ValueError):
                broker.onebot_send(url, MUSE_TOKEN, OWNER, "Synthetic reply.")
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
