# Contributing

Keep the adapter a transport. Do not add an implicit model backend, public listener,
personal-QQ login, automatic publishing, relaxed authentication or unknown-send retries.

The bridge's existing OneBot contract may be documented for self-managed account
transports. Keep that compatibility distinct from the bundled official adapter;
do not imply a personal-account installer, durable send journal, supported login
session or live end-to-end verification that this repository does not provide.

Run `python -m unittest discover -v` in a fresh environment. Tests must use dummy
identities, temporary stores and loopback ports; never require a live account or
secret. SDK API changes need request/receipt contract tests. Describe real gateway
testing separately from simulated tests.

Share only sanitized versions, error codes and reproduction steps. See SECURITY.md
before reporting issues involving credentials or private message data.

Use a GitHub-provided noreply address for public author and committer metadata.
Check merge commits as well: a private email can be reintroduced by the hosting
account's default merge identity even when all source commits use noreply.
