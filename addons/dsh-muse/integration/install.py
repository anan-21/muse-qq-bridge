#!/usr/bin/env python3
"""Offline, checksum-pinned dsh-fish integration. It never starts a service."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

UPSTREAM_COMMIT = "a16bb96e1f0997fda063ec46bc965622e58d6bb7"
INDEX_SHA256 = "6de60ecdde82c82cbda75123231407a849c3da2f0f05770a39212f618da045cd"
BACKUP = ".muse-switch-backup"
HERE = Path(__file__).resolve().parent


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise ValueError("pinned upstream anchor mismatch")
    return text.replace(old, new, 1)


def patch_index(raw: bytes) -> bytes:
    if digest(raw) != INDEX_SHA256:
        raise ValueError("unknown lib/index.js: checksum does not match pinned upstream")
    text = raw.decode("utf-8")
    text = replace_once(text, 'import { createMindClient } from "./mind-client.js";',
                        'import { createMindClient } from "./mind-client.js";\nimport { createFishMuse, readFishSession } from "./muse-switch/fish-muse.mjs";')
    text = replace_once(text, '\tconst mind = createMindClient(logger);', '''\tconst mind = createMindClient(logger);
\t// Inert without FISH_MUSE_CONFIG_FILE. No provider call during export.
\tconst museSwitch = createFishMuse({
\t\tdataDir, owners,
\t\tgetSession: (chat) => readFishSession({ chat, live, sessionIds, persistence: ctx.get("sessionPersistence") }),
\t\tsendControl: async ({ chat, text }) => {
\t\t\tconst receipt = await bot.call("send_private_msg", { user_id: chat.id, message: [{ type: "text", data: { text } }] });
\t\t\tif (receipt?.message_id === undefined || receipt.message_id === null) throw new Error("muse_control_receipt_missing");
\t\t}
\t});''')
    text = replace_once(text, '\t\tlet { text, quote, mentioned, targets, stickers } = await renderMessage(event.message, bot.selfId, lookupFor(isGroup ? chat.id : 0));', '''\t\t// Reject unsupported owner-private segments BEFORE render/collectSticker.
\t\tconst museInput = { chat, sender: { userId, isOwner }, event };
\t\tif (museSwitch.rejectsMedia(museInput)) {
\t\t\tawait enqueue(chat, () => museSwitch.rejectMedia(museInput)).catch(() => logger.warn("Muse unsupported-media notice failed"));
\t\t\treturn;
\t\t}
\t\tlet { text, quote, mentioned, targets, stickers } = await renderMessage(event.message, bot.selfId, lookupFor(isGroup ? chat.id : 0));''')
    text = replace_once(text, '\t\tawait enqueue(chat, async () => {\n\t\t\tlogger.info(`${chat.key} <- ${sender.nickname}: ${shown}${spontaneous ? "（旁观）" : ""}`);', '''\t\tawait enqueue(chat, async () => {
\t\t\t// Route before raw content logging, anchor/recall/observe or followup.
\t\t\tconst route = await museSwitch.intercept({ chat, sender, event, text, quote });
\t\t\tif (route.handled) return;
\t\t\tif (route.text !== undefined) text = route.text;
\t\t\tlogger.info(`${chat.key} <- ${sender.nickname}: ${shown}${spontaneous ? "（旁观）" : ""}`);''')
    text = replace_once(text, '\t\t\tbot.close();', '\t\t\tawait museSwitch.dispose();\n\t\t\tbot.close();')
    return text.encode("utf-8")


def regular(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError("expected a regular file")
    return path.read_bytes()


def atomic_write(path: Path, value: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".muse-switch-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(value)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def source_files() -> dict[str, bytes]:
    return {
        "lib/muse-switch/fish-muse.mjs": regular(HERE / "fish-muse.mjs"),
        "lib/muse-switch/router.mjs": regular(HERE.parent / "client" / "router.mjs"),
    }


def check_original(plugin: Path) -> bytes:
    package = json.loads(regular(plugin / "package.json"))
    if package.get("name") != "dsh-fish" or package.get("version") != "0.2.8":
        raise ValueError("expected pinned dsh-fish 0.2.8 package")
    raw = regular(plugin / "lib" / "index.js")
    if digest(raw) != INDEX_SHA256:
        raise ValueError("unknown lib/index.js: refusing to patch or overwrite")
    if (plugin / "lib" / "muse-switch").exists():
        raise ValueError("lib/muse-switch already exists without a recognized installation")
    return raw


def installed_manifest(plugin: Path) -> dict:
    backup = plugin / BACKUP
    if backup.is_symlink() or not backup.is_dir():
        raise ValueError("backup is not a regular directory")
    manifest = json.loads(regular(backup / "manifest.json"))
    if manifest.get("version") != 1 or manifest.get("upstream") != UPSTREAM_COMMIT:
        raise ValueError("unrecognized install manifest")
    expected = {"lib/index.js", "lib/muse-switch/fish-muse.mjs", "lib/muse-switch/router.mjs"}
    if set(manifest.get("installed", {})) != expected:
        raise ValueError("unrecognized installed file set")
    for relative, checksum in manifest["installed"].items():
        if digest(regular(plugin / relative)) != checksum:
            raise ValueError("installed file changed: refusing overwrite or rollback")
    if digest(regular(backup / "index.js")) != INDEX_SHA256:
        raise ValueError("original backup checksum mismatch")
    return manifest


def install(plugin: Path) -> None:
    if (plugin / BACKUP).exists():
        installed_manifest(plugin)
        raise ValueError("already installed; use --check or --restore first")
    original = check_original(plugin)
    payload = {"lib/index.js": patch_index(original), **source_files()}
    backup = plugin / BACKUP
    backup.mkdir(mode=0o700)
    old_mode = (plugin / "lib" / "index.js").stat().st_mode & 0o777
    changed = False
    try:
        atomic_write(backup / "index.js", original, 0o600)
        manifest = {"version": 1, "upstream": UPSTREAM_COMMIT, "original": INDEX_SHA256, "original_mode": old_mode,
                    "installed": {name: digest(raw) for name, raw in payload.items()}}
        atomic_write(backup / "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode(), 0o600)
        (plugin / "lib" / "muse-switch").mkdir(mode=0o755)
        for relative, raw in payload.items():
            if relative == "lib/index.js":
                continue
            atomic_write(plugin / relative, raw, 0o644)
        atomic_write(plugin / "lib" / "index.js", payload["lib/index.js"], old_mode)
        changed = True
        installed_manifest(plugin)
    except Exception:
        if changed:
            atomic_write(plugin / "lib" / "index.js", original, old_mode)
        switch = plugin / "lib" / "muse-switch"
        if switch.exists():
            shutil.rmtree(switch)
        shutil.rmtree(backup)
        raise


def restore(plugin: Path) -> None:
    manifest = installed_manifest(plugin)
    backup = plugin / BACKUP
    atomic_write(plugin / "lib" / "index.js", regular(backup / "index.js"), manifest["original_mode"])
    shutil.rmtree(plugin / "lib" / "muse-switch")
    # Preserve the backup and manifest as a non-secret audit record. Rename it so
    # a subsequent explicit installation cannot mistake it for an active patch.
    os.rename(backup, plugin / (BACKUP + ".restored-" + os.urandom(6).hex()))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True, help="absolute dsh-fish package directory")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--install", action="store_true")
    actions.add_argument("--restore", action="store_true")
    args = parser.parse_args()
    try:
        if not args.plugin.is_absolute() or args.plugin.is_symlink() or not args.plugin.is_dir():
            raise ValueError("--plugin must name an existing absolute directory")
        plugin = args.plugin.resolve()
        if args.check:
            if (plugin / BACKUP).exists():
                installed_manifest(plugin)
                print("installed checksums verified; no service was started")
            else:
                patch_index(check_original(plugin))
                source_files()
                print("pinned dsh-fish 0.2.8 is compatible; no files were changed")
        elif args.install:
            install(plugin)
            print("integration installed; inactive until FISH_MUSE_CONFIG_FILE is set; restart DSH yourself")
        else:
            restore(plugin)
            print("original index.js restored; backup retained; restart DSH yourself")
    except (ValueError, OSError, json.JSONDecodeError) as error:
        # Exception details are controlled codes/messages, never config values.
        print(f"refused: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
