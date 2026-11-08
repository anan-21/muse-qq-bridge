# API contracts

Preserve the bridge /health, /inbox, /send, /ack contract and exact-byte event HMAC.
GET /inbox-tailnet returns the same pending rows without mutation; authorize the
actual socket peer against 100.64.0.0/10 or loopback before loading the store.
Never derive this authorization from forwarding headers. A loopback reverse
proxy must itself remain Tailnet-only; no public proxy or Funnel exposure.
Keep loopback listeners, bridge token and internal token distinct.
Only an actual message receipt can produce retcode 0; unknown sends are never automatically repeated.
Changes to SDK transport must retain verified TLS and no redirects or hidden send retries.

The optional DSH/Muse broker contracts are maintained in anan-21/dsh-muse-qq,
independently of this bridge's existing endpoints and authentication.
