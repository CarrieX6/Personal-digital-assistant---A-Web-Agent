# 图片风格化独立服务部署与迁移

功能实现负责人：**Xianggang Ma**
文档维护：**项目组**
更新日期：2026-08-31  
对应任务：`STYLE-001`、`CAP-002`

## 结论先行

图片风格化入口已经集成进 Web Agent，但真实生成效果依赖真实 SDXL Provider。产品默认
使用独立 `pic-style` 服务；macOS MPS 暂时使用仓库内 `sdxl-local` 工程路径。对于独立
服务，只有 `GET /api/photo-style-transfers/provider` 同时满足以下条件，工具库才应显示
“SDXL 已就绪”：

1. 独立服务 `/health/ready` 返回 200；
2. `/health/provider` 返回真实 Provider，而不是 `fake`；
3. Web Agent 状态中的 `details.production_quality=true`。

项目提供跨平台部署管理器：

```text
macOS / Linux  ./scripts/photo-style.sh <command>
Windows        .\scripts\photo-style.ps1 <command>
```

它负责固定源码版本、独立虚拟环境、配置、进程、健康检查和部署状态记录。主项目统一
`setup` 默认选择真实模型；`deploy-test` 仅作为显式 Fake 契约测试。真实权重约
9.84 GiB，必须由操作者审阅许可证并显式确认。真实模型现在有三条明确路径：

1. **Windows + NVIDIA CUDA 独立服务**：已有 RTX 4060 工程冒烟证据，仍需每台设备复验；
2. **Apple Silicon + PyTorch MPS 本机 Provider**：代码和一键准备已支持，但当前 M4
   尚未下载权重、未完成真实生成与质量门禁，因此只能标为工程验证路径；
3. **远程 GPU Provider**：沿用 `pic-style-http` 契约，公网必须 HTTPS，API Key 单独注入。

## 为什么是独立服务

```text
Web / 飞书 / Agent Tool
          ↓
Web Agent API 与持久化 Job
          ↓ HTTP + tenant + idempotency
pic-style API / Queue / Asset Store
          ↓
Fake Provider（链路测试）或 SDXL + IP-Adapter Worker（真实生成）
```

独立服务避免 CUDA、Diffusers、模型权重和显存生命周期污染 Agent 主进程；GPU 服务可以
迁移到另一台电脑，而 Web、飞书、资产和 Agent Tool 契约保持不变。服务端口默认只监听
`127.0.0.1:18000`。分机部署应使用专用网络、VPN 或 SSH Tunnel，不得直接暴露无鉴权
HTTP 端口。

## 当前 Mac 的真实状态

2026-08-31 在 Apple M4、16 GB 内存、macOS 的本机验证结果：

- Agent 控制台、FastAPI、飞书长连接正常；
- 固定提交 `c8e0b641f7f334faf73167211b5f0a8033e3b1dc` 的 `pic-style` 已安装到
  `.capabilities/pic-style`；
- GitHub Git 端点异常时，安装器通过 GitHub API 固定 commit 归档完成安装；
- 独立 Python 3.12 环境、SQLite、本地资产、队列和 Fake Provider 正常；
- 上游 Agent Adapter smoke 成功；
- Web Agent 端到端合成图任务完成，结果记录为 `pic-style-http / fake`；
- 未下载 SDXL、IP-Adapter 或 LCM-LoRA 权重；
- 仓库内 `sdxl-local` 已增加 MPS 设备选择、attention slicing、MPS OOM/内存清理、
  设备元数据和设备级质量门禁；
- 当前 M4 已通过 `doctor` 的 MPS 预检，但尚未接受许可证、下载权重和执行真实推理，
  因此不能声明真实 SDXL 已可用或已达到生产质量。

## 命令总览

