#!/usr/bin/env python3
"""Optional, independent DSH/Muse handoff queue. Python standard library only."""

import argparse
import fcntl
import hashlib
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_BODY = 2 * 1024 * 1024
JOB_ID = re.compile(r"dshm:[0-9a-f]{64}\Z")
TAILNET = ipaddress.ip_network("100.64.0.0/10")


class Rejected(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def private_peer(value):
    try:
        ip = ipaddress.ip_address(value)
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return ip.is_loopback or (ip.version == 4 and ip in TAILNET)
    except ValueError:
        return False


def private_file(path, min_length=32):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("credential file must be a regular owner-only file")
        value = handle.read(4097).strip()
    if len(value) < min_length or len(value) > 4096 or "\n" in value or "\r" in value:
        raise ValueError("invalid credential file")
    return value


def validate_handoff(body, owner):
    if not isinstance(body, dict) or set(body) != {
        "version", "id", "from", "conversation", "message", "snapshot"
    }:
        raise Rejected(400, "invalid_handoff")
    if type(body["version"]) is not int or body["version"] != 1 or not JOB_ID.fullmatch(str(body["id"])):
        raise Rejected(400, "invalid_handoff")
    if body["from"] != owner or body["conversation"] != "private:" + owner:
        raise Rejected(403, "owner_only")
    message, snapshot = body["message"], body["snapshot"]
    if not isinstance(message, dict) or set(message) != {"id", "text", "time"}:
        raise Rejected(400, "invalid_message")
    if (not isinstance(message["id"], str) or not message["id"] or
            not isinstance(message["text"], str) or not message["text"].strip() or
            not isinstance(message["time"], (int, float, str)) or
            isinstance(message["time"], bool) or
            (isinstance(message["time"], (int, float)) and not math.isfinite(message["time"]))):
        raise Rejected(400, "invalid_message")
    if (not isinstance(snapshot, dict) or
            set(snapshot) - {"history", "memories", "persona"} or
            not isinstance(snapshot.get("history"), list) or
            not isinstance(snapshot.get("memories"), list)):
        raise Rejected(400, "invalid_snapshot")
    if "persona" in snapshot and not isinstance(snapshot["persona"], str):
        raise Rejected(400, "invalid_persona")
    for row in snapshot["history"]:
        if (not isinstance(row, dict) or set(row) != {"role", "text"} or
                row["role"] not in ("user", "assistant") or
                not isinstance(row["text"], str)):
            raise Rejected(400, "invalid_history")
    scopes = {"chat:private:" + owner, "user:" + owner}
    for row in snapshot["memories"]:
        if (not isinstance(row, dict) or set(row) - {"id", "scope", "text", "kind"} or
                not {"id", "scope", "text"} <= set(row) or
                not all(isinstance(v, str) for v in row.values()) or
                row["scope"] not in scopes):
            raise Rejected(403, "invalid_memory_scope")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def onebot_send(base_url, token, owner, text):
    parsed = urlsplit(base_url)
    # No DNS, public URL, proxy, redirect or automatic send retry.
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    if (parsed.scheme != "http" or not loopback or parsed.username or parsed.password or
            parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("ONEBOT_HTTP must use a literal loopback HTTP address")
    request = Request(base_url.rstrip("/") + "/send_private_msg",
                      data=canonical({"user_id": int(owner), "message": text, "auto_escape": True}).encode(),
                      headers={"Content-Type": "application/json",
                               **({"Authorization": "Bearer " + token} if token else {})})
    opener = build_opener(ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(65537)
            if response.status != 200 or len(raw) > 65536:
                raise ValueError("unconfirmed OneBot response")
            body = json.loads(raw)
    except HTTPError as error:
        error.close()
        raise ValueError("unconfirmed OneBot HTTP status") from None
    if not isinstance(body, dict) or not isinstance(body.get("data"), dict):
        raise ValueError("unconfirmed OneBot response")
    receipt = body["data"].get("message_id")
    if (body.get("status") != "ok" or type(body.get("retcode")) is not int or
            body["retcode"] != 0 or isinstance(receipt, bool) or
            not isinstance(receipt, (str, int)) or not str(receipt).strip()):
        raise ValueError("unconfirmed OneBot receipt")
    return str(receipt)


class Store:
    def __init__(self, directory, owner, send):
        directory = Path(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            raise ValueError("state directory must be owner-only and not a symlink")
        db_path = directory / "handoffs.sqlite3"
        if db_path.is_symlink():
            raise ValueError("state database must not be a symlink")
        lock_fd = os.open(directory / ".broker.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        self.process_lock = os.fdopen(lock_fd, "a+")
        try:
            fcntl.flock(self.process_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.process_lock.close()
            raise ValueError("another broker owns this state directory") from None
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        os.chmod(db_path, 0o600)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.owner, self.send = owner, send
        with self.db:
            self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, phase TEXT NOT NULL, reservation TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS sends (id TEXT PRIMARY KEY, ids TEXT NOT NULL, text TEXT NOT NULL, phase TEXT NOT NULL, receipt TEXT)")
            # A process exit during a send leaves its outcome unknown, never retryable.
            self.db.execute("UPDATE jobs SET phase='unknown' WHERE phase='sending'")
            self.db.execute("UPDATE sends SET phase='unknown' WHERE phase='sending'")

    def close(self):
        self.db.close()
        self.process_lock.close()

    def status(self):
        with self.lock:
            counts = dict(self.db.execute("SELECT phase, COUNT(*) FROM jobs GROUP BY phase"))
            return {"ok": True, "pending": sum(n for p, n in counts.items() if p != "acked"),
                    "unknown": counts.get("unknown", 0)}

    def job(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT phase FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise Rejected(404, "not_found")
            return {"ok": True, "id": job_id, "phase": row["phase"]}

    def enqueue(self, payload):
        validate_handoff(payload, self.owner)
        packed = canonical(payload)
        if len(packed.encode()) > MAX_BODY:
            raise Rejected(413, "context_too_large")
        with self.lock, self.db:
            old = self.db.execute("SELECT payload FROM jobs WHERE id=?", (payload["id"],)).fetchone()
            if old and old["payload"] != packed:
                raise Rejected(409, "id_conflict")
            self.db.execute("INSERT OR IGNORE INTO jobs VALUES (?, ?, 'queued', NULL)", (payload["id"], packed))
            return self.job(payload["id"])

    def context(self, job_id):
        with self.lock:
            row = self.db.execute("SELECT payload FROM jobs WHERE id=? AND phase!='acked'", (job_id,)).fetchone()
            if row is None:
                raise Rejected(404, "not_found")
            return json.loads(row["payload"])

    def inbox(self):
        with self.lock:
            rows = self.db.execute("SELECT payload, phase FROM jobs WHERE phase!='acked' ORDER BY rowid").fetchall()
        messages = []
        for row in rows:
            value = json.loads(row["payload"])
            context = canonical(value).encode()
            messages.append({"id": value["id"], "from": value["from"],
                             "text": value["message"]["text"], "time": value["message"]["time"],
                             "handoff": {"context_id": value["id"],
                                         "context_sha256": hashlib.sha256(context).hexdigest(),
                                         "context_bytes": len(context),
                                         "history_count": len(value["snapshot"]["history"]),
                                         "memories_count": len(value["snapshot"]["memories"])},
                             "phase": row["phase"]})
        return {"messages": messages}

    def reply(self, body):
        if not isinstance(body, dict) or set(body) != {"user_id", "text", "ids"}:
            raise Rejected(400, "invalid_send")
        if str(body["user_id"]) != self.owner or isinstance(body["user_id"], bool):
            raise Rejected(403, "owner_only")
        text, ids = body["text"], body["ids"]
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise Rejected(400, "invalid_text")
        ids = self.validate_ids(ids)
        signature = hashlib.sha256(canonical({"ids": ids, "text": text}).encode()).hexdigest()
        with self.lock, self.db:
            old = self.db.execute("SELECT * FROM sends WHERE id=?", (signature,)).fetchone()
            if old:
                if old["phase"] == "sent":
                    return {"ok": True, "message_id": old["receipt"], "duplicate": True}
                raise Rejected(409, "send_" + old["phase"])
            for job_id in ids:
                row = self.db.execute("SELECT phase FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row or row["phase"] != "queued":
                    raise Rejected(409, "job_not_sendable")
            self.db.execute("INSERT INTO sends VALUES (?, ?, ?, 'sending', NULL)", (signature, canonical(ids), text))
            for job_id in ids:
                self.db.execute("UPDATE jobs SET phase='sending', reservation=? WHERE id=?", (signature, job_id))
        try:
            receipt = self.send(self.owner, text)
            if not isinstance(receipt, (str, int)) or isinstance(receipt, bool) or not str(receipt).strip():
                raise ValueError("unconfirmed receipt")
        except Exception:
            with self.lock, self.db:
                self.db.execute("UPDATE sends SET phase='unknown' WHERE id=?", (signature,))
                self.db.execute("UPDATE jobs SET phase='unknown' WHERE reservation=?", (signature,))
            raise Rejected(502, "send_unknown") from None
        with self.lock, self.db:
            self.db.execute("UPDATE sends SET phase='sent', receipt=? WHERE id=?", (str(receipt), signature))
            self.db.execute("UPDATE jobs SET phase='sent' WHERE reservation=?", (signature,))
        return {"ok": True, "message_id": str(receipt)}

    @staticmethod
    def validate_ids(ids):
        if (not isinstance(ids, list) or not ids or len(ids) > 100 or
                not all(isinstance(i, str) and JOB_ID.fullmatch(i) for i in ids) or
                len(set(ids)) != len(ids)):
            raise Rejected(400, "invalid_ids")
        return sorted(ids)

    def ack(self, body):
        if not isinstance(body, dict) or set(body) != {"ids"}:
            raise Rejected(400, "invalid_ack")
        ids = self.validate_ids(body["ids"])
        with self.lock, self.db:
            for job_id in ids:
                row = self.db.execute("SELECT phase FROM jobs WHERE id=?", (job_id,)).fetchone()
                if not row or row["phase"] not in ("sent", "acked"):
                    raise Rejected(409, "receipt_required")
            for job_id in ids:
                self.db.execute("UPDATE jobs SET phase='acked' WHERE id=?", (job_id,))
        return {"ok": True}


def make_server(bind, port, store, dsh_token, muse_token):
    if not private_peer(bind) or ipaddress.ip_address(bind).version != 4:
        raise ValueError("bind must be a literal loopback or Tailscale IPv4 address")
    if dsh_token == muse_token:
        raise ValueError("DSH and Muse credentials must be distinct")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, value):
            raw = canonical(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def auth(self, token):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
                raise Rejected(401, "unauthorized")

        def read_body(self):
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise Rejected(400, "content_length_required")
            try:
                length = int(self.headers["Content-Length"])
            except (ValueError, TypeError):
                raise Rejected(400, "invalid_length") from None
            if length < 1 or length > MAX_BODY:
                raise Rejected(413, "body_too_large")
            self.connection.settimeout(15)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise Rejected(400, "incomplete_body")
            try:
                return json.loads(raw)
            except (ValueError, UnicodeError):
                raise Rejected(400, "invalid_json") from None

        def dispatch(self):
            path = unquote(self.path)
            if self.command == "GET" and path == "/health":
                return store.status()
            if self.command == "GET" and path == "/inbox-tailnet":
                # Authorize real socket peer before reading any queue data.
                if not private_peer(self.client_address[0]):
                    raise Rejected(403, "tailnet_only")
                return store.inbox()
            if self.command == "GET" and path.startswith("/dsh/context-tailnet/"):
                if not private_peer(self.client_address[0]):
                    raise Rejected(403, "tailnet_only")
                return store.context(path.removeprefix("/dsh/context-tailnet/"))
            if path == "/dsh/handoff" and self.command == "POST":
                self.auth(dsh_token)
                return store.enqueue(self.read_body())
            if self.command == "GET" and path == "/dsh/status":
                self.auth(dsh_token)
                return store.status()
            if self.command == "GET" and path.startswith("/dsh/jobs/"):
                self.auth(dsh_token)
                return store.job(path.removeprefix("/dsh/jobs/"))
            if self.command == "GET" and path.startswith("/dsh/context/"):
                self.auth(muse_token)
                return store.context(path.removeprefix("/dsh/context/"))
            if self.command == "GET" and path == "/inbox":
                self.auth(muse_token)
                return store.inbox()
            if self.command == "POST" and path in ("/send", "/ack"):
                self.auth(muse_token)
                return (store.reply if path == "/send" else store.ack)(self.read_body())
            raise Rejected(404, "not_found")

        def handle_request(self):
            self.close_connection = True
            try:
                self.respond(200, self.dispatch())
            except Rejected as error:
                self.respond(error.status, {"ok": False, "error": error.code})
            except (BrokenPipeError, ConnectionError, TimeoutError):
                pass
            except Exception:
                self.respond(500, {"ok": False, "error": "internal_error"})

        do_GET = do_POST = handle_request

    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    config = json.loads(Path(args.config).read_text())
    owner = str(config["owner"])
    if not re.fullmatch(r"[1-9][0-9]{4,19}", owner):
        raise ValueError("owner must be a numeric private-chat QQ identifier")
    dsh_token = private_file(config["dshTokenFile"])
    muse_token = private_file(config["museTokenFile"])
    onebot_token = private_file(config["onebotTokenFile"], min_length=1) if config.get("onebotTokenFile") else ""
    store = Store(config["stateDir"], owner,
                  lambda recipient, text: onebot_send(config["onebotHttp"], onebot_token, recipient, text))
    server = make_server(config.get("bind", "127.0.0.1"), config.get("port", 18765), store, dsh_token, muse_token)
    print("handoff broker listening", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
