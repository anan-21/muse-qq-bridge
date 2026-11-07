# Changelog

## Unreleased

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