| 命令 | 是否联网 | 是否下载模型 | 用途 |
| --- | --- | --- | --- |
| `doctor` | 否 | 否 | 检查平台、架构、Python、磁盘、Docker、NVIDIA、安装和健康状态 |
| `status --json` | 仅访问本机服务 | 否 | 输出可供诊断或自动化读取的部署状态 |
| `deploy-test` | 是 | 否 | 一键安装固定源码、隔离依赖、Fake 服务并配置 Agent |
| `install-service` | 是 | 否 | 只安装服务，不启动 |
| `configure-agent` | 否 | 否 | 将 Agent 配置为访问本机独立服务并启用自动启动 |
| `configure-real-windows` | 否 | 否 | 配置 Docker API、随机本机 Key 与 Windows Host GPU Worker |
| `start` / `stop` | 否 | 否 | 管理由本部署器启动的服务进程 |
| `plan-real` | 否 | 否 | 打印固定模型、下载量和许可证链接 |
| `prepare-windows-gpu` | 是 | 默认否 | Windows NVIDIA 依赖和模型准备；未确认许可证时在下载前停止 |
| `prepare-macos-mps` | 是 | 默认否 | Apple Silicon MPS 依赖和模型准备；未确认许可证时在下载前停止 |
| `configure-remote` | 否 | 否 | 配置远程 HTTP Provider；拒绝公网明文 HTTP，不接收 API Key |

## macOS / Linux：链路测试部署

默认完整部署直接运行 `./scripts/setup.sh --accept-model-licenses`。只有需要隔离排查
Web/飞书/Job 契约而不下载模型时，才显式执行：

```bash
./scripts/photo-style.sh doctor
./scripts/photo-style.sh deploy-test
./scripts/photo-style.sh status --json
```

`deploy-test` 会：

1. 获取与本项目 Manifest 一致的固定 `pic-style` commit；
2. 在 `.capabilities/pic-style/.venv` 创建隔离环境；
3. 启用 SQLite、本地文件、in-process queue 和 Fake Provider；
4. 更新主项目 `.env` 的 HTTP Provider 和自动启动配置；
5. 启动 `127.0.0.1:18000` 并验证 readiness 与 Provider。

重新运行 `./scripts/start.sh` 时，如果 `PHOTO_STYLE_AUTO_START=true`，启动器会检查并启动
已安装服务；失败只会给出警告，Agent 控制台仍会启动并显示“未部署”，不会降级伪装。

停止独立服务：

```bash
./scripts/photo-style.sh stop
```

## macOS：真实 SDXL + IP-Adapter 的兼容处理

现有 CUDA 代码不能只把字符串从 `cuda` 改成 `mps`。本项目为 MPS 路径增加了以下兼容：

- 加速器预检从 CUDA 专用改为 `auto/cuda/mps`，Torch 仍在短生命周期子进程中探测；
- CUDA 保留 `enable_model_cpu_offload()`；MPS 改用整条 Pipeline `.to("mps")` 加
  `enable_attention_slicing()`，不混用 CUDA 导向的 offload；
- CUDA 与 MPS 分别处理内存统计、OOM 和 `empty_cache()`；
- 继续单任务、单输出、最长边 768、VAE tiling，并默认任务后退出隔离 Worker；
- 设置 `PYTORCH_ENABLE_MPS_FALLBACK=1` 兼容暂不支持的算子，但必须在验收中记录回退造成
  的 CPU 延迟；
- Windows CUDA 的 `engineering_smoke_passed` 不继承给 MPS。MPS 在完成本机固定样本、
  十次稳定性、统一内存、功耗和人工质量检查前始终为 `production_quality=false`。

只读检查与计划：

```bash
./scripts/photo-style.sh doctor
./scripts/photo-style.sh plan-real
./scripts/photo-style.sh prepare-macos-mps
```

最后一条会安装推理依赖并展示计划，但不接受许可证、不下载模型。逐项审阅输出中的
SDXL、IP-Adapter、LCM-LoRA 许可证后，才能显式运行：

```bash
./scripts/photo-style.sh prepare-macos-mps --accept-model-licenses
```

完成后重启 Agent，并先用非个人固定图片执行 `smoke_photo_style_sdxl.py`。至少记录：

- macOS、芯片、统一内存、Python、Torch、Diffusers 版本；
- 冷启动、热生成 P50/P95、成功率和输出哈希；
- `torch.mps.driver_allocated_memory()`、系统内存、swap、温度与整机功耗；
- 是否发生 CPU fallback、黑图、NaN、OOM、构图/身份破坏；
- preview/standard 两档、连续十次以及任务结束后的内存回收。

当前安装器已经把第一轮非个人合成图 smoke 纳入 `prepare-macos-mps`；也可在模型已准备后
单独重跑：

```bash
./scripts/photo-style.sh validate-macos-mps
```

