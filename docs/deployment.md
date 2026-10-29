# Linux 部署与迁移

## 新安装目录

目标是服务专用 Linux 用户拥有的目录，例如把仓库克隆到该用户的 HOME。代码、虚拟环境、私有 `data/` 分开管理；不要把运行数据提交 Git。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python configure.py --appid YOUR_APP_ID
```

脚本只在服务器交互终端隐藏输入 AppSecret，不接受命令行 Secret。AppID 可放命令行；真实 AppID 不必写进公开示例。配置结构如下，值均为说明占位符：

```json
{"appid":"YOUR_APP_ID","secret":"SERVER_ONLY_SECRET","is_sandbox":true}
```

脚本还会生成内部签名和 token，以及 Muse 使用的桥 token。重复初始化同一 AppID 不旋转桥密钥；换 AppID 或换本地主人别名时使用新的数据目录，避免把旧 OpenID 映射错配给另一个应用。

## systemd

安装模板前，先完成初始化。以实际运行服务的普通用户执行：

```bash
.venv/bin/python install_services.py --data-dir ./data
```

脚本渲染当前用户、绝对代码目录和虚拟环境路径，只有写 `/etc/systemd/system/` 时使用 sudo；模板里不嵌入任何 Secret。发现同名且内容不同的旧 unit 会停止，供你人工检查。

如果之前跑过前台实例，先 Ctrl-C 退出它们。然后启动：

```bash
sudo systemctl enable --now muse-qq-adapter muse-qq-bridge
.venv/bin/python wait_ready.py --timeout 30
```

看到 bridge_ok、sandbox、gateway_ready 为 true 后，再生成绑定语句并由本人发给机器人：

```bash
.venv/bin/python issue_binding.py --data-dir ./data
.venv/bin/python wait_ready.py --require-bound --timeout 30
```

进程锁拒绝同一数据目录的重复 writer。服务启动失败时，先读结构化日志与本地脱敏错误文件，不循环重启碰运气：

```bash
systemctl status muse-qq-adapter muse-qq-bridge
journalctl -u muse-qq-adapter -n 20 --no-pager
```

适配器的本地错误文件在 `data/state/startup-error.json`。即使文件已经做过已知密钥替换，也应人工复核后再分享；不要上传整个 data 目录。

## 网络验收

```bash
ss -ltn
curl -fsS http://127.0.0.1:3001/health
curl -fsS http://127.0.0.1:8765/health
```

应用仅监听 `127.0.0.1:3001`、`127.0.0.1:8765`。官方网关是适配器主动发起的 WebSocket，不要求入站公网端口。Tailscale Serve 会另外在设备的 Tailnet 地址提供私网入口；不要启用 Funnel 或打开云端公网端口。

安装 Tailscale 后，由本人在其授权链接登录和添加设备。网络接通后，用实际 Muse 工具环境访问桥的设备域名和端口，验证它能收到匿名 health，然后再配置桥 token 和测试收发。手机浏览器能打开，不等于云端工具执行环境也能打开。

## 已有小桥的迁移

初始化工具用于新安装，不会读取你已有部署的凭证，也不负责原地改造旧服务。迁移时：

1. 记录运行中桥的代码、数据位置、真实下游地址和 token 是否存在；不要打印 token。
2. 备份配置和队列，保持 600 权限。停止原协议端但保留其容器和数据，避免同时运行两个发送入口。
3. 在独立目录启动官方适配器，沿用原来的内部 OneBot token、事件签名密钥和主人兼容 ID。本人填写官方 Secret、完成显式 OpenID 绑定。
4. 先用临时队列和 dummy key 验证格式链路；模拟消息不要混进正式队列。
5. ready 后仅替换旧桥的 ONEBOT_HTTP 为 `http://127.0.0.1:3001`。备份后重启桥一次，再用有界只读 health 探测等待可用。
6. 比较受保护的 token/签名/白名单没有变化，再验真实 QQ 收发。旧消息是否 ack 必须获得针对具体 ID 的明确授权；不要清空整个队列。

**辅助命令失败后先检查实际状态，不要盲目重跑切换。** 若变更已经生效，再次覆盖配置或重启可能把诊断问题变成服务问题。这个仓库不包含任何现场账号的旧消息 ID 或自动清理脚本。
