# Project bug memory

## Bug 004 — A second optional broker could reconcile an active send

- Symptom: without an OS process lock, starting another broker with the same SQLite directory could mark the first process's active reservation unknown during startup recovery.
- Reproduction: create two Store instances against one state directory while the first owns it; process-local thread locks cannot protect this path.
- Cause: crash reconciliation ran before proving exclusive ownership of the persistent state.
- Fix: take an exclusive nonblocking process lock before opening/reconciling the database and hold it until close.
- Regression: the original `addons/dsh-muse/test_broker.py` tested second-writer rejection, reservation-before-I/O and crash recovery independently; it is now maintained as [test_broker.py](https://github.com/anan-21/dsh-muse-qq/blob/main/test_broker.py) in the separate project.
- Prevention: acquire cross-process ownership before recovering durable network operations; a thread lock cannot establish that another process died.
- Scope: optional DSH/Muse broker only; retained here as historical evidence. The feature moved to the independent dsh-muse-qq repository on 2026-11-08; no running deployment was changed.

## Bug 001 — Quoted scalar fields prevented systemd unit loading

- Symptom: Linux CI rejected generated units: WorkingDirectory path was not absolute; user/group quotes were treated as part of the identity.
- Reproduction: render either service, then run systemd-analyze verify on Linux. The existing Linux-only test caught it.
- Cause: the renderer applied ExecStart argument quoting to scalar WorkingDirectory/User/Group directives, which retain literal quotes.
- Fix: validate and render scalar identities/working directory separately; keep quoting for ExecStart and ReadWritePaths list entries.
- Regression: test_configuration.py checks rendered scalar values and runs real Linux systemd-analyze with code/data paths containing spaces.
- Scope: public service installer/templates only; message transport and running deployments were not changed.
- Fixed in: initial-publication follow-up pull request.

Historical operational lessons remain in docs/lessons.md with their evidence limits.

## Bug 003 — Hosted merges can reintroduce a private commit identity

- Symptom: source commits used a public noreply identity, but hosted squash merges selected a personal author email from the account's default identity. A historical documentation version also retained a private agent nickname.
- Reproduction: inspect all reachable commit author/committer fields and historical blobs, rather than scanning only the current source tree. Review output must report categories and counts without reproducing private values.
- Cause: release review covered exported files and feature commits but did not verify the final hosted merge metadata and all retained history.
- Fix: use an explicit noreply author and committer for local commits, promote the verified commit without a hosted identity substitution, and prepare an isolated history cleanup with a private rollback mirror. Remove old private literals without changing the current source tree.
- Regression: compare current tree IDs before/after cleanup and scan the complete reachable commit metadata and blobs for private literals. A local pre-push guard must reject the original affected history and accept the cleaned history. Hosted read-only PR refs and old cached views remain a separate removal step.
- Prevention: verify the identity and contents of the exact published head; a clean current tree or clean feature commit cannot establish that public repository history is clean.
- Scope: public repository documentation and Git metadata only; no running QQ or Muse service changes.

## Bug 002 — Exported helper assumptions did not form a portable client contract

- Symptom: the exported GNU-date timestamp format produced a literal `3N` on macOS; the direct-connect flag only skipped an explicit proxy argument; a bare `ok=true` could reach acknowledgement without a receipt.
- Reproduction: run the original export with BSD date, inherited proxy variables, or a loopback fixture returning `{"ok":true}` without `message_id`.
- Cause: environment-specific shell behavior and insufficient validation at the exported HTTP client boundary.
- Fix: use Python UTC millisecond timestamps, an explicit curl proxy bypass, isolated state configuration, and pre-send ID plus receipt validation.
- Regression: `python -m unittest test_muse_helpers -v` exercises the public scripts against temporary loopback fixtures and private temporary files.
- Prevention: verify exported helpers as clients with their documented environment overrides, output protocol, failure paths and private-log behavior.
- Scope: public Muse helper copies and fixtures. Existing running deployments are separate from this publication.
- Fixed in: the autofeed/timing publication update.
