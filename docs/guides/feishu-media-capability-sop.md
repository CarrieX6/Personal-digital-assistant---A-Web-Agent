# 飞书图片与 2.5D 能力接入 SOP

作者：**Zhuofan Xie**

更新日期：2026-08-24
适用范围：空间照片、图片风格化，以及未来虚拟试衣、虚拟宠物、3D 模型等媒体能力

## 1. 结论先行

飞书用户不应只收到一句“生成完成”，也不能把 2.5D 资产误当成普通图片发送。不同
结果应使用不同的回传组合：

| 资产类型 | 飞书内直接展示 | 交互入口 | 降级方案 |
| --- | --- | --- | --- |
| 2.5D 空间照片 | 静态封面图 | 带短时签名的 HTTPS Viewer 按钮 | 3–5 秒 MP4，最后回退静态图 |
| 图片风格化 | 风格化结果图 | 下载结果、再次生成按钮 | 原图/结果拼图；失败时重试卡片 |
| 3D / 3DGS | 封面或转台视频 | Web Viewer | MP4 + 可选 GLB/PLY 文件 |

飞书普通图片气泡只能展示静态图像，不能在气泡内部运行本项目的 Three.js/WebGL
Viewer。因此 **2.5D 的正确交付不是某个特殊图片扩展名，而是“封面图 + Viewer
卡片 + 可选视频”**。这是产品和渠道能力边界，不应通过伪造动态图片规避。

本项目建议把渠道接入做成以下稳定结构：

```text
飞书消息 / 卡片动作
        ↓
Feishu Adapter：鉴权、幂等、附件下载
        ↓
MediaCollectionSession：图片角色与会话状态
        ↓
Capability Service：创建持久化 Job
        ↓
Asset Repository：保存源图、结果和派生预览
        ↓
Result Presenter：图片 / 卡片 / Viewer / 视频
        ↓
Outbox：可靠回传与失败重试
```

## 2. 当前事实与本次目标

### 2.1 当前已经实现

- `backend/app/feishu.py` 能接收飞书图片，并通过持久化媒体会话判断空间照片、内容图和
  风格参考图角色；
- 空间照片完成后按“静态封面 + 2.5D 结果卡片 + 短时 Viewer URL 按钮”返回；
- 图片风格化可在飞书依次收集 1 张内容图和 1–3 张参考图，提交本地异步 Job，并直接
  返回风格化结果图和参数卡片；
- 空间照片和图片风格化失败后均可发送带任务 ID 的“重新生成”卡片，并通过 owner
  校验和原子抢占阻止越权、重复重试；
- Web Root 控制台能显示飞书图片的本地受控预览；
- 图片风格化已有 `PhotoStyleService`、异步 Job、资产结果和
  `create_photo_style_transfer` Tool；
- 风格化 Source Image 的读取、删除、任务和结果资产均传递同一 owner。

### 2.2 当前缺口

- 会话、结果卡片和模型分支仍集中在 `FeishuChannelRuntime`，尚未拆成通用 Channel
  Contract、`MediaResultEnvelope` 和独立 Presenter；
- 会话过期后数据库记录会清理，但未提交临时图片仍依赖 24 小时 Source Image 清理，
  尚无独立定时回收 Worker；
- 结果发送尚未进入持久化 Outbox，任务完成时断网可能丢失回传；
- Viewer 仍需完成固定公网 HTTPS 域名、撤销、限流和真实手机验收；
- 尚未生成 2.5D MP4 降级预览。

### 2.3 本 SOP 的完成定义

完成本 SOP 后，一个新媒体 Capability 至少能够：

1. 从功能卡片或自然语言进入明确的收集状态；
2. 安全接收单图或多图，不依赖上传顺序猜测业务角色；
3. 创建 owner 隔离、可幂等恢复的异步 Job；
4. 在飞书返回渠道适配的结果，而不是本地文件路径；
5. 在失败、断网、重复事件和重复点击下保持可恢复；
6. 在 Web Root 控制台保留同一条消息与资产关联；
7. 通过 Fake Channel 自动化测试和真实飞书租户验收。

## 3. 先定义结果契约

新增功能前先定义 `MediaResultEnvelope`，渠道层不直接解析各模型私有目录：

```python
@dataclass(frozen=True)
class MediaResultEnvelope:
    capability_id: str
    job_id: str
    asset_id: str
    owner_id: str
    result_kind: Literal["image", "spatial_scene", "video", "model_3d"]
    title: str
    width: int | None
    height: int | None
    preview_path: Path | None
    preview_media_url: str | None
    result_path: Path | None
    viewer_url: str | None
    download_url: str | None
    model_name: str | None
    provider_name: str | None
```

规则：

