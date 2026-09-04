# 个人数字助手完整本地部署与迁移手册

作者：**Zhuofan Xie**

适用仓库：`CarrieX6/Personal-digital-assistant---A-Web-Agent`

更新日期：2026-09-02

本文是新电脑部署的主入口。目标是让安装者从 GitHub 私有仓库开始，完整复现 Web
控制台、个人助手 Agent、功能库、本地数据、真实图片风格化、空间照片和飞书控制链路，
并能把旧电脑的数据安全迁移到新电脑。

专题文档用于理解某个模块；发生命令冲突时，以本文和 `scripts/deploy.py --help` 为准。

## 1. 部署完成的定义

“部署完成”不是看到进程或网页，而是同时满足：

1. `http://localhost:3000/` 返回 Web 控制台；
2. `http://127.0.0.1:8000/health` 返回 `status=ok`；
3. LLM 在 UI 中通过普通问答和 Tool Calling 测试；
4. 功能库能区分已安装、测试模式、未安装和待上线；
5. 空间照片能完成深度估计、主体分割、资产生成和 Viewer 拖动；
6. 图片风格化运行真实 SDXL + IP-Adapter，Provider 不能是 `fake` 或
   `local-preview`；
7. 会话、记忆、任务和个人资产重启后仍存在，并按用户/会话隔离；
8. 飞书长连接、文本、图片、卡片、结果图和 Viewer 链接完成真实租户验收；
9. `python scripts/deploy.py check` 通过。

其中第 6、8 项必须在目标 GPU 和真实飞书应用上验证。自动测试或 Fake Provider 不能
替代真实验收。

## 2. 一条主路径

### 2.1 macOS / Linux

```bash
git clone https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent.git
cd Personal-digital-assistant---A-Web-Agent
git switch main
chmod +x scripts/setup.sh scripts/start.sh

# 默认 complete + real：会展示模型许可证，并下载真实模型。
./scripts/setup.sh --accept-model-licenses
./scripts/start.sh
```

另开终端验收：

```bash
.venv/bin/python scripts/deploy.py doctor
.venv/bin/python scripts/deploy.py check
```

### 2.2 Windows 10 / 11

以普通用户打开 PowerShell：

```powershell
git clone https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent.git
Set-Location Personal-digital-assistant---A-Web-Agent
git switch main
Set-ExecutionPolicy -Scope Process Bypass

# 默认 complete + real；Windows NVIDIA 还需要 Docker Desktop + WSL2。
.\scripts\setup.ps1 --accept-model-licenses
.\scripts\start.ps1
```

另开 PowerShell 验收：

```powershell
.\.venv\Scripts\python.exe scripts\deploy.py doctor
.\.venv\Scripts\python.exe scripts\deploy.py check
```

不希望在命令行声明接受许可证时，省略 `--accept-model-licenses`。交互安装器会先显示
固定模型、版本、下载量和许可证链接，只有输入 `ACCEPT` 才继续。CI 或无交互环境必须
显式传参，安装器不会代替部署者接受第三方条款。

## 3. 前置条件

| 项目 | 基础 Agent | 完整本地能力 |
| --- | --- | --- |
| Git | 能访问私有仓库 | 同左 |
| Python | 3.11+；当前推荐 3.12 | 同左 |
| Node.js | 22.13+，带 Corepack 或独立 pnpm | 同左 |
| 内存 | 建议至少 8 GB | 需按本机模型和并发重新评测 |
| 磁盘 | Python、Node 依赖与用户资产空间 | SDXL 套件固定下载计划约 9.84 GiB，另计环境、缓存和容器 |
| 加速器 | CPU 可运行控制面与部分空间照片 | Apple Silicon MPS、Windows NVIDIA，或受保护远程 GPU |
| Windows 真实风格化 | 不需要 | Docker Desktop、WSL2、NVIDIA 驱动，建议至少约 8 GB 显存 |
| 网络 | GitHub、LLM、飞书 | 首次还需访问模型源；运行时模型强制本地读取 |

