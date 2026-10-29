#!/usr/bin/env python3
"""Start the bridge from server-local JSON; one writer, loopback only."""
import fcntl
import os
from pathlib import Path
import runpy
import sys

from adapter import private_json

if __name__ == '__main__':
    os.umask(0o077)
    root = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).parent / 'data').resolve()
    state = root / 'state'; state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = open(state / 'bridge.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    conf = private_json(root / 'config' / 'bridge.json')
    keys = ('BRIDGE_TOKEN', 'ALLOWED_QQ', 'ONEBOT_TOKEN', 'ONEBOT_REPORT_SECRET', 'ONEBOT_HTTP')
    if any(not isinstance(conf.get(k), str) or not conf[k] for k in keys):
        raise SystemExit('Bridge configuration incomplete')
    os.environ.update({k: conf[k] for k in keys})
    os.environ.update(BRIDGE_HOST='127.0.0.1', BRIDGE_PORT='8765',
                      BRIDGE_STORE=str(state / 'inbox.jsonl'))
    runpy.run_path(str(Path(__file__).with_name('mac_bridge.py')), run_name='__main__')
