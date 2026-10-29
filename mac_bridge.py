#!/usr/bin/env python3
"""QQ–Muse local bridge, compatible with the supplied v0 HTTP endpoints.

Required: BRIDGE_TOKEN, ALLOWED_QQ, ONEBOT_REPORT_SECRET. Optional:
ONEBOT_HTTP (http://127.0.0.1:3001), ONEBOT_TOKEN, BRIDGE_HOST (127.0.0.1),
BRIDGE_PORT (8765), BRIDGE_STORE (inbox.jsonl beside this file).

OneBot HTTP POST uses X-Signature: sha1=<HMAC-SHA1 of the exact request body>
with ONEBOT_REPORT_SECRET; Muse uses X-Bridge-Token for /inbox, /ack, /send.
Only /health is anonymous. Keep the listener local and use private Tailscale
Serve for remote access. This process never generates replies or retries sends.
"""

import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOKEN = os.environ.get("BRIDGE_TOKEN", "")
ALLOWED = os.environ.get("ALLOWED_QQ", "")
ONEBOT = os.environ.get("ONEBOT_HTTP", "http://127.0.0.1:3001").rstrip("/")
ONEBOT_TOKEN = os.environ.get("ONEBOT_TOKEN", "")
REPORT_SECRET = os.environ.get("ONEBOT_REPORT_SECRET", "")
HOST = os.environ.get("BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("BRIDGE_PORT", "8765"))
STORE = os.environ.get("BRIDGE_STORE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "inbox.jsonl"))
MAX_BODY = 65536
MAX_CHUNK_LINE = 128
MAX_CHUNKS = 1024
MAX_CHUNK_OVERHEAD = 65536
LOCK = threading.RLock()


def load():
    """Read only complete JSONL records; a corrupt store is never overwritten."""
    with LOCK:
        try:
            with open(STORE, encoding="utf-8") as f:
                rows = [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []
        if any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in rows):
            raise ValueError("invalid inbox store")
        return rows


def save(rows):
    """Caller holds LOCK for compound read-modify-write operations."""
    with LOCK:
        directory = os.path.dirname(os.path.abspath(STORE))
        fd, temporary = tempfile.mkstemp(prefix=".inbox-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary, STORE)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


class OneBotError(Exception):
    def __init__(self, message, *, outcome="unknown", retcode=None):
        super().__init__(message)
        self.outcome = outcome
        self.retcode = retcode


def onebot_send(user_id, text):
    data = json.dumps({"user_id": int(user_id), "message": text, "auto_escape": True}).encode()
    req = urllib.request.Request(
        ONEBOT + "/send_private_msg", data=data,
        headers={"Content-Type": "application/json", **(
            {"Authorization": "Bearer " + ONEBOT_TOKEN} if ONEBOT_TOKEN else {})},
    )
    # No automatic retry: an HTTP timeout can occur after QQ accepted the send.
    # Disallow redirects so a misconfigured API cannot forward the token elsewhere.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(req, timeout=10) as resp:
            raw = resp.read(MAX_BODY + 1)
            if resp.status != 200 or len(raw) > MAX_BODY:
                raise OneBotError("unconfirmed OneBot response")
            reply = json.loads(raw)
    except OneBotError:
        raise
    except urllib.error.HTTPError as error:
        error.close()
        raise OneBotError("OneBot HTTP response did not confirm the send") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise OneBotError("OneBot send could not be confirmed") from None
    if not isinstance(reply, dict):
        raise OneBotError("unconfirmed OneBot response")
    retcode = reply.get("retcode")
    if reply.get("status") != "ok" or type(retcode) is not int or retcode != 0:
        raise OneBotError("OneBot rejected or did not confirm the send", outcome="unknown",
                          retcode=retcode if type(retcode) is int else None)
    if not isinstance(reply.get("data"), dict) or not _identifier(reply["data"].get("message_id")):
        raise OneBotError("OneBot did not return a message receipt")
    return {"onebot_status": 200, "message_id": reply["data"]["message_id"]}


def _identifier(value):
    return (type(value) is int or isinstance(value, str)) and bool(str(value)) and len(str(value)) <= 128


def _private_text(event):
    if (event.get("post_type") != "message" or event.get("message_type") != "private"
            or str(event.get("user_id")) != ALLOWED
            or str(event.get("user_id")) == str(event.get("self_id"))):
        return None
    message = event.get("message")
    if isinstance(message, list):
        if not message or any(not isinstance(part, dict) or part.get("type") != "text"
                              or not isinstance(part.get("data"), dict)
                              or not isinstance(part["data"].get("text"), str) for part in message):
            return None
        text = "".join(part["data"]["text"] for part in message)
    else:
        text = event.get("raw_message", message)
        if not isinstance(text, str) or "[CQ:" in text:
            return None
    return text if text.strip() else None


class H(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def _read_body(self):
        """Read one unambiguous, bounded HTTP body without changing its bytes."""
        transfer = self.headers.get_all("Transfer-Encoding", [])
        lengths = self.headers.get_all("Content-Length", [])
        deadline = time.monotonic() + 10

        def exact(length):
            parts = []
            while length:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("request body deadline exceeded")
                self.connection.settimeout(remaining)
                part = self.rfile.read1(length)
                if not part:
                    raise ValueError("truncated request body")
                parts.append(part)
                length -= len(part)
            return b"".join(parts)

        def line():
            value = bytearray()
            while len(value) < MAX_CHUNK_LINE:
                value.extend(exact(1))
                if value[-1] == 10:
                    if not value.endswith(b"\r\n"):
                        raise ValueError("invalid chunk line ending")
                    return bytes(value)
            raise ValueError("chunk line too long")

        try:
            if not transfer:
                if (len(lengths) != 1 or not lengths[0].isascii()
                        or not lengths[0].isdecimal()):
                    raise ValueError("one Content-Length required")
                length = int(lengths[0])
                if length > MAX_BODY:
                    raise OverflowError("request body too large")
                return exact(length)
            if (self.path != "/onebot/event" or lengths or len(transfer) != 1
                    or transfer[0].strip().lower() != "chunked"):
                raise ValueError("unsupported or ambiguous request framing")
            body = bytearray()
            chunks = overhead = 0
            while True:
                size_line = line()
                overhead += len(size_line)
                if overhead > MAX_CHUNK_OVERHEAD:
                    raise ValueError("chunk framing too large")
                size_text = size_line[:-2]
                if (not size_text
                        or any(c not in b"0123456789abcdefABCDEF" for c in size_text)):
                    raise ValueError("invalid chunk size")
                size = int(size_text, 16)
                if size > MAX_BODY - len(body):
                    raise OverflowError("request body too large")
                if size == 0:
                    ending = line()
                    overhead += len(ending)
                    if ending != b"\r\n" or overhead > MAX_CHUNK_OVERHEAD:
                        raise ValueError("chunk trailers are unsupported")
                    return bytes(body)
                chunks += 1
                if chunks > MAX_CHUNKS:
                    raise ValueError("too many chunks")
                body.extend(exact(size))
                if exact(2) != b"\r\n":
                    raise ValueError("invalid chunk ending")
                overhead += 2
                if overhead > MAX_CHUNK_OVERHEAD:
                    raise ValueError("chunk framing too large")
        finally:
            self.connection.settimeout(10)

    def _auth(self):
        supplied = self.headers.get("X-Bridge-Token", "")
        return bool(TOKEN) and hmac.compare_digest(supplied.encode(), TOKEN.encode())

    def _event_auth(self, raw):
        if not REPORT_SECRET:
            return False
        expected = "sha1=" + hmac.new(REPORT_SECRET.encode(), raw, hashlib.sha1).hexdigest()
        return hmac.compare_digest(self.headers.get("X-Signature", "").encode(), expected.encode())

    def _json(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path not in ("/health", "/inbox"):
            return self._json(404, {"error": "not found"})
        if self.path == "/inbox" and not self._auth():
            return self._json(401, {"error": "bad token"})
        try:
            rows = [r for r in load() if not r.get("acked")]
        except (OSError, ValueError):
            return self._json(503, {"error": "inbox unavailable"})
        if self.path == "/health":
            return self._json(200, {"ok": True, "pending": len(rows)})
        return self._json(200, {"messages": rows})

    def do_POST(self):
        if self.path not in ("/onebot/event", "/ack", "/send"):
            return self._json(404, {"error": "not found"})
        if self.path != "/onebot/event" and not self._auth():
            return self._json(401, {"error": "bad token"})
        try:
            raw = self._read_body()
        except OverflowError:
            self.close_connection = True
            return self._json(413, {"error": "body too large"})
        except (OSError, ValueError):
            self.close_connection = True
            return self._json(400, {"error": "invalid request body"})
        if self.path == "/onebot/event" and not self._event_auth(raw):
            return self._json(401, {"error": "bad event signature"})
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "expected JSON object"})
        try:
            if self.path == "/onebot/event":
                text = _private_text(body)
                if text is not None:
                    if not _identifier(body.get("message_id")):
                        return self._json(400, {"error": "message_id required"})
                    identifier = str(body["message_id"])
                    with LOCK:
                        rows = load()
                        if not any(row["id"] == identifier for row in rows):
                            rows.append({"id": identifier, "from": ALLOWED, "text": text,
                                         "time": int(time.time()), "acked": False})
                            save(rows)
                return self._json(200, {"ok": True})
            if self.path == "/ack":
                identifiers = body.get("ids")
                if not isinstance(identifiers, list) or any(not _identifier(i) for i in identifiers):
                    return self._json(400, {"error": "ids must be an array of message IDs"})
                ids = {str(i) for i in identifiers}
                with LOCK:
                    rows = load()
                    for row in rows:
                        if row["id"] in ids:
                            row["acked"] = True
                    save(rows)
                return self._json(200, {"ok": True})
        except (OSError, ValueError):
            return self._json(503, {"error": "inbox unavailable"})
        recipient = body.get("user_id")
        if not _identifier(recipient) or str(recipient) != ALLOWED:
            return self._json(403, {"error": "recipient not whitelisted"})
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            return self._json(400, {"error": "text must contain 1–2000 characters"})
        try:
            result = onebot_send(recipient, text)
        except OneBotError as error:
            return self._json(502, {"error": str(error), "outcome": error.outcome,
                                    "retry_safe": False, "onebot_retcode": error.retcode})
        return self._json(200, {"ok": True, **result})


if __name__ == "__main__":
    os.umask(0o077)
    if HOST != "127.0.0.1":
        raise SystemExit("BRIDGE_HOST must remain 127.0.0.1")
    if not TOKEN or not ALLOWED.isascii() or not ALLOWED.isdecimal() or int(ALLOWED) <= 0 or not REPORT_SECRET:
        raise SystemExit("Set BRIDGE_TOKEN, numeric ALLOWED_QQ, and ONEBOT_REPORT_SECRET first")
    url = urllib.parse.urlparse(ONEBOT)
    if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1")
            or url.username is not None or url.password is not None):
        raise SystemExit("ONEBOT_HTTP must be a local HTTP endpoint")
    if os.path.exists(STORE):
        os.chmod(STORE, 0o600)
    server = ThreadingHTTPServer((HOST, PORT), H)
    print(f"bridge on {HOST}:{PORT}; single-recipient whitelist enabled", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