- `Path` 只在受信任的本机服务层内部流转，绝不能写入卡片按钮或 Agent 回答；
- 飞书和 Web 只消费 `asset_id`、受控 `media_url`、签名 URL 与展示元数据；
- `owner_id` 必须从当前飞书 App、发送者 Open ID 和会话绑定产生，不能由 LLM 提供；
- `capability_id + job_id + delivery_kind` 作为出站幂等键的一部分；
- 结果展示失败不应把已经完成的模型 Job 改成失败，应单独记录 `delivery_failed`。

## 4. 新功能接入标准流程

### Step 0：登记任务和边界

在 `docs/project-board.md` 创建或领取任务，至少写明：

- Capability ID 和负责人；
- 输入图片角色及数量；
- 输出资产类型；
- 模型/Provider、设备与下载要求；
- 是否向公网发送图片或只发送 Viewer 链接；
- 风险等级、owner 范围、保留时间和删除语义；
- Fake 测试与真实租户验收标准。

### Step 1：注册 Capability，而不是改 Agent 主循环

按照[新功能 / Capability 接入指南](capability-integration.md)定义 Service、Provider、
Tool Schema 与 Manifest。渠道层只调用稳定 Service，不直接操作 SDXL、Depth Anything
V2 或模型进程。

Manifest 至少增加渠道输出声明：

```json
{
  "id": "photo-style-transfer",
  "input_roles": {
    "content": {"min": 1, "max": 1},
    "style": {"min": 1, "max": 3}
  },
  "channel_outputs": ["image", "result_card", "download_link"],
  "supports_retry": true,
  "supports_viewer": false
}
```

空间照片对应：

```json
{
  "id": "spatial-photo",
  "input_roles": {"source": {"min": 1, "max": 1}},
  "channel_outputs": ["image", "viewer_link", "video_fallback"],
  "supports_retry": true,
  "supports_viewer": true
}
```

### Step 2：创建图片收集会话

风格化不能把几张图片按到达顺序直接交给模型。增加持久化
`MediaCollectionSession`：

```text
id
capability_id
owner_id
chat_id
sender_id
state
content_source_id
style_source_ids_json
parameters_json
created_at / updated_at / expires_at
version
```

推荐状态机：

```text
awaiting_content
      ↓ 收到一张内容图
awaiting_styles
      ↓ 收到 1–3 张风格图
ready ──点击生成──> submitted ──> completed / failed
  │                      │
  └──取消──> cancelled   └──超时──> expired
```

隔离键使用：

```text
app_id + sender_open_id + chat_id + capability_id
```

群聊必须包含 `sender_open_id`，否则 A 用户发起的风格化会话可能错误吸收 B 用户的图片。
会话建议 10–15 分钟过期；过期后清理未被 Job 领取的临时 `source_image_id`。

### Step 3：由卡片明确图片角色

推荐交互：

1. 用户点击“图片风格化”；
2. 机器人发送“请发送 1 张内容图”；
3. 收到后回复“内容图已保存，请发送 1–3 张风格参考图”；
4. 每收到一张参考图，发送或更新状态卡片；
5. 参考图数量达到 1 后启用“开始生成”；
6. 同时提供“继续添加”“重新选择内容图”“取消”按钮。

卡片动作只携带不可猜测的 `session_id` 和动作名：

```json
{
  "command": "submit_media_session",
  "session_id": "random-uuid"
}
```

服务端收到动作后仍要重新校验：操作者 Open ID、会话 owner、状态、过期时间、图片数量
和 Capability 是否可用。不能因为按钮来自机器人就信任其参数。

### Step 4：接收、清洗并保存图片

飞书收到的图片是资源引用，不是可长期访问的公网 URL。实现步骤：

1. 从 `im.message.receive_v1` 取得 `message_id` 与 `image_key/file_key`；
2. 以 `message_id` 做 Inbox 幂等，重复事件直接返回已有处理结果；
3. 校验 Open ID 白名单、应用可用范围映射和群聊 `@机器人` 规则；
4. 使用“获取消息中的资源文件”下载用户消息图片；
5. 限制 MIME、单文件大小、像素数和解码耗时；
6. 规范 EXIF 方向，重编码为 WebP/JPEG，删除 EXIF 和不需要的 ICC/文本元数据；
7. 保存为随机 `source_image_id`，绑定 `owner_id`、原 `message_id` 和角色；
8. 在本地渠道镜像中记录受控 `media_url`，不记录任意绝对路径；
9. 将 `source_image_id` 写入收集会话。

飞书侧至少需要接收消息、读取消息资源、上传资源和以机器人身份回复的权限。当前项目
配置见[飞书机器人配置与使用指南](feishu-setup.md)。权限修改后必须创建并发布新版本，
旧失败消息不会自动重放，应发送新图片回归。

