import contextlib
import io
import json
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest

from configure_addon import configure


OWNER = "12345678"


class ConfigurationAddonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, name="addon", **kwargs):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            directory = configure(self.root / name, OWNER, **kwargs)
        return directory, output.getvalue(), errors.getvalue()

    def test_fresh_directory_private_files_distinct_tokens_and_disabled_export(self):
        directory, output, errors = self.create()
        self.assertEqual(directory, self.root / "addon")
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        expected = {"dsh-token", "muse-token", "broker.json", "dsh-router.json", "muse-client.json"}
        self.assertEqual({file.name for file in directory.iterdir()}, expected)
        for file in directory.iterdir():
            self.assertTrue(file.is_file())
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        dsh_token = (directory / "dsh-token").read_text().strip()
        muse_token = (directory / "muse-token").read_text().strip()
        self.assertRegex(dsh_token, r"\A[0-9a-f]{64}\Z")
        self.assertRegex(muse_token, r"\A[0-9a-f]{64}\Z")
        self.assertNotEqual(dsh_token, muse_token)
        self.assertEqual(output, "")
        self.assertEqual(errors, "")
        broker = json.loads((directory / "broker.json").read_text())
        router = json.loads((directory / "dsh-router.json").read_text())
        muse = json.loads((directory / "muse-client.json").read_text())
        self.assertEqual({broker["owner"], router["owner"], muse["owner"]}, {OWNER})
        self.assertEqual(broker["bind"], "127.0.0.1")
        self.assertEqual(broker["port"], 18765)
        self.assertEqual(router["baseUrl"], muse["baseUrl"])
        self.assertEqual(router["baseUrl"], "http://127.0.0.1:18765")
        self.assertEqual(router["tokenFile"], broker["dshTokenFile"])
        self.assertEqual(muse["tokenFile"], broker["museTokenFile"])
        self.assertFalse(router["enabled"])
        for permission in ("consentExport", "sharePersona", "shareUserMemories", "allowCrossChatUserMemories"):
            self.assertFalse(router[permission])
        self.assertEqual(router["normalTurnsToReturn"], 2)
        self.assertEqual(broker["onebotTokenFile"], "")
        for value, suffix in ((broker["stateDir"], "broker-state"), (router["stateDir"], "router-state"), (muse["stateDir"], "muse-state")):
            self.assertEqual(Path(value), directory / suffix)
            self.assertFalse(Path(value).exists(), "configuration must not start runtime state")
        public_settings = json.dumps([broker, router, muse])
        self.assertTrue(all(token not in public_settings for token in (dsh_token, muse_token)), "tokens belong only in private token files")

    def test_refuses_existing_directory_without_overwriting_live_settings(self):
        directory = self.root / "existing"
        directory.mkdir(mode=0o700)
        (directory / "dsh-token").write_bytes(b"[synthetic existing credential]\n")
        (directory / "broker.json").write_bytes(b'{"existing":true}\n')
        (directory / "unrelated.txt").write_bytes(b"[existing unrelated settings]\n")
        before = {file.name: file.read_bytes() for file in directory.iterdir()}
        with self.assertRaises(FileExistsError):
            configure(directory, OWNER)
        self.assertEqual({file.name: file.read_bytes() for file in directory.iterdir()}, before)

    def test_second_configuration_does_not_rotate_tokens_or_enable_router(self):
        directory, _, _ = self.create()
        before = {file.name: file.read_bytes() for file in directory.iterdir()}
        with self.assertRaises(FileExistsError):
            configure(directory, OWNER, onebot_token="synthetic-onebot-token")
        self.assertEqual({file.name: file.read_bytes() for file in directory.iterdir()}, before)
        self.assertFalse(json.loads((directory / "dsh-router.json").read_text())["enabled"])

    def test_invalid_owner_is_rejected_before_any_directory_is_created(self):
        for index, owner in enumerate(("", "012345", "1234", "not-numeric", "12345\n", "1" * 21, 0)):
            with self.subTest(owner_case=index):
                target = self.root / f"invalid-owner-{index}"
                with self.assertRaisesRegex(ValueError, "numeric owner"):
                    configure(target, owner)
                self.assertFalse(target.exists())

    def test_public_or_nonliteral_bind_is_rejected_before_directory_creation(self):
        for index, bind in enumerate(("0.0.0.0", "192.168.1.1", "example.tail0000.ts.net", "::1", "100.63.255.255", "100.128.0.1", "127.0.0.1:18765", "")):
            with self.subTest(bind_case=index):
                target = self.root / f"invalid-bind-{index}"
                with self.assertRaisesRegex(ValueError, "private IPv4"):
                    configure(target, OWNER, bind=bind)
                self.assertFalse(target.exists())

    def test_tailnet_literal_bind_is_preserved_without_public_listener(self):
        directory, output, errors = self.create(bind="100.64.0.23")
        broker = json.loads((directory / "broker.json").read_text())
        router = json.loads((directory / "dsh-router.json").read_text())
        self.assertEqual(broker["bind"], "100.64.0.23")
        self.assertEqual(router["baseUrl"], "http://100.64.0.23:18765")
        self.assertEqual(output + errors, "")

    def test_optional_onebot_token_only_in_its_private_file_and_never_output(self):
        token = "synthetic-onebot-secret-for-offline-test"
        directory, output, errors = self.create(onebot_token=token)
        secret_path = directory / "onebot-token"
        self.assertEqual(secret_path.read_text(), token + "\n")
        self.assertEqual(stat.S_IMODE(secret_path.stat().st_mode), 0o600)
        broker = json.loads((directory / "broker.json").read_text())
        self.assertEqual(broker["onebotTokenFile"], str(secret_path))
        self.assertTrue(token not in output + errors, "configuration must not emit the supplied credential")
        for file in directory.iterdir():
            if file != secret_path:
                self.assertTrue(token not in file.read_text(), "OneBot credential must not be copied into other settings")

    @unittest.skipUnless(shutil.which("node"), "Node.js required for generated-router contract")
    def test_generated_names_are_accepted_by_real_node_router_with_export_still_disabled(self):
        directory, _, _ = self.create()
        module = (Path(__file__).parent / "client" / "router.mjs").resolve().as_uri()
        config_file = directory / "dsh-router.json"
        code = """
import fs from 'node:fs';
const {createRouter} = await import(process.argv[1]);
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const input = {chat:{type:'private',id:config.owner},sender:{userId:config.owner},
 event:{message_type:'private',user_id:config.owner,message_id:1,time:123},text:'NSFW [synthetic topic label]'};
let snapshots = 0;
const disabled = createRouter({config,snapshot:async()=>{snapshots++;return {history:[],memories:[]}}});
const initial = await disabled.intercept(input);
await disabled.dispose();
const enabled = createRouter({config:{...config,enabled:true},snapshot:async()=>{snapshots++;return {history:[],memories:[]}}});
const gated = await enabled.intercept(input);
await enabled.dispose();
process.stdout.write(JSON.stringify({initial,gated,snapshots}));
"""
        completed = subprocess.run(["node", "--input-type=module", "-e", code, module, str(config_file)],
                                   capture_output=True, text=True, timeout=15, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["initial"], {"handled": False, "route": "dsh", "reason": "disabled"})
        self.assertEqual(result["gated"], {"handled": True, "route": "blocked", "reason": "export_not_authorized"})
        self.assertEqual(result["snapshots"], 0)
        self.assertFalse(json.loads(config_file.read_text())["enabled"], "in-memory check must not activate the stored configuration")
        self.assertTrue(all(file.read_text().strip() not in completed.stdout + completed.stderr for file in (directory / "dsh-token", directory / "muse-token")), "runtime contract check must not print credentials")


if __name__ == "__main__":
    unittest.main()
