// Pinned dsh-fish adapter. No model, mind API, or shell command is used here.
import fs from 'node:fs';
import path from 'node:path';
import { createRouter } from './router.mjs';

const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = code => { throw new Error(code); };

function readJson(file, { optional = false, privateFile = false, maximum = 32 * 1024 * 1024 } = {}) {
  let stat;
  try { stat = fs.lstatSync(file); } catch (error) {
    if (optional && error.code === 'ENOENT') return undefined;
    fail('muse_snapshot_file_unavailable');
  }
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > maximum) fail('muse_snapshot_file_invalid');
  if (privateFile && ((stat.mode & 0o077) !== 0 || (process.getuid && stat.uid !== process.getuid()))) fail('muse_config_permissions');
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { fail('muse_snapshot_json_invalid'); }
}

function* eventsOf(session) {
  if (typeof session?.eventAt === 'function' && Number.isSafeInteger(session.seq) && session.seq >= 0) {
    for (let seq = 0; seq < session.seq; seq++) {
      const event = session.eventAt(seq);
      if (event !== undefined) yield event;
    }
    return;
  }
  if (Array.isArray(session?.events)) { yield* session.events; return; }
  fail('muse_session_shape_unsupported');
}

/** Never agents.resume(): that API can start recovered work and repair the log. */
export async function readFishSession({ chat, live, sessionIds, persistence }) {
  const current = live.get(chat.key)?.handle?.agent?.session;
  if (current) return current;
  const id = sessionIds[chat.key];
  if (!id) return undefined;
  if (typeof id !== 'string' || typeof persistence?.open !== 'function') fail('muse_readonly_session_unavailable');
  const signal = AbortSignal.timeout(5000);
  const handle = await persistence.open(id, 'read', { signal });
  try {
    if (handle?.header?.id !== id || typeof handle.read !== 'function') fail('muse_persisted_session_identity_mismatch');
    const result = await handle.read(0, undefined, { signal });
    if (!Array.isArray(result?.events)) fail('muse_persisted_session_shape_unsupported');
    return { events: result.events };
  } finally {
    await handle?.close();
  }
}

/** Strip the verified dsh-fish wrapper, including all recalled/extension memory. */
export function privateUserText(content, owner) {
  if (!Array.isArray(content) || content.length !== 1 || content[0]?.type !== 'text' || typeof content[0].text !== 'string') fail('muse_user_message_shape_unsupported');
  const value = content[0].text;
  const marker = '【新消息】QQ私聊｜说话人：';
  const start = value.lastIndexOf(marker);
  if (start < 0) {
    // Upstream diary/initiative tasks are not private QQ conversation history.
    if (/(?:^|\n)【任务】/u.test(value)) return undefined;
    fail('muse_user_wrapper_unsupported');
  }
  if (start > 0 && value[start - 1] !== '\n') fail('muse_user_wrapper_unsupported');
  const tail = value.slice(start).split('\n');
  const suffix = `(${owner})｜身份：主人`;
  if (!tail[0].startsWith(marker) || !tail[0].endsWith(suffix)) fail('muse_user_identity_mismatch');
  const nickname = tail[0].slice(marker.length, -suffix.length);
  if (!nickname || nickname.length > 24 || /[\r\n【】()｜]/u.test(nickname)) fail('muse_user_wrapper_unsupported');
  let cursor = 1;
  if (tail[cursor]?.startsWith('【点名】')) cursor++;
  if (tail[cursor] === '【引用了这条消息】') {
    cursor++;
    while (tail[cursor]?.startsWith('> ')) cursor++;
  }
  if (tail[cursor++] !== '【正文】') fail('muse_user_wrapper_unsupported');
  const body = tail.slice(cursor);
  if (!body.length || body.some(line => !line.startsWith('> '))) fail('muse_user_wrapper_unsupported');
  return body.map(line => line.slice(2)).join('\n');
}

/** Read user text + the final assistant text of each turn; never tool/reasoning. */
export function sessionHistory(session, owner) {
  if (!session) return [];
  const history = [];
  let assistant;
  let privateTurn = false;
  const finish = () => {
    if (privateTurn && assistant) history.push({ role: 'assistant', text: assistant });
    assistant = undefined;
  };
  for (const event of eventsOf(session)) {
    if (!plain(event) || typeof event.type !== 'string') fail('muse_session_event_invalid');
    if (event.type === 'user/message') {
      finish();
      const message = event.data;
      if (!plain(message) || (message.role !== undefined && message.role !== 'user')) fail('muse_user_message_shape_unsupported');
      const text = privateUserText(message.content, owner);
      privateTurn = text !== undefined;
      if (privateTurn) history.push({ role: 'user', text });
    } else if (event.type === 'assistant/message' && privateTurn) {
      const message = event.data?.message;
      if (!plain(message) || !Array.isArray(message.content)) fail('muse_assistant_message_shape_unsupported');
      const blocks = message.content.filter(block => block?.type === 'text');
      if (blocks.some(block => typeof block.text !== 'string')) fail('muse_assistant_message_shape_unsupported');
      const text = blocks.map(block => block.text).join('');
      if (text) assistant = text;
    } else if (event.type === 'turn/end') {
      finish();
      privateTurn = false;
    }
  }
  finish();
  return history;
}

