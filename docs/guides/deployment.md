# macOS / Windows 本地部署

作者：**Zhuofan Xie**

如果是在新电脑安装某个媒体能力，而不只是启动基础服务，请同时按
[飞书图片与 2.5D 能力接入 SOP](feishu-media-capability-sop.md#5-新电脑安装与配置)
完成拓扑选择、模型/Provider 配置、数据迁移和安装后验收。

## 推荐拓扑

默认采用原生部署：Web 控制台和 API 只监听本机回环地址，飞书通过出站长连接收发
消息；只有带 HMAC 签名、短时有效的空间照片 Viewer 按需监听局域网 `8766` 端口。
这样既能使用 macOS MPS 或 Windows CUDA，也不会为了手机预览暴露整个 Root 控制台。

## Windows 10 / 11

前置：Python 3.11/3.12、Node.js 22.13+、Git。PowerShell 在仓库根目录运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\scripts\start.ps1
```

控制台地址为 `http://localhost:3000`。Windows Defender Firewall 首次询问时，只允许
Viewer 的 Python 进程访问“专用网络”，不要开放公用网络。NVIDIA 机器如需真实 SDXL，
应先按 PyTorch 官方 CUDA 安装器替换 `torch/torchvision`，再安装
`backend/requirements-gpu.txt`；CPU 预览 Provider 不要求 CUDA。

图片风格化的产品路径不是在 Agent 进程内加载模型，而是单独部署
[`frogi-m/pic-style`](https://github.com/frogi-m/pic-style)：Windows 推荐使用其
Docker API/Redis/PostgreSQL/MinIO，加宿主机单并发 NVIDIA Worker 的混合方式。本项目
配置 `PHOTO_STYLE_PROVIDER=pic-style-http` 与
`PHOTO_STYLE_SERVICE_URL=http://127.0.0.1:18000`。如果 Agent 和 GPU 服务不在同一台
电脑，应使用受保护的专用网络或 SSH Tunnel，不要把未加鉴权的 `18000` 端口直接暴露
到公网。

## macOS / Linux

```bash
chmod +x scripts/setup.sh scripts/start.sh
./scripts/setup.sh
./scripts/start.sh
```

## 局域网空间照片

生成完成后，飞书会收到形如 `http://192.168.x.x:8766/v/<签名>` 的链接。手机和电脑
必须在同一局域网，且 AP 隔离/VPN/防火墙不能阻断端口。链接默认 12 小时过期，只能
读取该空间照片的白名单分层文件，不能访问 Agent API、配置、记忆或其他资产。

可配置：

```text
LAN_VIEWER_PORT=8766
LAN_VIEWER_TTL_SECONDS=43200
LAN_VIEWER_BIND=0.0.0.0
LAN_VIEWER_PUBLIC_BASE_URL=
```

若电脑同时连接 VPN，程序会优先选择非隧道局域网网卡；自动选择仍不正确时，可把
`LAN_VIEWER_PUBLIC_BASE_URL` 显式设为 `http://<电脑局域网IP>:8766`。

## 临时公网 HTTPS Viewer（仅测试）

部分聊天软件内置浏览器会限制 `http://192.168.x.x`。项目提供显式 opt-in 的
TryCloudflare 测试入口，只转发 `127.0.0.1:8766` 的签名 Viewer，不公开 Root 控制台
`3000` 或 Agent API `8000`。

先安装 `cloudflared`：macOS 可运行 `brew install cloudflared`；Windows 从
[Cloudflare 官方下载页](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/)
下载 64 位 MSI 或可执行文件并加入 PATH。确认空间图片将经 Cloudflare 公网转发后，
另开终端运行：

```bash
python scripts/start_public_viewer.py --acknowledge-public-media
```

进程输出 `https://<随机>.trycloudflare.com` 后保持运行。后端无需重启，新生成的飞书
Viewer 链接会自动使用该 HTTPS 地址；停止进程后自动回退局域网地址。签名链接的持有者
在有效期内可以查看对应私人图片，因此不要转发到无关群聊。

Quick Tunnel 仅用于开发测试：随机域名会随进程重启变化，没有可用性保证。根据
[Cloudflare 官方说明](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)，
正式上线应使用账号、固定域名和命名 Tunnel，并增加用户登录、资产授权、撤销、限流
与审计。

### 开发期自动重连与链接刷新

如果已经明确接受带签名的私人图片经 Cloudflare 公网转发，可以在本机 `.env` 中启用：

```text
LAN_VIEWER_TTL_SECONDS=604800
PUBLIC_VIEWER_AUTO_START=true
PUBLIC_VIEWER_ACKNOWLEDGE_PUBLIC_MEDIA=true
```

此后 `scripts/start.sh` 和 `scripts/start.ps1` 会随 Web Agent 启动公网 Viewer，并在
Quick Tunnel 异常退出后使用最长 30 秒的退避自动重连。隧道进程退出时，本地状态文件
会被删除，后端不会继续把已失效域名回传给用户。

Quick Tunnel 重连后仍会得到新随机域名，因此旧飞书卡片里的“打开可动预览”可能失效。
用户可以点击同一卡片中的“刷新预览链接”，机器人会先按 Open ID 校验资产归属，再返回
使用当前公网域名的新卡片。Token 最长有效 7 天，但电脑关机或公网隧道断开期间仍不能
访问。关闭自动公网转发时，把两个 `PUBLIC_VIEWER_*` 开关改回 `false` 并重启服务。

该方案解决开发阶段的自动恢复，不替代固定域名。购买域名后应关闭 Quick Tunnel，配置
Named Tunnel，并将 `LAN_VIEWER_PUBLIC_BASE_URL` 设置为固定 HTTPS Viewer 域名。

## 为什么当前不把 Docker 作为默认方案

Docker 适合复现 CPU API 环境，但 macOS 容器不能直接使用 Apple MPS；Windows GPU
容器还依赖 WSL2、NVIDIA 驱动与 Container Toolkit。生成模型的显存和驱动问题不会
因容器自动消失。因此当前以原生一键脚本为默认，后续补充 CPU-only Docker Compose
作为 CI/演示方案，并为 Windows CUDA 单独提供经过实机验证的镜像。

## 固定域名公网阶段计划

公网版本不能直接映射 `8000/8766`。需增加域名、HTTPS 反向代理、用户身份、资产级
授权、速率限制、审计、撤销、对象存储/CDN 或中继，并完成 Viewer URL 泄露与重放
测试后才能上线。
