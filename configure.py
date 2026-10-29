#!/usr/bin/env python3
"""Initialize server-only configuration without printing or rotating existing keys."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
import tempfile


def save_private(path, data):
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix='.private-')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2); f.write('\n')
            f.flush(); os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def initialize(root, appid, secret, owner_id='10001'):
    if not isinstance(appid, str) or not appid.isascii() or not appid.isdecimal() or int(appid) <= 0:
        raise ValueError('AppID must contain positive ASCII digits')
    if not owner_id.isascii() or not owner_id.isdecimal() or int(owner_id) <= 0 or owner_id == appid:
        raise ValueError('owner compatibility ID must be positive and distinct from AppID')
    if not isinstance(secret, str) or not secret.strip():
        raise ValueError('AppSecret must not be empty')
    root = Path(root).expanduser().resolve()
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config, state = root / 'config', root / 'state'
    for d in (root, config, state):
        if d.is_symlink():
            raise ValueError('configuration directories must not be symlinks')
        d.mkdir(mode=0o700, exist_ok=True); d.chmod(0o700)
    credentials_path, runtime_path = config / 'credentials.json', config / 'runtime.json'
    for p in (credentials_path, runtime_path, config / 'bridge.json'):
        if p.is_symlink():
            raise ValueError('configuration files must not be symlinks')
    if credentials_path.exists():
        old = json.loads(credentials_path.read_text())
        if old.get('appid') != appid:
            raise ValueError('use a fresh data directory for a different AppID')
    if runtime_path.exists():
        runtime = json.loads(runtime_path.read_text())
        if runtime.get('allowed_qq') != owner_id:
            raise ValueError('owner compatibility ID differs; existing state was not changed')
    else:
        runtime = {'allowed_qq': owner_id, 'onebot_token': secrets.token_urlsafe(32),
                   'report_secret': secrets.token_urlsafe(32)}
        save_private(runtime_path, runtime)
    bridge_path = config / 'bridge.json'
    if not bridge_path.exists():
        save_private(bridge_path, {'BRIDGE_TOKEN': secrets.token_urlsafe(32),
            'ALLOWED_QQ': owner_id, 'ONEBOT_TOKEN': runtime['onebot_token'],
            'ONEBOT_REPORT_SECRET': runtime['report_secret'],
            'ONEBOT_HTTP': 'http://127.0.0.1:3001'})
    else:
        bridge = json.loads(bridge_path.read_text())
        if (bridge.get('ALLOWED_QQ') != owner_id or
            bridge.get('ONEBOT_TOKEN') != runtime['onebot_token'] or
            bridge.get('ONEBOT_REPORT_SECRET') != runtime['report_secret']):
            raise ValueError('existing bridge/adapter authentication differs; nothing was rotated')
    save_private(credentials_path, {'appid': appid, 'secret': secret.strip(), 'is_sandbox': True})
    for p in (credentials_path, runtime_path, bridge_path):
        p.chmod(0o600)
    return root


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-dir', default=str(Path(__file__).parent / 'data'))
    p.add_argument('--appid', required=True, help='public Tencent application ID')
    p.add_argument('--owner-id', default='10001', help='local numeric compatibility alias, not an OpenID')
    a = p.parse_args()
    import sys
    if not sys.stdin.isatty():
        raise SystemExit('Enter AppSecret on the server interactive terminal.')
    secret = getpass.getpass('AppSecret (hidden, server only): ')
    root = initialize(a.data_dir, a.appid, secret, a.owner_id)
    print('Private configuration saved with mode 600 in ' + str(root / 'config'))
    print('Sandbox enabled. Existing bridge authentication keys were preserved.')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError):
        raise SystemExit('Configuration failed; check paths and existing AppID/owner settings. No keys were printed.')
