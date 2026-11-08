import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("fish_muse_install", HERE / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.plugin = Path(self.temporary.name) / "fish"
        (self.plugin / "lib").mkdir(parents=True)
        self.original = (HERE / "fixtures" / "pinned-index.js.txt").read_bytes()
        (self.plugin / "lib" / "index.js").write_bytes(self.original)
        (self.plugin / "package.json").write_bytes((HERE / "fixtures" / "pinned-package.json").read_bytes())

    def test_pinned_install_syntax_backup_and_restore(self):
        self.assertEqual(installer.digest(self.original), installer.INDEX_SHA256)
        installer.install(self.plugin)
        manifest = installer.installed_manifest(self.plugin)
        self.assertEqual(manifest["upstream"], installer.UPSTREAM_COMMIT)
        self.assertEqual((self.plugin / installer.BACKUP / "index.js").read_bytes(), self.original)
        patched = (self.plugin / "lib" / "index.js").read_text()
        self.assertLess(patched.index("museSwitch.rejectsMedia(museInput)"), patched.index("await renderMessage(event.message"))
        self.assertLess(patched.index("museSwitch.rejectsMedia(museInput)"), patched.index("mind.collectSticker(chat"))
        hook = patched.index("const route = await museSwitch.intercept")
        self.assertLess(hook, patched.index("logger.info(`${chat.key} <-"))
        self.assertLess(hook, patched.index("const reply = await ask(chat, sender, text"))
        self.assertIn('getSession: (chat) => readFishSession', patched)
        # Existing agent initialization remains byte-for-byte upstream.
        self.assertIn('async function ensureAgent(chat) {', patched)
        subprocess.run(["node", "--check", str(self.plugin / "lib" / "index.js")], check=True, capture_output=True)
        subprocess.run(["node", "--check", str(self.plugin / "lib" / "muse-switch" / "fish-muse.mjs")], check=True, capture_output=True)
        installer.restore(self.plugin)
        self.assertEqual((self.plugin / "lib" / "index.js").read_bytes(), self.original)
        self.assertFalse((self.plugin / "lib" / "muse-switch").exists())
        self.assertTrue(list(self.plugin.glob(".muse-switch-backup.restored-*")))

    def test_unknown_upstream_is_unchanged(self):
        target = self.plugin / "lib" / "index.js"
        value = self.original + b"\n// local customization\n"
        target.write_bytes(value)
        with self.assertRaisesRegex(ValueError, "unknown"):
            installer.install(self.plugin)
        self.assertEqual(target.read_bytes(), value)
        self.assertFalse((self.plugin / installer.BACKUP).exists())

    def test_local_changes_block_rollback(self):
        installer.install(self.plugin)
        target = self.plugin / "lib" / "muse-switch" / "router.mjs"
        changed = target.read_bytes() + b"\n// local edit\n"
        target.write_bytes(changed)
        with self.assertRaisesRegex(ValueError, "changed"):
            installer.restore(self.plugin)
        self.assertEqual(target.read_bytes(), changed)

    def test_cli_check_changes_nothing(self):
        result = subprocess.run(["python3", str(HERE / "install.py"), "--plugin", str(self.plugin), "--check"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("no files were changed", result.stdout)
        self.assertEqual((self.plugin / "lib" / "index.js").read_bytes(), self.original)
        self.assertFalse((self.plugin / installer.BACKUP).exists())

    def test_unknown_package_and_existing_directory_refused(self):
        (self.plugin / "package.json").write_text(json.dumps({"name": "dsh-fish", "version": "0.2.9"}))
        with self.assertRaisesRegex(ValueError, "0.2.8"):
            installer.install(self.plugin)
        (self.plugin / "package.json").write_bytes((HERE / "fixtures" / "pinned-package.json").read_bytes())
        (self.plugin / "lib" / "muse-switch").mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            installer.install(self.plugin)


if __name__ == "__main__":
    unittest.main()
