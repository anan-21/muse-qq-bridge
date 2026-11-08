import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { execFileSync } from 'node:child_process';
import { test } from 'node:test';

const here = path.dirname(fileURLToPath(import.meta.url));

test('patched real upstream event loop intercepts text and rejects media before any mind/model/logging', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fish-muse-hook-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.mkdirSync(path.join(dir, 'lib'));
  fs.copyFileSync(path.join(here, 'fixtures', 'pinned-index.js.txt'), path.join(dir, 'lib', 'index.js'));
  fs.copyFileSync(path.join(here, 'fixtures', 'pinned-package.json'), path.join(dir, 'package.json'));
  execFileSync('python3', [path.join(here, 'install.py'), '--plugin', dir, '--install']);
  // Keep the real patched control flow and adapter; replace only dependencies.
  let source = fs.readFileSync(path.join(dir, 'lib', 'index.js'), 'utf8');
  source = source.split('\n').filter(line => !(line.startsWith('import ') && !line.includes('"node:') && !line.includes('./muse-switch/'))).join('\n');
  source = `
const fixture = globalThis.__fishMuseHookFixture;
const value = { default() { return this; } };
const z = { object() { return value; }, string() { return value; }, number() { return value; }, boolean() { return value; }, array() { return value; } };
const installModelSelection = () => { throw new Error('unexpected model selection'); };
const createUserMessage = () => { throw new Error('unexpected user model message'); };
const loadExtensions = () => { throw new Error('unexpected extension load'); };
const createMindClient = () => new Proxy({}, { get(_target, key) { return () => { fixture.trace.push('mind:' + String(key)); throw new Error('unexpected mind call'); }; } });
class OneBot { constructor(options) { this.selfId = 999; fixture.bot = options; } async call(action) { fixture.trace.push('onebot:' + action); return { message_id: 1 }; } close() {} }
const renderMessage = async message => { fixture.trace.push('render'); return { text: message[0].data.text, quote: '', mentioned: false, targets: [], stickers: [] }; };
const splitReply = text => [text];
class Qzone {}
const GUIDE = '', cleanName = text => text, fence = text => text, oneLine = text => text, scrubReply = text => text;
const installToolPolicy = () => { throw new Error('unexpected agent tool setup'); };
` + source;
  fs.writeFileSync(path.join(dir, 'lib', 'index.js'), source);
  fs.writeFileSync(path.join(dir, 'lib', 'muse-switch', 'router.mjs'), `export function createRouter() { return { async intercept() { globalThis.__fishMuseHookFixture.trace.push('intercept'); return { handled: true }; }, async dispose() {} }; }`);
  const configPath = path.join(dir, 'config.json');
  fs.writeFileSync(configPath, JSON.stringify({ owner: '10001', consentExport: true }), { mode: 0o600 });
  const previous = process.env.FISH_MUSE_CONFIG_FILE;
  process.env.FISH_MUSE_CONFIG_FILE = configPath;
  t.after(() => { if (previous === undefined) delete process.env.FISH_MUSE_CONFIG_FILE; else process.env.FISH_MUSE_CONFIG_FILE = previous; delete globalThis.__fishMuseHookFixture; });
  globalThis.__fishMuseHookFixture = { trace: [] };
  const fixture = globalThis.__fishMuseHookFixture;
  const { apply } = await import(pathToFileURL(path.join(dir, 'lib', 'index.js')));
  const ctx = { effect() {}, get(name) { throw new Error('unexpected provider/session service: ' + name); } };
  apply(ctx, { dataDir: dir, owners: [10001], allowUsers: [], allowGroups: [], stickerEnabled: true, stickerCollect: true, qzoneEnabled: false });
  const event = { message_type: 'private', message_id: 10, user_id: 10001, sender: { nickname: 'Owner' }, message: [{ type: 'text', data: { text: 'NSFW topic label' } }] };
  await fixture.bot.onMessage(event);
  assert.deepEqual(fixture.trace, ['render', 'intercept']);
  assert.equal(fs.existsSync(path.join(dir, 'fish.log')), false);
  fixture.trace.length = 0;
  await fixture.bot.onMessage({ ...event, message_id: 11, message: [{ type: 'image', data: { file: 'not-read' } }] });
  assert.deepEqual(fixture.trace, ['onebot:send_private_msg']);
  assert.equal(fs.existsSync(path.join(dir, 'fish.log')), false);
});
