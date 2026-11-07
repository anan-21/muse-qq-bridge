#!/usr/bin/env bash
# qq-bridge-reply.sh — QQ 小桥出站 helper
# 用法: qq-bridge-reply.sh <user_id> <id1,id2,...>   回复正文从 stdin 读入
# 做两件事: POST /send 成功后 POST /ack 本批全部 id
# 输出有且只有一行: ok (退出码0) 或 fail:send / fail:ack / fail:args / fail:token / fail:empty (退出码1)
# 密钥只在本地从文件读, 绝不打印。测试可用环境变量覆盖:
#   QQ_BRIDGE_BASE_URL / QQ_BRIDGE_TOKEN_FILE / QQ_BRIDGE_PROXY / QQ_BRIDGE_NO_PROXY=1 / QQ_BRIDGE_REPLY_LOG
#
# 计时观测（2026-11-06 加入）: 只往本地私有日志记三个事件
#   helper_start / send_done / ack_done, 每行一个 JSON, 字段仅 ts(UTC 毫秒) /
#   event / ids(本次调用的消息 id, 多条合并即入站 id 列表) / result(简短结果)。
# 绝不记正文、QQ 号码、令牌、请求头或原始响应。日志写入失败不影响主流程。
set -uo pipefail
umask 077

LOG_FILE="${QQ_BRIDGE_REPLY_LOG:-$HOME/hooks/logs/qq-bridge-reply-timing.log}"
now_utc() {
  python3 -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"))'
}
START_TS="$(now_utc)"   # 进入脚本即抓取, 解析完 id 后才落盘
log_event() { # $1=event $2=ts $3=result
  {
    _dir="$(dirname "$LOG_FILE")"
    [ -d "$_dir" ] || mkdir -p "$_dir" 2>/dev/null
    [ -e "$LOG_FILE" ] || { (umask 077; touch "$LOG_FILE") 2>/dev/null; }
    chmod 600 "$LOG_FILE" 2>/dev/null
    printf '{"ts":"%s","event":"%s","ids":"%s","result":"%s"}\n' \
      "$2" "$1" "${ids_csv:-}" "$3" >> "$LOG_FILE"
  } 2>/dev/null || true
}

fail() { printf 'fail:%s\n' "$1"; exit 1; }

[ $# -eq 2 ] || fail args
user_id="$1"; ids_csv="$2"
case "$user_id" in ''|*[!0-9]*) fail args ;; esac
case "$ids_csv" in ''|*[!0-9A-Za-z:,_-]*) fail args ;; esac
ids_json="$(jq -nce --arg ids "$ids_csv" '
  $ids | split(",") | select(all(.[]; length > 0 and length <= 128))
  ')" || fail args
log_event helper_start "$START_TS" start

text="$(cat)"
# 去掉末尾换行, 正文为空不发
text="${text%$'\n'}"
[ -n "$text" ] || fail empty

token_file="${QQ_BRIDGE_TOKEN_FILE:-$HOME/.config/muse-qq-bridge/bridge_token.txt}"
[ -r "$token_file" ] || fail token
token="$(tr -d '\r\n' < "$token_file")"
[ -n "$token" ] || fail token

base="${QQ_BRIDGE_BASE_URL:-http://your-bridge-host:8765}"
base="${base%/}"
curl_args=(--silent --show-error --max-time 30)
# 可配置代理入口：优先 QQ_BRIDGE_PROXY，其次标准 HTTPS_PROXY（原样使用）；
# QQ_BRIDGE_NO_PROXY=1 强制直连。原部署用平台专用代理到达桥所在的私有网络。
proxy="${QQ_BRIDGE_PROXY:-${HTTPS_PROXY:-}}"
if [ "${QQ_BRIDGE_NO_PROXY:-0}" = "1" ]; then
  curl_args+=(--noproxy '*')
elif [ -n "$proxy" ]; then
  curl_args+=(--proxy "$proxy")
fi

post() { # $1=path $2=json-body -> stdout 最后一行为 http 码, 其余为响应体
  curl "${curl_args[@]}" \
      -H "X-Bridge-Token: $token" -H 'Content-Type: application/json' \
      --data "$2" -w $'\n%{http_code}' "$base$1" 2>/dev/null
}

send_body="$(jq -nc --arg u "$user_id" --arg t "$text" '{user_id:$u, text:$t}')" || fail args
if ! send_all="$(post /send "$send_body")"; then
  log_event send_done "$(now_utc)" curl_fail; fail send
fi
send_code="${send_all##*$'\n'}"; send_resp="${send_all%$'\n'*}"
if [ "$send_code" != "200" ]; then
  log_event send_done "$(now_utc)" "http_$send_code"; fail send
fi
if ! printf '%s' "$send_resp" | jq -e '
    type == "object" and .ok == true and
    ((.message_id | type) == "string" or
      ((.message_id | type) == "number" and (.message_id == (.message_id | floor)))) and
    (.message_id | tostring | length) > 0 and
    (.message_id | tostring | length) <= 128
    ' >/dev/null 2>&1; then
  log_event send_done "$(now_utc)" resp_not_ok; fail send
fi
log_event send_done "$(now_utc)" ok

ack_body="$(jq -nc --argjson ids "$ids_json" '{ids: $ids}')" || fail args
if ! ack_all="$(post /ack "$ack_body")"; then
  log_event ack_done "$(now_utc)" curl_fail; fail ack
fi
ack_code="${ack_all##*$'\n'}"; ack_resp="${ack_all%$'\n'*}"
if [ "$ack_code" != "200" ]; then
  log_event ack_done "$(now_utc)" "http_$ack_code"; fail ack
fi
if ! printf '%s' "$ack_resp" | jq -e '.ok == true' >/dev/null 2>&1; then
  log_event ack_done "$(now_utc)" resp_not_ok; fail ack
fi
log_event ack_done "$(now_utc)" ok

printf 'ok\n'
