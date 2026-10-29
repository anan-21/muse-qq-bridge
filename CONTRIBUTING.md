# Contributing

Keep the adapter a transport. Do not add an implicit model backend, public listener,
personal-QQ login, automatic publishing, relaxed authentication or unknown-send retries.

Run `python -m unittest discover -v` in a fresh environment. Tests must use dummy
identities, temporary stores and loopback ports; never require a live account or
secret. SDK API changes need request/receipt contract tests. Describe real gateway
testing separately from simulated tests.

Share only sanitized versions, error codes and reproduction steps. See SECURITY.md
before reporting issues involving credentials or private message data.
