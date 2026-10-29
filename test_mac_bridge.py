"""HTTP contract tests; uses fake accounts, temporary inboxes and a fake OneBot."""
import concurrent.futures
import contextlib
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import mac_bridge as bridge


class FakeOneBot(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with self.server.requests_lock:
            self.server.requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(raw)})
        body = self.server.reply
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(self.server.reply_status)
        for key, value in self.server.reply_headers.items():
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError):
            self.wfile.write(body)


class BridgeContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.H)
        cls.server.daemon_threads = True
        cls.server.request_queue_size = 32
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.bot = ThreadingHTTPServer(("127.0.0.1", 0), FakeOneBot)
        cls.bot.daemon_threads = True
        cls.bot.requests_lock = threading.Lock()
        cls.bot_thread = threading.Thread(target=cls.bot.serve_forever, daemon=True)
        cls.bot_thread.start()

    @classmethod
    def tearDownClass(cls):
        for server, thread in [(cls.server, cls.thread), (cls.bot, cls.bot_thread)]:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="qq-bridge-contract-")
        self.addCleanup(self.directory.cleanup)
        self.store = Path(self.directory.name) / "inbox.jsonl"
        self.token = "dummy-bridge-secret"
        self.report_secret = "dummy-report-secret"
        self.allowed = "101010"
        self.patchers = [patch.object(bridge, key, value) for key, value in {
            "TOKEN": self.token,
            "ALLOWED": self.allowed,
            "REPORT_SECRET": self.report_secret,
            "ONEBOT_TOKEN": "dummy-onebot-token",
            "ONEBOT": "http://127.0.0.1:" + str(self.bot.server_port),
            "STORE": str(self.store),
            "LOCK": threading.RLock(),
        }.items()]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)
        self.bot.requests = []
        self.bot.reply_status = 200
        self.bot.reply = {"status": "ok", "retcode": 0, "data": {"message_id": 9001}}
        self.bot.reply_headers = {}

    def request(self, method, path, body=None, *, headers=None, raw=None):
        if raw is None and body is not None:
            raw = json.dumps(body, ensure_ascii=False).encode()
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            conn.request(method, path, body=raw, headers=headers or {})
            response = conn.getresponse()
            payload = response.read()
            return response.status, json.loads(payload)
        finally:
            conn.close()

    def auth(self):
        return {"X-Bridge-Token": self.token, "Content-Type": "application/json"}

    def event(self, identifier=1, **overrides):
        event = {"post_type": "message", "message_type": "private", "user_id": int(self.allowed),
                 "self_id": 202020, "message_id": identifier, "raw_message": "fixture text",
                 "message": [{"type": "text", "data": {"text": "fixture text"}}]}
        event.update(overrides)
        return event

    def post_event(self, body, *, signature=True, raw=None):
        if raw is None:
            raw = json.dumps(body, ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json"}
        if signature:
            headers["X-Signature"] = "sha1=" + hmac.new(self.report_secret.encode(), raw, hashlib.sha1).hexdigest()
        return self.request("POST", "/onebot/event", raw=raw, headers=headers)

    def inbox(self):
        status, data = self.request("GET", "/inbox", headers=self.auth())
        self.assertEqual(status, 200)
        return data["messages"]

    def send(self, **overrides):
        body = {"user_id": int(self.allowed), "text": "fixture reply"}
        body.update(overrides)
        return self.request("POST", "/send", body, headers=self.auth())

    def test_health_is_anonymous_and_empty(self):
        self.assertEqual(self.request("GET", "/health"), (200, {"ok": True, "pending": 0}))

    def test_non_health_endpoints_require_bridge_token(self):
        for method, path, body in [("GET", "/inbox", None), ("POST", "/ack", {"ids": []}),
                                   ("POST", "/send", {"user_id": self.allowed, "text": "fixture"})]:
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path, body)[0], 401)
                self.assertEqual(self.request(method, path, body, headers={"X-Bridge-Token": "wrong"})[0], 401)
        self.assertEqual(self.bot.requests, [])

    def test_signed_event_persists_expected_message(self):
        self.assertEqual(self.post_event(self.event())[0], 200)
        rows = self.inbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual({k: rows[0][k] for k in ("id", "from", "text", "acked")},
                         {"id": "1", "from": self.allowed, "text": "fixture text", "acked": False})
        self.assertIsInstance(rows[0]["time"], int)
        self.assertEqual(self.request("GET", "/health")[1]["pending"], 1)
        self.assertEqual(stat.S_IMODE(self.store.stat().st_mode), 0o600)

    def test_unsigned_and_forged_events_are_rejected(self):
        self.assertEqual(self.post_event(self.event(), signature=False)[0], 401)
        raw = json.dumps(self.event()).encode()
        headers = self.auth()
        headers["X-Signature"] = "sha1=" + "0" * 40
        self.assertEqual(self.request("POST", "/onebot/event", raw=raw, headers=headers)[0], 401)
        self.assertEqual(self.inbox(), [])

    def test_signature_covers_exact_bytes_not_parsed_json(self):
        raw = json.dumps(self.event(), separators=(",", ":")).encode()
        wrong_representation = json.dumps(self.event(), indent=2).encode()
        headers = {"X-Signature": "sha1=" + hmac.new(self.report_secret.encode(), wrong_representation, hashlib.sha1).hexdigest()}
        self.assertEqual(self.request("POST", "/onebot/event", raw=raw, headers=headers)[0], 401)
        self.assertEqual(self.post_event(self.event(), raw=raw)[0], 200)
        self.assertEqual(len(self.inbox()), 1)

    def test_missing_report_secret_fails_closed(self):
        with patch.object(bridge, "REPORT_SECRET", ""):
            self.assertEqual(self.post_event(self.event())[0], 401)
        self.assertEqual(self.inbox(), [])

    def test_other_contacts_groups_self_and_non_messages_are_ignored(self):
        cases = [{"user_id": 303030}, {"message_type": "group"},
                 {"self_id": int(self.allowed)}, {"post_type": "message_sent"},
                 {"post_type": "notice"}]
        for i, values in enumerate(cases):
            with self.subTest(values=values):
                self.assertEqual(self.post_event(self.event(i, **values))[0], 200)
        self.assertEqual(self.inbox(), [])

    def test_non_text_or_mixed_segments_and_cq_raw_are_ignored(self):
        cases = [
            {"message": [{"type": "image", "data": {"file": "fixture.png"}}]},
            {"message": [{"type": "text", "data": {"text": "fixture"}}, {"type": "at", "data": {"qq": "all"}}]},
            {"message": [], "raw_message": ""},
            {"message": None, "raw_message": "[CQ:image,file=fixture.png]"},
            {"message": [{"type": "text", "data": {"text": "  "}}]},
        ]
        for i, overrides in enumerate(cases):
            with self.subTest(overrides=overrides):
                self.assertEqual(self.post_event(self.event(i, **overrides))[0], 200)
        self.assertEqual(self.inbox(), [])

    def test_text_array_is_joined_and_plain_raw_is_supported(self):
        body = self.event(message=[{"type": "text", "data": {"text": "hello "}},
                                   {"type": "text", "data": {"text": "世界"}}])
        self.assertEqual(self.post_event(body)[0], 200)
        raw_only = self.event(2)
        raw_only.pop("message")
        raw_only["raw_message"] = "plain raw fixture"
        self.assertEqual(self.post_event(raw_only)[0], 200)
        self.assertEqual([r["text"] for r in self.inbox()], ["hello 世界", "plain raw fixture"])

    def test_duplicate_delivery_does_not_duplicate_inbox_even_after_ack(self):
        for _ in range(2):
            self.assertEqual(self.post_event(self.event())[0], 200)
        self.assertEqual(len(self.inbox()), 1)
        self.assertEqual(self.request("POST", "/ack", {"ids": [1]}, headers=self.auth())[0], 200)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(self.post_event(self.event())[0], 200)
        self.assertEqual(self.inbox(), [])
        self.assertEqual(len(bridge.load()), 1)

    def test_ack_accepts_string_ids_and_is_idempotent(self):
        self.post_event(self.event())
        self.post_event(self.event(2))
        for _ in range(2):
            self.assertEqual(self.request("POST", "/ack", {"ids": ["1", "not-present"]}, headers=self.auth())[0], 200)
        self.assertEqual([r["id"] for r in self.inbox()], ["2"])

    def test_concurrent_distinct_events_and_ack_do_not_lose_updates(self):
        for i in range(5):
            self.post_event(self.event(i))
        def write(i):
            return self.post_event(self.event(i))[0]
        def ack():
            return self.request("POST", "/ack", {"ids": list(range(5))}, headers=self.auth())[0]
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(write, i) for i in range(5, 35)] + [pool.submit(ack)]
            self.assertEqual([f.result() for f in futures], [200] * 31)
        rows = bridge.load()
        self.assertEqual(len(rows), 35)
        self.assertEqual(len({r["id"] for r in rows}), 35)
        self.assertEqual(sum(r["acked"] for r in rows), 5)
        self.assertEqual(self.request("GET", "/health")[1]["pending"], 30)

    def test_concurrent_duplicate_events_stay_single(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(lambda _: self.post_event(self.event())[0], range(20)))
        self.assertEqual(statuses, [200] * 20)
        self.assertEqual(len(self.inbox()), 1)

    def test_invalid_message_id_is_rejected_without_persistence(self):
        for identifier in [None, [], {}, True, ""]:
            with self.subTest(identifier=identifier):
                self.assertEqual(self.post_event(self.event(identifier))[0], 400)
        self.assertEqual(self.inbox(), [])

    def test_json_request_must_be_an_object(self):
        for body in [[], 7, "fixture", None]:
            with self.subTest(body=body):
                self.assertEqual(self.request("POST", "/ack", raw=json.dumps(body).encode(), headers=self.auth())[0], 400)
        self.assertEqual(self.request("POST", "/ack", raw=b"{", headers=self.auth())[0], 400)
        self.assertEqual(self.post_event({}, raw=b"{")[0], 400)

    def test_oversized_requests_are_rejected(self):
        self.assertEqual(self.request("POST", "/ack", raw=b" " * 65537, headers=self.auth())[0], 413)
        self.assertEqual(self.post_event({}, raw=b" " * 65537)[0], 413)
        self.assertEqual(self.inbox(), [])

    def test_missing_invalid_or_chunked_content_length_is_rejected(self):
        cases = [{}, {"Content-Length": "-1"}, {"Content-Length": "invalid"},
                 {"Content-Length": "0", "Transfer-Encoding": "chunked"}]
        for extra in cases:
            with self.subTest(headers=extra):
                conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
                try:
                    conn.putrequest("POST", "/ack")
                    conn.putheader("X-Bridge-Token", self.token)
                    for key, value in extra.items():
                        conn.putheader(key, value)
                    conn.endheaders()
                    response = conn.getresponse()
                    response.read()
                    self.assertEqual(response.status, 400)
                finally:
                    conn.close()

    def test_invalid_ack_ids_are_rejected(self):
        for ids in [None, "1", [None], [{}], [True]]:
            with self.subTest(ids=ids):
                self.assertEqual(self.request("POST", "/ack", {"ids": ids}, headers=self.auth())[0], 400)

    def test_corrupt_store_is_not_overwritten(self):
        raw = b'{"id": "old"}\npartial record\n'
        self.store.write_bytes(raw)
        self.assertEqual(self.request("GET", "/health")[0], 503)
        self.assertEqual(self.post_event(self.event())[0], 503)
        self.assertEqual(self.store.read_bytes(), raw)

    def test_successful_send_requires_receipt_and_preserves_text(self):
        self.assertEqual(self.send(text="literal [CQ:at,qq=all] 世界"),
                         (200, {"ok": True, "onebot_status": 200, "message_id": 9001}))
        self.assertEqual(len(self.bot.requests), 1)
        request = self.bot.requests[0]
        self.assertEqual(request["path"], "/send_private_msg")
        self.assertEqual(request["headers"].get("Authorization"), "Bearer dummy-onebot-token")
        self.assertEqual(request["body"], {"user_id": int(self.allowed),
                                         "message": "literal [CQ:at,qq=all] 世界", "auto_escape": True})

    def test_send_to_other_recipient_is_rejected_before_onebot(self):
        for recipient in [303030, "303030", None, True, {}]:
            with self.subTest(recipient=recipient):
                self.assertEqual(self.send(user_id=recipient)[0], 403)
        self.assertEqual(self.bot.requests, [])

    def test_send_rejects_invalid_text_without_coercion_or_truncation(self):
        for text in [None, 7, {}, "", "  ", "x" * 2001]:
            with self.subTest(text_type=type(text).__name__):
                self.assertEqual(self.send(text=text)[0], 400)
        self.assertEqual(self.bot.requests, [])

    def test_http_200_with_onebot_error_is_not_success_and_is_not_retried(self):
        self.bot.reply = {"status": "failed", "retcode": 100, "message": "dummy rejection"}
        status, data = self.send()
        self.assertEqual(status, 502)
        self.assertIsNot(data.get("ok"), True)
        self.assertFalse(data["retry_safe"])
        self.assertEqual(len(self.bot.requests), 1)
        self.assertEqual(data["onebot_retcode"], 100)

    def test_unconfirmed_onebot_responses_are_unknown_and_not_retried(self):
        replies = [b"not json", [], {"status": "ok", "retcode": False, "data": {"message_id": 5}},
                   {"status": "ok", "retcode": 0}, {"status": "ok", "retcode": 0, "data": {}},
                   {"status": "ok", "retcode": 0, "data": {"message_id": None}}]
        for reply in replies:
            with self.subTest(reply=reply):
                self.bot.reply = reply
                self.bot.requests = []
                status, data = self.send()
                self.assertEqual(status, 502)
                self.assertEqual(data["outcome"], "unknown")
                self.assertFalse(data["retry_safe"])
                self.assertEqual(len(self.bot.requests), 1)

    def test_onebot_http_error_is_not_retried(self):
        self.bot.reply_status = 503
        status, data = self.send()
        self.assertEqual(status, 502)
        self.assertFalse(data["retry_safe"])
        self.assertEqual(len(self.bot.requests), 1)

    def test_onebot_redirect_is_not_followed(self):
        self.bot.reply_status = 307
        self.bot.reply_headers = {"Location": "http://127.0.0.1:" + str(self.bot.server_port) + "/redirected"}
        self.assertEqual(self.send()[0], 502)
        self.assertEqual(len(self.bot.requests), 1)


if __name__ == "__main__":
    unittest.main()
