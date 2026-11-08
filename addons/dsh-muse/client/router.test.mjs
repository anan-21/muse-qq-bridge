import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { createRouter, validateBaseUrl } from './router.mjs';

const OWNER = '10001';
const TOKEN = 'test-only-handoff-token-not-a-live-secret';
const empty = () => ({ history: [], memories: [] });
function message(id, text, overrides = {}) {
  return { chat: { type: 'private', id: Number(OWNER), key: `private:${OWNER}` }, sender: { userId: Number(OWNER) }, event: { message_type: 'private', user_id: Number(OWNER), message_id: id, time: 1700000000 }, text, ...overrides };
}
async function fixture(t, options = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'dsh-router-test-'));
  fs.chmodSync(root, 0o700);
  const tokenFile = path.join(root, 'token');
  fs.writeFileSync(tokenFile, TOKEN, { mode: 0o600 });
  const posts = []; const gets = []; const controls = []; const jobs = new Map();
  let snapshotCalls = 0;
  const behavior = { pending: 0, unknown: 0, postStatus: 202, postPhase: 'queued', broken: false, ...options.behavior };
  const server = http.createServer(async (req, res) => {
    assert.equal(req.headers.authorization, `Bearer ${TOKEN}`);
    if (req.method === 'GET') {
      gets.push(req.url);
      if (req.url === '/dsh/status') {
        res.writeHead(behavior.statusStatus ?? 200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, pending: behavior.pending, unknown: behavior.unknown }));
        return;
      }
      const id = decodeURIComponent(req.url.slice('/dsh/jobs/'.length));
      if (!jobs.has(id)) { res.writeHead(404); res.end(JSON.stringify({ ok: false })); return; }
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: true, id, phase: jobs.get(id) }));
      return;
    }
    assert.equal(req.method, 'POST'); assert.equal(req.url, '/dsh/handoff');
    const chunks = [];
    for await (const chunk of req) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks));
    posts.push(body);
    jobs.set(body.id, behavior.postPhase);
    if (behavior.broken) { req.socket.destroy(); return; }
    if (behavior.hang) return;
    res.writeHead(behavior.postStatus, { 'Content-Type': 'application/json', ...(behavior.redirect ? { Location: behavior.redirect } : {}) });
    res.end(JSON.stringify({ ok: behavior.postStatus < 300, id: body.id, phase: behavior.postPhase }));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const config = { enabled: true, owner: OWNER, baseUrl: `http://127.0.0.1:${server.address().port}`, tokenFile, stateDir: path.join(root, 'state'), consentExport: true, timeoutMs: 1000, ...options.config };
  const routers = [];
  function create(overrides = {}) {
    const router = createRouter({ config: { ...config, ...overrides }, snapshot: async input => { snapshotCalls++; return (options.snapshot ?? empty)(input); }, sendControl: async value => { controls.push(value); } });
    routers.push(router);
    return router;
  }
  t.after(async () => {
    for (const router of routers) await router.dispose();
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    fs.rmSync(root, { recursive: true, force: true });
  });
  return { root, config, posts, gets, controls, jobs, behavior, create, get snapshotCalls() { return snapshotCalls; } };
}

test('ordinary owner messages reach DSH without snapshot or handoff; duplicates do not call DSH again', async t => {
  const f = await fixture(t); const router = f.create();
  assert.deepEqual(await router.intercept(message(1, '今天有什么安排')), { handled: false, route: 'dsh', reason: 'ordinary_topic' });
  assert.equal(f.snapshotCalls, 0); assert.equal(f.posts.length, 0);
  assert.deepEqual(await router.intercept(message(1, '今天有什么安排')), { handled: true, route: 'dsh', reason: 'duplicate' });
  const raw = fs.readFileSync(path.join(f.config.stateDir, 'router-state.json'), 'utf8');
  assert.equal(raw.includes('今天有什么安排'), false);
});

test('groups and other users are not intercepted and cannot export a snapshot', async t => {
  const f = await fixture(t); const router = f.create();
  for (const input of [
    message(1, 'NSFW', { chat: { type: 'group', id: 10001 } }),
    message(2, 'NSFW', { sender: { userId: 10002 } }),
    message(3, 'NSFW', { event: { message_id: 3, message_type: 'group', user_id: 10001 } }),
  ]) assert.equal((await router.intercept(input)).handled, false);
  assert.equal(f.snapshotCalls, 0); assert.equal(f.posts.length, 0);
});