### Step 5：提交异步 Job

卡片点击“开始生成”后：

1. 事务内检查收集会话仍为 `ready`；
2. 使用条件更新 `ready → submitted` 抢占执行权；
3. 调用 Capability Service，显式传入当前 `owner_id`；
4. 保存 `job_id`、`asset_id` 与源消息关联；
5. Source Image 被 Job 成功领取后再删除临时副本；
6. 立即向飞书返回“已创建任务”，不要在卡片回调里同步等待 GPU；
7. Worker 在后台执行，进度写入通用 Job；
8. 完成后生成 `MediaResultEnvelope` 并投递 Outbox。

图片风格化接入时必须审查 `create_transfer_from_sources()` 的 owner 传播：读取内容图、
参考图、删除临时图和创建结果资产都要使用同一个 owner，不能依赖默认 `local`。

### Step 6：回传 2.5D 空间照片

推荐顺序：

1. **封面图片消息**：先让用户在聊天内立即看到结果；
2. **完成卡片**：名称、分辨率、模型、任务状态；
3. **打开可动预览**：URL 按钮指向签名 HTTPS Viewer；
4. **重新生成**：动作按钮携带 `job_id`；
5. **可选 MP4**：当 Viewer 不可达或设备不支持时提供轻量演示。

示意卡片：

```python
card = (
    new_card()
    .header(
        title="空间照片已生成",
        subtitle="点击打开可移动视角",
        template="green",
    )
    .markdown("**尺寸**：1600 × 1200\n**资产**：窗边的猫")
    .buttons([
        {
            "label": "打开可动预览",
            "url": signed_https_viewer_url,
            "style": "primary",
        },
        {
            "label": "重新生成",
            "action": {"command": "retry_spatial_job", "job_id": job_id},
        },
    ])
    .footer("预览链接短时有效；原始资产仍保存在你的电脑")
    .build()
)
```

Viewer 安全要求：

- 产品环境优先 HTTPS；同一局域网 HTTP 仅作为明确标注的开发模式；
- Token 至少包含 `asset_id`、过期时间、版本和不可预测签名；
- 建议增加 audience（用户或会话）、nonce、撤销记录、限流与访问日志；
- 只允许读取 Manifest 中白名单文件，禁止将相对路径直接映射到文件系统；
- 链接过期时展示友好页面，并允许用户回飞书请求新链接；
- Viewer 首屏先加载封面，深度层按需加载；低性能手机暂停或降低渲染帧率。

MP4 是未实现的增强项，不应在上线说明中写成已支持。建议由 Viewer 离屏渲染固定左右
平移轨迹，导出 3–5 秒 H.264/MP4；视频优先于 GIF，以降低体积与移动端解码能耗。

### Step 7：回传风格化图片

风格化结果是普通二维图像，应直接以飞书图片消息返回，而不是只发本地 Viewer：

1. 从资产 Manifest 解析 `result_file`，通过资产仓库白名单得到本机路径；
2. 用飞书上传图片接口取得 `image_key`，或继续使用当前 `lark-channel` 的本地图片
   `source` 抽象完成“上传 + 回复”；
3. 回复原始触发消息，发送清晰的结果图；
4. 再发送结果卡片，展示模式、质量档位、seed、Provider 和尺寸；
5. 提供“下载结果”“使用相同参考再次生成”“更换参考图”操作；
6. Web Root 渠道镜像记录结果资产的受控 `media_url`。

推荐交付两张派生图：

- `result.webp`：纯结果，用于飞书图片消息与下载；
- `comparison.webp`：原图与结果左右对比，用于可选对比预览。

不要只发送对比拼图，因为用户下载后还需要干净的最终结果。也不要在卡片中暴露可能
包含敏感信息的完整 Prompt；仅展示用户可理解、可复现且允许公开的参数。

风格化完成卡片示意：

```python
card = (
    new_card()
    .header(
        title="图片风格化已完成",
        subtitle="SDXL + IP-Adapter",
        template="green",
    )
    .markdown(
        "**模式**：保留布局\n"
        "**质量**：标准\n"
        f"**Seed**：{seed}"
    )
    .buttons([
        {
            "label": "下载结果",
            "url": signed_download_url,
            "style": "primary",
        },
        {
            "label": "再次生成",
            "action": {"command": "rerun_style_job", "job_id": job_id},
        },
    ])
    .footer("图片由本机能力生成；结果已进入个人资产库")
    .build()
)
```

“再次生成”应创建新的 Job 和新资产，并记录 `derived_from_asset_id`，不能覆盖旧结果。
按钮重复点击使用幂等键阻止多次创建。

