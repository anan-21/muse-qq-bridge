# Release scope

Portable official QQ sandbox adapter, bridge, initializer, owner binding, systemd templates, Muse-side inbox/reply helpers, documentation and offline tests.
The brain remains the existing Muse agent; no extra model API is introduced.
Affected flows: server setup, QQ text transport, source-restricted private reads, wake payloads, pending-only fallback, message acknowledgement and private timing logs.
No application UI, model implementation or changes to any existing deployment.
Release gates: clean source/privacy review, local tests, published Linux CI, public README and MIT license.
Keep live-deployment timing evidence separate from public-code tests. A single sample cannot establish a latency guarantee or attribute savings to context size or caching.