先检查：

```bash
git --version
python3 --version
node --version
corepack pnpm --version
```

Windows 将 `python3` 换成 `py -3.12`。如果私有仓库 HTTPS 克隆要求认证，使用 GitHub
Credential Manager、SSH Key 或已登录的 GitHub CLI；不要把 Token 写进仓库 URL、脚本
或聊天记录。

## 4. 统一部署器做了什么

所有平台最终进入同一个 `scripts/deploy.py`，Shell/PowerShell 文件只负责找到 Python。

### 4.1 默认档位

```text
deploy install
  profile     = complete
  photo_style = real
```

默认动作：

1. 检查 Python、Node 和 pnpm/Corepack；
2. 不覆盖已有 `.env`；缺失时从 `.env.example` 创建；
3. 创建 `.venv` 并安装固定范围的后端依赖；
4. 安装 BiRefNet 依赖，下载并哈希锁定 Depth Anything V2 Small 与 BiRefNet，随后改为
   本地只读加载；
5. 使用锁文件安装前端依赖；
6. 下载、固定并 smoke test 本地 Granite 多语种 Embedding，启用语义记忆；
7. 展示图片模型计划和许可证；
8. 根据平台部署真实 SDXL + IP-Adapter；
9. 写入不含用户 LLM/飞书 Secret 的本机配置。

### 4.2 可选档位

| 命令 | 用途 | 结果边界 |
| --- | --- | --- |
| `deploy.py install` | 默认完整部署 | 要求真实风格化 |
| `deploy.py install --profile core --photo-style skip` | 只装控制台和 Agent 基础依赖 | 风格化明确不可用 |
| `deploy.py install --skip-spatial-models` | 不预下载深度/分割权重 | 首次运行需联网，不属于完整离线部署 |
| `deploy.py install --skip-memory-model` | 不下载本地 Transformer | 长期记忆回退词法/Hash 召回 |
| `deploy.py install --photo-style test` | 显式部署 Fake 契约服务 | 只能测交互链路，`check` 必然不通过 |
| `deploy.py install --photo-style-url https://...` | 使用远程真实 GPU | URL 需受保护；API Key 单独注入 |

Fake 不属于默认降级。真实 Provider 失败时，功能库应显示未部署或维护中，而不是给用户
返回低质量调色图。

## 5. 服务拓扑、端口和进程

```text
浏览器 / Root 管理员
        │ 127.0.0.1:3000
        ▼
Web 控制台（React / vinext）
        │ 127.0.0.1:8000
        ▼
FastAPI + LangGraph Agent
        ├── SQLite：会话、记忆、Checkpoint、身份、任务
        ├── 本机资产：图片、深度、前景、背景、结果文件
        ├── 8766：按需启动的签名空间照片 Viewer
        ├── 飞书：出站长连接，不要求公开 Webhook
        └── 图片风格化
              ├── macOS：本机 MPS NativeSDXLStyleProvider
              ├── Windows NVIDIA：127.0.0.1:18000 独立 pic-style
              └── 分机：受保护 HTTP(S) Provider
```

| 端口 | 组件 | 默认暴露范围 |
| --- | --- | --- |
| `3000` | Root Web 控制台 | 仅 `127.0.0.1` |
| `8000` | Agent API / Swagger | 仅 `127.0.0.1` |
| `8766` | 带 HMAC 签名的空间照片 Viewer | 专用局域网或受控 HTTPS Tunnel |
| `18000` | 独立图片风格化 API | 本机回环或 Agent→GPU 专用网络 |

不要把 `3000`、`8000` 或无鉴权的 `18000` 直接映射到公网。Web 控制台是本机 Root
视图，能看到全部已授权渠道会话，不是普通用户门户。

## 6. 启动、停止和健康检查

### 6.1 启动

```bash
./scripts/start.sh
```

