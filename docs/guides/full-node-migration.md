# Web Agent 单机完整节点架构与迁移方案

作者：**Zhuofan Xie**

对应任务：`CAP-003`

更新日期：2026-09-14

## 1. 决策结论

本项目不再把 Mac 固定为控制与数据入口、把另一台设备只作为 GPU 计算节点。该拓扑要求
两台设备长期互通，并引入内网寻址、共享文件、密钥分发、重试对账和双机运维问题，不适合
用户更换网络、关闭 Mac 或把系统完整交付到一台新电脑的目标。

正式默认架构改为**单机完整节点**：目标电脑同时承载 Web Agent、飞书长连接、用户数据、
模型服务和结果 Viewer。Mac、Windows NVIDIA、Linux x86_64 NVIDIA 与 DGX Spark 是不同的
目标机档位，而不是控制节点和计算节点的固定角色。

```text
手机飞书 / 本机浏览器
          │
          ▼
┌────────────────────── 一台目标电脑 ──────────────────────┐
│ Web UI :3000                                             │
│   └── 设置中心：LLM、飞书、数据、模型安装、诊断           │
│                                                          │
│ FastAPI + LangGraph :8000                                │
│   ├── 会话、身份、权限、审批、幂等和任务状态              │
│   ├── SQLite：记忆、消息、Checkpoint、安装记录            │
│   ├── 本机资产：原图、深度、蒙版、结果、3D 资产            │
│   ├── 飞书长连接：由本机主动出站，不依赖 Mac 中转          │
│   └── Viewer :8766                                       │
│                                                          │
│ 本机隔离 Provider                                        │
│   ├── Depth Anything V2 + BiRefNet                       │
│   ├── Granite Embedding                                  │
│   ├── SDXL + IP-Adapter :18000 或本机 MPS                │
│   └── Flux-GS :18100（仅通过兼容门禁后启用）              │
└──────────────────────────────────────────────────────────┘
```

“单机”不代表所有依赖放进同一个 Python 进程。控制面与模型面仍要用虚拟环境、受控子进程或
本机容器隔离；区别是它们位于同一台物理设备，通过回环地址或本机挂载通信，不再依赖跨设备
内网。

## 2. 为什么比双机方案更适合产品化

| 维度 | Mac 控制 + Spark 计算 | 单机完整节点 |
| --- | --- | --- |
| 网络 | 两机必须互通，网络变化会中断任务 | 核心服务只走回环地址 |
| 数据 | 需要跨机上传、哈希对账和清理 | 输入、模型和输出在同一资产域 |
| 可用性 | 任一设备离线都可能不可用 | 目标机独立运行 |
| 密钥 | 需要在两机分发 Provider 凭证 | LLM/飞书 Secret 只在目标机保存 |
| 调试 | 日志、任务和 GPU 状态分散 | 一处查看任务与模型日志 |
| 迁移 | 需要重建跨机关系 | 备份数据后在新机恢复 |
| 代价 | 可独立扩容 GPU | 单机容量有限，需要资源配额和降级 |

单机方案没有消除所有问题：模型依赖仍可能冲突，长任务仍需持久化，手机 Viewer 仍需要稳定
HTTPS 地址。它消除的是**非必要的双机依赖**，不是取消工程边界。

## 3. 产品界面重新划分

产品只保留一个 Web Agent，不再把“控制端”做成另一套产品：

1. **对话**：日常图文对话、任务进度和结果；不堆放全局运维信息。
2. **工具库**：说明用户能调用什么，按已就绪、测试、未安装和待适配展示。
3. **设置与模型安装**：只允许目标机本地所有者访问，集中管理：
   - LLM 供应商、模型和 API Key；
   - 飞书应用、允许用户和群聊策略；
   - 记忆、资产、备份与迁移；
   - 目标机预检、模型下载、许可证、安装进度、日志、重试和验收；
   - 高级审批、恢复、诊断和版本信息。
4. **外部用户**：通过飞书使用，不进入本机所有者设置中心，也不能安装模型或查看其他人的
   会话与资产。

