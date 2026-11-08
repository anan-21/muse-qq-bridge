# Pre-model topic router

This optional Node module chooses a provider **before** DSH runs. It uses local
literal rules, never a classifier API or a model's refusal response. The `NSFW`
and `18+` strings are topic labels for a provider preference. This module does
not create content or change either provider's rules.

It has no package dependencies. Use Node 22 or later in a Linux/macOS source
installation. Enabled creation explicitly rejects other operating systems:
POSIX permission checks do not establish safe Windows ACLs. The upstream
packaged Windows application is not supported by this addon version.

## Integration contract

```js
import { createRouter } from './router.mjs';

const router = createRouter({
  config: {
    enabled: true,
    owner: '10001',                 // synthetic example; one private-chat owner
    baseUrl: 'http://127.0.0.1:18765',
    tokenFile: '/private/path/dsh-muse.token',
    stateDir: '/private/path/router-state',
    consentExport: true,
    sharePersona: false,
    normalTurnsToReturn: 2,
  },
  snapshot: async ({ chat, sender, event, text, quote }) => ({
    history: [],                    // trusted normalized user/assistant history
    memories: [],                   // complete authorized owner-scoped export
  }),
  sendControl: async ({ chat, text, reason }) => {
    // Send this generic status notice through the existing QQ transport.
  },
});

// Run in the existing per-chat queue, before logger/ask/observe/model work.
const decision = await router.intercept({ chat, sender, event, text, quote });
if (decision.handled) return;
await existingDshFlow(decision.text ?? text);

// On clean plugin shutdown:
await router.dispose();
```

`chat` must contain `{type: 'private', id: owner}` and `sender.userId` must match
the same owner. If provided, `event.user_id` and `event.message_type` must agree.
Other chats return `handled: false` without reading a snapshot or contacting the
broker. Their handling remains the caller's responsibility. A stable
`event.message_id` is required for an intercepted owner message; `event.time` is
used when available. Pure text is the supported integration path.

The returned object has `handled`, `route` (`muse`, `dsh`, `control`, or
`blocked`), and `reason`. Only `handled: false` permits the caller to run DSH.
Checking `route` alone would be unsafe: an already-handled duplicate may retain
`route: 'dsh'` while `handled` is true. Inline `/dsh <text>` returns the stripped
text in an additional `text` field. Control notices never contain exported
context or credentials. Errors in `sendControl` cannot cause model fallthrough.

## Routing and automatic return

The initial route is DSH. Defaults route the literal labels `NSFW`, `18+`,
`成人向`, `成人话题`, and `暧昧` to Muse. Once Muse is active, ambiguous
continuations stay there. Two consecutive ordinary-topic matches select a
return to DSH; a strong ordinary task such as `帮我写代码` selects it immediately.
Before any return, locally journaled unknown handoffs are reconciled by GET;
each must become known. Authenticated `/dsh/status` must then report both
`pending: 0` and `unknown: 0`. A remote empty queue cannot erase a local unknown
request. Outstanding work defers the return and sends that message to Muse.
An unavailable status blocks that message instead of guessing.

These are predictable literal rules, not semantic understanding. A quoted label
may match; an unlisted euphemism may not. Configure `museKeywords`, `dshKeywords`,
and `strongDshKeywords` for the intended vocabulary. A configured empty list
disables that category. Muse-topic matches take precedence over ordinary-topic
matches. No refusal text is inspected and no refusal-triggered retry exists.

`/muse`, `/dsh`, and `/auto` change the persistent mode; each also accepts inline
text. `/muse` remains manual until another mode command. `/auto` restores topic
rules. A manual `/dsh` return still waits for the status gate. Commands without
inline text produce a control notice without exporting a snapshot.

## Snapshot and consent

The caller must provide a **trusted, normalized export**, not raw runtime or
agent objects. `snapshot` is called only for a Muse handoff after
`consentExport: true`. Its exact allowed shape is:

```js
{
  history: [ { role: 'user' /* or 'assistant' */, text: '...' } ],
  memories: [ {
    id: 'local-memory-id',
    scope: 'chat:private:10001', // or 'user:10001', matching config.owner exactly
    text: '...',
    kind: 'preference'
  } ],
  // Optional; allowed only when sharePersona === true:
  persona: 'explicitly shared persona text'
}
```

Supply the conversation's normalized prior history and all memories explicitly
authorized for these two scopes. The current message has its own `message`
field in the handoff; do not silently substitute a top-K memory search for the
requested complete scoped export. Export neither other users/groups nor
system/tool/reasoning messages, raw configuration, credentials, or runtime
state. Extra fields and other scopes are rejected, not copied. Some known
credential labels and the active DSH handoff token are also rejected; this
secondary filter is not a general-purpose secret detector.

Limits never silently truncate. Defaults are 10,000 history rows, 10,000 memory
rows and 2 MiB for the complete UTF-8 handoff JSON. `maxHistory`, `maxMemories`,
and `maxContextBytes` can lower the limits. A failed export or exceeded limit
returns `handled: true, route: 'blocked'` and sends no handoff.

## Transport, persistence, and recovery

Only loopback, Tailscale IPv4 `100.64.0.0/10`, or a `.ts.net` destination is
accepted. MagicDNS names must resolve to Tailscale IPv4 addresses on every
connection. No public destinations, URL credentials, path prefixes, redirects,
or proxy environment settings are used. HTTPS retains Node's normal certificate
verification. The dedicated broker token is read from the absolute `tokenFile`;
it is distinct from `BRIDGE_TOKEN` and is never returned or logged.

The token must be a private regular file (mode `0600`, current user), containing
24–1024 token characters. The state directory must be private (mode `0700`,
current user). Symlinks and permissive existing files/directories are refused.
State is replaced atomically with mode `0600`, contains only routing metadata
and hashes, and uses an exclusive process lock. Use one router instance and one
state directory for a given owner. A dead-process lock can be recovered; an
active or ambiguous lock is refused. There is no automatic journal pruning;
after `maxJournalEvents` (default 50,000), new events are blocked until the
operator archives state deliberately while the plugin is stopped.

Job IDs are `dshm:` plus SHA-256 of
`JSON.stringify(['private:' + owner, String(event.message_id)])`. An event is
journaled before provider dispatch; the same event never dispatches again,
including across a restart. A reused message ID with different text is blocked.
This also means a crash after a DSH decision cannot safely trigger an automatic
DSH retry. Source message IDs must remain unique across reconnects.

Muse requests use a single `POST /dsh/handoff` with a dedicated bearer token.
An unconfirmed response persists `phase: 'unknown'` and blocks DSH fallthrough.
On another delivery of that same event, the module only performs authenticated
`GET /dsh/jobs/<id>` reconciliation; **it never automatically repeats the POST**.
A 404 does not prove that sending never occurred and therefore remains unknown.
`timeoutMs` defaults to 5,000. Recovery from a blocked/unknown send is an explicit
operator decision, not a different-provider retry.

## Offline verification

```sh
node --check addons/dsh-muse/client/router.mjs
node --test addons/dsh-muse/client/router.test.mjs
```

Tests use a local fake HTTP broker and synthetic chat data. They do not call
QQ, Muse, or DeepSeek and do not establish live account delivery or latency.
