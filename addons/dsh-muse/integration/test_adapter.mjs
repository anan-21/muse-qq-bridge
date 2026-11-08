import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { test, after } from 'node:test';

const here = path.dirname(fileURLToPath(import.meta.url));
const moduleDir = fs.mkdtempSync(path.join(os.tmpdir(), 'fish-muse-adapter-'));
fs.copyFileSync(path.join(here, 'fish-muse.mjs'), path.join(moduleDir, 'fish-muse.mjs'));
fs.writeFileSync(path.join(moduleDir, 'router.mjs'), 'export function createRouter() { throw new Error("unexpected_router_init"); }\n');
const { createFishMuse, sessionHistory, mindSnapshot, privateUserText, readFishSession } = await import(pathToFileURL(path.join(moduleDir, 'fish-muse.mjs')));
after(() => fs.rmSync(moduleDir, { recursive: true, force: true }));
const owner = '10001';
const input = { chat: { type: 'private', key: `private:${owner}`, id: Number(owner) }, sender: { userId: Number(owner), isOwner: true }, event: { message_id: 8, message_type: 'private', user_id: Number(owner), message: [{ type: 'text', data: { text: 'example' } }] }, text: 'example' };
const wrapped = text => `recalled other-chat memory must be absent\n\n【新消息】QQ私聊｜说话人：Owner(${owner})｜身份：主人\n【正文】\n${text.split('\n').map(line => `> ${line}`).join('\n')}`;
const user = text => ({ type: 'user/message', data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] } });
const assistant = (text, extra = []) => ({ type: 'assistant/message', data: { message: { role: 'assistant', content: [{ type: 'text', text }, ...extra] } } });
function fixture(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'fish-muse-data-'));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  fs.mkdirSync(path.join(dir, 'mind'));
  const json = (file, value) => fs.writeFileSync(path.join(dir, 'mind', file), JSON.stringify(value));
  return { dir, json };
}

test('inert without env; disabled config never constructs router', async t => {
  const { dir } = fixture(t);
  const a = createFishMuse({ dataDir: dir, owners: new Set([10001]), env: {} });
  assert.equal(a.enabled, false);
  assert.equal((await a.intercept(input)).handled, false);
  assert.equal(a.rejectsMedia({ ...input, event: { ...input.event, message: '[CQ:image,file=x]' } }), false);
  const file = path.join(dir, 'config.json');
  fs.writeFileSync(file, JSON.stringify({ enabled: false }), { mode: 0o600 });
  assert.equal(createFishMuse({ env: { FISH_MUSE_CONFIG_FILE: file } }).enabled, false);
});

test('history strips wrappers and only final assistant text, no tools/system/reasoning', () => {
  const events = [
    { type: 'system/message', data: { content: 'never export system' } },
    user(wrapped('hello\nsecond line')),
    assistant('intermediate tool commentary'),
    { type: 'tool/result', data: { text: 'never export tools' } },
    assistant('final reply', [{ type: 'reasoning', text: 'never export reasoning' }]),
    { type: 'turn/end', data: {} },
    user('【任务】internal diary, not a QQ message'), assistant('private task result'), { type: 'turn/end', data: {} },
  ];
  const expected = [{ role: 'user', text: 'hello\nsecond line' }, { role: 'assistant', text: 'final reply' }];
  assert.deepEqual(sessionHistory({ events }, owner), expected);
  assert.deepEqual(sessionHistory({ seq: events.length, eventAt: index => events[index] }, owner), expected);
  assert.equal(JSON.stringify(expected).includes('other-chat'), false);
});

test('unknown/foreign user wrappers fail closed; quote data is not exported', () => {
  assert.throws(() => privateUserText([{ type: 'text', text: 'raw unverified history' }], owner), /wrapper/);
  assert.throws(() => privateUserText([{ type: 'text', text: wrapped('hi').replace('(10001)', '(10002)') }], owner), /identity/);
  assert.throws(() => privateUserText([{ type: 'text', text: wrapped('hi') + '\nnot a fenced body' }], owner), /wrapper/);
  const value = wrapped('hello').replace('【正文】', '【引用了这条消息】\n> do not export a quoted third-party message\n【正文】');
  assert.equal(privateUserText([{ type: 'text', text: value }], owner), 'hello');
  assert.throws(() => privateUserText([{ type: 'image', url: 'ignored' }], owner), /shape/);
});

test('active memories exact private provenance; user/cross-chat requires explicit consent', t => {
  const { dir, json } = fixture(t);
  json('memories.json', [
    { id: 'private', scope: 'chat:private:10001', source: 'private:10001', text: 'own private fact' },
    { id: 'spoof', scope: 'chat:private:10001', source: 'group:777', text: 'wrong provenance' },
    { id: 'user', scope: 'user:10001', source: 'private:10001', text: 'own user fact' },
    { id: 'cross', scope: 'user:10001', source: 'group:777', text: 'cross-chat own user fact' },
    { id: 'manual', scope: 'chat:private:10001', source: 'manual', text: 'manual has no provenance' },
    { id: 'other', scope: 'user:10002', source: 'private:10001', text: 'other person' },
    { id: 'group', scope: 'chat:group:777', source: 'group:777', text: 'group fact' },
  ]);
  json('archive.json', [{ id: 'archive', scope: 'chat:private:10001', text: 'archived' }]);
  json('pending.json', [{ id: 'pending', text: 'not confirmed' }]);
  assert.deepEqual(mindSnapshot(dir, owner).memories.map(row => row.id), ['private']);
  assert.throws(() => mindSnapshot(dir, owner, { shareUserMemories: true }), /cross_chat_consent/);
  assert.throws(() => mindSnapshot(dir, owner, { allowCrossChatUserMemories: true }), /cross_chat_consent/);
  assert.deepEqual(mindSnapshot(dir, owner, { shareUserMemories: true, allowCrossChatUserMemories: true }).memories.map(row => row.id), ['private', 'user', 'cross']);
});

