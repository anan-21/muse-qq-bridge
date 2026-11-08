#!/usr/bin/env python3
"""Private context pickup and receipt-first reply helper for the optional broker."""

import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
from urllib.parse import quote, urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

JOB_ID = re.compile(r"dshm:[0-9a-f]{64}\Z")
MAX_CONTEXT = 2 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or path.stat().st_mode & 0o077:
        raise ValueError("private_directory_required")
    return path


def atomic_private(path, raw):
    # Random exclusive temp file; never follow an existing cache/state symlink.
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".handoff-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


class Client:
    def __init__(self, config):
        self.config = config
        self.base = str(config["baseUrl"]).rstrip("/")
        url = urlsplit(self.base)
        host = url.hostname or ""
        try:
            ip = ipaddress.ip_address(host)
            allowed = ip.is_loopback or (ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"))
        except ValueError:
            allowed = bool(re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.ts\.net", host))
        if (not allowed or url.scheme not in ("http", "https") or url.username or
                url.password or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError("private_url_required")
        self.state = private_directory(config["stateDir"])
        self.contexts = private_directory(self.state / "contexts")
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def token(self):
        fd = os.open(self.config["tokenFile"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError("private_token_file_required")
            token = handle.read(4097).strip()
        if len(token) < 32 or len(token) > 4096 or "\n" in token or "\r" in token:
            raise ValueError("invalid_token")
        return token

    def request(self, path, body=None, auth=False, limit=MAX_CONTEXT):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + self.token()
        request = Request(self.base + path, headers=headers,
                          data=canonical(body).encode() if body is not None else None)
        try:
            with self.opener.open(request, timeout=35) as response:
                raw = response.read(limit + 1)
                if response.status != 200 or len(raw) > limit:
                    raise ValueError("invalid_response")
        except HTTPError as error:
            error.close()
            raise ValueError("unconfirmed_http_response") from None
        return raw

    def cache_path(self, job_id):
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise ValueError("invalid_job_id")
        return self.contexts / (job_id.removeprefix("dshm:") + ".json")

    def poll(self, now=None, dry_run=False):
        now = time.time() if now is None else now
        health = json.loads(self.request("/health", limit=65536))
        if (health.get("ok") is not True or type(health.get("pending")) is not int or
                health["pending"] < 0):
            raise ValueError("invalid_health")
        if health["pending"] == 0:
            if not dry_run:
                (self.state / "last-wake.json").unlink(missing_ok=True)
            return {"wake": False, "reason": "empty"}
        inbox = json.loads(self.request("/inbox-tailnet", limit=MAX_CONTEXT))
        rows = inbox.get("messages")
        if not isinstance(rows, list) or not rows:
            raise ValueError("invalid_inbox")
        # Unknown/reserved sends require a human check; sent jobs need ack recovery,
        # not another generated answer. Never let the hook silently retry a send.
        if any(row.get("phase") != "queued" for row in rows):
            return {"wake": False, "reason": "needs_reconciliation"}
        batch = rows[:10]
        batch_key = hashlib.sha256(canonical([r["id"] for r in batch]).encode()).hexdigest()
        marker = self.state / "last-wake.json"
        if marker.exists():
            previous = json.loads(marker.read_text())
            if now - previous.get("time", 0) < 240:
                return {"wake": False, "reason": "cooldown"}
        messages = []
        for row in batch:
            if str(row.get("from")) != str(self.config["owner"]):
                raise ValueError("owner_mismatch")
            meta = row["handoff"]
            raw = self.request("/dsh/context-tailnet/" + quote(row["id"], safe=""))
            if (len(raw) != meta["context_bytes"] or
                    hashlib.sha256(raw).hexdigest() != meta["context_sha256"]):
                raise ValueError("context_integrity_failed")
            value = json.loads(raw)
            if (value["id"] != row["id"] or value["from"] != str(self.config["owner"]) or
                    value["conversation"] != "private:" + str(self.config["owner"])):
                raise ValueError("context_owner_mismatch")
            path = self.cache_path(row["id"])
            if not dry_run:
                atomic_private(path, raw)
            messages.append({"id": row["id"], "from": row["from"], "text": row["text"],
                             "time": row["time"], "context_file": str(path),
                             "context_sha256": meta["context_sha256"]})
        if not dry_run:
            atomic_private(marker, canonical({"batch": batch_key, "time": now}).encode())
        return {"wake": not dry_run, "reason": "ready" if not dry_run else "dry_run",
                "data": {"pending": health["pending"], "messages": messages}}

    @staticmethod
    def ids(values):
        if not values or len(values) != len(set(values)) or not all(JOB_ID.fullmatch(v) for v in values):
            raise ValueError("invalid_ids")
        return values

    def ack(self, ids):
        self.ids(ids)
        result = json.loads(self.request("/ack", {"ids": ids}, auth=True, limit=65536))
        if result.get("ok") is not True:
            raise ValueError("ack_unconfirmed")
        for job_id in ids:
            self.cache_path(job_id).unlink(missing_ok=True)

    def reply(self, ids, text):
        self.ids(ids)
        if not isinstance(text, str) or not text.strip() or len(text) > 2000:
            raise ValueError("invalid_text")
        result = json.loads(self.request("/send", {"ids": ids, "user_id": str(self.config["owner"]),
                                                   "text": text}, auth=True, limit=65536))
        receipt = result.get("message_id")
        if (result.get("ok") is not True or isinstance(receipt, bool) or
                not isinstance(receipt, (str, int)) or not str(receipt).strip()):
            raise ValueError("send_unconfirmed")
        self.ack(ids)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("action", choices=("poll", "reply", "ack"))
    parser.add_argument("ids", nargs="?", help="comma-separated job IDs for reply/ack")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        client = Client(json.loads(Path(args.config).read_text()))
        if args.action == "poll":
            result = client.poll(dry_run=os.environ.get("HATCH_HOOK_DRY_RUN") == "1")
            print(canonical(result))
        elif args.action == "reply":
            client.reply((args.ids or "").split(","), sys.stdin.read(16001))
            print("ok")
        else:
            client.ack((args.ids or "").split(","))
            print("ok")
    except Exception:
        # No raw exception/HTTP response/path/token/chat content in output.
        print('{"wake":false,"reason":"fetch_failed"}' if args.action == "poll" else "fail:reconcile")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
