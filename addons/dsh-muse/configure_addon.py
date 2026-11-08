#!/usr/bin/env python3
"""Create a NEW private add-on configuration; never modify an existing setup."""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets

from broker import private_peer


def configure(directory, owner, bind="127.0.0.1", onebot_token=""):
    directory = Path(directory).expanduser().absolute()
    if not re.fullmatch(r"[1-9][0-9]{4,19}", str(owner)) or not private_peer(bind) or ":" in bind:
        raise ValueError("numeric owner and literal private IPv4 bind required")
    # A new directory makes accidental overwrite of live credentials impossible.
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    def write(name, value, as_json=False):
        raw = json.dumps(value, ensure_ascii=False, indent=2) + "\n" if as_json else value + "\n"
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
    write("dsh-token", secrets.token_hex(32))
    write("muse-token", secrets.token_hex(32))
    if onebot_token:
        write("onebot-token", onebot_token)
    base = "http://" + bind + ":18765"
    write("broker.json", {"owner": str(owner), "bind": bind, "port": 18765,
                          "stateDir": str(directory / "broker-state"),
                          "dshTokenFile": str(directory / "dsh-token"),
                          "museTokenFile": str(directory / "muse-token"),
                          "onebotHttp": "http://127.0.0.1:3000",
                          "onebotTokenFile": str(directory / "onebot-token") if onebot_token else ""}, True)
    write("dsh-router.json", {"enabled": False, "owner": str(owner), "baseUrl": base,
                              "tokenFile": str(directory / "dsh-token"),
                              "stateDir": str(directory / "router-state"),
                              "consentExport": False, "sharePersona": False,
                              "shareUserMemories": False, "allowCrossChatUserMemories": False,
                              "normalTurnsToReturn": 2}, True)
    write("muse-client.json", {"owner": str(owner), "baseUrl": base,
                              "tokenFile": str(directory / "muse-token"),
                              "stateDir": str(directory / "muse-state")}, True)
    return directory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--onebot-token", action="store_true", help="ask for OneBot token with hidden input")
    args = parser.parse_args()
    os.umask(0o077)
    token = getpass.getpass("OneBot token (hidden): ") if args.onebot_token else ""
    configure(args.directory, args.owner, args.bind, token)
    print("Private add-on files created; router remains disabled. No credentials printed.")


if __name__ == "__main__":
    main()
