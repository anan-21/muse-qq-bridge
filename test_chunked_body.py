"""HTTP integration tests for bounded chunked events, using dummy data only."""

import contextlib
import hashlib
import hmac
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import mac_bridge as bridge


class ChunkedEventContracts(unittest.TestCase):
    """Exercise the public HTTP interface; do not call the body reader directly."""

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
        directory = tempfile.TemporaryDirectory(prefix="qq-chunked-http-test-")
        self.addCleanup(directory.cleanup)
        self.store = Path(directory.name) / "dummy-inbox.jsonl"
        self.allowed = "101010"
        self.report_secret = "dummy-chunked-report-secret"
        self.bridge_token = "dummy-chunked-reader-token"
        values = {
            "TOKEN": self.bridge_token,
            "ALLOWED": self.allowed,
            "REPORT_SECRET": self.report_secret,
            "ONEBOT_TOKEN": "dummy-unused-onebot-token",
            "ONEBOT": "http://127.0.0.1:1",
            "STORE": str(self.store),
            "LOCK": threading.RLock(),
        }
        for key, value in values.items():
            patcher = patch.object(bridge, key, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def event_bytes(self, *, text="分块雪🙂 fixture", user_id=101010, identifier=1):
        event = {
            "post_type": "message",
            "message_type": "private",
            "user_id": user_id,
            "self_id": 202020,
            "message_id": identifier,
            "message": [{"type": "text", "data": {"text": text}}],
        }
        # Legal whitespace and literal UTF-8 ensure that JSON reserialization
        # would change the HMAC input even when the parsed object is identical.
        return b" \n" + json.dumps(event, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"

    def signature(self, raw):
        return "sha1=" + hmac.new(
            self.report_secret.encode(), raw, hashlib.sha1
        ).hexdigest()

    @staticmethod
    def framed(chunks, *, trailer=b"", line_width=None):
        wire = bytearray()
        for chunk in chunks:
            if not chunk:
                raise ValueError("test fixture contains an empty data chunk")
            size = format(len(chunk), "X").encode("ascii")
            if line_width is not None:
                size = size.rjust(line_width, b"0")
            wire.extend(size + b"\r\n" + chunk + b"\r\n")
        wire.extend(b"0\r\n" + trailer + b"\r\n")
        return bytes(wire)

    def headers_bytes(self, headers, *, path="/onebot/event"):
        lines = [
            f"POST {path} HTTP/1.1",
            f"Host: 127.0.0.1:{self.server.server_port}",
            "Connection: close",
        ]
        lines.extend(f"{name}: {value}" for name, value in headers)
        return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")

    def raw_request(self, wire, headers, *, path="/onebot/event"):
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5) as sock:
            sock.settimeout(12)
            sock.sendall(self.headers_bytes(headers, path=path) + wire)
            # EOF makes a truncated body fail promptly instead of waiting for
            # the production timeout; the response side remains readable.
            sock.shutdown(socket.SHUT_WR)
            response = http.client.HTTPResponse(sock)
            response.begin()
            payload = response.read()
            return response.status, json.loads(payload)

    def post_chunked(self, raw, *, chunks=None, wire=None, headers=None, signature=None,
                     path="/onebot/event"):
        if wire is None:
            wire = self.framed(chunks or [raw])
        if headers is None:
            headers = [("Transfer-Encoding", "chunked")]
        headers = list(headers) + [
            ("Content-Type", "application/json"),
            ("X-Signature", self.signature(raw) if signature is None else signature),
        ]
        return self.raw_request(wire, headers, path=path)

    def inbox(self):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_port, timeout=5
        )
        try:
            connection.request("GET", "/inbox", headers={"X-Bridge-Token": self.bridge_token})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            return json.loads(response.read())["messages"]
        finally:
            connection.close()

    def assert_empty(self):
        self.assertEqual(self.inbox(), [])
        self.assertFalse(self.store.exists(), "rejected or ignored events must not create the store")

    def test_unicode_split_inside_utf8_and_whitespace_preserves_hmac(self):
        raw = self.event_bytes()
        snow = raw.index("雪".encode("utf-8"))
        chunks = [raw[:11], raw[11:snow + 1], raw[snow + 1:snow + 2], raw[snow + 2:]]
        self.assertEqual(self.post_chunked(raw, chunks=chunks), (200, {"ok": True}))
        rows = self.inbox()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            {key: rows[0][key] for key in ("id", "from", "text", "acked")},
            {"id": "1", "from": self.allowed, "text": "分块雪🙂 fixture", "acked": False},
        )

    def test_signature_for_equivalent_reserialized_json_is_rejected(self):
        raw = self.event_bytes()
        equivalent = json.dumps(json.loads(raw), ensure_ascii=False, separators=(",", ":")).encode()
        self.assertNotEqual(raw, equivalent)
        self.assertEqual(self.post_chunked(raw, signature=self.signature(equivalent))[0], 401)
        self.assert_empty()

    def test_plain_content_length_event_regression(self):
        raw = self.event_bytes(text="ordinary CL fixture")
        status, payload = self.raw_request(raw, [
            ("Content-Length", str(len(raw))),
            ("Content-Type", "application/json"),
            ("X-Signature", self.signature(raw)),
        ])
        self.assertEqual((status, payload), (200, {"ok": True}))
        self.assertEqual([row["text"] for row in self.inbox()], ["ordinary CL fixture"])

    def test_signed_non_whitelisted_chunked_event_is_ignored(self):
        raw = self.event_bytes(user_id=303030)
        self.assertEqual(self.post_chunked(raw), (200, {"ok": True}))
        self.assert_empty()

    def test_bad_signature_is_401_without_persisting(self):
        raw = self.event_bytes()
        self.assertEqual(self.post_chunked(raw, signature="sha1=" + "0" * 40)[0], 401)
        self.assert_empty()

    def test_duplicate_chunked_delivery_remains_idempotent(self):
        raw = self.event_bytes()
        self.assertEqual(self.post_chunked(raw)[0], 200)
        self.assertEqual(self.post_chunked(raw, chunks=[raw[:1], raw[1:]])[0], 200)
        self.assertEqual(len(self.inbox()), 1)

    def test_exact_decoded_body_limit_is_accepted(self):
        base = self.event_bytes(text="")
        raw = self.event_bytes(text="x" * (65536 - len(base)))
        self.assertEqual(len(raw), 65536)
        self.assertEqual(self.post_chunked(raw, chunks=[raw[:32768], raw[32768:]])[0], 200)
        self.assertEqual(len(self.inbox()), 1)

    def test_decoded_body_one_byte_over_limit_is_413(self):
        base = self.event_bytes(text="")
        raw = self.event_bytes(text="x" * (65537 - len(base)))
        self.assertEqual(len(raw), 65537)
        self.assertEqual(self.post_chunked(raw, chunks=[raw[:32768], raw[32768:]])[0], 413)
        self.assert_empty()

    def test_declared_oversize_chunk_rejected_before_body(self):
        raw = self.event_bytes()
        self.assertEqual(self.post_chunked(raw, wire=b"10001\r\n")[0], 413)
        self.assert_empty()

    def test_ambiguous_and_unknown_transfer_headers_are_400(self):
        raw = self.event_bytes()
        cases = [
            [("Transfer-Encoding", "chunked"), ("Content-Length", str(len(raw)))],
            [("Transfer-Encoding", "chunked"), ("Content-Length", "")],
            [("Transfer-Encoding", "chunked"), ("Transfer-Encoding", "chunked")],
            [("Transfer-Encoding", "gzip"), ("Transfer-Encoding", "chunked")],
            [("Transfer-Encoding", "gzip")],
            [("Transfer-Encoding", "gzip, chunked")],
            [("Transfer-Encoding", "chunked, gzip")],
        ]
        for index, headers in enumerate(cases):
            with self.subTest(case=index):
                self.assertEqual(self.post_chunked(raw, headers=headers)[0], 400)
                self.assert_empty()

    def test_case_insensitive_chunked_header_with_ows(self):
        raw = self.event_bytes()
        self.assertEqual(self.post_chunked(raw, headers=[("transfer-encoding", "  ChUnKeD  ")])[0], 200)
        self.assertEqual(len(self.inbox()), 1)

    def test_invalid_hex_and_chunk_extensions_are_400(self):
        raw = self.event_bytes()
        for size_line in (b"zz", b"+1", b"-1", b"0x1", b" 1", b"1 ", b"1;x=y"):
            with self.subTest(size_line=size_line.decode("ascii")):
                wire = size_line + b"\r\nx\r\n0\r\n\r\n"
                self.assertEqual(self.post_chunked(raw, wire=wire)[0], 400)
                self.assert_empty()

    def test_invalid_crlf_and_truncated_framing_are_400(self):
        raw = self.event_bytes()
        size = format(len(raw), "x").encode()
        cases = [
            b"1\nx\r\n0\r\n\r\n",  # LF-only size line.
            b"1\r\nxXX0\r\n\r\n",  # Invalid data terminator.
            size + b"\r\n" + raw,  # No data CRLF or zero chunk.
            size + b"\r\n" + raw + b"\r\n",  # Missing zero chunk.
            size + b"\r\n" + raw + b"\r\n0\r\n",  # Missing final CRLF.
            format(len(raw) + 8, "x").encode() + b"\r\n" + raw,  # Truncated data.
        ]
        for index, wire in enumerate(cases):
            with self.subTest(case=index):
                self.assertEqual(self.post_chunked(raw, wire=wire)[0], 400)
                self.assert_empty()

    def test_trailers_are_rejected_even_with_valid_body_and_hmac(self):
        raw = self.event_bytes()
        wire = self.framed([raw], trailer=b"X-Fixture-Trailer: not-supported\r\n")
        self.assertEqual(self.post_chunked(raw, wire=wire)[0], 400)
        self.assert_empty()

    def test_empty_decoded_body_is_not_a_json_event(self):
        self.assertEqual(self.post_chunked(b"", wire=b"0\r\n\r\n")[0], 400)
        self.assert_empty()

    def test_overlong_chunk_size_line_is_400(self):
        raw = self.event_bytes()
        self.assertEqual(self.post_chunked(raw, wire=b"0" * 129 + b"\r\n\r\n")[0], 400)
        self.assert_empty()

    def test_chunk_size_line_at_128_byte_limit_is_accepted(self):
        raw = self.event_bytes()
        # 126 ASCII hex characters plus CRLF meet the inclusive line budget.
        self.assertEqual(self.post_chunked(raw, wire=self.framed([raw], line_width=126))[0], 200)
        self.assertEqual(len(self.inbox()), 1)

    def test_more_than_1024_data_chunks_is_400(self):
        raw = self.event_bytes(text="x" * 2000)
        chunks = [raw[index:index + 1] for index in range(1024)] + [raw[1024:]]
        self.assertEqual(len(chunks), 1025)
        self.assertEqual(self.post_chunked(raw, chunks=chunks)[0], 400)
        self.assert_empty()

    def test_1024_small_data_chunks_are_accepted(self):
        raw = self.event_bytes(text="x" * 2000)
        chunks = [raw[index:index + 1] for index in range(1023)] + [raw[1023:]]
        self.assertEqual(len(chunks), 1024)
        self.assertEqual(self.post_chunked(raw, chunks=chunks)[0], 200)
        self.assertEqual(len(self.inbox()), 1)

    def test_chunk_metadata_overhead_limit_is_400(self):
        raw = self.event_bytes(text="x" * 1000)
        # Each size line is below 128 bytes, the data chunk count is below
        # 1024 and the decoded body is small; only metadata exceeds 65536.
        step = len(raw) // 530
        chunks = [raw[index * step:(index + 1) * step] for index in range(529)]
        chunks.append(raw[529 * step:])
        wire = self.framed(chunks, line_width=124)
        self.assertGreater(len(wire) - len(raw), 65536)
        self.assertEqual(self.post_chunked(raw, wire=wire)[0], 400)
        self.assert_empty()

    def test_chunked_is_not_enabled_for_other_routes(self):
        raw = b'{"ids":[]}'
        status, _ = self.post_chunked(raw, path="/ack", headers=[
            ("Transfer-Encoding", "chunked"), ("X-Bridge-Token", self.bridge_token)
        ])
        self.assertEqual(status, 400)
        self.assert_empty()

    def test_total_body_deadline_stops_slow_drip_below_idle_timeout(self):
        raw = self.event_bytes()
        headers = [
            ("Transfer-Encoding", "chunked"),
            ("X-Signature", self.signature(raw)),
        ]
        stopped = threading.Event()
        sender = None
        with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5) as sock:
            sock.settimeout(13)
            sock.sendall(self.headers_bytes(headers))
            sock.sendall(format(len(raw), "x").encode() + b"\r\n" + raw[:1])

            def slow_sender():
                for byte in raw[1:]:
                    if stopped.wait(0.25):
                        break
                    try:
                        sock.sendall(bytes([byte]))
                    except OSError:
                        break

            sender = threading.Thread(target=slow_sender, daemon=True)
            started = time.monotonic()
            sender.start()
            try:
                response = http.client.HTTPResponse(sock)
                response.begin()
                payload = json.loads(response.read())
                elapsed = time.monotonic() - started
                self.assertEqual(response.status, 400, payload)
                self.assertGreaterEqual(elapsed, 9, "the advertised 10-second body budget must remain usable")
                self.assertLess(elapsed, 12, "an idle-only timeout allows the slow drip to run indefinitely")
            finally:
                stopped.set()
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_WR)
                sender.join(timeout=2)
        self.assert_empty()


if __name__ == "__main__":
    unittest.main()
