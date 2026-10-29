import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import subprocess
import sys
import tempfile
import unittest

from configure import initialize
from install_services import render
from adapter import Adapter


class ConfigurationContracts(unittest.TestCase):
    def test_fresh_config_private_keys_match_and_reconfigure_preserves(self):
        with tempfile.TemporaryDirectory() as t:
            root = initialize(Path(t) / 'data', '123456', 'dummy-app-secret')
            files = [root / 'config' / x for x in ('credentials.json', 'runtime.json', 'bridge.json')]
            for p in files:
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            for p in (root, root / 'config', root / 'state'):
                self.assertEqual(p.stat().st_mode & 0o777, 0o700)
            runtime, bridge = json.loads(files[1].read_text()), json.loads(files[2].read_text())
            self.assertEqual(runtime['onebot_token'], bridge['ONEBOT_TOKEN'])
            self.assertEqual(runtime['report_secret'], bridge['ONEBOT_REPORT_SECRET'])
            self.assertEqual(runtime['allowed_qq'], bridge['ALLOWED_QQ'])
            self.assertGreaterEqual(len(bridge['BRIDGE_TOKEN']), 32)
            old = [p.read_bytes() for p in files[1:]]
            initialize(root, '123456', 'dummy-new-secret')
            self.assertEqual(old, [p.read_bytes() for p in files[1:]])
            self.assertEqual(json.loads(files[0].read_text())['secret'], 'dummy-new-secret')

    def test_different_appid_does_not_replace_state_or_authentication(self):
        with tempfile.TemporaryDirectory() as t:
            root = initialize(Path(t) / 'data', '123456', 'dummy-app-secret')
            old = {p.name: p.read_bytes() for p in (root / 'config').iterdir()}
            with self.assertRaises(ValueError):
                initialize(root, '654321', 'dummy-new-secret')
            self.assertEqual(old, {p.name: p.read_bytes() for p in (root / 'config').iterdir()})

    def test_invalid_or_empty_identity_and_keys_fail_closed(self):
        with tempfile.TemporaryDirectory() as t:
            for appid, owner, secret in [('abc', '10001', 'dummy'), ('123456', '123456', 'dummy'), ('123456', '10001', '')]:
                with self.subTest(appid=appid, owner=owner):
                    with self.assertRaises(ValueError):
                        initialize(Path(t) / 'data', appid, secret, owner)
            for runtime in [
                {'allowed_qq': '10001', 'onebot_token': '', 'report_secret': 'dummy'},
                {'allowed_qq': '10001', 'onebot_token': 'dummy', 'report_secret': ''},
                {'allowed_qq': 'arbitrary', 'onebot_token': 'dummy', 'report_secret': 'dummy'},
            ]:
                with self.assertRaises(ValueError):
                    Adapter(t, runtime, {'appid': '123456', 'secret': 'dummy', 'is_sandbox': True})

    def test_unit_rendering_quotes_paths_and_does_not_contain_credentials(self):
        template = Path(__file__).with_name('deploy').joinpath('muse-qq-adapter.service.in').read_text()
        text = render(template, '/opt/test project', '/srv/test data', '/opt/test project/.venv/bin/python',
                      'botuser', 'botgroup', 'adapter.py')
        self.assertIn('"/opt/test project/.venv/bin/python"', text)
        self.assertIn('ReadWritePaths="/srv/test data"', text)
        self.assertNotIn('@PROJECT@', text)
        self.assertNotIn('AppSecret', text)

    @unittest.skipUnless(sys.platform == 'linux' and shutil.which('systemd-analyze'), 'requires Linux systemd-analyze')
    def test_rendered_systemd_units_pass_real_syntax_verification(self):
        project = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as t:
            data = Path(t) / 'private data'; data.mkdir()
            paths = []
            for name, script in [('muse-qq-adapter', 'adapter.py'), ('muse-qq-bridge', 'run_bridge.py')]:
                p = Path(t) / (name + '.service')
                p.write_text(render((project / 'deploy' / (name + '.service.in')).read_text(),
                    project, data, sys.executable, pwd.getpwuid(os.getuid()).pw_name,
                    grp.getgrgid(os.getgid()).gr_name, script))
                paths.append(str(p))
            q = subprocess.run(['systemd-analyze', 'verify', *paths], capture_output=True, text=True)
            self.assertEqual(q.returncode, 0, q.stderr)


if __name__ == '__main__':
    unittest.main()
