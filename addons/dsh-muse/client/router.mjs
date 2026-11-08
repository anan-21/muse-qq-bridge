import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import http from 'node:http';
import https from 'node:https';
import dns from 'node:dns';
import net from 'node:net';

const DEFAULT_MUSE = ['NSFW', '18+', '成人向', '成人话题', '暧昧'];
const DEFAULT_DSH = ['代码', '报错', '天气', '日程', '文档', '工作', '数学', '翻译', '食谱', '新闻', 'code', 'weather', 'schedule'];
const STRONG_DSH = ['帮我写代码', '解释这段代码', '修复报错', '查一下天气', '今天的天气', '安排日程', '总结文档', '翻译这段', '说回正事', '聊点正事', '恢复普通话题', '回到普通话题'];
const PHASES = new Set(['queued', 'sending', 'sent', 'acked', 'unknown']);
const PRIVATE_FIELDS = /(?:BRIDGE_TOKEN|DSH_MUSE_TOKEN|FISH_MIND_TOKEN)\s*[:=]|Authorization\s*:\s*Bearer\s+|-----BEGIN [A-Z ]*PRIVATE KEY-----/iu;
const sha = value => crypto.createHash('sha256').update(value).digest('hex');
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype;
const validOwner = value => /^(?:[1-9]\d{0,19})$/.test(String(value));

function fail(code) { const error = new Error(code); error.code = code; throw error; }
function onlyKeys(value, keys) { return plain(value) && Object.keys(value).every(key => keys.includes(key)); }
function numericLimit(value, fallback, min, max) {
  const result = value ?? fallback;
  if (!Number.isSafeInteger(result) || result < min || result > max) fail('invalid_limit');
  return result;
}
function isLoopback(address) {
  if (address === '::1') return true;
  return net.isIP(address) === 4 && Number(address.split('.')[0]) === 127;
}
function isTailnet(address) {
  if (net.isIP(address) !== 4) return false;
  const [a, b] = address.split('.').map(Number);
  return a === 100 && b >= 64 && b <= 127;
}

/** Validate the destination, including the resolved address for MagicDNS names. */
export function validateBaseUrl(value = 'http://127.0.0.1:18765') {
  let url;
  try { url = new URL(value); } catch { fail('invalid_base_url'); }
  const host = url.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== '/') fail('invalid_base_url');
  if (!(isLoopback(host) || isTailnet(host) || host === 'localhost' || (host.endsWith('.ts.net') && /^[a-z0-9.-]+$/.test(host)))) fail('non_private_base_url');
  return url;
}

function privateLookup(hostname, options, callback) {
  const host = hostname.toLowerCase();
  if (host === 'localhost') {
    return callback(null, options?.all ? [{ address: '127.0.0.1', family: 4 }] : '127.0.0.1', 4);
  }
  dns.lookup(host, { family: 4, all: true }, (error, addresses) => {
    if (error) return callback(Object.assign(new Error('private_dns_failed'), { code: 'private_dns_failed' }));
    if (!addresses.length || addresses.some(item => !isTailnet(item.address))) {
      return callback(Object.assign(new Error('non_private_dns'), { code: 'non_private_dns' }));
    }
    return callback(null, options?.all ? addresses : addresses[0].address, 4);
  });
}

function privateFile(file, maxBytes) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0 || (process.getuid && stat.uid !== process.getuid())) fail('insecure_private_file');
  if (stat.size > maxBytes) fail('private_file_too_large');
  return fs.readFileSync(file, 'utf8');
}

