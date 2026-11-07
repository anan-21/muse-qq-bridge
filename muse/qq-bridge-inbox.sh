#!/usr/bin/env bash
# qq-bridge-inbox.sh — Muse hook 轮询脚本：查小桥 /health，有待处理消息才唤醒 agent。
# 环境变量：QQ_BRIDGE_BASE_URL（桥地址）/ QQ_BRIDGE_PROXY（可选代理，直连可达时留空）。
set -euo pipefail
umask 077
source "$HATCH_HOOK_RUNTIME"

STATE_DIR="${QQ_BRIDGE_STATE_DIR:-$HOME/hooks/state}"
STATE_FILE="$STATE_DIR/qq-bridge-inbox.last_wake"
mkdir -p "$STATE_DIR"

# 访问桥的可选代理（例如经出口代理到达桥所在的私有网络）；直连可达时不设 QQ_BRIDGE_PROXY。
curl_args=(--silent --max-time 10)
if [ "${QQ_BRIDGE_NO_PROXY:-0}" = "1" ]; then
  curl_args+=(--noproxy '*')
elif [ -n "${QQ_BRIDGE_PROXY:-}" ]; then
  curl_args+=(--proxy "$QQ_BRIDGE_PROXY")
fi
base="${QQ_BRIDGE_BASE_URL:-http://your-bridge-host:8765}"
base="${base%/}"
url="$base/health"
feed_url="$base/inbox-tailnet"

resp="$(curl --fail --show-error "${curl_args[@]}" "$url" 2>/dev/null)" || {
  log "bridge health fetch failed" '{}'
  silent "bridge health unreachable" '{}'
  exit 0
}

if ! pending="$(printf '%s' "$resp" | jq -er '
    select(type == "object" and .ok == true) | .pending |
    select(type == "number" and . >= 0 and . == floor and . <= 2147483647)
    ' 2>/dev/null)"; then
  log "bridge health response invalid" '{}'
  silent "bridge health unavailable" '{}'
  exit 0
fi

if [ "$pending" -lt 1 ]; then
  # 上一批已处理完（pending 清零）则重置冷却：之后的新消息立即唤醒，不必等满 4 分钟
  if [ "${HATCH_HOOK_DRY_RUN:-0}" != "1" ]; then
    rm -f "$STATE_FILE"
  fi
  silent "inbox empty" '{"pending":0}'
  exit 0
fi

now="$(date +%s)"
last=0
if [ -f "$STATE_FILE" ]; then
  last="$(cat "$STATE_FILE" 2>/dev/null || echo 0)"
fi
case "$last" in ''|*[!0-9]*) last=0 ;; esac

# 同一批待处理消息 4 分钟内不重复唤醒（worker 处理中 pending 尚未清零时避免双开）
if [ "$((now - last))" -lt 240 ]; then
  silent "already woken recently" "{\"pending\":$pending}"
  exit 0
fi

if [ "${HATCH_HOOK_DRY_RUN:-0}" != "1" ]; then
  printf '%s' "$now" > "$STATE_FILE"
fi

# 入站自动带正文（2026-11-07 启用）：决定 wake 后先调无令牌取件口（服务器侧只认 Tailscale 来源），
# 把本批未 ack 消息的 id/from/text/time 放进唤醒数据；取件失败、格式不对或无正文，
# 一律退回只带 pending 的原唤醒，worker 仍可按现行 prompt 调 /inbox 补取。本脚本不含 BRIDGE_TOKEN。
payload="{\"pending\":$pending}"
if feed_resp="$(curl --fail "${curl_args[@]}" "$feed_url" 2>/dev/null)"; then
  if feed_payload="$(printf '%s' "$feed_resp" | jq -c --argjson pending "$pending" '
      if (.messages | type) == "array"
         and (.messages | length) >= 1 and (.messages | length) <= 10
         and all(.messages[];
           ((.id | type) == "string" or (.id | type) == "number") and
           ((.from | type) == "string" or (.from | type) == "number") and
           (.text | type) == "string" and (.text | length) > 0)
      then {pending: $pending, messages: [.messages[] |
        ({id: (.id | tostring), from: (.from | tostring), text} +
          (if has("time") then {time: .time} else {} end))]}
      else empty end' 2>/dev/null)" && [ -n "$feed_payload" ]; then
    payload="$feed_payload"
  fi
fi

wake "qq bridge has pending messages" "$payload"
