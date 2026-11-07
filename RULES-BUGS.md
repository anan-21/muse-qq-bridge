# Project bug memory

## Bug 001 — Quoted scalar fields prevented systemd unit loading

- Symptom: Linux CI rejected generated units: WorkingDirectory path was not absolute; user/group quotes were treated as part of the identity.
- Reproduction: render either service, then run systemd-analyze verify on Linux. The existing Linux-only test caught it.
- Cause: the renderer applied ExecStart argument quoting to scalar WorkingDirectory/User/Group directives, which retain literal quotes.
- Fix: validate and render scalar identities/working directory separately; keep quoting for ExecStart and ReadWritePaths list entries.
- Regression: test_configuration.py checks rendered scalar values and runs real Linux systemd-analyze with code/data paths containing spaces.
- Scope: public service installer/templates only; message transport and running deployments were not changed.
- Fixed in: initial-publication follow-up pull request.

Historical operational lessons remain in docs/lessons.md with their evidence limits.

## Bug 002 — Exported helper assumptions did not form a portable client contract

- Symptom: the exported GNU-date timestamp format produced a literal `3N` on macOS; the direct-connect flag only skipped an explicit proxy argument; a bare `ok=true` could reach acknowledgement without a receipt.
- Reproduction: run the original export with BSD date, inherited proxy variables, or a loopback fixture returning `{"ok":true}` without `message_id`.
- Cause: environment-specific shell behavior and insufficient validation at the exported HTTP client boundary.
- Fix: use Python UTC millisecond timestamps, an explicit curl proxy bypass, isolated state configuration, and pre-send ID plus receipt validation.
- Regression: `python -m unittest test_muse_helpers -v` exercises the public scripts against temporary loopback fixtures and private temporary files.
- Prevention: verify exported helpers as clients with their documented environment overrides, output protocol, failure paths and private-log behavior.
- Scope: public Muse helper copies and fixtures. Existing running deployments are separate from this publication.
- Fixed in: the autofeed/timing publication update.