test('local literal routing sends one scoped snapshot and never invokes DSH for a routed event', async t => {
  const snapshot = { history: [{ role: 'user', text: '之前讨论了一份阅读计划。' }, { role: 'assistant', text: '计划已记录。' }], memories: [{ id: 'm1', scope: `user:${OWNER}`, text: '偏好简短回复。', kind: 'preference' }, { id: 'm2', scope: `chat:private:${OWNER}`, text: '阅读计划每周更新。', kind: 'fact' }], persona: '以简短中文回复。' };
  const f = await fixture(t, { snapshot: () => snapshot, config: { sharePersona: true } }); const router = f.create();
  const outcome = await router.intercept(message(11, '切换到 NSFW 话题标签'));
  assert.equal(outcome.handled, true); assert.equal(outcome.route, 'muse'); assert.equal(f.posts.length, 1);
  const body = f.posts[0];
  const hash = crypto.createHash('sha256').update(JSON.stringify([`private:${OWNER}`, '11'])).digest('hex');
  assert.equal(body.id, `dshm:${hash}`); assert.equal(body.from, OWNER); assert.equal(body.conversation, `private:${OWNER}`);
  assert.deepEqual(body.snapshot, snapshot); assert.equal(body.message.time, 1700000000);
  assert.equal(JSON.stringify(body).includes(TOKEN), false);
});

test('ambiguous continuation stays Muse; ordinary streak returns only when all jobs are settled', async t => {
  const f = await fixture(t); const router = f.create();
  assert.equal((await router.intercept(message(1, 'NSFW'))).route, 'muse');
  assert.equal((await router.intercept(message(2, '继续刚才的话题'))).route, 'muse');
  assert.equal((await router.intercept(message(3, '工作安排'))).route, 'muse');
  f.behavior.pending = 1;
  assert.deepEqual(await router.intercept(message(4, '日程安排')), { handled: true, route: 'muse', reason: 'return_deferred_pending' });
  f.behavior.pending = 0; f.behavior.unknown = 1;
  assert.equal((await router.intercept(message(5, '文档内容'))).reason, 'return_deferred_pending');
  f.behavior.unknown = 0;
  assert.deepEqual(await router.intercept(message(6, '工作内容')), { handled: false, route: 'dsh', reason: 'ordinary_topic_return' });
  assert.equal(f.posts.length, 5); assert.equal(f.gets.filter(url => url === '/dsh/status').length, 3);
});

test('strong ordinary task returns immediately but status failure blocks dispatch', async t => {
  const f = await fixture(t); const router = f.create();
  await router.intercept(message(1, 'NSFW'));
  f.behavior.statusStatus = 503;
  assert.equal((await router.intercept(message(2, '帮我写代码'))).route, 'blocked');
  assert.equal(f.posts.length, 1);
  f.behavior.statusStatus = 200;
  assert.equal((await router.intercept(message(3, '帮我写代码'))).route, 'dsh');
});

test('manual modes, inline commands, and auto mode do not bounce on ambiguous replies', async t => {
  const f = await fixture(t); const router = f.create();
  assert.equal((await router.intercept(message(1, '/muse'))).route, 'control');
  assert.equal(f.snapshotCalls, 0);
  assert.equal((await router.intercept(message(2, '普通继续'))).route, 'muse');
  assert.equal((await router.intercept(message(3, '/auto'))).route, 'control');
  assert.equal((await router.intercept(message(4, '继续'))).route, 'muse');
  assert.deepEqual(await router.intercept(message(5, '/dsh 普通消息')), { handled: false, route: 'dsh', reason: 'manual_dsh', text: '普通消息' });
  assert.equal((await router.intercept(message(6, '/muse 继续'))).route, 'muse');
  assert.equal(f.posts.at(-1).message.text, '继续');
});

test('export consent is explicit and rejected snapshots never fall through', async t => {
  const f = await fixture(t, { config: { consentExport: false } }); const router = f.create();
  assert.equal((await router.intercept(message(1, 'NSFW'))).reason, 'export_not_authorized');
  assert.equal(f.snapshotCalls, 0); assert.equal(f.posts.length, 0);
});

