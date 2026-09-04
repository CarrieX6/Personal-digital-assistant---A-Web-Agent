# 图片风格化 Skill 集成说明

实现与文档作者：**Xianggang Ma**
更新日期：2026-08-20
任务：`STYLE-001`

## 结论

项目已按现有空间照片的产品形态接入 `photo-style-transfer`：Web/PC 工作台负责内容图、
一至三张风格参考图和业务参数输入；FastAPI 创建异步任务；结果、参考图、可复现参数
和清单写入现有本地个人资产库；Agent 通过 Capability Registry 中的
`create_photo_style_transfer` 工具调用同一服务。

接口保持 Provider 中立。产品默认使用 `pic-style-http` 调用
[`frogi-m/pic-style`](https://github.com/frogi-m/pic-style) 独立 SDXL + IP-Adapter
服务，主 Agent 不加载生成模型。`local-preview` 只保留给开发、合同测试和无 GPU 的
离线链路验证，并在 UI 明确标注“非生成模型”；不能再作为默认产品效果。也可显式选择
`sdxl-local`，但不建议让主 Agent 与约 10GB 的生成模型共享进程生命周期。集成参考
版本为提交 `c8e0b641f7f334faf73167211b5f0a8033e3b1dc`。

跨平台安装、自动启动、Windows GPU 准备、迁移与真实验收统一见
[图片风格化独立服务部署与迁移](guides/photo-style-deployment.md)。工具库会区分“未部署”、
“测试模式”和“SDXL 已就绪”；独立服务使用 Fake Provider 时，结果 Metadata 固定标记
`production_quality=false`。

## 用户链路

1. 在“图片风格化”工作台选择一张内容图和一至三张参考图；
2. 选择“保留布局”或“重新构图”、质量档位及三项强度；
3. `POST /api/photo-style-transfers` 返回资产和异步任务；
4. 前端轮询 `/api/jobs/{job_id}`；
5. 完成后并排显示原图与风格化结果，并提供本地下载；
6. 结果进入与空间照片共用的 SQLite/文件资产库。

Agent 控制台为内容图和风格参考图提供独立上传入口：内容图固定选择一张，风格参考图
可以批量选择 1–3 张，因此图片角色不依赖系统文件选择器返回的顺序；同时提供与
“生成空间照片”并列的“图片风格化”快捷指令。链路先用 `/api/source-images` 暂存图片，
只把随机附件 ID、文件名和尺寸提供给 LLM。
`create_photo_style_transfer` 成功领取任务后立即删除临时附件；LLM 不会接收原始图片字节。

## Capability Registry

`GET /api/capabilities` 返回运行时 Capability Manifest。图片风格化条目包含：

- ID：`photo-style-transfer`；
- 入口：`create_photo_style_transfer`；
- 作者：Xianggang Ma；
- 输入 Schema：内容图 ID、1–3 个风格图 ID、模式、质量、强度、提示词和种子；
- 本地模型、存储、权限与下载要求。

工具 Schema 同时进入 OpenAI-compatible Tool Calling，避免 Capability 声明与 Agent
实际可调用接口分叉。空间照片也迁入同一运行时 Manifest 查询接口。

## Provider 与配置

### 独立 SDXL + IP-Adapter 服务（产品默认）

先按参考仓库文档在 GPU 机器启动 API 与单并发 Worker，再配置：

```env
PHOTO_STYLE_PROVIDER=pic-style-http
PHOTO_STYLE_SERVICE_URL=http://127.0.0.1:18000
PHOTO_STYLE_SERVICE_API_KEY=
PHOTO_STYLE_TENANT_ID=personal-agent
PHOTO_STYLE_TIMEOUT_SECONDS=900
```

主项目会轮询 `/health/ready`；服务未就绪时 Web 按钮不可提交，API 也会拒绝创建任务，
不会悄悄降级成 CPU 调色。该路径把模型生命周期、队列和 GPU 隔离在独立服务。API Key
只从环境读取；服务端仍需完成固定模型、许可证与质量门禁。

### 本地开发预览（显式降级）

```env
PHOTO_STYLE_PROVIDER=local-preview
```

- 模型：无；
- 下载：无；
- 设备：CPU 即可；
- 用途：开发、自动测试、交互预览；
- 限制：不是生成式神经风格迁移，不能作为 SDXL 质量验收结果。

### 本机 SDXL Provider

本机路径使用参考仓库已经过 8 GB CUDA 工程门禁的参数映射：SDXL img2img latent 保持内容，
IP-Adapter 只向选定风格层注入等权参考 embedding，标准档使用
DPMSolverMultistepScheduler；可选 LCM-LoRA 预览默认关闭。CUDA 使用模型 CPU offload，
MPS 使用整条 Pipeline 上卡和 attention slicing；两者都启用 VAE tiling、单并发和单次
隔离子进程。生成完成或失败后子进程退出，由操作系统确定性回收权重，避免桌面 Agent
长期占用统一内存或 Windows/PyTorch 原生分配器保留工作集。

安装与准备：

Windows NVIDIA 先安装与当前驱动兼容的 CUDA 版 PyTorch，并确认
`torch.cuda.is_available()` 为 `True`，再安装其余 GPU 依赖。RTX 4060 Laptop 的本机
验证组合为 PyTorch 2.11.0、Torchvision 0.26.0 与 CUDA 12.8 wheel：

```powershell
python -m pip install torch==2.11.0 torchvision==0.26.0 `
  --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r backend/requirements-gpu.txt
python backend/scripts/prepare_photo_style_models.py --plan
# 逐项审阅输出中的许可证 URL 后，再由操作者明确接受：
python backend/scripts/prepare_photo_style_models.py `
  --download --accept-model-licenses --verify-checksums
```

然后在本机 `.env` 中配置：

```env
PHOTO_STYLE_PROVIDER=sdxl-local
PHOTO_STYLE_MODEL_ROOT=backend/models/photo-style
PHOTO_STYLE_MODEL_MANIFEST=backend/config/photo-style-models.json
PHOTO_STYLE_PROVIDER_GATE=backend/config/photo-style-provider-gate.json
PHOTO_STYLE_MODEL_LOCK=backend/models/photo-style/model-lock.json
PHOTO_STYLE_ACCELERATOR=cuda
PHOTO_STYLE_LCM_PREVIEW_ENABLED=false
PHOTO_STYLE_VERIFY_MODEL_HASHES=false
PHOTO_STYLE_UNLOAD_AFTER_GENERATION=true
```

`PHOTO_STYLE_UNLOAD_AFTER_GENERATION=true` 适合个人桌面 Agent，代价是下一次任务需要重新
加载权重。只有需要连续批处理且能够接受约 10 GB 常驻内存时，才建议显式改为 `false`。
Provider 状态接口会返回 `loaded`、`execution_mode`、`unload_count` 和最近一次进程退出
耗时；加速器预检也在短生命周期子进程中执行，不会因查看状态而把 Torch 常驻到 API
进程。

Apple Silicon 可改用一键准备：

```bash
./scripts/photo-style.sh doctor
./scripts/photo-style.sh prepare-macos-mps
# 审阅许可证后才显式执行：
./scripts/photo-style.sh prepare-macos-mps --accept-model-licenses
```

MPS 使用 `pipeline.to("mps") + attention slicing`，不使用 CUDA 路径的 model CPU
offload；同时单独处理 MPS OOM、allocator 清理和 CPU fallback 元数据。当前 MPS 只完成
代码与预检兼容，真实权重、质量、统一内存和功耗尚未在本机验收，状态必须保持
`production_quality=false`。完整门禁见
[macOS MPS 验收模板](experiments/style-004-macos-mps-validation.md)。

`GET /api/photo-style-transfers/provider` 只执行预检并返回依赖、CUDA/MPS、模型、门禁和加载
状态，不会加载权重或联网。真正开始任务时仍会再次验证许可证明、固定 revision、文件
尺寸、model lock 和可选 SHA-256；所有 Diffusers 调用均设置 `local_files_only=True`。

准备完成后可先绕过 Web 做单次 GPU 冒烟：

```bash
python backend/scripts/smoke_photo_style_sdxl.py \
  --content path/to/content.png --style path/to/style.png \
  --output path/to/result.png --quality standard --seed 1701
```

固定组件为：

| 组件 | 固定版本 | 许可证 | 计划下载 |
| --- | --- | --- | --- |
| SDXL Base 1.0 fp16 | `462165984030d82259a11f4367a4eed129e94a7b` | OpenRAIL++ | 6.46 GiB |
| IP-Adapter SDXL ViT-H | `018e402774aeeddd60609b4ecdb7e298259dc729` | Apache-2.0 | 3.01 GiB |
| LCM-LoRA SDXL | `a18548dd4956b174ec5b0d78d340c8dae0a129cd` | OpenRAIL++ | 375.62 MiB |

总计 10,561,842,988 字节（9.84 GiB），另需至少 2 GiB 磁盘余量。模型准备脚本会打印
每个许可证链接；缺少 `--accept-model-licenses` 时在网络请求前退出。本仓库忽略整个
`backend/models/`，不会提交权重、Hugging Face 缓存或机器生成的 model lock。

## 存储与权限

- `backend/data/assets.sqlite3`：资产和任务索引；
- `backend/data/assets/<asset-id>/`：规范化内容图、风格参考、结果和 `style.json`；
- `backend/data/source-images/`：Agent 临时附件，默认最长保留 24 小时；
- `backend/models/photo-style/`：本机真实模型与 model lock，仅本地存在并被 Git 忽略；
- 权限：读取用户主动选择的本地图片、写入本地个人资产；
- 只有选择 `pic-style-http` 时才需要访问配置的风格服务；
- API Key 只从环境读取，不写入资产、Manifest、Agent 轨迹或响应。

所有图片都执行格式验证、像素上限检查、EXIF 方向规范化和元数据剥离。支持 JPG、PNG、
WebP，单文件最大 20 MB；文件下载仍经过资产清单允许列表，不能用路径参数读取任意文件。

## 参数对应

| 业务参数 | 范围 | 含义 |
| --- | --- | --- |
| `mode` | `preserve_layout` / `recompose` | 保留原构图或允许更明显的参考纹理重组 |
| `quality` | `preview` / `standard` / `high` | Provider 的质量/成本档位 |
| `style_strength` | 0–1 | 风格注入程度 |
| `content_strength` | 0–1 | 主体和布局保持程度 |
| `detail_strength` | 0–1 | 边缘和细节保持程度 |
| `seed` | 非负 63 位整数 | 复现真实 Provider 结果；未提供时本机生成 |

## 代码位置

- `backend/app/style_transfer.py`：Provider、异步服务、Agent 工具与 Capability Manifest；
- `backend/app/sdxl_style_provider.py`：本机 SDXL 推理、8 GB 调度与状态预检；
- `backend/app/style_model_manifest.py`：固定清单、许可证明、quality gate 和 model lock 校验；
- `backend/app/assets.py`：通用资产/任务类型及安全文件访问；
- `backend/app/main.py`：HTTP API 与运行时注册；
- `app/components/PhotoStyleStudio.tsx`：Web/PC 工作台；
- `backend/scripts/prepare_photo_style_models.py`：显式模型准备与校验；
- `backend/scripts/smoke_photo_style_sdxl.py`：单次本机 GPU 冒烟生成；
- `backend/tests/`：API、资产、Manifest、参数映射、Provider 与 Agent 回归测试。

## 决策与限制

本任务不新增项目级 ADR：Provider 中立接口、本地异步任务、SQLite/文件资产存储和
临时附件隔离均沿用现有已记录架构，仅新增一个实现该边界的 Capability。若后续把
`pic-style` 服务作为默认生产 Provider、引入常驻 GPU Worker 或修改通用任务状态机，
应另建 ADR。

当前未实现任务取消、多输出选择、区域蒙版和视频风格化。本机 Provider 已在正式
`.venv` 上完成 SDXL + IP-Adapter 基础预览与 LCM-LoRA 预览各两次工程冒烟，门禁状态为
`engineering_smoke_passed`，但 `production_quality` 仍为 `false`：单个合成样本不能替代
固定图集质量盲评、十次稳定性与跨设备验证。

## 验证记录

2026-08-13 的本地工程验证结果：固定 revision 的 9.84 GiB 模型完成下载、model lock
生成与关键权重 SHA-256 复验；PyTorch 2.11.0+cu128 正式环境 CUDA 自检通过；最终代码
复跑的基础预览在 8.247 秒、6278 MiB 峰值预留显存完成，LCM 预览在 8.048 秒、6502 MiB
完成；两条路径各 2/2 成功且相同 seed 输出哈希稳定。后端、前端 lint、Vinext 构建和
渲染测试完成回归；上一轮桌面
与 390 × 844 移动视口已完成可见性检查。
独立 `tsc --noEmit` 仍受仓库既有的
`ModelSettingsDialog` 可空值和 Cloudflare 类型缺失影响，新增图片风格化文件未产生
TypeScript 报错。完整范围、失败项和未覆盖项见
[`STYLE-EXP-001`](experiments/style-001-local-preview.md) 和
[`STYLE-EXP-002`](experiments/style-002-native-sdxl-readiness.md)，真实 GPU 记录见
[`STYLE-EXP-003`](experiments/style-003-native-sdxl-gpu-smoke.md)。
