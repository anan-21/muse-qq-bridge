# Muse 收件与回复脚本

这两个脚本来自已完成真实 QQ 收发验收的部署，公开副本将地址、代理和私密文件路径改为配置项，并补齐独立测试。它们配合仓库根目录的小桥服务运行，回复由 Muse 中现有的智能体生成。

## 前提与安装

工具环境需要 Bash、curl、jq、Python 3，以及 Muse 提供的后台钩子能力。`HATCH_HOOK_RUNTIME` 应由平台指向可提供 `wake`、`silent`、`log` 的运行库；仓库不分发该运行库。收件脚本每被框架调用一次执行一次检查，5 秒间隔由钩子定义配置。

| 仓库文件 | 工具环境中的安装位置 |
| --- | --- |
| `muse/qq-bridge-inbox.sh` | `~/hooks/scripts/qq-bridge-inbox.sh` |
| `muse/qq-bridge-reply.sh` | `~/hooks/scripts/qq-bridge-reply.sh` |
| `muse/qq-bridge-inbox.template.json` | 按当前 hooks 接口登记或更新时使用的配置模板 |

将脚本复制到安装位置并设置可执行权限。运行目录应由当前用户独占，私密配置目录权限 `700`、令牌文件权限 `600`。通过部署者已有的安全传输方式，把桥 token 保存到 `~/.config/muse-qq-bridge/bridge_token.txt`，文件内容是 token 原文。不要把它写进脚本、钩子提示词或 Git 仓库。

以下变量必须在钩子和智能体工具实际执行的环境中生效。只在某次临时 shell 中 `export`，不能证明后续后台执行能继承。

| 变量 | 用途与默认值 |
| --- | --- |
| `QQ_BRIDGE_BASE_URL` | 必须配置为工具环境实际可达的私网桥地址。脚本默认值 `http://your-bridge-host:8765` 只是占位值 |
| `QQ_BRIDGE_PROXY` | 可选的私网访问代理。回复 helper 未设置该项时也读取 `HTTPS_PROXY` |
| `QQ_BRIDGE_NO_PROXY=1` | 两个脚本都强制直连，覆盖显式代理与 curl 继承的代理环境 |
| `QQ_BRIDGE_STATE_DIR` | 收件冷却状态目录，默认 `~/hooks/state`；离线测试必须使用隔离目录 |
| `QQ_BRIDGE_TOKEN_FILE` | 回复 helper 的私密 token 文件，默认 `~/.config/muse-qq-bridge/bridge_token.txt` |
| `QQ_BRIDGE_REPLY_LOG` | 私有计时日志，默认 `~/hooks/logs/qq-bridge-reply-timing.log` |

让原智能体核对环境和模板，将 `script_path` 替换为实际绝对路径，再登记或更新同一条钩子。模板记录现场的 5 秒轮询、600 秒脚本超时和主聊投递；平台的当前接口和后台能力仍需部署者核验。先保持新配置禁用，完成离线检查，切换时只保留一个消费会话。

## 入站与失败兜底

收件脚本查询 `/health`。pending 为 0 时静默，并在正常运行时清除上一批的冷却状态；health 失败或格式无效时静默，保留现有冷却状态。满足唤醒条件后，读取不带 token 的 `/inbox-tailnet`，校验 1–10 条消息，再输出包含 `pending` 与 `messages` 的唤醒数据。每行保留 `id`、`from`、`text` 和可用的 `time`。

取件失败、畸形响应、空消息或缺字段时，仍输出原 pending-only 唤醒。原智能体随后通过带令牌的 `/inbox` 补取。读消息不会确认或删除队列。

同批消息的重复唤醒节流为 240 秒，pending 清零后重置。它是唤醒节流，不是并发消费者锁。不要启动第二个消费者。`HATCH_HOOK_DRY_RUN=1` 保留状态文件，但仍可能读取配置的桥接口；本仓库测试使用本地替身和隔离状态。

脚本本身不调用模型。智能体收到唤醒后，沿用自己的搜索、记忆与工具判断。有关实际计时和单样本证据，见[延迟拆分](../docs/latency.md)。

## 回复 helper

接口是两个参数，正文从 stdin 输入。以下仅说明形式，应由已有消息 ID 与回复文本调用一次。

```bash
printf '%s' "$reply_text" | ~/hooks/scripts/qq-bridge-reply.sh "$from" "$ids_csv"
```

`from` 沿用消息中的数字原值：官方路线使用本地主人别名，自备小号协议端使用其白名单数字 QQ 号，不能在两条路线之间转换身份。消息 ID 以英文逗号连接。每个 ID 为非空、最多 128 字符，接受字母、数字、冒号、下划线和连字符，因此兼容官方机器人生成的 `qqc:` ID。第三方协议端的消息 ID 也需满足 helper 的字符约束。

helper 单次请求 `/send`，HTTP 200、`ok=true` 且有有效 `message_id` 回执后，才请求 `/ack` 这批 ID。输出只有一行。

| 输出 | 含义 |
| --- | --- |
| `ok`，退出码 0 | 发送与队列确认完成 |
| `fail:send`，退出码 1 | 发送失败或结果未确认；保留队列，不能自动重发 |
| `fail:ack`，退出码 1 | 发送已确认，队列确认失败；后续只恢复确认步骤 |
| `fail:args` / `fail:token` / `fail:empty`，退出码 1 | 参数、私密 token 文件或正文不符合要求 |

消息在 ack 前留在持久队列。未知发送结果仍需核查，不能把可读队列当作安全重发依据。

计时日志每行含 `ts`（UTC 毫秒）、`event`、`ids` 和 `result`。事件为 `helper_start`、`send_done`，以及确实调用确认时的 `ack_done`。日志文件权限 `600`，只记录简短状态；写日志失败不改变发送协议、退出码或请求顺序。原始正文、token、请求头和响应体不会写入计时日志。

## 私网边界

小桥默认绑定 `127.0.0.1`，通过 Tailnet-only Tailscale Serve 等受限入口提供访问。`/inbox-tailnet` 校验实际 socket 对端的 Tailscale IPv4 网段或回环地址。回环代理会掩盖原始客户端来源，因此代理入口自身必须限制为 Tailnet，并配置访问规则。不要使用公网代理入口或 Funnel。详见[安全说明](../SECURITY.md)。

`/inbox`、`/send`、`/ack` 继续使用桥 token。公开模板只保留从部署者本地私密文件读取它的机制。
