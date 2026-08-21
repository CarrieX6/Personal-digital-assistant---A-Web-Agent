# macOS / Windows 本地部署

作者：**Zhuofan Xie**

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

## 为什么当前不把 Docker 作为默认方案

Docker 适合复现 CPU API 环境，但 macOS 容器不能直接使用 Apple MPS；Windows GPU
容器还依赖 WSL2、NVIDIA 驱动与 Container Toolkit。生成模型的显存和驱动问题不会
因容器自动消失。因此当前以原生一键脚本为默认，后续补充 CPU-only Docker Compose
作为 CI/演示方案，并为 Windows CUDA 单独提供经过实机验证的镜像。

## 公网阶段计划

公网版本不能直接映射 `8000/8766`。需增加域名、HTTPS 反向代理、用户身份、资产级
授权、速率限制、审计、撤销、对象存储/CDN 或中继，并完成 Viewer URL 泄露与重放
测试后才能上线。
