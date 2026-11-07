# QQ 小号 / OneBot 接线说明

[项目首页](../README.md) · [Muse 脚本配置](../muse/README.md) · [HTTP API](api.md) · [安全边界](../SECURITY.md)

消息桥保留了 OneBot HTTP 接口，可以连接自备的 QQ 小号协议端，例如 NapCat。本页说明桥要求的接线合同；仓库没有打包第三方客户端、小号登录器或一键安装器，也没有把这条路线当作当前已完成长期在线验收的部署。

使用第三方客户端有掉线、风控、限制或封号风险。只用可承担风险的小号，由本人完成扫码和安全验证；遇到冻结、风险设备或反复下线提示时暂停排查。这里不提供规避腾讯验证或保证不掉线的方法。

## 两端各负责什么

| 部分 | 职责 |
| --- | --- |
| 自备协议端 | 登录小号，把私聊事件上报给桥，提供发送私聊的 HTTP 接口 |
| 消息桥 | 验签、检查唯一发送者白名单、保存消息、向 Muse 提供取件和回复接口 |
| Muse | 使用原会话生成回复，通过 helper 发送成功后确认消息 |

协议端安装和登录依照 [NapCat 的安装说明](https://napneko.github.io/guide/boot/Shell)及所选版本文档。HTTP 服务端用于收取发送请求，HTTP 客户端用于推送事件，两者是不同配置；参见 [上游网络配置说明](https://napneko.github.io/config/basic)。仅启用 WebSocket 不能直接接入本桥。

## 必须满足的接口合同

1. **发送入口在同一宿主机的回环地址。** 桥的 `ONEBOT_HTTP` 只接受 `http://127.0.0.1:端口`、`localhost` 或 `::1`。容器协议端需要把 HTTP 端口映射到宿主机 `127.0.0.1`，桥从宿主机访问映射后的端口。不能把容器内部的 `127.0.0.1` 当成宿主机地址。
2. **出站有鉴权。** 协议端接受 `Authorization: Bearer <ONEBOT_TOKEN>`，提供 `POST /send_private_msg`，接收 `{user_id,message,auto_escape}`，成功返回 `status="ok"`、整数 `retcode=0` 和非空 `data.message_id`。HTTP 200 但缺少回执会被桥判为未确认。
3. **入站精确验签。** 事件发到桥的 `POST /onebot/event`；请求头必须为 `X-Signature: sha1=<hex>`，用 `ONEBOT_REPORT_SECRET` 对原始 body 字节计算 HMAC-SHA1。桥支持有界 Content-Length 和 chunked body。仅设置一个 Bearer token 不能替代事件签名；所选协议端或本地适配层不支持这个合同时，先停止接入，不能关闭桥的验签。
4. **只接唯一白名单的纯文本私聊。** 事件至少提供 `post_type="message"`、`message_type="private"`、`user_id`、`self_id` 和稳定 `message_id`。`message` 可以是纯文本或全部由 text 段组成的数组。群聊、非白名单、自身消息、混合媒体会被丢弃。
5. **所有控制口保持私有。** 协议端 API、管理页与桥只走回环或经授权的私网入口，不能公开管理页、关闭 token 或通过 Funnel 暴露。跨容器上报还需单独验证来源与路由；不要为了连通而放宽桥监听或签名要求。

不同协议端、不同版本的配置字段可能不同。本项目只定义上述合同，不保证任何名为 OneBot 的实现都能直接互换。

## 桥的本地配置

在新建、独立的数据目录中准备 `config/bridge.json`；以下是字段说明，所有值都是占位符：

```json
{
  "BRIDGE_TOKEN": "LOCAL_PRIVATE_BRIDGE_TOKEN",
  "ALLOWED_QQ": "YOUR_MAIN_QQ_ID",
  "ONEBOT_TOKEN": "LOCAL_PRIVATE_API_TOKEN",
  "ONEBOT_REPORT_SECRET": "LOCAL_PRIVATE_EVENT_SECRET",
  "ONEBOT_HTTP": "http://127.0.0.1:3000"
}
```

`ALLOWED_QQ` 替换为允许发消息给小号、接收回复的大号数字 QQ 号。这个值与官方路线的本地数字别名含义不同。token 和签名密钥必须在本地安全生成、在两端匹配，不能使用这些占位符，不能放进公开聊天或命令参数。

目录使用 `700`、文件使用 `600` 权限。安装仓库依赖后，使用 `run_bridge.py /你的独立数据目录` 启动桥；它读取该配置、取得进程锁，并固定监听 `127.0.0.1:8765`。不要直接运行绕过锁的第二份桥，也不要复用仍在运行的官方路线数据目录。

`configure.py`、`adapter.py`、`issue_binding.py`、`wait_ready.py` 和双服务安装流程面向官方机器人，不是小号的初始化、登录或状态工具。不要为了小号接入运行它们覆盖现有配置；小号协议端的服务托管需另行配置。

## 验收与 Muse 接入

1. 先检查协议端的小号登录状态、HTTP API 监听和上述鉴权、事件签名合同；桥 `/health` 不能证明 QQ 仍在线。
2. 使用隔离队列与虚构身份验证验签、白名单、chunked 上报和有效消息回执判定，不混入正式队列。
3. 用大号给小号发一条新私聊，检查桥 pending 增加；本人确认发出的回复在 QQ 收到后，才确认对应 ID。不能靠清队列伪造成功。
4. 按 [官方指南的私网连接步骤](quickstart.md#4-通过私网连接-muse) 和 [Muse 接入约定](muse-integration.md) 配置私网与脚本。Muse 使用 `from` 原值作为 helper 的收件人。
5. 再完成一次 QQ → Muse → QQ 的真实收发，核对有效回执与 pending 清零。发送结果未知时先核查，不自动重发；不要把断线恢复变成循环扫码或反复重启。

官方适配器的 sandbox、gateway_ready、owner_bound 和本地被动回复时限只适用于官方路线，不能当成第三方协议端的状态或配额。更换路线前要保留旧配置和消息，先停止旧 writer，再在独立环境中验收。