M4/16GB 适配额外启用 VAE tiling+slicing，并在检测到较小统一内存预算时使用最大粒度的
attention slicing。每个任务仍在隔离 Worker 中执行并于完成后退出，避免 SDXL 常驻影响
Agent、飞书和 Viewer。工程 smoke 通过只会写入本机忽略目录中的
`device-validation.json`，不会自动宣称达到生产质量。

Hugging Face 的 MPS 指南确认 Diffusers 可通过 PyTorch `mps` 使用 Apple Silicon，并建议
在统一内存压力下启用 attention slicing；PyTorch 也提供 MPS 可用性检查、allocator 和
CPU fallback 环境变量。它们证明“框架路径存在”，不能替代本项目的 SDXL + IP-Adapter
组合实测。Apple Core ML 路线支持 SDXL，但当前项目的 IP-Adapter 运行时和任务契约基于
Diffusers；迁移 Core ML 需要重新实现 Adapter 注入、权重转换和结果一致性验证，不是本次
一键安装的等价替换。

## Windows：基础服务与真实 GPU 准备

先安装 Python 3.11/3.12、Node.js、Git、Docker Desktop + WSL2 和 NVIDIA 驱动。默认
完整部署：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup.ps1 --accept-model-licenses
.\scripts\start.ps1
```

只排查 Fake 契约链路时才执行：

```powershell
.\scripts\photo-style.ps1 doctor
.\scripts\photo-style.ps1 deploy-test
.\scripts\photo-style.ps1 status --json
```

真实模型前先运行只读计划：

```powershell
.\scripts\photo-style.ps1 plan-real
.\scripts\photo-style.ps1 prepare-windows-gpu
```

第二条命令会检查 Windows 与 `nvidia-smi`，安装 GPU extra 并再次显示上游固定模型计划；
由于没有许可确认参数，它会在下载前退出。操作者逐项审阅 SDXL、IP-Adapter、LCM-LoRA
许可证后，才可显式执行：

```powershell
.\scripts\photo-style.ps1 prepare-windows-gpu --accept-model-licenses
```

统一完整安装会继续执行 `configure-real-windows`，把 Agent、Docker API 和宿主机 GPU
Worker 串成自动启动链路；但不会代替本机质量判定。随后仍需在 `pic-style` 目录执行：

1. `hardware_report.py`；
2. `benchmark_local.py --suite preflight`；
3. 固定无敏感内容/风格图的 `--suite gate`；
4. 人工检查身份、构图、伪影、水印迁移与风格强度；
5. 记录本机 GPU、驱动、CUDA、峰值显存、冷/热延迟和结果；
6. 使用 Docker 基础设施 + Windows Host 单并发 Worker 启用真实 Provider；
7. 再跑上游 Adapter smoke 和 Web Agent 端到端任务。

不能把另一台 RTX 4060 的通过结论直接继承到新设备，也不能仅凭服务 200 响应认定模型
质量通过。

## 分机或远端 GPU

可以先用管理器写入不含密钥的配置：

```bash
./scripts/photo-style.sh configure-remote \
  --url https://gpu.example.com/photo-style \
  --tenant personal-agent
