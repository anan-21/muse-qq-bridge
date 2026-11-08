#!/usr/bin/env bash
set -euo pipefail
umask 077
source "$HATCH_HOOK_RUNTIME"
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
if ! result="$(python3 "$script_dir/muse_client.py" --config "$FISH_MUSE_CLIENT_CONFIG" poll)"; then
  silent "DSH handoff pickup unavailable" '{}'
  exit 0
fi
if [ "$(printf '%s' "$result" | jq -r '.wake')" = true ]; then
  payload="$(printf '%s' "$result" | jq -c '.data')"
  wake "DSH handoff has pending messages" "$payload"
else
  silent "DSH handoff unchanged" '{}'
fi
