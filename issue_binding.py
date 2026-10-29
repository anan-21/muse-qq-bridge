#!/usr/bin/env python3
"""Issue an expiring owner-binding message after the sandbox gateway is ready."""
import argparse
import hashlib
import secrets
import time
from pathlib import Path

from configure import save_private

p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--data-dir', default=str(Path(__file__).parent / 'data'))
a = p.parse_args()
root = Path(a.data_dir).expanduser().resolve()
if not (root / 'config').is_dir():
    raise SystemExit('Run configure.py first.')
phrase = '绑定Muse ' + secrets.token_hex(12)
save_private(root / 'config' / 'binding.json', {
    'sha256': hashlib.sha256(phrase.encode()).hexdigest(), 'expires_at': time.time() + 900})
print(phrase)
