# HTTP 接口

小桥默认监听 `127.0.0.1:8765`。远端通过经过授权的私网代理访问它。`/inbox`、`/send`、`/ack` 使用 `X-Bridge-Token` 请求头；不要把 token 放进 URL。自动取件可使用下面的只读 `/inbox-tailnet`。

## GET /health

匿名健康探测：

```json
{"ok":true,"pending":1}
```

它只证明桥与存储可用。官方登录连接还要看适配器的 `http://127.0.0.1:3001/health` 中 `gateway_ready`、`owner_bound`、`sandbox`。

## GET /inbox

需要桥 token。返回未确认的消息：

```json
{"messages":[{"id":"qqc:example-message-digest","from":"10001","text":"你好","time":1700000000,"acked":false}]}
```

`id` 是桥消息 ID，供去重和 ack；`from` 原样来自桥配置的 `ALLOWED_QQ`。官方适配器使用本地兼容别名，不是平台 `user_openid` 或真实 QQ 号；自备小号协议端使用其白名单数字 QQ 号。调用者应保留原值，不能在两条路线之间转换身份。

## GET /inbox-tailnet

该只读取件口供私网轮询脚本把正文随唤醒数据交给 Muse。返回格式与 `/inbox` 一致，仅返回未 ack 的行，保留 `id`、`from`、`text`、`time` 等原字段。GET 不确认、修改或删除队列。

接口不使用桥 token。小桥直接检查 TCP 连接对端 `client_address`，仅允许 `100.64.0.0/10` 和回环地址（含 `::1`）；对应的 IPv4-mapped IPv6 地址按其 IPv4 地址判断。其他或无法解析的来源返回 HTTP `403` 和 `{"error":"forbidden"}`，在拒绝前不读取消息存储。`X-Forwarded-For`、`Forwarded`、`X-Real-IP` 等请求头不参与授权。该范围判断不会自动识别 Tailscale 的 IPv6 地址段。

部署时必须实测小桥看到的对端地址。Tailscale Serve 转发到回环监听时，小桥通常看到代理的回环地址，来源隔离实际由 Serve 的私网入口与 Tailnet 访问规则共同完成。任意公网反向代理也可能使后端看到回环地址，因此不能把这个端点转发到公网、Funnel 或混合公网入口。回环访问也允许本机进程读取内容。详见 [安全边界](../SECURITY.md)。

只有 GET 支持此路径，POST 返回 `404`。发送和确认仍分别使用原有带 token 的 `/send` 与 `/ack`。

## POST /send

需要桥 token 和 `Content-Type: application/json`：

```json
{"user_id":"10001","text":"你好，我收到了。"}
```

成功会包含实际回执：

```json
{"ok":true,"onebot_status":200,"message_id":"upstream-message-receipt"}
```

桥通过 `ONEBOT_HTTP` 所指协议端的 `/send_private_msg` 完成发送。仓库内官方适配器仅在上游给出消息回执后返回 `status=ok, retcode=0`；自备协议端也必须返回有效回执，接线要求见 [OneBot 说明](onebot.md)。

失败时桥通常返回 HTTP 502，以及 `outcome=unknown`、`retry_safe=false`、可用时的 `onebot_retcode`。**不要因为超时就重复发送，也不要先 ack。** 人工确认后再决定如何恢复。

以下是仓库内官方适配器的常见非零码，不是第三方 OneBot 协议端的通用错误码：

| 码 | 含义 |
| --- | --- |
| 1400 | 请求格式或正文不符合要求 |
| 1401 | 内部 Bearer token 错误 |
| 1403 | 不是已绑定主人或目标别名不匹配 |
| 1408 | 本地被动回复窗口已过期，需要新的入站上下文 |
| 1503 | 网关、身份绑定或回复上下文不可用 |
| 1504 | 缺少确认、网络结果未知或保留的未知发送 |

上游提供整数业务错误码时保留该非零值；不要把 SDK 调用未抛异常当作成功。

## POST /ack

需要桥 token：

```json
{"ids":["qqc:example-message-digest"]}
```

只确认列出的 ID，不清空整个队列。确认在发送之后独立发生；`/send` 本身不会 ack。

## 内部接口

`POST /onebot/event` 用精确原始正文计算 `X-Signature: sha1=<HMAC-SHA1>`，不使用桥读取 token。支持有界 Content-Length 与 chunked 正文；拒绝歧义 framing。

`POST 127.0.0.1:3001/send_private_msg` 使用内部 Bearer token，接受 `{user_id,message}`。这两个内部接口由适配器和桥调用，不需要暴露到公网。