### Step 8：失败、重试和可靠回传

区分三类失败：

| 失败位置 | 示例 | 恢复方式 |
| --- | --- | --- |
| 输入失败 | 权限不足、图片损坏、角色数量不对 | 保留收集会话，提示重新发送或补齐 |
| 生成失败 | Provider 未就绪、GPU OOM、任务超时 | 保留源资产，发送幂等重试卡片 |
| 回传失败 | 飞书断线、上传超时、消息限流 | Job 保持完成，Outbox 退避重试 |

Outbox 至少保存：

```text
delivery_id
idempotency_key
channel / chat_id / reply_to_message_id
owner_id
payload_kind
asset_id / job_id
status / attempt_count / next_attempt_at
last_error_code
created_at / delivered_at
```

不要把图片二进制或 Viewer Secret 放入 Outbox JSON；只保存受控资产 ID，发送时重新由
资产仓库解析路径。对平台限流执行带抖动的指数退避；对永久权限错误进入死信并提示
Root 管理员，不要无限重试。

### Step 9：同步 Web Root 控制台

渠道消息模型至少要表达：

```text
message_id
reply_to_message_id
direction
kind: image / card / status / video
asset_id
job_id
media_url
delivery_status
```

Web 只使用 `/api/assets/...` 等受控路由加载图片。飞书图片消息和结果图应显示真实预览，
2.5D 完成卡片显示封面与 Viewer 状态；本地镜像删除不应删除飞书原消息、个人资产或
长期记忆。

## 5. 新电脑安装与配置

新增功能只有在另一台干净电脑上能够按文档完成安装、配置和验收，才算完成交付。不要
把开发机上的 Python 缓存、模型权重、系统钥匙串或已登录状态当成部署前提。

### 5.1 先选择部署拓扑

| 拓扑 | 适用场景 | 空间照片 | 图片风格化 | 主要限制 |
| --- | --- | --- | --- | --- |
| 基础单机 | macOS / Windows 日常开发 | 本机 Depth Anything V2 | 仅显式 `local-preview` | 预览调色不能用于质量验收 |
| Windows NVIDIA 单机 | 个人产品完整体验 | 本机运行 | 独立 `pic-style` 服务运行在同机 | 需要 CUDA；权重约 9.84 GiB，另需至少 2 GiB 余量和环境空间 |
| Agent 与 GPU 分机 | Mac 运行 Agent，Windows 运行模型 | Agent 机运行 | 通过专用网络调用 GPU 机 | 需要鉴权、网络隔离和故障监控 |
| 公网产品 | 非同一局域网的真实用户 | HTTPS Viewer | 私有模型服务 | 还需身份、对象存储、域名、审计与 SLO，当前未完成 |

推荐先完成“Windows NVIDIA 单机”，再验证分机部署。不要把 `3000`、`8000` 或未鉴权的
`18000` 直接映射到公网；手机只访问带签名的 Viewer。

### 5.2 获取代码与检查环境

在新电脑上安装 Git、Python 3.11/3.12 和 Node.js 22.13+，然后获取私有仓库主分支：

```bash
git clone https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent.git
cd Personal-digital-assistant---A-Web-Agent
git switch main
git pull --ff-only
```

macOS / Linux 检查：

```bash
git --version
python3 --version
node --version
corepack enable
corepack prepare pnpm@11.9.0 --activate
pnpm --version
```

Windows PowerShell 检查：

```powershell
git --version
py -3.12 --version
node --version
corepack enable
corepack prepare pnpm@11.9.0 --activate
pnpm --version
```

基础 Agent 推荐至少 8 GB 内存。空间照片首次使用会下载约 100 MB 深度模型。真实本机
SDXL 路径的固定权重为 9.84 GiB，另需至少 2 GiB 余量；这不包含 Python、CUDA 和容器
镜像。模型许可证必须由安装者逐项审阅并主动接受，权重和缓存不能提交 Git。

### 5.3 安装并启动基础 Agent

macOS / Linux：

```bash
chmod +x scripts/setup.sh scripts/start.sh
./scripts/setup.sh
./scripts/start.sh
```

Windows PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1
.\scripts\start.ps1
```

安装脚本会创建 `.venv`，安装 `backend/requirements.txt` 和前端依赖。启动后检查：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/capabilities
```

Windows 可以改用：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/api/capabilities
```

浏览器打开 `http://localhost:3000`，API 文档位于 `http://127.0.0.1:8000/docs`。基础
控制台和 API 应继续只监听回环地址。

### 5.4 配置本机环境变量与 UI 密钥

在仓库根目录创建只存在于本机的 `.env`。下列是无密钥模板，不能把真实值写进文档或
提交 Git：