```

局域网私有 IP 可使用 HTTP；公网地址会被强制要求 HTTPS。URL 禁止包含用户名、密码、
query token 或 fragment。`PHOTO_STYLE_SERVICE_API_KEY` 必须通过目标电脑的 Secret 配置
单独注入，避免出现在 shell history、Git diff 和部署状态中。

Agent 机设置：

```env
PHOTO_STYLE_PROVIDER=pic-style-http
PHOTO_STYLE_SERVICE_URL=http://<GPU电脑专用网络IP>:18000
PHOTO_STYLE_SERVICE_API_KEY=<单独安全注入>
PHOTO_STYLE_TENANT_ID=personal-agent
PHOTO_STYLE_AUTO_START=false
```

GPU 机只允许 Agent 机 IP 访问服务。跨互联网必须增加 HTTPS、鉴权、限流、审计和密钥
轮换，优先使用 VPN/SSH Tunnel；不要用 Quick Tunnel 暴露生成 API。图片属于私人数据，
需要明确保留期、删除策略和访问日志。

## 迁移到新电脑

不要复制 `.venv`、容器层或系统 CUDA。代码与运行数据分开迁移。推荐流程：

1. 在旧机运行 `status --json`，保存 source commit、profile 和 Provider 状态；
2. 停止旧机 Agent 与风格服务，防止两个实例同时消费相同任务；
3. 旧机用 `scripts/deploy.py backup --output <文件>.pdabundle` 导出口令加密运行数据；
4. 新机从 GitHub 拉取 Web Agent 的同一版本并运行默认完整 `setup`；
5. 用 `scripts/deploy.py restore <文件>.pdabundle` 恢复会话、记忆、资产与可迁移 Secret；
6. Windows GPU 机重新审阅许可证、下载并校验固定权重，不能复制未知来源缓存；
7. API Key 只进入系统钥匙串或加密迁移包，不进入 Git、普通压缩包或聊天；
8. 依次验收 upstream smoke、Web、飞书图片输入、结果回传和失败重试；
9. 新机稳定后再停用旧机，保留短期只读回滚备份。

安装来源记录位于 `.capabilities/pic-style/.web-agent-source.json`，部署记录位于
`.web-agent-deployment.json`。整个 `.capabilities/` 被 Git 忽略；它包含第三方代码、
环境和可能的数据，不能提交到主仓库。

## 安全与失败边界

- 部署器固定上游 commit，不跟随浮动 `main`；
- Git 连接失败时只回退到同一 commit 的 GitHub API 归档，不下载任意镜像；
- 归档拒绝符号链接/硬链接，避免路径逃逸；
- 现有安装目录来源不明或 Git 工作区脏时失败关闭；
- 模型运行时下载保持关闭；
- 普通 Agent 对话不能直接执行依赖安装和许可证接受；
- `PHOTO_STYLE_SERVICE_API_KEY` 不写入部署状态或日志；
- Fake Provider 只能显示“测试模式”，结果 Metadata 的 `production_quality=false`。

## 验收清单

```bash
curl http://127.0.0.1:18000/health/ready
curl http://127.0.0.1:18000/health/provider
curl http://127.0.0.1:8000/api/photo-style-transfers/provider
./scripts/photo-style.sh status --json
```

测试模式期望：服务 readiness 为 `ready`，Provider 为 `fake`，Agent
`production_quality=false`。真实模式期望 Provider 为经本机门禁批准的
`sdxl_ip_adapter_8gb_v1`，并用一组非敏感固定图完成真实结果人工检查。

代码回归：

```bash
python -m pytest backend/tests/test_photo_style_deployment.py \
  backend/tests/test_sdxl_style_provider.py backend/tests/test_photo_style.py -q
pnpm test
```

## 已知限制与下一步

- 当前管理器没有自动卸载，避免误删模型和用户任务；卸载前必须先分类服务代码、模型、
  数据和备份，再设计可恢复操作；
- macOS MPS 工程路径已实现但尚未在本机下载权重或跑真实图；MPS 质量门禁仍为 pending；
- Windows GPU 已封装 Docker API + Host Worker 自动配置/启动，但尚未替代上游 benchmark、
  目标实机重启验证和人工质量确认；
- `frogi-m/pic-style` 独立服务的真实 Worker 仍为 CUDA 专用；当前 MPS 是主仓库内
  `sdxl-local` 工程路径。待 MPS 实测稳定后，应把同一加速器抽象下沉到独立服务；
- 生产环境仍需服务版本升级/回滚、签名制品、SBOM、漏洞扫描、队列监控和备份恢复演练；
- 后续可以把 `doctor/status/plan` 暴露给 Agent，把实际安装保留为管理员审批动作，不应让
  LLM 自主接受许可或执行大规模下载。

## 官方依据

- [Hugging Face Diffusers：Apple Silicon MPS 优化](https://huggingface.co/docs/diffusers/main/optimization/mps)
- [PyTorch：MPS backend](https://docs.pytorch.org/docs/stable/notes/mps.html)
- [PyTorch：MPS 环境变量](https://docs.pytorch.org/docs/stable/mps_environment_variables.html)
- [Hugging Face Diffusers：IP-Adapter](https://huggingface.co/docs/diffusers/using-diffusers/ip_adapter)
- [Apple Core ML Stable Diffusion：SDXL 转换与运行](https://github.com/apple/ml-stable-diffusion#using-stable-diffusion-xl)
