"""Public Muse helper contracts with isolated state and a loopback fake bridge."""
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest


SCRIPTS = Path(__file__).resolve().parent / "muse"


class FakeBridge(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def respond(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        with self.server.lock:
            self.server.requests.append({
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers),
                "body": json.loads(raw) if raw else None,
            })
            status, body = self.server.responses[self.path]
        if body is None:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class MuseHelperContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in ("bash", "curl", "jq", "python3"):
            if not shutil.which(tool):
                raise RuntimeError(f"required helper test tool is missing: {tool}")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeBridge)
        cls.server.daemon_threads = True
        cls.server.lock = threading.Lock()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="muse-helper-contract-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.state_dir = self.directory / "state"
        self.state_file = self.state_dir / "qq-bridge-inbox.last_wake"
        self.log_file = self.directory / "logs" / "timing.log"
        self.token = "dummy-private-bridge-token"
        self.token_file = self.directory / "token.txt"
        self.token_file.write_text(self.token + "\r\n", encoding="utf-8")
        self.token_file.chmod(0o600)
        self.runtime = self.directory / "hook-runtime.sh"
        self.runtime.write_text(
            "wake() { printf '{\"decision\":\"wake\",\"data\":%s}\\n' \"$2\"; }\n"
            "silent() { printf '{\"decision\":\"silent\",\"data\":%s}\\n' \"$2\"; }\n"
            "log() { :; }\n",
            encoding="utf-8",
        )
        self.env = os.environ.copy()
        for key in tuple(self.env):
            if key.lower().endswith("_proxy") or key.startswith("QQ_BRIDGE_"):
                self.env.pop(key)
        self.env.update({
            "QQ_BRIDGE_BASE_URL": f"http://127.0.0.1:{self.server.server_port}",
            "QQ_BRIDGE_NO_PROXY": "1",
            "QQ_BRIDGE_STATE_DIR": str(self.state_dir),
            "QQ_BRIDGE_TOKEN_FILE": str(self.token_file),
            "QQ_BRIDGE_REPLY_LOG": str(self.log_file),
            "HATCH_HOOK_RUNTIME": str(self.runtime),
        })
        self.env.pop("HATCH_HOOK_DRY_RUN", None)
        self.messages = [{"id": "qqc:fixture-1", "from": "10001", "text": "fixture", "time": 1}]
        with self.server.lock:
            self.server.requests = []
            self.server.responses = {
                "/health": (200, {"ok": True, "pending": 1}),
                "/inbox-tailnet": (200, {"messages": self.messages}),
                "/send": (200, {"ok": True, "message_id": "fixture-receipt"}),
                "/ack": (200, {"ok": True}),
            }

    def response(self, path, status, body):
        with self.server.lock:
            self.server.responses[path] = status, body

    def requests(self):
        with self.server.lock:
            return list(self.server.requests)

    def run_script(self, name, args=(), text="", env=None):
        return subprocess.run(
            ["bash", str(SCRIPTS / name), *args],
            input=text, text=True, capture_output=True,
            env=self.env if env is None else env, timeout=12,
        )

    def hook(self, env=None):
        result = self.run_script("qq-bridge-inbox.sh", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def reply(self, ids="qqc:fixture-1", text="fixture reply", *, user="10001", env=None):
        return self.run_script("qq-bridge-reply.sh", (user, ids), text, env)

    def assert_reply(self, result, output, code):
        self.assertEqual(result.stdout, output + "\n")
        self.assertEqual(result.returncode, code, result.stderr)
        self.assertEqual(result.stderr, "")

    def events(self):
        return [json.loads(line) for line in self.log_file.read_text(encoding="utf-8").splitlines()]

    def test_pending_zero_is_silent_and_does_not_fetch_body(self):
        self.response("/health", 200, {"ok": True, "pending": 0})
        self.assertEqual(self.hook(), {"decision": "silent", "data": {"pending": 0}})
        self.assertEqual([r["path"] for r in self.requests()], ["/health"])
        self.assertFalse(self.state_file.exists())

    def test_feed_preserves_batch_fields_and_json_text_without_token(self):
        rows = [
            {"id": "qqc:first", "from": "10001", "text": '你好 "桥"\n$(exit 9)', "time": 1},
            {"id": 42, "from": 10001, "text": "second", "time": "fixture-time"},
        ]
        self.response("/health", 200, {"ok": True, "pending": 2})
        self.response("/inbox-tailnet", 200, {"messages": rows})
        result = self.hook()
        expected = [rows[0], {**rows[1], "id": "42", "from": "10001"}]
        self.assertEqual(result, {"decision": "wake", "data": {"pending": 2, "messages": expected}})
        self.assertEqual([r["path"] for r in self.requests()], ["/health", "/inbox-tailnet"])
        for request in self.requests():
            self.assertNotIn("X-Bridge-Token", request["headers"])
            self.assertNotIn("Authorization", request["headers"])
        self.assertEqual(stat.S_IMODE(self.state_file.stat().st_mode), 0o600)

    def test_feed_errors_fall_back_to_pending_only(self):
        bad_feeds = [
            (500, {"error": "fixture failure"}),
            (403, {"error": "forbidden"}),
            (200, b"not-json"),
            (200, {"messages": [{"id": "qqc:one", "from": "10001"}]}),
            (200, {"messages": []}),
            (200, {"messages": self.messages * 11}),
        ]
        for status, body in bad_feeds:
            with self.subTest(status=status, body=body):
                self.state_file.unlink(missing_ok=True)
                self.response("/inbox-tailnet", status, body)
                self.assertEqual(self.hook(), {"decision": "wake", "data": {"pending": 1}})
        self.assertTrue(all(r["method"] == "GET" for r in self.requests()))

    def test_cooldown_suppresses_fetch_and_zero_resets_it(self):
        self.assertEqual(self.hook()["decision"], "wake")
        original_state = self.state_file.read_bytes()
        before = len(self.requests())
        self.assertEqual(self.hook()["decision"], "silent")
        self.assertEqual([r["path"] for r in self.requests()[before:]], ["/health"])
        self.assertEqual(self.state_file.read_bytes(), original_state)
        self.response("/health", 200, {"ok": True, "pending": 0})
        self.assertEqual(self.hook()["decision"], "silent")
        self.assertFalse(self.state_file.exists())
        self.response("/health", 200, {"ok": True, "pending": 1})
        self.assertEqual(self.hook()["decision"], "wake")

    def test_dry_run_never_changes_existing_or_absent_wake_state(self):
        env = {**self.env, "HATCH_HOOK_DRY_RUN": "1"}
        self.assertEqual(self.hook(env)["decision"], "wake")
        self.assertFalse(self.state_file.exists())
        self.state_dir.mkdir(exist_ok=True)
        self.state_file.write_text(str(int(time.time())), encoding="utf-8")
        original = self.state_file.read_bytes()
        self.assertEqual(self.hook(env)["decision"], "silent")
        self.response("/health", 200, {"ok": True, "pending": 0})
        self.assertEqual(self.hook(env)["decision"], "silent")
        self.assertEqual(self.state_file.read_bytes(), original)

    def test_invalid_health_preserves_cooldown_and_never_fetches_body(self):
        self.state_dir.mkdir()
        self.state_file.write_text("1234567890", encoding="utf-8")
        for body in (b"not-json", {}, {"ok": False, "pending": 0},
                     {"ok": True, "pending": -1}, {"ok": True, "pending": 1.5}):
            with self.subTest(body=body):
                self.response("/health", 200, body)
                self.assertEqual(self.hook()["decision"], "silent")
                self.assertEqual(self.state_file.read_text(), "1234567890")
        self.assertTrue(all(r["path"] == "/health" for r in self.requests()))

    def test_reply_sends_then_acks_once_and_records_private_utc_events(self):
        text = '你好 "world"\nnext line $HOME \\'
        ids = "qqc:first,42,id_03-X"
        self.assert_reply(self.reply(ids, text), "ok", 0)
        requests = self.requests()
        self.assertEqual([r["path"] for r in requests], ["/send", "/ack"])
        self.assertEqual(requests[0]["body"], {"user_id": "10001", "text": text})
        self.assertEqual(requests[1]["body"], {"ids": ids.split(",")})
        self.assertTrue(all(r["headers"]["X-Bridge-Token"] == self.token for r in requests))
        events = self.events()
        self.assertEqual([e["event"] for e in events], ["helper_start", "send_done", "ack_done"])
        self.assertEqual([e["result"] for e in events], ["start", "ok", "ok"])
        for event in events:
            self.assertEqual(set(event), {"ts", "event", "ids", "result"})
            self.assertEqual(event["ids"], ids)
            self.assertRegex(event["ts"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
            self.assertIsNotNone(datetime.fromisoformat(event["ts"].replace("Z", "+00:00")).tzinfo)
        content = self.log_file.read_text()
        self.assertNotIn(self.token, content)
        self.assertNotIn("world", content)
        self.assertNotIn("10001", content)
        self.assertEqual(stat.S_IMODE(self.log_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.log_file.parent.stat().st_mode), 0o700)

    def test_send_http_error_has_no_ack_or_retry(self):
        self.response("/send", 500, {"ok": False})
        self.assert_reply(self.reply(), "fail:send", 1)
        self.assertEqual([r["path"] for r in self.requests()], ["/send"])
        self.assertEqual([e["event"] for e in self.events()], ["helper_start", "send_done"])
        self.assertEqual(self.events()[-1]["result"], "http_500")

    def test_unconfirmed_send_responses_never_ack(self):
        for body in ({"ok": True}, {"ok": True, "message_id": ""},
                     {"ok": True, "message_id": True}, {"ok": True, "message_id": 1.5},
                     {"ok": True, "message_id": "x" * 129},
                     {"ok": False, "message_id": "receipt"}, b"not-json"):
            with self.subTest(body=body):
                with self.server.lock:
                    self.server.requests.clear()
                self.response("/send", 200, body)
                self.assert_reply(self.reply(), "fail:send", 1)
                self.assertEqual([r["path"] for r in self.requests()], ["/send"])

    def test_connection_loss_is_unknown_send_without_retry(self):
        self.response("/send", 200, None)
        self.assert_reply(self.reply(), "fail:send", 1)
        self.assertEqual([r["path"] for r in self.requests()], ["/send"])
        self.assertEqual(self.events()[-1]["result"], "curl_fail")

    def test_ack_failure_does_not_repeat_confirmed_send(self):
        self.response("/ack", 500, {"ok": False})
        self.assert_reply(self.reply(), "fail:ack", 1)
        self.assertEqual([r["path"] for r in self.requests()], ["/send", "/ack"])
        self.assertEqual(self.events()[-1]["event"], "ack_done")
        self.assertEqual(self.events()[-1]["result"], "http_500")

    def test_malformed_ack_still_reports_ack_failure(self):
        self.response("/ack", 200, b"not-json")
        self.assert_reply(self.reply(), "fail:ack", 1)
        self.assertEqual([r["path"] for r in self.requests()], ["/send", "/ack"])
        self.assertEqual(self.events()[-1]["result"], "resp_not_ok")

    def test_invalid_arguments_fail_before_any_send(self):
        for ids in ("", ",", ",qqc:a", "qqc:a,", "qqc:a,,qqc:b", "x" * 129, "qqc:a;exit"):
            with self.subTest(ids=ids):
                self.assert_reply(self.reply(ids), "fail:args", 1)
        self.assert_reply(self.reply(user="invalid"), "fail:args", 1)
        self.assert_reply(self.run_script("qq-bridge-reply.sh", ("10001",)), "fail:args", 1)
        self.assertEqual(self.requests(), [])

    def test_missing_or_empty_token_and_empty_text_never_send(self):
        self.token_file.unlink()
        self.assert_reply(self.reply(), "fail:token", 1)
        self.token_file.write_text("\r\n", encoding="utf-8")
        self.assert_reply(self.reply(), "fail:token", 1)
        self.assert_reply(self.reply(text="\n"), "fail:empty", 1)
        self.assertEqual(self.requests(), [])

    def test_unwritable_log_does_not_change_success_protocol(self):
        blocked_parent = self.directory / "not-a-directory"
        blocked_parent.write_text("fixture", encoding="utf-8")
        env = {**self.env, "QQ_BRIDGE_REPLY_LOG": str(blocked_parent / "timing.log")}
        self.assert_reply(self.reply(env=env), "ok", 0)
        self.assertEqual([r["path"] for r in self.requests()], ["/send", "/ack"])

    def test_force_direct_bypasses_explicit_and_inherited_proxies(self):
        env = {**self.env, "QQ_BRIDGE_PROXY": "http://127.0.0.1:1",
               "http_proxy": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
               "ALL_PROXY": "http://127.0.0.1:1", "no_proxy": "", "NO_PROXY": ""}
        self.assertEqual(self.hook(env)["decision"], "wake")
        self.assert_reply(self.reply(env=env), "ok", 0)
        self.assertEqual([r["path"] for r in self.requests()],
                         ["/health", "/inbox-tailnet", "/send", "/ack"])

    def test_helpers_work_without_explicit_proxy_arguments(self):
        env = dict(self.env)
        env.pop("QQ_BRIDGE_NO_PROXY")
        self.response("/send", 200, {"ok": True, "message_id": 9001})
        self.assertEqual(self.hook(env)["decision"], "wake")
        self.assert_reply(self.reply(env=env), "ok", 0)


if __name__ == "__main__":
    unittest.main()