for (const [name, snapshot, config] of [
  ['other-user memory', () => ({ history: [], memories: [{ id: 'x', scope: 'user:10002', text: '无关用户的记录。', kind: 'fact' }] }), {}],
  ['system history', () => ({ history: [{ role: 'system', text: '不得导出。' }], memories: [] }), {}],
  ['raw tool data', () => ({ history: [], memories: [], tools: [] }), {}],
  ['unapproved persona', () => ({ history: [], memories: [], persona: '一份设定。' }), {}],
  ['credential-shaped text', () => ({ history: [{ role: 'user', text: 'BRIDGE_TOKEN=not-a-real-credential' }], memories: [] }), {}],
  ['history limit', () => ({ history: [{ role: 'user', text: '一份记录。' }], memories: [] }), { maxHistory: 0 }],
  ['byte limit', () => ({ history: [{ role: 'user', text: '示例'.repeat(1000) }], memories: [] }), { maxContextBytes: 512 }],
]) test(`snapshot rejection: ${name} (no silent truncation)`, async t => {
  const f = await fixture(t, { snapshot, config }); const router = f.create();
  assert.deepEqual(await router.intercept(message(1, 'NSFW')), { handled: true, route: 'blocked', reason: 'snapshot_rejected' });
  assert.equal(f.posts.length, 0);
});

test('concurrent duplicates produce exactly one handoff and survive restart', async t => {
  const f = await fixture(t); let router = f.create();
  const results = await Promise.all([router.intercept(message(1, 'NSFW')), router.intercept(message(1, 'NSFW'))]);
  assert.equal(results[0].route, 'muse'); assert.equal(results[1].reason, 'duplicate');
  assert.equal(f.posts.length, 1); assert.equal(f.snapshotCalls, 1);
  await router.dispose(); router = f.create();
  assert.equal((await router.intercept(message(1, 'NSFW'))).reason, 'duplicate');
  assert.equal(f.posts.length, 1);
  assert.equal((await router.intercept(message(1, '不同内容'))).reason, 'event_conflict');
  assert.equal(f.posts.length, 1);
});

test('lost handoff response is unknown, reconciles by GET, and is never automatically posted again', async t => {
  const f = await fixture(t, { behavior: { broken: true } }); let router = f.create();
  assert.deepEqual(await router.intercept(message(1, 'NSFW')), { handled: true, route: 'blocked', reason: 'handoff_unknown' });
  assert.equal(f.posts.length, 1);
  await router.dispose(); router = f.create();
  f.behavior.broken = false;
  const duplicate = await router.intercept(message(1, 'NSFW'));
  assert.equal(duplicate.handled, true); assert.equal(duplicate.route, 'muse');
  assert.equal(f.posts.length, 1); assert.equal(f.gets.length, 1); assert.match(f.gets[0], /^\/dsh\/jobs\/dshm%3A/);
});

test('remote pending zero cannot erase a local unknown, including after restart', async t => {
  const f = await fixture(t, { behavior: { broken: true } }); let router = f.create();
  assert.equal((await router.intercept(message(1, 'NSFW'))).reason, 'handoff_unknown');
  const originalId = f.posts[0].id;
  f.jobs.clear(); f.behavior.broken = false;
  assert.deepEqual(await router.intercept(message(2, '帮我写代码')), { handled: true, route: 'muse', reason: 'return_deferred_unknown' });
  await router.dispose(); router = f.create();
  assert.deepEqual(await router.intercept(message(3, '说回正事')), { handled: true, route: 'muse', reason: 'return_deferred_unknown' });
  assert.equal(f.posts.filter(row => row.id === originalId).length, 1);
  assert.equal(f.posts.length, 3);
  assert.equal(f.gets.filter(url => url === '/dsh/status').length, 0);
});

test('unrecognized/failed HTTP responses do not fall through or retry POST', async t => {
  const f = await fixture(t, { behavior: { postStatus: 401 } }); const router = f.create();
  assert.equal((await router.intercept(message(1, 'NSFW'))).route, 'blocked');
  f.jobs.clear();
  assert.equal((await router.intercept(message(1, 'NSFW'))).route, 'blocked');
  assert.equal(f.posts.length, 1);
});

test('redirect responses are not followed', async t => {
  const f = await fixture(t, { behavior: { postStatus: 302, redirect: '/redirect-target' } }); const router = f.create();
  assert.equal((await router.intercept(message(1, 'NSFW'))).reason, 'handoff_unknown');
  assert.equal(f.posts.length, 1); assert.equal(f.gets.length, 0);
});

test('a timed-out request stays unknown without a second POST', async t => {
  const f = await fixture(t, { config: { timeoutMs: 50 }, behavior: { hang: true } }); const router = f.create();
  assert.equal((await router.intercept(message(1, 'NSFW'))).reason, 'handoff_unknown');
  assert.equal(f.posts.length, 1);
  assert.equal((await router.intercept(message(1, 'NSFW'))).route, 'muse');
  assert.equal(f.posts.length, 1);
});