```env
# 空间照片 Viewer
LAN_VIEWER_BIND=0.0.0.0
LAN_VIEWER_PORT=8766
LAN_VIEWER_TTL_SECONDS=43200
LAN_VIEWER_PUBLIC_BASE_URL=
PUBLIC_VIEWER_AUTO_START=false
PUBLIC_VIEWER_ACKNOWLEDGE_PUBLIC_MEDIA=false

# 推荐的独立图片风格化服务
PHOTO_STYLE_PROVIDER=pic-style-http
PHOTO_STYLE_SERVICE_URL=http://127.0.0.1:18000
PHOTO_STYLE_SERVICE_API_KEY=
PHOTO_STYLE_TENANT_ID=personal-agent
PHOTO_STYLE_TIMEOUT_SECONDS=900
```

随后在 Web 控制台完成两类配置：

1. 在“模型设置”选择 LLM 供应商，填写 Base URL、模型和 API Key，先测试再保存；
2. 在“飞书设置”填写 App ID、App Secret、允许的 Open ID，测试连接后再启用长连接。

这些密钥由系统钥匙串保存，钥匙串不可用时才回退到本机加密文件。换电脑时默认应重新
填写，不要通过 Git、群聊或普通压缩包迁移密钥。修改 `.env` 后必须重启后端。

### 5.5 安装空间照片能力

基础安装已包含空间照片代码和 CPU/MPS/CUDA 自动设备选择。新电脑第一次生成时需要能
访问模型下载源；下载完成后可以离线推理。安装验收步骤：

1. 在 Web 上传一张无敏感信息的测试图并生成空间照片；
2. 等待模型下载和首个 Job 完成，检查封面、深度和分层资产均可读取；
3. 在电脑浏览器打开 Viewer 并拖动视角；
4. 只在专用网络放行 TCP `8766`，用同一局域网手机打开新生成的签名链接；
5. 若自动选择了错误网卡，把 `LAN_VIEWER_PUBLIC_BASE_URL` 设置为
   `http://<电脑局域网IP>:8766` 后重启。

开发期需要手机跨网络访问时，可在用户明确接受私人图片经公网转发后，将
`LAN_VIEWER_TTL_SECONDS` 调整为 `604800`，并把两个 `PUBLIC_VIEWER_*` 开关设为
`true`。启动脚本会自动维护临时 TryCloudflare 隧道；随机域名变化后，从原空间照片
结果卡片点击“刷新预览链接”获取最新地址。正式发布仍必须换成固定域名的 Named Tunnel。

飞书或手机内置浏览器拒绝局域网 HTTP 时，可按[跨平台部署指南](deployment.md)显式
启动 TryCloudflare 测试入口。它会转发私人媒体，只适合测试；产品环境应使用固定域名、
命名 Tunnel、HTTPS、用户身份和资产级授权。

### 5.6 安装图片风格化能力

产品推荐把 SDXL + IP-Adapter 作为独立服务部署，Agent 仅通过稳定 HTTP 契约调用它。
这能隔离 CUDA 崩溃、显存释放、依赖版本和模型升级，也方便以后把 GPU Worker 移到另一
台 Windows 电脑。

#### 方案 A：Agent 与 GPU 服务在同一台 Windows NVIDIA 电脑

先安装 Docker Desktop（WSL 2 后端）、与显卡驱动匹配的 NVIDIA/CUDA 环境和 Python 3.11
环境。然后在 Web Agent 仓库之外单独获取服务，并记录实际部署 commit：

```powershell
git clone https://github.com/frogi-m/pic-style.git
Set-Location pic-style
git rev-parse HEAD
Copy-Item .env.example .env
docker compose up --build --detach
docker compose ps --all
Invoke-RestMethod http://127.0.0.1:18000/health/ready
```

这一步先启动 Fake Provider，目的是验证 API、数据库、Redis、MinIO、迁移和 Agent 契约，
不是验收风格效果。Compose 开发环境的默认 API Key 只能用于本机联调；非开发环境必须
在 `pic-style/.env` 中更换，并同步安全注入 Web Agent 的
`PHOTO_STYLE_SERVICE_API_KEY`。

在真实 GPU 机上先生成硬件报告、查看不可变下载计划，不要因普通测试而下载模型：

```powershell
python scripts/hardware_report.py --output hardware_report.json
python scripts/prepare_models.py --manifest config/model-manifest.yaml --plan
```

安装者审阅输出中的 SDXL、IP-Adapter 等许可证并明确接受后，再下载、校验并执行工程门禁：

```powershell
python scripts/prepare_models.py --manifest config/model-manifest.yaml `
  --download --accept-model-licenses --verify-checksums