## 4. 模型安装器不是聊天生成 Shell

模型安装属于高风险本机写操作，不能让 LLM 临时生成命令。安装器采用声明式清单和固定动作：

```text
主机预检
  → 兼容性判定
  → 展示版本、下载量、磁盘、许可证和风险
  → 本机所有者明确确认
  → 运行仓库内白名单动作
  → 持久化进度与脱敏日志
  → 离线加载 / smoke test / 真实 Provider 质量门禁
  → 只有验收通过才进入 ready
```

统一状态机为：

```text
not_installed
  → checking
  → license_required
  → ready_to_install
  → downloading
  → installing
  → validating
  → ready

任意阶段 → failed / degraded / incompatible
ready → update_available
```

当前实现将安装任务写入 `backend/data/capability_setup.sqlite3`。页面刷新不会丢失任务；若
Web Agent 在安装中重启，未完成任务会被标记为 `service_restarted`，由所有者检查后重试，
不会未经确认自动执行。同一时间只允许一个模型安装任务，避免多个 pip/模型下载同时修改
虚拟环境和共享缓存。日志会限制条数并脱敏 Key、Token、Secret 和用户主目录。

### 4.1 能力门禁

| 能力 | macOS Apple Silicon | Windows NVIDIA | Linux x86 NVIDIA | DGX Spark ARM64 |
| --- | --- | --- | --- | --- |
| Agent / SQLite / 飞书 | 支持 | 支持 | 支持 | 需完成基础运行时验收 |
| 空间照片模型 | MPS/CPU | CUDA/CPU | CUDA/CPU | CUDA 路径待实机验收 |
| 语义记忆模型 | 支持 | 支持 | 支持 | 需验证 ARM64 wheel |
| SDXL + IP-Adapter | MPS 工程预览 | Docker/WSL2 路径 | 需固定容器 | 当前阻断，待 Spark 容器 |
| Flux-GS | 不支持真实训练 | WSL 可调研 | 手动部署基线 | 当前阻断，待 ARM64/CUDA 扩展 |

“阻断”表示安装器知道为什么不能安全执行，并给出下一验证门；不是把 Preview 或 Fake
Provider 当成真实功能。

## 5. DGX Spark 专用架构