function openState(directory, owner) {
  if (typeof directory !== 'string' || !path.isAbsolute(directory)) fail('invalid_state_dir');
  fs.mkdirSync(directory, { mode: 0o700, recursive: true });
  const stat = fs.lstatSync(directory);
  if (!stat.isDirectory() || stat.isSymbolicLink() || (stat.mode & 0o077) !== 0 || (process.getuid && stat.uid !== process.getuid())) fail('insecure_state_dir');
  const lockPath = path.join(directory, 'router.lock');
  const nonce = crypto.randomBytes(16).toString('hex');
  function acquire() {
    const fd = fs.openSync(lockPath, 'wx', 0o600);
    try { fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, nonce })); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  }
  try { acquire(); } catch (error) {
    if (error.code !== 'EEXIST') throw error;
    let lock;
    try { lock = JSON.parse(privateFile(lockPath, 4096)); } catch { fail('invalid_state_lock'); }
    if (!Number.isInteger(lock.pid) || lock.pid <= 0) fail('invalid_state_lock');
    try { process.kill(lock.pid, 0); fail('state_locked'); } catch (probe) {
      if (probe.code !== 'ESRCH') throw probe;
    }
    fs.unlinkSync(lockPath);
    acquire();
  }
  const statePath = path.join(directory, 'router-state.json');
  let state;
  function release() {
    try {
      const lock = JSON.parse(privateFile(lockPath, 4096));
      if (lock.nonce === nonce) fs.unlinkSync(lockPath);
    } catch { /* Never remove a lock we cannot prove is ours. */ }
  }
  try {
    if (fs.existsSync(statePath)) {
      try { state = JSON.parse(privateFile(statePath, 32 * 1024 * 1024)); } catch { fail('invalid_state'); }
      if (!plain(state) || state.version !== 1 || state.owner !== owner || !['auto', 'muse', 'dsh'].includes(state.mode) || !['muse', 'dsh'].includes(state.current) || !Number.isSafeInteger(state.normalStreak) || state.normalStreak < 0 || !plain(state.events)) fail('invalid_state');
      for (const [id, entry] of Object.entries(state.events)) {
        if (!/^[a-f0-9]{64}$/.test(id) || !plain(entry) || !['dsh', 'muse', 'control', 'blocked'].includes(entry.route) || typeof entry.fingerprint !== 'string') fail('invalid_state');
      }
    } else {
      state = { version: 1, owner, mode: 'auto', current: 'dsh', normalStreak: 0, events: {} };
    }
  } catch (error) { release(); throw error; }
  function save() {
    const temp = path.join(directory, `.router-state-${nonce}.tmp`);
    const fd = fs.openSync(temp, 'wx', 0o600);
    try {
      fs.writeFileSync(fd, `${JSON.stringify(state)}\n`);
      fs.fsyncSync(fd);
    } finally { fs.closeSync(fd); }
    try { fs.renameSync(temp, statePath); } catch (error) { fs.unlinkSync(temp); throw error; }
    const dir = fs.openSync(directory, 'r');
    try { fs.fsyncSync(dir); } finally { fs.closeSync(dir); }
  }
  return { state, save, release };
}

function request(base, token, timeout, method, suffix, payload) {
  return new Promise((resolve, reject) => {
    const url = new URL(suffix, base);
    const body = payload === undefined ? undefined : Buffer.from(JSON.stringify(payload));
    const headers = { Authorization: `Bearer ${token}`, Accept: 'application/json' };
    if (body) { headers['Content-Type'] = 'application/json'; headers['Content-Length'] = String(body.length); }
    const transport = url.protocol === 'https:' ? https : http;
    // Node core does not inherit HTTP_PROXY; redirects are never followed.
    const req = transport.request(url, { method, headers, agent: false, lookup: privateLookup, timeout }, res => {
      const chunks = []; let bytes = 0;
      res.on('data', chunk => {
        bytes += chunk.length;
        if (bytes > 65536) { res.destroy(); reject(new Error('response_too_large')); return; }
        chunks.push(chunk);
      });
      res.on('error', () => reject(new Error('response_failed')));
      res.on('end', () => {
        let data;
        try { data = JSON.parse(Buffer.concat(chunks).toString('utf8')); } catch { reject(new Error('invalid_response')); return; }
        resolve({ status: res.statusCode, data });
      });
    });
    req.on('timeout', () => req.destroy(new Error('request_timeout')));
    req.on('error', () => reject(new Error('transport_failed')));
    req.end(body);
  });
}