python scripts/benchmark_local.py --manifest config/model-manifest.yaml --suite preflight
python scripts/benchmark_local.py --manifest config/model-manifest.yaml --suite gate `
  --content D:\private-eval\content.png --style D:\private-eval\style.png
```

只有工程门禁和人工质量评审都通过，才按该仓库
[`docs/docker.md`](https://github.com/frogi-m/pic-style/blob/main/docs/docker.md)切换为
Docker 基础设施 + Windows Host 单并发 GPU Worker。切换后再次确认：

1. `http://127.0.0.1:18000/health/ready` 返回真实 Provider 就绪；
2. Web Agent 使用上一节的 `pic-style-http` 配置；
3. `GET /api/photo-style-transfers/provider` 显示服务可用；
4. 使用非敏感固定图片完成一次 Adapter smoke 和一次 Web Agent 端到端生成；
5. 记录模型锁、门禁文件、GPU、峰值显存、冷/热启动时间和结果人工结论。

独立服务的安装命令、模型 revision 和数据库迁移以它自己的版本化文档为准；本 SOP 不
复制所有可能变化的内部参数。每次部署都应记录已验证的 `pic-style` commit/tag，并在
升级后重新执行其 smoke、门禁和 Web Agent 契约测试。

#### 方案 B：Agent 与 Windows GPU 服务分机

在 Agent 机配置：

```env
PHOTO_STYLE_PROVIDER=pic-style-http
PHOTO_STYLE_SERVICE_URL=http://<GPU电脑专用网络IP>:18000
PHOTO_STYLE_SERVICE_API_KEY=<由安装者安全注入>
PHOTO_STYLE_TENANT_ID=personal-agent
```

GPU 机防火墙只允许 Agent 机的专用网 IP 访问 `18000`。跨网络优先使用 VPN、SSH Tunnel
或带双向认证的 HTTPS 网关；不要把端口直接暴露到公网。至少监控 readiness、队列长度、
GPU OOM、任务耗时和磁盘余量。

#### 方案 C：只做前后端开发

```env
PHOTO_STYLE_PROVIDER=local-preview
```

该模式不下载模型，只验证上传、Job、卡片、图片回传和重试链路。它不是生成式风格迁移，
结果不能用于模型效果验收，也不能在真实服务故障时静默充当降级结果。

如果暂时不部署风格服务，菜单应明确显示“未安装/不可用”；不应让用户提交任务后才得知
Provider 不存在。独立服务版本锁、自动安装器和能力开关仍是待实现的产品化项。

### 5.7 在新电脑上接通飞书

若复用原飞书应用，云端已发布的权限、事件订阅和应用可用范围通常无需重新创建，但本机
App Secret、Open ID 白名单和启用状态必须重新配置。迁移流程：

1. 先停止旧电脑的长连接，避免两个实例竞争消费同一应用的事件；
2. 在新电脑 Web 控制台录入并测试 App ID / App Secret；
3. 核对应用版本已发布、机器人能力已启用、消息事件与卡片动作回调已配置；
4. 核对用户或群成员位于应用可用范围，群聊中按配置要求 `@机器人`；
5. 启用新电脑长连接，依次发送文本、菜单、图片和卡片按钮做真实回归；
6. 确认稳定后再把旧电脑作为停用或回滚节点。

飞书权限与事件的当前配置以[飞书机器人配置与使用指南](feishu-setup.md)为准。不要让
两个不知道彼此状态的桌面实例同时承担同一 App 的生产消费；未来应通过设备租约、主节点
选举或服务端队列实现正式高可用。

### 5.8 新装与数据迁移必须分开

全新安装默认创建空的会话、记忆和资产库，这是最安全、最容易验收的路径。若确实需要把
旧电脑作为整体迁移：

1. 停止旧、新两台电脑的 Agent，避免复制正在写入的 SQLite/WAL；
2. 对旧电脑 `backend/data/` 做加密、带哈希的管理员备份，并把它视为包含聊天、图片、
   记忆和密钥材料的敏感数据；
3. 在受控介质中迁移所需数据库与资产目录，不得提交 Git；若需要让旧 Viewer 链接继续
   有效才迁移 `viewer-secret.key`，否则在新机重新生成并让旧链接自然失效；
4. 模型权重优先按锁文件重新下载和校验，不把它们混入用户数据备份；
5. 默认不要迁移 `.secret_master_key`、`*.enc` 或操作系统钥匙串内容；LLM Key 和飞书
   App Secret 在新电脑 UI 中重新录入；
6. 启动后验证 owner 隔离、历史资产、记忆、任务状态和 Viewer 签名；
7. 验收前保留只读旧备份，不要立即删除源数据。

当前尚无自动化导出/导入与数据库迁移工具，因此上述流程属于管理员手工迁移，不是普通
用户功能。产品化前应增加版本化 Backup Manifest、SQLite 一致性检查、加密导出、恢复
演练和跨平台路径重写。

### 5.9 端口、网络和安装后验收

| 端口/方向 | 用途 | 推荐暴露范围 |
| --- | --- | --- |
| `3000/TCP` | Root Web 控制台 | 仅本机回环 |
| `8000/TCP` | Agent API | 仅本机回环 |
| `8766/TCP` | 签名空间照片 Viewer | 专用局域网或受控 HTTPS Tunnel |
| `18000/TCP` | 独立图片风格化服务 | 本机回环或 Agent 到 GPU 的专用网络 |
| 出站 `443/TCP` | 飞书、LLM、首次模型下载 | 按目标域名最小放行 |

新电脑必须完成以下验收并记录操作系统、版本、设备和网络：

- [ ] `/health`、`/api/capabilities` 和 Provider 状态均符合所选拓扑；
- [ ] Web 文本对话、会话隔离、删除和图片预览正常；
- [ ] 飞书文本消息与功能卡片可收可回；
- [ ] 飞书图片能下载、清洗并在 Web Root 显示；
- [ ] 空间照片完成后收到封面和 Viewer，真机可拖动视角；
- [ ] 风格化完成后收到清晰结果图和操作卡片；
- [ ] 重复事件/重复点击不产生重复任务；
- [ ] 中断 Provider 或网络后可恢复，完成 Job 不因回传失败被错误标记失败；
- [ ] 日志、接口响应和 Git 状态中没有 Key、App Secret 或私人图片；
- [ ] 重启整机后，持久化任务、能力状态和飞书连接行为符合预期。

### 5.10 更新、回滚与卸载边界

更新前先备份数据并记录当前 commit、配置和 Provider 版本；在测试分支通过迁移与真实
租户回归后再切换主实例。回滚应恢复上一已验证版本及其兼容配置，不能只回退代码却继续
使用新版本数据库。模型服务不可用时，应把能力标记为“未安装/维护中”，保留已完成资产，
不要自动改用低质量 Provider。

当前项目还缺少正式 Release tag、配置 Schema 迁移、单能力禁用开关和一键数据迁移工具。
在这些能力完成前，更新与回滚必须由管理员执行并保留审计记录。删除模型或用户资产属于
破坏性操作，不纳入自动卸载脚本。

## 6. 建议的代码拆分

不要继续把所有能力分支写进 `FeishuChannelRuntime`。推荐逐步拆分：

```text
backend/app/channels/contracts.py
    InboundMessage / OutboundMessage / MediaResultEnvelope

backend/app/channels/media_sessions.py
    MediaCollectionSessionRepository / 状态机 / 过期清理

backend/app/channels/feishu_adapter.py
    事件解析、权限、资源下载、消息与卡片发送

backend/app/channels/result_presenter.py
    SpatialResultPresenter / ImageResultPresenter / VideoResultPresenter

backend/app/outbox.py
    出站持久化、重试、死信、幂等

backend/app/feishu.py
    长连接生命周期和旧接口兼容；逐步收缩
```

第一阶段可以保留单体部署和 SQLite，不需要立刻引入 Redis、Kafka 或独立微服务。只有
任务量、并发、SLO 或 SQLite 锁等待的实测证据达到升级阈值后再拆分基础设施。

## 7. 实施顺序

### P0：先完成可用闭环

- [x] 新增 `MediaCollectionSession` SQLite 表与状态机；
- [x] “图片风格化”卡片进入内容图/风格图收集流程；
- [x] 图片下载、清洗、owner 绑定和会话过期判断；
- [x] 修正风格化 Source Image 全链路 owner 传播；
- [ ] 抽象通用 Job 等待与 `MediaResultEnvelope`（通用等待已完成，Envelope/Presenter
  仍待拆分）；
- [x] 风格化结果图直接回复飞书；
- [x] 空间照片完成卡片改为封面 + Viewer URL 按钮；
- [x] 风格化和空间照片均提供幂等失败重试；
- [ ] Web Root 能预览输入图和结果图。

### P1：补可靠性与体验

- [ ] 持久化 Outbox、退避、死信和断网重放；
- [ ] 生成风格化对比图；
- [ ] 生成 2.5D MP4 降级预览；
- [ ] 卡片进度更新或阶段性状态卡片；
- [ ] Viewer 固定 HTTPS 域名、短期 Token、撤销和审计；
- [ ] 飞书内置浏览器、iOS、Android、局域网与公网真机测试。

### P2：再做扩展

- [ ] 把相同会话和 Presenter 接到虚拟试衣、宠物和 3D 资产；
- [ ] 支持结果多选、局部重绘和参数调整；
- [ ] 统一微信/企业微信 `ChannelAdapter`；
- [ ] 根据真实 SLO 决定是否引入独立 Worker/队列服务。

## 8. 测试与验收清单

### 8.1 自动化测试

- [x] 未授权 Open ID 无法创建收集会话；
- [ ] 群聊其他成员的图片不能进入当前用户会话；
- [x] 重复 `message_id` 不重复保存图片；
- [x] 重复卡片点击只创建一个 Job；
- [x] 内容图和风格图角色、数量、MIME、大小、像素限制正确；
- [x] EXIF 被清理，畸形图片和解压炸弹被拒绝；
- [x] owner 越权读取 Source Image、Job、Asset 均失败；Viewer audience 绑定仍待增强；
- [ ] Job 完成但飞书发送失败时进入 Outbox，而非修改 Job 为失败；
- [x] 空间结果依次产生封面和 Viewer 卡片；
- [x] 风格结果产生结果图和完成卡片；
- [x] 失败卡片的重试动作可恢复且幂等；
- [ ] Web 渠道镜像只能访问受控 `media_url`。

### 8.2 真实飞书租户验收

至少准备两个用户和一个群聊，记录 App 版本、权限、系统、网络与时间：

1. 私聊生成空间照片，手机打开 Viewer 并移动视角；
2. 群聊 A 发起风格化，B 的图片不会串入；
3. 发送 1 张内容图和 1、2、3 张参考图分别完成生成；
4. 飞书收到可放大的结果图，下载文件与本地资产哈希一致；
5. 点击同一“开始生成”两次，不产生重复任务；
6. 生成中断网，恢复后 Outbox 能补发结果；
7. 停止 Provider 后收到可操作失败卡片，启动后重试成功；
8. Viewer 链接过期、撤销和越权访问均被拒绝；
9. Web Root 中能看到对应图文记录，普通飞书用户不能进入 Root 控制台。

验收产物只保存脱敏日志、时间、结果哈希和平台错误码；不要把个人测试图片提交 Git。

## 9. 发布检查

- [ ] 更新 Capability Manifest、功能菜单和用户说明；
- [ ] 更新飞书权限后创建并发布应用新版本；
- [ ] `card.action.trigger` 回调已启用；
- [ ] 所有 Key、Viewer Secret、数据库、用户图片和模型权重均被 Git 忽略；
- [ ] 后端测试、前端 lint/build、Fake Channel 测试通过；
- [ ] 至少一次真实租户全链路通过；
- [ ] 记录已验证平台和仍未验证平台；
- [ ] 提供回滚开关，可关闭新 Capability 而不影响文本 Agent；
- [ ] 在第二台干净电脑按第 5 节完成安装、配置和验收；
- [ ] 记录代码版本、Provider 版本、模型锁、操作系统和已验证 GPU；
- [ ] 看板只把本次已验收的局部交付标记完成，长期任务保留真实状态。

## 10. 官方与项目资料

- [飞书上传图片](https://open.feishu.cn/document/server-docs/im-v1/image/create?lang=zh-CN)：
  将本地结果上传为可用于消息的图片资源；
- [飞书回复消息](https://open.feishu.cn/document/server-docs/im-v1/message/reply?lang=zh-CN)：
  将图片、卡片或文字回复到原消息；
- [飞书获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2?lang=zh-CN)：
  下载用户明确发送给机器人的图片；
- [飞书事件概述](https://open.feishu.cn/document/server-docs/event-subscription-guide/overview?lang=zh-CN)：
  事件回调、重试与处理边界；
- [飞书卡片交互配置与常见问题](https://open.feishu.cn/document/develop-a-card-interactive-bot/faqs)：
  卡片按钮回调与应用配置；
- [项目飞书配置指南](feishu-setup.md)：当前权限、长连接、Open ID、群聊和故障排查；
- [项目跨平台部署指南](deployment.md)：macOS、Windows、局域网 Viewer 与临时公网测试；
- [项目图片风格化技术文档](../photo-style-transfer.md)：本机模型、版本锁、许可和 GPU
  工程门禁；
- [`pic-style` README](https://github.com/frogi-m/pic-style)：独立服务当前能力、Fake
  路径、模型准备和 Agent 契约；
- [`pic-style` Docker / Windows 混合部署](https://github.com/frogi-m/pic-style/blob/main/docs/docker.md)：
  Docker 基础设施、回环端口和 Windows Host 单并发 GPU Worker；
- [项目外部聊天控制调研](../research/external-chat-control.md)：2.5D、3D、视频和 Viewer
  的渠道降级策略。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
