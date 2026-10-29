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
