# HTTP 接口

小桥固定监听 `127.0.0.1:8765`。远端通过经过授权的私网代理访问它。除 health 外，调用者使用 `X-Bridge-Token` 请求头；不要把 token 放进 URL。

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

`id` 是桥消息 ID，供去重和 ack；`from` 是初始化时的本地兼容别名。不要将它解释为平台 `user_openid` 或真实 QQ 号。

## POST /send

需要桥 token 和 `Content-Type: application/json`：

```json
{"user_id":"10001","text":"你好，我收到了。"}
```

成功会包含实际回执：

```json
{"ok":true,"onebot_status":200,"message_id":"upstream-message-receipt"}
```

桥通过适配器的 `/send_private_msg` 完成发送。适配器仅在上游给出消息回执后返回 `status=ok, retcode=0`。

失败时桥通常返回 HTTP 502，以及 `outcome=unknown`、`retry_safe=false`、可用时的 `onebot_retcode`。**不要因为超时就重复发送，也不要先 ack。** 人工确认后再决定如何恢复。

适配器常见非零码：

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
