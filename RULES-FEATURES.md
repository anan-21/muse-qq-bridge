# Release scope

Portable official QQ sandbox adapter, bridge, initializer, owner binding, systemd templates, Muse-side inbox/reply helpers, documentation and offline tests.
The core bridge's brain remains the existing Muse agent; no extra model API is introduced there.
Affected flows: server setup, QQ text transport, source-restricted private reads, wake payloads, pending-only fallback, message acknowledgement and private timing logs.
No application UI, model implementation or changes to any existing deployment.
Release gates: clean source/privacy review, local tests, published Linux CI, public README and MIT license.
Keep live-deployment timing evidence separate from public-code tests. A single sample cannot establish a latency guarantee or attribute savings to context size or caching.
Documentation may describe the existing bridge OneBot contract for self-managed QQ account transports. Distinguish this advanced wiring from the bundled official adapter; do not imply a personal-account installer, verified session stability or identical send-journal guarantees. Adult text-chat claims need attributed evidence and separate Muse capability from QQ delivery rules.
Public release review includes historical blobs, commit messages, author and committer metadata. New commits and hosted merges must use a GitHub noreply identity; never print private identifiers in review output.

The DSH/Muse optional feature is maintained separately in anan-21/dsh-muse-qq.
This repository keeps only an old-path migration pointer and links to that project;
its implementation, contracts and tests belong to the independent repository.
