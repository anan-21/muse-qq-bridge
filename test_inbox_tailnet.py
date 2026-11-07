"""Offline contracts for the peer-restricted, read-only inbox endpoint."""

import http.client
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

import mac_bridge as bridge


class TailnetPeerPolicy(unittest.TestCase):
    def test_only_tailnet_ipv4_and_loopback_peers_are_allowed(self):
        allowed = (
            "100.64.0.0", "100.100.100.100", "100.127.255.255",
            "127.0.0.1", "127.255.255.255", "::1",
            "::ffff:100.64.0.1", "::ffff:127.0.0.1",
        )
        denied = (
            "100.63.255.255", "100.128.0.0", "10.0.0.1", "172.16.0.1",
            "192.168.1.1", "203.0.113.7", "0.0.0.0", "::",
            "2001:db8::1", "fd7a:115c:a1e0::1", "::ffff:203.0.113.7",
            "localhost", "100.64.0.1:1234", "", "invalid-peer",
        )
        for peer in allowed:
            with self.subTest(peer=peer):
                self.assertTrue(bridge._tailnet_peer(peer))
        for peer in denied:
            with self.subTest(peer=peer):
                self.assertFalse(bridge._tailnet_peer(peer))

    def test_denial_happens_before_store_access_even_with_forged_headers(self):
        handler = object.__new__(bridge.H)
        handler.path = "/inbox-tailnet"
        handler.client_address = ("203.0.113.7", 12345)
        handler.headers = {
            "X-Forwarded-For": "100.64.0.1",
            "X-Real-IP": "127.0.0.1",
            "Forwarded": "for=100.64.0.1",
            "X-Bridge-Token": "dummy-token",
        }
        handler._json = Mock()
        with patch.object(bridge, "load") as load, patch.object(bridge, "save") as save:
            handler.do_GET()
        handler._json.assert_called_once_with(403, {"error": "forbidden"})
        load.assert_not_called()
        save.assert_not_called()


class TailnetInboxContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), bridge.H)
        cls.server.daemon_threads = True
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="tailnet-inbox-contract-")
        self.addCleanup(directory.cleanup)
        self.store = Path(directory.name) / "inbox.jsonl"
        for key, value in {
            "STORE": str(self.store),
            "TOKEN": "dummy-token",
            "LOCK": threading.RLock(),
        }.items():
            patcher = patch.object(bridge, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.rows = [
            {"id": "qqc:fixture-pending", "from": "10001", "text": "占位正文\n含引号\"",
             "time": 1700000000, "acked": False},
            {"id": "qqc:fixture-acked", "from": "10001", "text": "old fixture",
             "time": 1699999999, "acked": True},
        ]
        bridge.save(self.rows)

    def request(self, method, path, *, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            payload = json.loads(response.read())
            return response.status, payload
        finally:
            connection.close()

    def test_loopback_get_matches_authenticated_inbox_and_preserves_store(self):
        before = self.store.read_bytes()
        status, result = self.request("GET", "/inbox-tailnet")
        self.assertEqual(status, 200)
        self.assertEqual(result, {"messages": [self.rows[0]]})
        self.assertEqual(
            (status, result),
            self.request("GET", "/inbox", headers={"X-Bridge-Token": "dummy-token"}),
        )
        self.assertEqual(self.store.read_bytes(), before)

    def test_allowed_socket_peer_is_not_overridden_by_forwarding_headers(self):
        status, result = self.request("GET", "/inbox-tailnet", headers={
            "X-Forwarded-For": "203.0.113.7", "Forwarded": "for=203.0.113.7",
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["messages"], [self.rows[0]])

    def test_empty_queue_has_same_messages_envelope(self):
        bridge.save([])
        self.assertEqual(self.request("GET", "/inbox-tailnet"), (200, {"messages": []}))

    def test_unreadable_store_returns_no_contents(self):
        self.store.write_text("invalid JSON\n")
        before = self.store.read_bytes()
        self.assertEqual(self.request("GET", "/inbox-tailnet"),
                         (503, {"error": "inbox unavailable"}))
        self.assertEqual(self.store.read_bytes(), before)

    def test_existing_authenticated_endpoints_still_require_token(self):
        before = self.store.read_bytes()
        for method, path in (("GET", "/inbox"), ("POST", "/send"), ("POST", "/ack")):
            with self.subTest(path=path):
                self.assertEqual(self.request(method, path)[0], 401)
        self.assertEqual(self.request("GET", "/health"), (200, {"ok": True, "pending": 1}))
        self.assertEqual(self.store.read_bytes(), before)

    def test_tailnet_endpoint_does_not_accept_post_or_query_alias(self):
        before = self.store.read_bytes()
        self.assertEqual(self.request("POST", "/inbox-tailnet")[0], 404)
        self.assertEqual(self.request("GET", "/inbox-tailnet?token=unused")[0], 404)
        self.assertEqual(self.store.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
