# API contracts

Preserve the bridge /health, /inbox, /send, /ack contract and exact-byte event HMAC.
Keep loopback listeners, bridge token and internal token distinct.
Only an actual message receipt can produce retcode 0; unknown sends are never automatically repeated.
Changes to SDK transport must retain verified TLS and no redirects or hidden send retries.
