# 接入 dsh-fish：在调用模型前切换

这是可安装的 dsh-fish 附加层，适用于 Linux / macOS。它在现有 QQ 私聊串行队列内决定去向：交给 Muse 的文字不会先调用 DSH 模型，也不会进入 DSH 的 `mind.observe`。正常话题继续走原来的大肥鱼流程；自动回切、短句续聊和 `/muse`、`/dsh`、`/auto` 由 [client/router.mjs](../client/router.mjs) 处理。

安装器不连接 QQ、不启动服务、不修改小桥原接口。没有设置 `FISH_MUSE_CONFIG_FILE` 时附加层不启用。它不处理模型拒绝后的重试或转发。

## 1. 核对版本并安装

目前只支持上游 [dsh-fish 0.2.8，提交 a16bb96e1f0997fda063ec46bc965622e58d6bb7](https://github.com/kylian-08/dafeiyu-qq-bot/tree/a16bb96e1f0997fda063ec46bc965622e58d6bb7)。插件目录是同时包含 `package.json`、`lib/index.js` 的目录；在该源码仓库中为 `plugin/`。上游升级或本地自改都会导致校验失败，应先审查差异，不能绕过摘要强装。

先停止自己正在运行的 dsh-fish，再在本仓库根目录执行，把示例路径替换为实际插件目录：

```sh
python3 addons/dsh-muse/integration/install.py \
  --plugin /absolute/path/to/dsh-fish --check

python3 addons/dsh-muse/integration/install.py \
  --plugin /absolute/path/to/dsh-fish --install
```

安装器只修改插件的 `lib/index.js`，并增加 `lib/muse-switch/fish-muse.mjs` 和 `lib/muse-switch/router.mjs`。原文件和摘要保存在插件的 `.muse-switch-backup/`，不会修改 DSH 控制台、人格库或记忆库。上游 `lib/index.js` 的 SHA-256 固定为：

```text
6de60ecdde82c82cbda75123231407a849c3da2f0f05770a39212f618da045cd
```

## 2. 配置并启用

先按 [附加版部署说明](../README.md) 准备好独立的 Muse broker 和 token 文件。这里的 `tokenFile` 是附加版令牌，不是旧小桥的 `BRIDGE_TOKEN`，也不是模型 API key。

在运行 DSH 的同一台机器创建私有 JSON，例如 `/absolute/private/fish-muse.json`：

```json
{
  "owner": "YOUR_OWNER_QQ_NUMBER",
  "baseUrl": "http://127.0.0.1:18765",
  "tokenFile": "/absolute/private/dsh-muse-token",
  "stateDir": "/absolute/private/dsh-muse-state",
  "consentExport": true,
  "shareUserMemories": false,
  "allowCrossChatUserMemories": false,
  "sharePersona": false
}
```

`owner` 必须替换为字符串形式的主人 QQ 号码，且已经在 dsh-fish 的 `owners` 中。broker 在其他设备时只能使用 Tailscale 私网地址或 `.ts.net` 域名；默认拒绝公网目的地址。配置文件和 token 文件仅本人可读，状态目录仅本人可访问：

```sh
chmod 600 /absolute/private/fish-muse.json /absolute/private/dsh-muse-token
mkdir -p /absolute/private/dsh-muse-state
chmod 700 /absolute/private/dsh-muse-state
export FISH_MUSE_CONFIG_FILE=/absolute/private/fish-muse.json
```

随后在**带有这个环境变量的同一环境**运行原来的 dsh-fish 启动命令。桌面启动器不会自动继承另一个终端刚设置的变量，应在启动脚本或启动器环境中设置。不要把 token 写入启动命令、QQ 或 README。

启用后，该主人私聊只接受纯文字。图片、语音、文件、CQ 码和引用等非文字消息段会在 `renderMessage`、表情采集之前被明确拒绝，因此不会意外触发 DSH 的看图模型。其他主人、非主人和群聊继续使用原有流程。

## 3. 上下文和记忆的实际范围

导出的 `history` 来自当前主人私聊对应的 DSH 会话：已加载会话直接读取；重启后通过 DSH 的只读 persistence handle 读取已有会话。不会调用 `agents.resume`、`followup`、`mind.recall`、`mind.flush` 或记忆提取模型。旧 SDK 缺少只读接口、历史格式无法识别、文件损坏或超出上下文限额时，该次转交停止，不会悄悄丢掉旧上下文继续发送。

会话用户消息包含上游注入的人格和召回记忆，因此不能直接整段导出。附加层只解析已核对的“主人 QQ 私聊”正文包裹，移除前面的人格、召回及扩展前缀；保留各轮最后一条助手文字。工具输入输出、系统消息、推理块、内部日记任务和引用中的第三方正文不导出。这是上游已渲染、已转义的正文，不能保证还原 QQ 原始字节；例如 `【】` 已被上游转换为 `[]`。

默认只读取 `dataDir/mind/memories.json` 中仍活跃的记录，同时满足：

- `scope` 等于 `chat:private:<owner>`；
- `source` 等于 `private:<owner>`。

`archive.json`、`pending.json`、其他私聊和群聊 scope、缺来源或 `manual` 来源的聊天记录都不读取。内存中尚未整理落盘的记忆缓冲也不导出，因此不是“全部历史记忆”。

`shareUserMemories` 和 `allowCrossChatUserMemories` 必须**同时为 true** 才会另行授权整个 `user:<owner>` scope；只打开一个会拒绝启动。两者默认关闭。上游重复记忆强化会更新文字，却保留创建时的 `source`，所以即便创建于本主人私聊，也无法证明后续文字没有来自群聊或其他聊天。开启两项等于明确授权该用户 scope 中可能跨聊天强化、手工添加或缺来源的活跃记录。任何情况下都不会导出其他用户的 scope。

`sharePersona: true` 另行授权导出当前绑定人格卡的 `name/identity/personality/style/rules/examples` 字段，以及只绑定本主人私聊的绝对记忆。共享、全局或其他聊天的绝对记忆不导出。人格卡以数据传给 Muse，不作为更高权限的系统指令；不会导出上游完整 anchor 或工具权限提示。

## 4. 回滚

取消 `FISH_MUSE_CONFIG_FILE` 并重启原 dsh-fish 即可停用。恢复原插件文件：

```sh
python3 addons/dsh-muse/integration/install.py \
  --plugin /absolute/path/to/dsh-fish --restore
```

恢复前会核对已安装文件摘要；如果安装后又有本地修改，会拒绝覆盖，先人工审查。恢复后保留 `.muse-switch-backup.restored-*` 作为代码备份。安装和回滚都不会自动重启服务。

## 检查与来源

```sh
python3 -m unittest discover -s addons/dsh-muse/integration -p 'test_*.py' -v
node --test addons/dsh-muse/integration/test_*.mjs
```

这些是离线安装、回滚、格式、权限、来源和事件钩子测试。钩子测试使用真实固定版本事件循环加替代依赖，验证路由发生在日志、心智和模型调用之前；不代表已经在真实 QQ、DSH 或 Muse 账号完成端到端验收。

只读 API 依据为官方 DeepSeek Harness 提交 `fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159` 的 [SessionPersistence.open](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/session/session-persistence/src/index.ts) 和 [SessionHandle](https://github.com/deepseek-ai/deepseek-harness/blob/fcc0ad462172522aaf5c93a7b2e01f6b9f0b0159/packages/session/session-persistence/src/handle.ts)：`open(id, 'read')` 不获取写入所有权，读取后 `close()`。不使用会开启代理循环并修复日志的 `agents.resume`。

测试夹具中的 `pinned-index.js.txt`、`pinned-package.json` 来自上述 dsh-fish 固定提交，保留在 [UPSTREAM-LICENSE](fixtures/UPSTREAM-LICENSE) 下，仅用于离线核验；安装目标必须已有受支持的上游插件。
