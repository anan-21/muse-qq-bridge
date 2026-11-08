import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

import broker
from muse_client import Client


OWNER = "12345678"
DSH_TOKEN = "d" * 40
MUSE_TOKEN = "m" * 40


def event(number=1):
    return {"version": 1, "id": "dshm:" + format(number, "064x"), "from": OWNER,
            "conversation": "private:" + OWNER,
            "message": {"id": str(number), "text": "[synthetic handoff]", "time": 123},
            "snapshot": {"history": [{"role": "user", "text": "[synthetic history]"}],
                         "memories": [{"id": "m1", "scope": "chat:private:" + OWNER,
                                       "text": "[synthetic memory]", "kind": "ordinary"}]}}


class MuseClientTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sent = []
        self.store = broker.Store(self.root / "server", OWNER, self.send)
        self.server = broker.make_server("127.0.0.1", 0, self.store, DSH_TOKEN, MUSE_TOKEN)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token_path = self.root / "muse-token"
        self.token_path.write_text(MUSE_TOKEN)
        self.token_path.chmod(0o600)
        self.config = {"owner": OWNER, "baseUrl": "http://127.0.0.1:" + str(self.server.server_port),
                       "stateDir": str(self.root / "client"), "tokenFile": str(self.token_path)}
        self.client = Client(self.config)

    def send(self, owner, text):
        self.sent.append((owner, text))
        return "fixture-receipt"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()
        self.temp.cleanup()

    def test_context_download_integrity_private_cache_and_from(self):
        value = event()
        self.store.enqueue(value)
        result = self.client.poll(now=1000)
        self.assertTrue(result["wake"])
        row = result["data"]["messages"][0]
        self.assertEqual(row["from"], OWNER)
        path = Path(row["context_file"])
        self.assertEqual(json.loads(path.read_text()), value)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(MUSE_TOKEN, json.dumps(result))
        self.assertNotIn("synthetic memory", json.dumps(result))
        self.assertEqual(self.store.status()["pending"], 1)

    def test_pickup_does_not_need_muse_token(self):
        self.token_path.unlink()
        self.store.enqueue(event())
        self.assertTrue(self.client.poll(now=1000)["wake"])

    def test_send_once_receipt_then_ack_and_context_cleanup(self):
        value = event()
        self.store.enqueue(value)
        self.client.poll(now=1000)
        self.client.reply([value["id"]], "[synthetic reply]")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.status()["pending"], 0)
        self.assertFalse(self.client.cache_path(value["id"]).exists())
        with self.assertRaises(broker.Rejected) as missing:
            self.store.context(value["id"])
        self.assertEqual(missing.exception.status, 404)
        self.client.reply([value["id"]], "[synthetic reply]")
        self.assertEqual(len(self.sent), 1)

    def test_unknown_send_stops_poll_and_no_ack(self):
        self.store.enqueue(event())
        def unknown(*_args):
            raise TimeoutError("[fixture]")
        self.store.send = unknown
        with self.assertRaises(Exception):
            self.client.reply([event()["id"]], "[synthetic reply]")
        self.assertEqual(self.store.job(event()["id"])["phase"], "unknown")
        self.assertEqual(self.client.poll(now=1000)["reason"], "needs_reconciliation")

    def test_sent_requires_ack_recovery_not_new_reply(self):
        value = event()
        self.store.enqueue(value)
        self.store.reply({"ids": [value["id"]], "user_id": OWNER, "text": "[reply]"})
        self.assertFalse(self.client.poll(now=1000)["wake"])
        self.client.ack([value["id"]])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.status()["pending"], 0)

    def test_cooldown_new_message_does_not_start_overlapping_consumer(self):
        self.store.enqueue(event())
        self.assertTrue(self.client.poll(now=1000)["wake"])
        self.store.enqueue(event(2))
        self.assertFalse(self.client.poll(now=1001)["wake"])

    def test_dry_run_creates_no_context_or_marker_and_never_wakes(self):
        self.store.enqueue(event())
        self.assertFalse(self.client.poll(now=1000, dry_run=True)["wake"])
        self.assertFalse((self.client.state / "last-wake.json").exists())
        self.assertEqual(list(self.client.contexts.iterdir()), [])

    def test_owner_integrity_and_private_urls(self):
        self.store.enqueue(event())
        self.client.config = {**self.config, "owner": "99999999"}
        with self.assertRaisesRegex(ValueError, "owner_mismatch"):
            self.client.poll(now=1000)
        for url in ("http://example.com:18765", "http://127.0.0.1:18765/path",
                    "http://user:secret@127.0.0.1", "http://192.168.1.2"):
            with self.assertRaisesRegex(ValueError, "private_url_required"):
                Client({**self.config, "baseUrl": url})

    def test_tampered_manifest_fails_without_wake_marker(self):
        value = event()
        self.store.enqueue(value)
        actual = self.client.request
        def altered(path, *args, **kwargs):
            raw = actual(path, *args, **kwargs)
            if path == "/inbox-tailnet":
                result = json.loads(raw)
                result["messages"][0]["handoff"]["context_sha256"] = "0" * 64
                return json.dumps(result).encode()
            return raw
        self.client.request = altered
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.client.poll(now=1000)
        self.assertFalse((self.client.state / "last-wake.json").exists())

    def test_bad_token_permissions_cannot_send(self):
        self.store.enqueue(event())
        self.token_path.chmod(0o644)
        with self.assertRaises(ValueError):
            self.client.reply([event()["id"]], "[reply]")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.job(event()["id"])["phase"], "queued")

    @unittest.skipUnless(shutil.which("bash") and shutil.which("jq"), "hook requires bash and jq")
    def test_real_shell_hook_delivers_current_text_and_private_context_path(self):
        self.store.enqueue(event())
        runtime = self.root / "runtime.sh"
        runtime.write_text('wake() { printf \'%s\\n\' "$2"; }\nsilent() { printf \'{}\\n\'; }\n')
        config_path = self.root / "muse-client.json"
        config_path.write_text(json.dumps(self.config))
        env = {**os.environ, "HATCH_HOOK_RUNTIME": str(runtime),
               "FISH_MUSE_CLIENT_CONFIG": str(config_path)}
        result = subprocess.run(["bash", str(Path(__file__).parent / "muse-inbox.sh")],
                                env=env, capture_output=True, text=True, check=True, timeout=10)
        wake = json.loads(result.stdout)
        self.assertEqual(wake["messages"][0]["from"], OWNER)
        self.assertEqual(wake["messages"][0]["text"], event()["message"]["text"])
        self.assertTrue(Path(wake["messages"][0]["context_file"]).exists())
        self.assertNotIn(MUSE_TOKEN, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
