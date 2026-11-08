# API contracts

Preserve the bridge /health, /inbox, /send, /ack contract and exact-byte event HMAC.
GET /inbox-tailnet returns the same pending rows without mutation; authorize the
actual socket peer against 100.64.0.0/10 or loopback before loading the store.
Never derive this authorization from forwarding headers. A loopback reverse
proxy must itself remain Tailnet-only; no public proxy or Funnel exposure.
Keep loopback listeners, bridge token and internal token distinct.
Only an actual message receipt can produce retcode 0; unknown sends are never automatically repeated.
Changes to SDK transport must retain verified TLS and no redirects or hidden send retries.

Optional DSH/Muse broker endpoints are separate from the original bridge and use
distinct DSH/Muse Bearer credentials. Context must preserve owner provenance,
reject oversize rather than truncate, and never contain tool/reasoning/config
exports. Its socket-peer read-only gate follows the same Tailnet rule. Persist
send reservations before I/O; reconcile unknown outcomes without a second POST.