Windows：

```powershell
.\scripts\start.ps1
```

统一启动器按配置拉起图片服务、公网 Viewer、FastAPI 和 Web，并等待 Web HTTP 200 与
API 健康检查。首次导入 PyTorch、Transformers 和 LangGraph 可能较慢；进程存在不代表
服务已经可用。

### 6.2 停止

前台启动时按 `Ctrl+C`。启动器会终止它创建的 Web、API 和 Viewer 子进程。Windows
独立风格化服务可单独停止：

```powershell
.\.venv\Scripts\python.exe scripts\manage_photo_style.py stop
```

停止不会删除模型、容器卷、SQLite 或个人资产。不要使用 `docker compose down --volumes`
作为普通停止命令，它会删除独立服务的数据卷。

### 6.3 三层检查

```bash
# 安装与运行状态，不要求全部通过
.venv/bin/python scripts/deploy.py doctor

# 后端详细健康
curl http://127.0.0.1:8000/health

# 完整验收：真实风格化不是 Fake 才通过
.venv/bin/python scripts/deploy.py check
```

Windows 使用 `Invoke-RestMethod` 或 `.\.venv\Scripts\python.exe`。

## 7. 首次进入 Web 控制台

打开 [http://localhost:3000/](http://localhost:3000/)，按顺序配置：

1. **模型设置**：选择 DeepSeek、OpenAI、Qwen、GLM 或自定义 OpenAI 兼容服务；填写
   Base URL、模型和 API Key；先“测试连接”，再“保存并启用”。
2. **外部接入**：填写飞书/Lark App ID、App Secret、白名单 Open ID 和群聊策略；先
   测试凭证，再启用长连接。
3. **功能库**：确认空间照片、个人记忆、文本工具、飞书连接器和视觉理解状态；图片
   风格化必须显示真实 SDXL 已就绪，而不是“测试模式”。
4. **Agent 对话**：新建会话完成文本问答、工具调用、图片问答和异步任务测试。
5. **记忆中心**：验证显式写入、检索、修改和删除。

LLM API Key 与飞书 App Secret 优先进入操作系统钥匙串；无可用钥匙串时才使用
`backend/data/*.enc` 与 `.secret_master_key`。浏览器不保存明文 Key。

## 8. 功能库逐项部署与验收

### 8.1 Web Agent 与 LangGraph

当前注册 8 个 Tool：文本统计、关键词提取、当前时间、能力列表、个人资产查询、空间
照片创建、异步任务状态和图片风格化。普通视觉问答由多模态 LLM 处理，不作为本地图像
生成 Tool。

LangGraph 执行 `plan → policy → approval（可选）→ execute_tool → observe → decide →
finalize/fail`。Schema、owner、风险、幂等、步数、错误数和总时长由代码约束；SQLite
Checkpoint 支持审批与重启恢复。

验收示例：

```text
统计“个人数字助手 Agent”的字符数并提取 5 个关键词。
现在几点？
查看我的个人资产。
记住：我的图片默认只在本机处理。
```

### 8.2 空间照片

完整档位安装 BiRefNet 运行依赖，并预下载、锁定两套空间模型。处理链路是：图片安全重编码 → Depth Anything V2
Small → Apple Vision（macOS 14+）或 BiRefNet 主体分割 → 分层背景补全 → 资产清单 →
Three.js 双层视差。

默认安装完成后运行时不再联网下载模型。手工轻量安装或使用
`--skip-spatial-models` 时，首次使用才会下载；这种状态不算完整离线部署。对应配置为：

```dotenv
SPATIAL_FOREGROUND_SEGMENTER=auto
SPATIAL_BIREFNET_LOCAL_FILES_ONLY=true
```

验收至少包含人物、猫狗、细发丝、四肢/尾巴、低对比背景和横竖屏图片。主体被深度阈值
切断不算通过；主体完整性应由语义分割负责，深度模型只负责相对远近。

### 8.3 分层记忆与上下文

默认完整安装准备 IBM Granite Embedding 97M Multilingual R2，并将运行时设置为本地
只读模型；显式记忆内容和向量均使用工作区密钥保护。无需语义模型时可跳过下载，系统
保留词法/Hash 基线。

迁移历史记忆后，可在停机或低流量时回填：

```bash
.venv/bin/python backend/scripts/backfill_memory_embeddings.py \
  --model-path backend/models/embeddings/granite-embedding-97m-multilingual-r2 \
  --all-owners
```

### 8.4 真实图片风格化

默认部署必须是真实 SDXL img2img + ViT-H IP-Adapter；固定模型计划约 9.84 GiB，运行
时禁止下载。Provider 健康不等于质量通过，最终还需要目标设备固定样本、连续稳定性、
峰值内存/显存和人工质量门禁。

#### Apple Silicon

安装器选择 `sdxl-local` + `mps`。模型在主 Agent 的隔离进程中执行，默认单任务完成后
卸载权重，减少长期占用。MPS 路径是完整模型，但当前仍需在每种 Mac 机型补充统一内存、
功耗、十次稳定性和人工质量记录；不能直接继承 Windows CUDA 结论。

#### Windows NVIDIA

安装器固定获取独立 `frogi-m/pic-style` 源码，创建独立虚拟环境，准备真实模型，配置
Docker PostgreSQL/Redis/MinIO/API，并由 Windows 宿主机运行单并发 CUDA Worker。Agent
只通过 `127.0.0.1:18000` 和随机生成的本机 API Key 调用。

第一次完整启动会构建容器，耗时明显长于普通 Agent。检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18000/health/ready
Invoke-RestMethod http://127.0.0.1:18000/health/provider
Invoke-RestMethod http://127.0.0.1:8000/api/photo-style-transfers/provider
```

Provider 名称必须是 `sdxl_ip_adapter_8gb_v1`，而不是 `fake`。Docker API 本身不加载
GPU 权重，所以它的 `/health/provider.ready` 可以为 `false`；真实就绪要同时验证 API
readiness、部署器记录的 Host GPU Worker 存活，以及至少一次真实任务成功。Windows
自动化还需要在目标机做 Docker、CUDA、重启和十次连续生成验收；Mac 上的单元测试不能
证明 Windows 运行结论。

#### 远程 GPU

```bash
python scripts/deploy.py install \
  --photo-style-url https://gpu.example.com/photo-style
```

公网必须使用 HTTPS、服务鉴权、租户隔离、限流和审计；API Key 不接受命令行参数，应
通过本机 Secret 管理单独注入。内网 HTTP 只允许专用网/VPN 地址。

### 8.5 通用视觉理解

无需额外本地图像模型，但配置的 LLM 必须兼容 Chat Completions 的 `image_url` 多模态
输入。DeepSeek 文本模型不应被假定具备视觉能力；用 UI 测试真实模型后再启用。普通
图片问答才会把当前图片发送给供应商；空间照片与风格化工具只让 LLM 看本地随机资产
ID，原图由本地 Tool 读取。

### 8.6 待上线能力

虚拟试衣和桌面宠物目前只显示路线，不属于可部署功能。不得在部署验收中把 UI 卡片
视为能力已经交付。

## 9. 飞书完整接入

飞书使用企业自建应用和出站长连接，不需要公开 Agent API。部署者需要：

1. 启用机器人；
2. 订阅 `im.message.receive_v1`；
3. 申请单聊/群聊消息、消息资源读取与图片/文件上传权限；
4. 配置卡片动作回调；
5. 发布版本并把用户加入应用可用范围；
6. 在 Web UI 录入 App ID、App Secret；
7. 通过首次消息得到 Open ID，再加入白名单；
8. 群聊启用后使用 `@机器人`；
9. 真机验证文本、菜单、图片、空间照片、风格化和失败重试。

复用同一个飞书应用迁移电脑时，先停止旧电脑长连接，再启用新电脑，避免两个节点竞争
消费。详细权限名、批量导入 JSON 和错误排查见[飞书机器人配置指南](feishu-setup.md)。

## 10. 手机空间照片 Viewer

同一局域网时，空间照片返回 `http://<LAN-IP>:8766/v/<签名>`。Viewer 只暴露该资产的
白名单文件，Token 默认 12 小时有效。手机打不开时检查：同一 Wi-Fi、AP 隔离、VPN、
系统防火墙和自动网卡选择。

```dotenv
LAN_VIEWER_BIND=0.0.0.0
LAN_VIEWER_PORT=8766
LAN_VIEWER_TTL_SECONDS=43200
LAN_VIEWER_PUBLIC_BASE_URL=http://192.168.x.x:8766
```

临时跨网络测试可显式启用 TryCloudflare，但随机域名会变化，旧卡片要刷新链接。正式
上线需要固定域名、Named Tunnel、HTTPS、用户身份、资产级授权、撤销、对象存储/CDN、
限流和审计；当前临时 Tunnel 不是产品公网部署。

## 11. 从旧电脑迁移全部运行数据

GitHub 只提供代码，不包含 `.env`、会话、记忆、图片、任务、模型和系统钥匙串。统一
部署器提供口令加密迁移包，使用 scrypt 派生密钥和 AES-256-GCM 完整性保护。

### 11.1 旧电脑备份

先用 `Ctrl+C` 停止 Web、API、Viewer 和图片服务。工具检测到相关端口仍在线会拒绝
备份，以免复制不一致的 SQLite/WAL。

```bash
.venv/bin/python scripts/deploy.py backup \
  --output /受控目录/personal-assistant-2026-09-02.pdabundle
```

默认包含：

- `.env`；
- `backend/data/` 下的会话、记忆、身份、Checkpoint、渠道记录、资产与密钥文件；
- 从系统钥匙串读取到的 LLM Key、飞书 App Secret；
- 长期记忆加密密钥。

不会包含临时公网域名状态。模型和独立能力目录默认不打包，避免迁移包膨胀；离线迁移
才追加：

```bash
.venv/bin/python scripts/deploy.py backup \
  --output /受控目录/personal-assistant-full.pdabundle \
  --include-models --include-capabilities
```

迁移包包含私人聊天和图片。口令不得与迁移包存放在同一位置；不要把包提交 Git、发送
到普通群聊或放入无访问控制的网盘。

### 11.2 新电脑恢复

先克隆代码并完成依赖/模型安装，但不要启动服务：

```bash
./scripts/setup.sh --accept-model-licenses
.venv/bin/python scripts/deploy.py restore \
  /受控目录/personal-assistant-2026-09-02.pdabundle
```

如果新电脑已经产生同名数据，恢复默认拒绝覆盖。只有确认这些数据可以被替换时才使用
`--force`；这是破坏性操作，执行前另做备份。

恢复后：

```bash
.venv/bin/python scripts/deploy.py doctor
./scripts/start.sh
# 另开终端
.venv/bin/python scripts/deploy.py check
```

还要人工验证历史会话、owner 隔离、显式记忆、资产缩略图、旧任务、LLM、飞书和 Viewer。
迁移包会把可读取的 Secret 写入新系统钥匙串；若旧系统钥匙串拒绝导出，备份会警告，
对应 Secret 需要在新 UI 重新录入。

## 12. 更新与回滚

更新前：

```bash
.venv/bin/python scripts/deploy.py backup --output /受控目录/pre-update.pdabundle
git status --short
git fetch origin
git pull --ff-only origin main
./scripts/setup.sh --accept-model-licenses
```

安装器复用 `.venv`、模型目录和 `.env`，不会覆盖现有本机配置。依赖更新后运行自动测试、
真实 Provider smoke 和真实飞书回归。

回滚不能只切代码：数据库 Schema、模型锁和独立服务版本也必须与目标 commit 兼容。
推荐保留上一个 Git tag/commit、部署诊断 JSON 和加密迁移包。删除容器卷、模型目录或
`backend/data` 都属于破坏性操作，不放进普通一键卸载。

## 13. 常见问题

### Node 或 pnpm 不存在

安装 Node.js 22.13+。部署器会优先使用 `pnpm`，不存在时使用 `corepack pnpm`。系统只有
旧 Node 时不要绕过版本检查。

### 启动后暂时打不开控制台

首次导入模型与 Agent 依赖可能较慢。等待启动器输出“控制台已就绪”，再访问页面；用
`deploy doctor` 区分依赖缺失、进程退出和单纯冷启动。

### `check` 报图片风格化不是真实 Provider

查看：

```bash
curl http://127.0.0.1:8000/api/photo-style-transfers/provider
```

`fake`、`local-preview` 或 `production_quality=false` 都不能通过完整验收。检查模型锁、
MPS/CUDA、Docker、GPU Worker 日志和 `PHOTO_STYLE_PROVIDER`。

### MPS 内存不足或算子回退

保持单并发和任务后卸载；降低输入分辨率；记录 `PYTORCH_ENABLE_MPS_FALLBACK` 是否触发。
回退 CPU 会显著增加时延，必须写入验收结果。

### Windows GPU Worker 不就绪

检查 Docker Desktop/WSL2、`nvidia-smi`、容器状态和
`.capabilities/pic-style/.web-agent-logs/gpu-worker.log`。API 就绪但 Worker 不就绪不算
真实功能可用。

### LLM 可以聊天但不会调用工具

UI 的“测试连接”要同时通过普通问答和 Tool Calling。确认 Base URL、模型兼容
OpenAI Tool Calling，不要把仅文本生成成功当成 Agent 就绪。

### 飞书没有回复或图片下载失败

确认应用版本已发布、用户在可用范围、Open ID 白名单、长连接状态、消息资源读取权限
和图片/文件上传权限。群聊还需开启群聊策略并 `@机器人`。

### 手机打不开空间照片

先在电脑打开同一签名链接，再检查局域网 IP、防火墙、AP 隔离和飞书内置浏览器对 HTTP
的限制。跨网络使用受控 HTTPS Viewer，不要公开 Agent API。

## 14. 上线前仍需完成

- Windows NVIDIA 完整自动化需在目标实机验证 Docker、CUDA、重启恢复和十次连续生成；
- Apple Silicon MPS 需补机型矩阵、统一内存、功耗、时延和人工质量门禁；
- 固定域名 Viewer、用户登录、链接撤销、对象存储/CDN、限流与审计尚未生产化；
- 飞书持久化 Outbox、弱网恢复和多节点主租约仍需完善；
- 微信个人号无稳定公开 Bot 接口，优先企业微信、公众号或小程序；
- 虚拟试衣与桌面宠物仍是待上线能力。

这些缺口不能用“代码存在”或“本机跑通一次”替代产品上线验收。

## 15. 相关文档

- [系统架构](../architecture/system-architecture.md)
- [LangGraph 循环 Agent](../architecture/langgraph-agent-loop.md)
- [分层长短期记忆](../architecture/layered-memory.md)
- [空间照片端侧部署](../spatial-scene-device-deployment.md)
- [图片风格化独立服务部署](photo-style-deployment.md)
- [飞书机器人配置](feishu-setup.md)
- [飞书图片与 2.5D 能力 SOP](feishu-media-capability-sop.md)
- [Capability 接入规范](capability-integration.md)

外部安装依据：

- [Node.js 下载](https://nodejs.org/en/download)
- [Python 下载](https://www.python.org/downloads/)
- [PyTorch 安装选择器](https://pytorch.org/get-started/locally/)
- [Docker Desktop for Windows](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/)
