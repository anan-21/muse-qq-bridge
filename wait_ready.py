#!/usr/bin/env python3
"""Bounded read-only readiness probe. Never restarts or reconfigures a service."""
import argparse
import json
import time
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--timeout', type=float, default=30)
parser.add_argument('--require-bound', action='store_true')
args = parser.parse_args()
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
end = time.monotonic() + max(1, min(args.timeout, 300))
result = {}
while time.monotonic() < end:
    result = {'bridge_ok': False, 'sandbox': False, 'gateway_ready': False, 'owner_bound': False}
    try:
        with opener.open('http://127.0.0.1:8765/health', timeout=1) as r:
            result['bridge_ok'] = r.status == 200 and json.load(r).get('ok') is True
        with opener.open('http://127.0.0.1:3001/health', timeout=1) as r:
            a = json.load(r)
            for k in ('sandbox', 'gateway_ready', 'owner_bound'):
                result[k] = a.get(k) is True
        if all(result[k] for k in ('bridge_ok', 'sandbox', 'gateway_ready')) and (not args.require_bound or result['owner_bound']):
            print(json.dumps(result)); raise SystemExit(0)
    except (OSError, ValueError):
        pass
    time.sleep(0.5)
print(json.dumps(result))
raise SystemExit(1)