function validateSnapshot(value, config, token) {
  if (!onlyKeys(value, ['history', 'memories', 'persona']) || !Array.isArray(value.history) || !Array.isArray(value.memories)) fail('invalid_snapshot');
  if (value.history.length > config.maxHistory || value.memories.length > config.maxMemories) fail('context_limit');
  const checkedText = text => {
    if (typeof text !== 'string' || PRIVATE_FIELDS.test(text) || (token.length >= 16 && text.includes(token))) fail('unsafe_snapshot');
    return text;
  };
  const history = value.history.map(row => {
    if (!onlyKeys(row, ['role', 'text']) || !['user', 'assistant'].includes(row.role)) fail('invalid_snapshot');
    return { role: row.role, text: checkedText(row.text) };
  });
  const allowedScopes = new Set([`chat:private:${config.owner}`, `user:${config.owner}`]);
  const memories = value.memories.map(row => {
    if (!onlyKeys(row, ['id', 'scope', 'text', 'kind']) || !allowedScopes.has(row.scope) || !['string', 'number'].includes(typeof row.id) || typeof row.kind !== 'string' || !row.kind || row.kind.length > 128) fail('invalid_snapshot');
    return { id: String(row.id), scope: row.scope, text: checkedText(row.text), kind: row.kind };
  });
  const result = { history, memories };
  if (value.persona !== undefined) {
    if (!config.sharePersona) fail('persona_not_authorized');
    result.persona = checkedText(value.persona);
  }
  return result;
}

function keywordHit(text, words) {
  const normalized = text.normalize('NFKC').toLowerCase();
  return words.some(word => {
    const needle = word.normalize('NFKC').toLowerCase();
    if (/^[a-z]+$/.test(needle)) return new RegExp(`(?:^|[^a-z])${needle}(?:$|[^a-z])`, 'u').test(normalized);
    return normalized.includes(needle);
  });
}

/**
 * One owner, one state directory, one active writer. Call intercept before any
 * model, memory-observation, or content logging side effect in the caller.
 */