/** Only the active file is read. Archive, pending, tools and raw prompts are absent. */
export function mindSnapshot(dataDir, owner, config = {}) {
  const chat = `private:${owner}`;
  const chatScope = `chat:${chat}`;
  const userScope = `user:${owner}`;
  if ((config.shareUserMemories === true) !== (config.allowCrossChatUserMemories === true)) fail('muse_user_scope_requires_cross_chat_consent');
  const active = readJson(path.join(dataDir, 'mind', 'memories.json'), { optional: true }) ?? [];
  if (!Array.isArray(active)) fail('muse_memory_shape_unsupported');
  const memories = [];
  for (const row of active) {
    if (!plain(row)) fail('muse_memory_shape_unsupported');
    const inChat = row.scope === chatScope && row.source === chat;
    // Upstream reinforcement rewrites text but retains its creation source.
    // No user-scope row has provable exclusively-private provenance.
    const inUser = config.shareUserMemories === true && config.allowCrossChatUserMemories === true && row.scope === userScope;
    if (!inChat && !inUser) continue;
    if (!['string', 'number'].includes(typeof row.id) || typeof row.text !== 'string') fail('muse_memory_shape_unsupported');
    memories.push({ id: String(row.id), scope: row.scope, text: row.text, kind: 'active-long-term' });
  }
  const result = { memories };
  if (config.sharePersona === true) {
    const store = readJson(path.join(dataDir, 'mind', 'cards.json'), { optional: true });
    if (store !== undefined) {
      if (!plain(store) || !Array.isArray(store.cards) || (store.bindings !== undefined && !plain(store.bindings))) fail('muse_cards_shape_unsupported');
      const selected = store.bindings?.[chat] ?? store.defaultCard;
      const card = store.cards.find(row => row?.id === selected);
      if (!card) fail('muse_card_not_found');
      const sections = [];
      for (const field of ['name', 'identity', 'personality', 'style', 'rules', 'examples']) {
        if (card[field] !== undefined && typeof card[field] !== 'string') fail('muse_cards_shape_unsupported');
        if (card[field]) sections.push(`${field}: ${card[field]}`);
      }
      result.persona = sections.join('\n\n');
      if (card.absolute !== undefined && !Array.isArray(card.absolute)) fail('muse_cards_shape_unsupported');
      // Shared/global absolute memories are deliberately not exported.
      for (const row of card.absolute ?? []) {
        if (row?.scope !== chat) continue;
        if (!['string', 'number'].includes(typeof row.id) || typeof row.text !== 'string') fail('muse_cards_shape_unsupported');
        memories.push({ id: String(row.id), scope: chatScope, text: row.text, kind: 'scoped-card-absolute' });
      }
    }
  }
  return result;
}

const disabled = () => ({ enabled: false, rejectsMedia: () => false, rejectMedia: async () => {}, intercept: async () => ({ handled: false, route: 'dsh', reason: 'disabled' }), dispose: async () => {} });

export function createFishMuse({ dataDir, owners, getSession, sendControl, env = process.env, routerFactory = createRouter }) {
  const file = env.FISH_MUSE_CONFIG_FILE;
  if (!file) return disabled();
  if (!path.isAbsolute(file)) fail('muse_config_path_not_absolute');
  const config = readJson(file, { privateFile: true, maximum: 256 * 1024 });
  if (!plain(config)) fail('muse_config_invalid');
  if (config.enabled === false) return disabled();
  const owner = String(config.owner);
  if (!/^[1-9]\d{0,19}$/u.test(owner) || ![...owners].some(value => String(value) === owner)) fail('muse_owner_not_fish_owner');
  if ((config.shareUserMemories === true) !== (config.allowCrossChatUserMemories === true)) fail('muse_user_scope_requires_cross_chat_consent');
  if (typeof getSession !== 'function' || typeof sendControl !== 'function') fail('muse_adapter_hooks_required');
  const isOwner = ({ chat, sender, event } = {}) => chat?.type === 'private' && chat.key === `private:${owner}` && String(chat.id) === owner && String(sender?.userId) === owner && event?.message_type === 'private' && String(event.user_id) === owner;
  const router = routerFactory({
    config,
    snapshot: async input => {
      if (!isOwner(input)) fail('muse_snapshot_scope_mismatch');
      const session = await getSession(input.chat);
      return { history: sessionHistory(session, owner), ...mindSnapshot(dataDir, owner, config) };
    },
    sendControl: async input => {
      if (input.chat?.type !== 'private' || String(input.chat.id) !== owner) fail('muse_control_scope_mismatch');
      await sendControl(input);
    },
  });
  return {
    enabled: true,
    rejectsMedia(input) {
      if (!isOwner(input)) return false;
      const message = input.event.message;
      if (typeof message === 'string') return /\[CQ:/iu.test(message);
      return !Array.isArray(message) || message.some(segment => segment?.type !== 'text' || typeof segment?.data?.text !== 'string');
    },
    async rejectMedia(input) {
      if (isOwner(input)) await sendControl({ chat: input.chat, text: 'Muse 自动切换附加版目前只接收纯文字；图片、语音、引用和其他消息段未交给模型。', reason: 'unsupported_media' });
    },
    intercept: input => router.intercept(input),
    dispose: () => router.dispose(),
  };
}