DGX Spark 使用 20 核 ARM64 CPU、GB10 GPU 和 128 GB 统一内存，并预装 DGX OS、Docker
和 NVIDIA Container Runtime；因此它适合成为**完整节点本身**，而不是必须由 Mac 控制的
从属 GPU。[NVIDIA 系统概览](https://docs.nvidia.com/dgx/dgx-spark/system-overview.html)

但它不是普通 x86_64 CUDA 工作站。当前 NVIDIA 页面把 GB10 列为 Compute Capability
12.1；Founders Edition 当前发布说明列出的栈为 DGX OS 7.5.0、Driver 580.159.03、CUDA
Toolkit 13.0.2，合作厂商设备更新时间可能不同。[CUDA GPU 列表](https://developer.nvidia.com/cuda/gpus)、
[DGX Spark 发布说明](https://docs.nvidia.com/dgx/dgx-spark/release-notes.html)

因此迁移到 Spark 时采用以下约束：

1. Web、FastAPI、飞书、SQLite、资产和 Viewer 全部在 Spark 上部署；Mac 可关机。
2. 控制面可在宿主机虚拟环境或 CPU 容器运行；GPU Provider 使用固定的 ARM64 NGC/CUDA
   容器，并只绑定 `127.0.0.1`。
3. 不直接复用写死 `cu126`、x86 wheel 或 `TORCH_CUDA_ARCH_LIST=12.0` 的脚本。
4. 所有自定义 CUDA 扩展在 Spark/ARM64 环境重新编译，并以 GB10 的 SM 12.1 进行真实
   加载和最小推理/训练测试。
5. `tmc3` 等原生二进制必须构建 ARM64 版本；不能把 x86 可执行文件复制过去。
6. 统一内存不能按传统“独立显存”解释。调度器要同时观察系统内存压力、GPU 工作集、
   并发和 OOM；默认重型模型单并发。
7. 优先使用 NVIDIA 已验证容器。DGX Spark 已预装并配置 NVIDIA Container Runtime，
   可先用官方 CUDA 容器验证 GPU 透传。
   [NVIDIA Container Runtime 指南](https://docs.nvidia.com/dgx/dgx-spark/nvidia-container-runtime-for-docker.html)

### 5.1 Spark 开放一键安装前的验收门

- `uname -m`、`nvidia-smi`、Docker GPU smoke test 通过；
- Python/Node/SQLite 与飞书 SDK 在 ARM64 上通过仓库回归；
- 模型依赖存在 ARM64 wheel，或已固定可重复构建的源码流程；
- SDXL 完成固定图片的质量、冷/热启动、P50/P95、统一内存峰值和失败率测试；
- Flux-GS 完成固定 COLMAP 数据集的训练、压缩、发布、重启恢复和 Viewer 测试；
- CUDA 扩展和第三方模型许可证逐项确认；
- 生成锁文件、镜像摘要、SBOM 和回滚方案。

完成这些条件前，设置中心显示“需要适配”，不能显示“可安装”或“已支持”。

## 6. 完整迁移流程

### 6.1 旧设备冻结与导出

1. 通知用户短暂停止新任务，等待运行中的空间照片/风格化任务结束。
2. 使用统一部署器导出运行数据、资产和配置清单；需要迁移本机模型时才显式包含
   `.capabilities`，但跨 CPU 架构通常应在新机重新安装模型。
3. 记录 Git commit、数据库 schema、模型 lock、文件数量和 SHA-256。
4. 不把 LLM API Key、飞书 App Secret 或系统钥匙串明文放入备份。

```bash
.venv/bin/python scripts/deploy.py backup --output personal-assistant.pdabundle
```

### 6.2 目标设备部署

```bash
git clone https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent.git
cd Personal-digital-assistant---A-Web-Agent
git switch main
./scripts/setup.sh --profile core --photo-style skip
./scripts/start.sh
```

先用 core 档位启动设置中心，再由 `http://localhost:3000/` 的“设置”完成设备检测和模型
安装。这避免基础部署失败时把 10 GiB 级模型下载混入排障。

### 6.3 恢复数据与重新录入 Secret

```bash
.venv/bin/python scripts/deploy.py restore personal-assistant.pdabundle
```

恢复后在目标机重新录入 LLM API Key 和飞书 App Secret。macOS Keychain、Windows 凭据和
Linux Secret Store 不保证跨系统可迁移；重新录入比复制主密钥更安全。

### 6.4 重新安装模型并验收

1. 打开“设置 → 本机能力”；
2. 查看目标机档位、架构、GPU、内存、磁盘和 Docker；
3. 逐项查看计划，接受对应许可证并安装；
4. 模型安装后重启 Web Agent；
5. 启用新节点飞书连接前先停止旧节点长连接，保证同一 App 只有一个活跃消费者；
6. 完成文本 Tool Calling、记忆、空间照片、真实图片风格化和飞书手机端回归；
7. 如果需要回滚，必须先停止新节点，再重新启用旧节点，避免重复消费或回复。

## 7. 上线前仍需完成

当前设置中心解决的是可理解、可审计的本机部署闭环，不等于已经完成所有硬件认证：

- DGX Spark 的基础运行时、SDXL 和 Flux-GS 尚缺实机结果；
- 图片风格化的 macOS MPS 路径尚缺固定图片质量与内存门禁；
- 安装下载尚未实现分块校验、镜像源选择和跨重启续传；
- 缺少模型卸载、版本升级、签名制品、SBOM 和自动回滚；
- 飞书出站消息仍需持久化 Outbox；
- 手机空间 Viewer 上公网仍需固定域名、认证、撤销、限流和访问审计。

这些缺口必须继续留在看板中，不能用“页面已有安装按钮”替代真实设备与生产验收。
