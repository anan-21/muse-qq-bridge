# Changelog

## Unreleased

- Present the project as Muse-to-QQ integration, distinguish the bundled official adapter from self-managed OneBot account connections, and document the scope of reported adult text chat capabilities without delivery guarantees.

- Reorganize the README around capabilities, setup prerequisites and document navigation; move the complete setup walkthrough into `docs/quickstart.md`.
- Add a source-restricted, read-only `/inbox-tailnet` endpoint for wake-time message delivery.
- Include configurable Muse-side inbox and reply helpers with pending-only fallback and private timing logs.
- Document one-consumer hook operation, autonomous tool use, send-before-ack handling, and latency boundaries.
- Record the November 7 live deployment's single-sample timing comparison separately from the public port's automated checks.

## 0.1.0 — 2026-10-29

- Publish the Muse ↔ private bridge ↔ official QQ sandbox integration method.
- Include botpy adapter, explicit OpenID binding, signed bridge events, persistent queue and send-result ledger.
- Keep token-authenticated private APIs, receipt-based success, and no automatic retry of unknown sends.
- Add portable server configuration, systemd templates, Muse-side instructions and offline contract tests.
- Document original user-confirmed end-to-end acceptance separately from public-code automated verification.