export function createRouter({ config = {}, snapshot, sendControl } = {}) {
  if (config.enabled === false) return { intercept: async () => ({ handled: false, route: 'dsh', reason: 'disabled' }), dispose: async () => {} };
  if (!['linux', 'darwin'].includes(process.platform)) fail('unsupported_platform');
  if (!validOwner(config.owner)) fail('invalid_owner');
  if (typeof snapshot !== 'function') fail('snapshot_required');
  const settings = {
    ...config, owner: String(config.owner),
    maxContextBytes: numericLimit(config.maxContextBytes, 2 * 1024 * 1024, 256, 2 * 1024 * 1024),
    maxHistory: numericLimit(config.maxHistory, 10000, 0, 100000),
    maxMemories: numericLimit(config.maxMemories, 10000, 0, 100000),
    maxJournalEvents: numericLimit(config.maxJournalEvents, 50000, 1, 100000),
    normalTurnsToReturn: numericLimit(config.normalTurnsToReturn, 2, 1, 100),
    timeoutMs: numericLimit(config.timeoutMs, 5000, 50, 60000),
    museKeywords: config.museKeywords ?? DEFAULT_MUSE,
    dshKeywords: config.dshKeywords ?? DEFAULT_DSH,
    strongDshKeywords: config.strongDshKeywords ?? STRONG_DSH,
  };
  for (const words of [settings.museKeywords, settings.dshKeywords, settings.strongDshKeywords]) {
    if (!Array.isArray(words) || words.some(word => typeof word !== 'string' || !word || word.length > 128)) fail('invalid_keywords');
  }
  const base = validateBaseUrl(settings.baseUrl);
  if (typeof settings.tokenFile !== 'string' || !path.isAbsolute(settings.tokenFile)) fail('invalid_token_file');
  let token;
  try { token = privateFile(settings.tokenFile, 4096).trim(); } catch { fail('invalid_token_file'); }
  if (!/^[A-Za-z0-9._~+\/-]{24,1024}={0,2}$/.test(token)) fail('invalid_token');
  const store = openState(settings.stateDir, settings.owner);
  const { state } = store;
  let queue = Promise.resolve(); let disposed = false;

  async function control(input, text, reason) {
    if (typeof sendControl === 'function') {
      try { await sendControl({ chat: input.chat, text, reason }); } catch { /* Provider dispatch must remain blocked. */ }
    }
  }
  const result = (handled, route, reason) => ({ handled, route, reason });
  function record(key, entry) { state.events[key] = entry; store.save(); }
  async function blocked(input, key, fingerprint, reason, entry = {}) {
    record(key, { fingerprint, route: 'blocked', reason, ...entry });
    await control(input, '当前消息未转交给任何新的模型请求，请稍后查看桥接状态。', reason);
    return result(true, 'blocked', reason);
  }
  async function status() {
    const response = await request(base, token, settings.timeoutMs, 'GET', '/dsh/status');
    const value = response.data;
    if (response.status !== 200 || !plain(value) || value.ok !== true || !Number.isSafeInteger(value.pending) || value.pending < 0 || !Number.isSafeInteger(value.unknown) || value.unknown < 0) fail('status_unavailable');
    return value;
  }
  async function reconcile(entry, id) {
    try {
      const response = await request(base, token, settings.timeoutMs, 'GET', `/dsh/jobs/${encodeURIComponent(id)}`);
      if (response.status === 200 && response.data?.ok === true && response.data.id === id && PHASES.has(response.data.phase)) {
        entry.phase = response.data.phase; store.save();
      }
    } catch { /* A failed GET never authorizes another POST or DSH dispatch. */ }
    return entry.phase !== 'unknown';
  }

  async function run(input) {
    if (disposed) fail('router_disposed');
    const { chat, sender, event } = input ?? {};
    if (chat?.type !== 'private' || String(chat.id) !== settings.owner || String(sender?.userId) !== settings.owner || (event?.message_type !== undefined && event.message_type !== 'private') || (event?.user_id !== undefined && String(event.user_id) !== settings.owner)) return result(false, 'dsh', 'outside_owner_private_chat');
    const text = input.text;
    const messageId = event?.message_id;
    if (typeof text !== 'string' || !text.trim() || !['string', 'number'].includes(typeof messageId) || !String(messageId) || String(messageId).length > 256 || /[\x00-\x1f]/.test(String(messageId)) || (typeof messageId === 'number' && !Number.isSafeInteger(messageId))) {
      await control(input, '消息缺少稳定标识，未交给模型处理。', 'invalid_event');
      return result(true, 'blocked', 'invalid_event');
    }
    const conversation = `private:${settings.owner}`;
    const key = sha(JSON.stringify([conversation, String(messageId)]));
    const id = `dshm:${key}`;
    const fingerprint = sha(JSON.stringify([conversation, String(messageId), text]));
    const old = state.events[key];
    if (old) {
      if (old.fingerprint !== fingerprint) {
        await control(input, '消息标识与已有记录冲突，未再次处理。', 'event_conflict');
        return result(true, 'blocked', 'event_conflict');
      }
      if (old.phase === 'unknown') {
        await reconcile(old, id);
      }
      return result(true, old.phase === 'unknown' ? 'blocked' : old.route, old.phase === 'unknown' ? 'handoff_unknown' : 'duplicate');
    }
    if (Object.keys(state.events).length >= settings.maxJournalEvents) {
      await control(input, '本地去重记录已满，未交给模型处理。', 'journal_full');
      return result(true, 'blocked', 'journal_full');
    }
    const command = text.match(/^\/(muse|dsh|auto)(?:\s+([\s\S]*))?$/iu);
    const content = command ? (command[2] ?? '').trim() : text;
    const priorMode = state.mode;
    if (command) state.mode = command[1].toLowerCase();
    let target;
    let reason;
    if (state.mode === 'muse') { target = 'muse'; reason = 'manual_muse'; state.normalStreak = 0; }
    else if (state.mode === 'dsh') { target = 'dsh'; reason = 'manual_dsh'; state.normalStreak = 0; }
    else if (keywordHit(content, settings.museKeywords)) { target = 'muse'; reason = 'topic_muse'; state.normalStreak = 0; }
    else if (state.current === 'muse') {
      if (keywordHit(content, settings.dshKeywords)) state.normalStreak++;
      else state.normalStreak = 0;
      target = keywordHit(content, settings.strongDshKeywords) || state.normalStreak >= settings.normalTurnsToReturn ? 'dsh' : 'muse';
      reason = target === 'dsh' ? 'ordinary_topic_return' : 'muse_continuation';
    } else { target = 'dsh'; reason = 'ordinary_topic'; state.normalStreak = 0; }
    if (target === 'dsh' && state.current === 'muse') {
      try {
        // The broker cannot report a request that failed before arrival. Its
        // empty queue must not erase a locally persisted unknown dispatch.
        for (const entry of Object.values(state.events)) {
          if (entry.phase === 'unknown' && !(await reconcile(entry, entry.jobId))) {
            target = 'muse'; reason = 'return_deferred_unknown'; break;
          }
        }
        if (target === 'dsh') {
          const active = await status();
          if (active.pending || active.unknown) { target = 'muse'; reason = 'return_deferred_pending'; }
        }
      } catch {
        state.mode = priorMode;
        return blocked(input, key, fingerprint, 'return_status_unavailable');
      }
    }
    state.current = target;
    if (command && !content) {
      record(key, { fingerprint, route: 'control', reason });
      const notice = reason.startsWith('return_deferred_') ? 'Muse 仍有未完成或状态不明的任务，暂缓切回 DSH。' : (target === 'muse' ? '当前由 Muse 接续；自动模式下普通话题会切回 DSH。' : '当前由 DSH 接续。');
      await control(input, notice, reason);
      return result(true, 'control', reason);
    }
    if (target === 'dsh') {
      record(key, { fingerprint, route: 'dsh', reason });
      // A command with inline text must not be sent literally to DSH.
      return { ...result(false, 'dsh', reason), ...(command ? { text: content } : {}) };
    }
    if (settings.consentExport !== true) return blocked(input, key, fingerprint, 'export_not_authorized');
    let payload;
    try {
      const exported = validateSnapshot(await snapshot({ ...input, text: content }), settings, token);
      const rawTime = event.time;
      const time = typeof rawTime === 'number' && Number.isFinite(rawTime) ? rawTime : (typeof rawTime === 'string' && rawTime.length < 128 ? rawTime : new Date().toISOString());
      payload = { version: 1, id, from: settings.owner, conversation, message: { id: String(messageId), text: content, time }, snapshot: exported };
      if (PRIVATE_FIELDS.test(content) || (token.length >= 16 && content.includes(token))) fail('unsafe_snapshot');
      if (Buffer.byteLength(JSON.stringify(payload)) > settings.maxContextBytes) fail('context_limit');
    } catch {
      return blocked(input, key, fingerprint, 'snapshot_rejected');
    }
    const entry = { fingerprint, route: 'muse', reason, phase: 'unknown', jobId: id, payloadHash: sha(JSON.stringify(payload)) };
    // Persist unknown BEFORE handing bytes to the socket. Crash recovery can only
    // GET this job; it must never repeat this POST automatically.
    record(key, entry);
    try {
      const response = await request(base, token, settings.timeoutMs, 'POST', '/dsh/handoff', payload);
      if (![200, 201, 202].includes(response.status) || response.data?.ok !== true || response.data.id !== id || !PHASES.has(response.data.phase)) fail('handoff_unconfirmed');
      entry.phase = response.data.phase; store.save();
      if (entry.phase === 'unknown') {
        await control(input, 'Muse 取件状态尚未确认，未改由 DSH 重发。', 'handoff_unknown');
        return result(true, 'blocked', 'handoff_unknown');
      }
      return result(true, 'muse', reason);
    } catch {
      await control(input, 'Muse 取件状态尚未确认，未改由 DSH 重发。', 'handoff_unknown');
      return result(true, 'blocked', 'handoff_unknown');
    }
  }
  return {
    intercept(input) {
      const pending = queue.then(() => run(input)).catch(async () => {
        await control(input ?? {}, '路由器暂不可用，未交给模型处理。', 'router_unavailable');
        return result(true, 'blocked', 'router_unavailable');
      });
      queue = pending.catch(() => {});
      return pending;
    },
    async dispose() { await queue; if (!disposed) { disposed = true; store.release(); } },
  };
}