test('persona opt-in, selected card only, global and other-chat absolutes excluded', t => {
  const { dir, json } = fixture(t);
  json('cards.json', { defaultCard: 'default', bindings: { 'private:10001': 'selected' }, cards: [
    { id: 'default', identity: 'unselected' },
    { id: 'selected', identity: 'selected data', absolute: [
      { id: 'own', scope: 'private:10001', text: 'own absolute' },
      { id: 'global', scope: '', text: 'shared global' },
      { id: 'other', scope: 'private:10002', text: 'private other' },
    ] },
  ] });
  assert.equal(mindSnapshot(dir, owner).persona, undefined);
  const value = mindSnapshot(dir, owner, { sharePersona: true });
  assert.equal(value.persona, 'identity: selected data');
  assert.deepEqual(value.memories.map(row => row.id), ['own']);
});

test('config ownership, source hooks, session snapshot and early media filter', async t => {
  const { dir, json } = fixture(t);
  json('memories.json', [{ id: 1, scope: 'chat:private:10001', source: 'private:10001', text: 'known fact' }]);
  const file = path.join(dir, 'config.json');
  fs.writeFileSync(file, JSON.stringify({ owner, consentExport: true }), { mode: 0o600 });
  let hooks;
  const notices = [];
  const adapter = createFishMuse({ dataDir: dir, owners: new Set([10001]), env: { FISH_MUSE_CONFIG_FILE: file },
    getSession: async () => ({ events: [user(wrapped('previous text')), assistant('previous answer')] }),
    sendControl: async value => notices.push(value),
    routerFactory: value => { hooks = value; return { intercept: async () => ({ handled: true }), dispose: async () => {} }; },
  });
  assert.equal(adapter.enabled, true);
  assert.equal(adapter.rejectsMedia(input), false);
  for (const message of ['[CQ:image,file=x]', [{ type: 'image', data: {} }], [{ type: 'reply', data: { id: 'x' } }]]) {
    assert.equal(adapter.rejectsMedia({ ...input, event: { ...input.event, message } }), true);
  }
  const other = { ...input, chat: { type: 'group', key: 'group:77', id: 77 }, event: { ...input.event, message_type: 'group', message: '[CQ:image,file=x]' } };
  assert.equal(adapter.rejectsMedia(other), false);
  await adapter.rejectMedia(input);
  assert.equal(notices.length, 1);
  assert.equal(notices[0].reason, 'unsupported_media');
  const snapshot = await hooks.snapshot(input);
  assert.deepEqual(snapshot.history, [{ role: 'user', text: 'previous text' }, { role: 'assistant', text: 'previous answer' }]);
  assert.equal(snapshot.memories.length, 1);
  await assert.rejects(hooks.snapshot(other), /scope/);
  assert.throws(() => createFishMuse({ dataDir: dir, owners: new Set([10002]), env: { FISH_MUSE_CONFIG_FILE: file } }), /owner/);
  fs.writeFileSync(file, JSON.stringify({ owner, shareUserMemories: true }), { mode: 0o600 });
  assert.throws(() => createFishMuse({ dataDir: dir, owners: new Set([10001]), env: { FISH_MUSE_CONFIG_FILE: file } }), /cross_chat_consent/);
  fs.chmodSync(file, 0o644);
  assert.throws(() => createFishMuse({ dataDir: dir, owners: new Set([10001]), env: { FISH_MUSE_CONFIG_FILE: file } }), /permissions/);
});

test('malformed active files fail closed instead of silently omitting memory', t => {
  const { dir } = fixture(t);
  fs.writeFileSync(path.join(dir, 'mind', 'memories.json'), '{ broken');
  assert.throws(() => mindSnapshot(dir, owner), /json/);
});

test('cold history uses read-only persistence; never resumes/creates an agent', async () => {
  const session = { events: [user(wrapped('old private turn'))] };
  const live = new Map([[input.chat.key, { handle: { agent: { session } } }]]);
  assert.equal(await readFishSession({ chat: input.chat, live, sessionIds: {} }), session);
  assert.equal(await readFishSession({ chat: input.chat, live: new Map(), sessionIds: {} }), undefined);
  const calls = [];
  const persistence = { open: async (id, mode, options) => {
    calls.push([id, mode, Boolean(options.signal)]);
    return { header: { id }, read: async () => ({ events: session.events }), close: async () => calls.push('closed') };
  } };
  const actual = await readFishSession({ chat: input.chat, live: new Map(), sessionIds: { [input.chat.key]: 'old-id' }, persistence });
  assert.deepEqual(actual, session);
  assert.deepEqual(calls, [['old-id', 'read', true], 'closed']);
  await assert.rejects(readFishSession({ chat: input.chat, live: new Map(), sessionIds: { [input.chat.key]: 'old-id' } }), /readonly/);
  let closed = false;
  const wrong = { open: async () => ({ header: { id: 'wrong' }, close: async () => { closed = true; } }) };
  await assert.rejects(readFishSession({ chat: input.chat, live: new Map(), sessionIds: { [input.chat.key]: 'old-id' }, persistence: wrong }), /identity/);
  assert.equal(closed, true);
});
