# DSH → Muse optional routing add-on

This add-on is developed separately from the existing bridge and is disabled until
configured. No live service, account, queue or credential is changed by this work.

## Scope and acceptance

| ID | Scope | Acceptance | State |
| --- | --- | --- | --- |
| D1 | DSH private-chat ingress | Route before model/memory processing; only the configured owner; ordinary messages retain the DSH flow | Implemented; offline checks passed |
| D2 | Local routing state | Muse continuations stay together; ordinary topics return automatically only after outstanding Muse jobs finish; explicit commands available | Implemented; offline checks passed |
| D3 | Scoped context export | Export conversation text and authorized chat/user memories, never tools, reasoning, global memory or configuration; reject oversize instead of silently truncating | Implemented; offline checks passed |
| D4 | Dedicated handoff broker | Private bind, separate tokens, durable idempotency, owner-only sends, unknown sends never retried | Implemented; offline checks passed |
| D5 | Muse helpers | Retrieve private context before wake; send receipt before ack; retain messages on errors | Implemented; offline checks passed |
| D6 | Pinned upstream integration | Verify the supported upstream files, back up before patching, refuse unknown layouts; remain inert without configuration | Implemented; offline checks passed |
| D7 | Publication | Offline end-to-end tests, full existing regressions, privacy review and documented installation/limits | Local checks passed; published CI pending |

Affected interfaces: new add-on HTTP routes and configuration; DSHFish ingress,
session text export and local memory export; separate SQLite state; optional Muse
hook/reply helpers. Existing root HTTP contracts, official QQ adapter, HMAC,
whitelist and deployment remain unchanged. There is no new application UI.

## Decisions

- User requested automatic return to DSH when the topic returns to ordinary chat.
  Local routing rules distinguish explicit new topics from ambiguous continuations;
  this is not a promise of perfect semantic classification.
- Route before any DeepSeek generation. Never try DeepSeek first and forward a
  refusal. Muse applies its own rules.
- Context transfer is one-way into Muse. Returning to DSH resumes its existing
  context plus the new ordinary message; Muse transcripts are not silently fed
  back into DeepSeek.
- This is a published optional integration, not a deployment into the user's
  running QQ/Muse setup. Only synthetic test data may be committed.

## Progress

- 2026-11-08: created isolated feature worktree from the verified public main.
  Reviewed existing API/technology/privacy rules and pinned upstream DSHFish source.
  Implementation and independent interface/security review run in parallel.
- Implemented independent Python broker, owner-scoped handoff schema, durable
  send reservations and private configuration initializer. Review caught the
  missing process lock before recovery; fixed and regression-tested (Bug 004).
- Implemented local Node router with pre-model dispatch, continuation handling,
  automatic return gated by both local unknowns and remote pending state, and
  durable duplicate suppression. No refusal routing or secondary classifier API.
- Added Muse context download with byte/hash verification and private cache,
  receipt-first send/ack, unknown-send suppression and a separate hook template.
  Cache cleanup does not imply server history deletion.
- Cross-runtime Node → Python → Muse-helper → OneBot-fixture test passed, including
  ordinary-topic return and duplicate delivery across a router process restart.
- Initial root tests with system Python lacked existing adapter dependencies;
  reran with the already prepared project virtual environment: 95 tests passed,
  one Linux-only systemd verification skipped on macOS. Existing core unchanged.
  Dedicated add-on suite: 44 Python tests and 26 Node router tests passed at this
  stage. Integration installer/SDK contract checks are recorded in the final gate.
- Read-only review confirmed old DSH context must use SDK persistence open(read),
  never agents.resume (which starts a loop). Upstream private-message wrappers
  are parsed to remove recalled/global prefixes; current body only is exported.
  User-scope memory provenance is incomplete upstream, so cross-chat sharing must
  be explicitly authorized along with user-scope sharing.
- Final local gate: 45 Python add-on tests, 5 installer tests and 35 Node tests
  passed. This includes the real shell-hook shim, pinned upstream event loop
  with replacement dependencies, read-only SDK contract, backup/restore,
  media filtering and cross-runtime round trip. Python compilation, Bash syntax,
  rule validation and staged whitespace review passed. No live account test or
  deployment was performed. Linux CI will additionally run the original service
  syntax verification that macOS cannot execute.