test('inherited proxy settings cannot redirect the handoff transport', async t => {
  const f = await fixture(t);
  const script = `
    const {createRouter} = await import(process.argv[1]);
    const router = createRouter({config: JSON.parse(process.argv[2]), snapshot: async()=>({history:[],memories:[]})});
    const result = await router.intercept(JSON.parse(process.argv[3]));
    await router.dispose();
    process.stdout.write(JSON.stringify(result));
  `;
  const { stdout } = await promisify(execFile)(process.execPath, ['--input-type=module', '-e', script, new URL('./router.mjs', import.meta.url).href, JSON.stringify(f.config), JSON.stringify(message(1, 'NSFW'))], {
    env: { ...process.env, HTTP_PROXY: 'http://127.0.0.1:1', HTTPS_PROXY: 'http://127.0.0.1:1', ALL_PROXY: 'http://127.0.0.1:1', NO_PROXY: '', NODE_USE_ENV_PROXY: '1' },
    timeout: 10000,
  });
  assert.equal(JSON.parse(stdout).route, 'muse'); assert.equal(f.posts.length, 1);
});

test('state and lock are private; a second active writer is refused', async t => {
  const f = await fixture(t); const router = f.create();
  await router.intercept(message(1, '普通消息'));
  assert.equal(fs.statSync(f.config.stateDir).mode & 0o777, 0o700);
  assert.equal(fs.statSync(path.join(f.config.stateDir, 'router-state.json')).mode & 0o777, 0o600);
  assert.equal(fs.statSync(path.join(f.config.stateDir, 'router.lock')).mode & 0o777, 0o600);
  assert.throws(() => f.create(), /state_locked/);
  await router.dispose();
  assert.equal(fs.existsSync(path.join(f.config.stateDir, 'router.lock')), false);
});

test('stale locks can be recovered without erasing event deduplication', async t => {
  const f = await fixture(t); let router = f.create();
  await router.intercept(message(1, '普通消息')); await router.dispose();
  fs.writeFileSync(path.join(f.config.stateDir, 'router.lock'), JSON.stringify({ pid: 2147483647, nonce: 'stale' }), { mode: 0o600 });
  router = f.create();
  assert.equal((await router.intercept(message(1, '普通消息'))).reason, 'duplicate');
});

test('tokens and existing state directories reject unsafe permissions and symlinks', async t => {
  const f = await fixture(t);
  fs.chmodSync(f.config.tokenFile, 0o644);
  assert.throws(() => f.create(), /invalid_token_file/);
  fs.chmodSync(f.config.tokenFile, 0o600);
  const link = path.join(f.root, 'token-link'); fs.symlinkSync(f.config.tokenFile, link);
  assert.throws(() => f.create({ tokenFile: link }), /invalid_token_file/);
  fs.mkdirSync(f.config.stateDir, { mode: 0o755 }); fs.chmodSync(f.config.stateDir, 0o755);
  assert.throws(() => f.create(), /insecure_state_dir/);
});

test('private destination validation forbids public hosts, credentials, URL prefixes, and queries', () => {
  for (const url of ['http://127.0.0.1:18765', 'http://localhost:18765', 'http://[::1]:18765', 'http://100.64.0.1:18765', 'https://example.tail0000.ts.net:18765']) assert.doesNotThrow(() => validateBaseUrl(url));
  for (const url of ['https://example.com', 'http://100.63.255.255', 'http://100.128.0.1', 'http://192.168.1.1', 'http://user:pass@127.0.0.1', 'http://127.0.0.1/prefix', 'http://127.0.0.1/?secret=value', 'file:///tmp/example']) assert.throws(() => validateBaseUrl(url));
});

test('missing stable IDs, exhausted journal, and disabled mode remain explicit', async t => {
  const f = await fixture(t, { config: { maxJournalEvents: 1 } }); const router = f.create();
  assert.equal((await router.intercept(message(undefined, '普通消息'))).route, 'blocked');
  assert.equal((await router.intercept(message(1, '普通消息'))).handled, false);
  assert.equal((await router.intercept(message(2, '普通消息'))).reason, 'journal_full');
  assert.equal((await createRouter({ config: { enabled: false } }).intercept(message(1, 'NSFW'))).handled, false);
});
