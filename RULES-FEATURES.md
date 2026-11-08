# Release scope

Portable official QQ sandbox adapter, bridge, initializer, owner binding, systemd templates, Muse-side inbox/reply helpers, documentation and offline tests.
The core bridge's brain remains the existing Muse agent; no extra model API is introduced there.
Affected flows: server setup, QQ text transport, source-restricted private reads, wake payloads, pending-only fallback, message acknowledgement and private timing logs.
No application UI, model implementation or changes to any existing deployment.
Release gates: clean source/privacy review, local tests, published Linux CI, public README and MIT license.
Keep live-deployment timing evidence separate from public-code tests. A single sample cannot establish a latency guarantee or attribute savings to context size or caching.
Documentation may describe the existing bridge OneBot contract for self-managed QQ account transports. Distinguish this advanced wiring from the bundled official adapter; do not imply a personal-account installer, verified session stability or identical send-journal guarantees. Adult text-chat claims need attributed evidence and separate Muse capability from QQ delivery rules.
Public release review includes historical blobs, commit messages, author and committer metadata. New commits and hosted merges must use a GitHub noreply identity; never print private identifiers in review output.

The optional addons/dsh-muse package has a separate queue, tokens and consumer.
Its local rules route owner-private text before DSH generation, never on refusal.
Automatic return requires no pending/unknown Muse work, including local unknown
handoffs. Context export is one-way, scoped and explicitly enabled. Source-run
Linux/macOS DSHFish is supported only at the checksum-pinned upstream baseline;
packaged Windows support and live installation are outside this publication.
