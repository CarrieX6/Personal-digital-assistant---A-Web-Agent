# 调研与实现中心

作者：**Zhuofan Xie**
更新日期：2026-09-10

本文件是项目技术调研与实现进度的统一入口。它回答四个问题：

1. 当前已经具备什么；
2. 哪些问题还没有调研；
3. 调研结论是否经过实验和技术决策；
4. 结论是否已经进入代码并完成验证。

具体调研内容、实验数据和代码实现分别存放在对应文档、实验记录、ADR、Issue 与
Pull Request 中，本看板只维护状态、负责人、依赖和链接，避免复制多份结论。

## 1. 使用方法

1. 从“当前优先队列”选择任务，在负责人列登记姓名。
2. 使用任务 ID 创建调研文档和 GitHub Issue，例如 `AGENT-001`。
3. 按[调研与决策流程](research/process.md)记录资料、候选方案和调研日志。
4. 需要实测时，从[实验模板](experiments/experiment-template.md)创建实验记录。
5. 需要确定项目级方案时，从 [ADR 模板](decisions/ADR-template.md)创建决策记录。
6. 进入实现后关联分支、PR、测试与文档，完成后同时更新两个状态。

### 贡献归属规则

- “负责人”记录该任务的主要设计与实现者，不以最终合并人、提交代操作人或文档整理人
  替代真实实现者；
- 合并、冲突处理、回归验证和部署文档整理应作为协作工作单独描述，不自动获得功能
  实现归属；
- 图片风格化与分层长短期记忆的主要实现人为 **Xianggang Ma**。阶段汇报中，这两项
  作为团队已有能力说明，不归入 Zhuofan Xie 的个人工作成果。

### Git Skill 自动同步

使用 `$team-git-workflow` 执行项目任务时，Skill 会把看板检查纳入 Git 流程：

- 开始任务时识别任务 ID，并读取负责人、优先级和当前状态；
- 提交前同步已经发生的负责人、调研、实验、ADR 和实现状态变化；
- PR 创建后，把实现状态更新为 `👀 评审中`，并在需要时追加真实 PR 链接；
- 只有 GitHub 已确认合并且验收通过后，才更新为 `✅ 已完成`；
- 合并后按照“删除远程分支 → 更新本地 main → 删除本地分支”的顺序清理。

这是由 Codex Skill 驱动的自动同步，不是常驻后台任务。团队成员手工在 GitHub
合并后，需要再次调用 `$team-git-workflow` 执行清理和最终状态同步。完全无人触发
的自动更新需要后续增加 GitHub Action。

### 调研状态

| 状态 | 含义 |
| --- | --- |
| `⬜ 未开始` | 只有问题或想法，尚未收集资料 |
| `🟡 调研中` | 正在收集资料、梳理候选方案 |
| `🧪 待实验` | 文献调研完成，但必须通过原型或基准测试验证 |
| `🟠 初步结论` | 已有推荐方向，证据或实验仍不完整 |
| `✅ 已决策` | 结论、证据和 ADR 完整，可以指导实现 |
| `🔁 需更新` | 上游模型、平台、价格、许可或项目约束已经变化 |
| `⛔ 暂缓` | 当前阶段不投入，保留原因和恢复条件 |

### 实现状态

| 状态 | 含义 |
| --- | --- |
| `⬜ 未开始` | 尚无对应代码 |
| `🧱 局部实现` | 已有 Demo 或只覆盖部分链路 |
| `🟡 开发中` | 已领取并存在开发分支 |
| `👀 评审中` | 已创建 PR，等待验证或评审 |
| `✅ 已完成` | 已合入 `main` 且通过验收 |
| `🔁 待重构` | 当前代码可用，但与目标架构或质量要求不一致 |

## 2. 当前优先队列

### P0：先打通个人数字助手主链路

- [ ] `LEARN-001`：建立个人数字助手分层知识库与学习导航。
- [ ] `AGENT-001`：确定 Agent Orchestrator 架构和工具执行循环。
- [ ] `REPORT-001`：完成个人数字助手阶段审计、贡献归属和公司汇报材料。
- [ ] `CHANNEL-001`：完成飞书长连接文本消息闭环 PoC。
- [ ] `SEC-001`：建立外部聊天控制的身份、权限与威胁模型。
- [ ] `MEMORY-001`：确定记忆分层、写入策略和 SQLite 首版 Schema。
- [ ] `MESSAGE-001`：统一 Web、飞书和未来微信的 Conversation、Message、Attachment 数据模型。
- [ ] `AUTH-001`：为 Web API、资产和配置接口增加身份认证与 owner 授权；已完成
  飞书身份 → 独立工作区 → 本机节点绑定及 Root 启停管理，待用户门户 OAuth、资产接口
  workspace 授权和跨电脑中心路由。
- [ ] `JOB-001`：把空间照片专用任务队列抽象为通用可恢复任务系统。
- [ ] `CAP-001`：确定 Capability Manifest、注册和调用边界。
- [ ] `LLM-001`：完成云端 LLM Tool Calling 与视觉能力横向评测。

### P1：让现有能力可安装、可调用、可返回

- [ ] `CHANNEL-002`：图片、视频、文件、卡片和任务进度回传。
- [ ] `CHANNEL-004`：Web 控制台同步展示外部渠道消息记录。
- [ ] `PREVIEW-001`：空间照片与 3D 资产的安全移动端预览。
- [ ] `CAP-002`：模型下载、依赖隔离、许可和设备兼容性。
- [ ] `SPATIAL-001`：深度、分割和背景补全模型横向评测。
- [ ] `SPATIAL-002`：LDI、Mesh、MPI 与单图 3DGS 技术路线评测。
- [ ] `PERF-001`：建立统一的延迟、内存、显存、功耗和温度测试方法。
- [ ] `EVAL-001`：建立 Agent 与生成能力的验收数据集和质量指标。

### P2：扩展个人内容生成能力

- [ ] `VTON-001`：2D 虚拟试衣模型选型与最小原型。
- [ ] `VTON-002`：个人数字人、多视角和 2D 到 3D 路线。
- [ ] `PET-001`：宠物单图多视角与 3D 重建模型选型。
- [ ] `PET-002`：宠物骨骼、动作库和物种适配。
- [ ] `CHANNEL-003`：企业微信、公众号和小程序接入评测。
- [ ] `STYLE-001`：图片个性化 Capability、Provider 与本地模型验证。

## 3. 已有文档登记

| ID | 文档 | 类型 | 调研状态 | 实现状态 | 已有结论 | 主要缺口 |
| --- | --- | --- | --- | --- | --- | --- |
| `ARCH-000` | [个人数字助手系统架构](architecture/system-architecture.md) | 架构初稿 | `🟠 初步结论` | `🧱 局部实现` | 本地优先、聊天入口、Capability、异步任务、结果降级 | Agent、记忆、队列和功能库尚未完成选型与接口验证 |
| `CHANNEL-000` | [外部聊天控制与结果回传](research/external-chat-control.md) | 平台调研 | `🟠 初步结论` | `🧱 局部实现` | 首期选择飞书企业自建应用和长连接；真实应用已完成连接与文本事件验证 | 缺少断线恢复、媒体权限和多类型回传实测 |
| `SPATIAL-000` | [Apple 锁屏与空间场景路线](apple-spatial-scene-research.md) | 产品与技术调研 | `🧪 待实验` | `🧱 局部实现` | 目标应参考 iOS/visionOS 空间场景，不是 macOS 航拍锁屏 | SHARP/3DGS 尚未实测，商业许可需要持续核对 |
| `SPATIAL-BASELINE` | [空间照片端侧部署与资产格式](spatial-scene-device-deployment.md) | 实现说明 | `🟠 初步结论` | `🧱 局部实现` | 当前使用 Depth Anything V2 Small、双层 LDI 与 Three.js | 缺少多模型基准、移动端实测和高质量路线验证 |
| `LEARN-000` | [个人数字助手知识库](learning/README.md) | 知识库 | `🟠 初步结论` | `🟡 开发中` | 路线 A–D 已形成分层正文、从零实战路线、证据审查、官方/论文来源和术语导航 | 路线 E 按计划仅保留大纲；微信/HarmonyOS 深度调研、平台 PoC、项目基准和 ADR 尚未完成 |
| `LEARN-ROADMAP-001` | [从零到可运行个人数字助手](learning/implementation-roadmap.md) | 学习与实现路线 | `🟠 初步结论` | `🟡 开发中` | 已按当前代码基线拆成环境、Tool、Agent、记忆、Job、飞书、预览、安全、功能库和评测阶段 | 各阶段代码、实验和验收仍需按任务推进 |
| `LEARN-RESEARCH-001` | [调研证据与文档维护方法](learning/research-quality.md) | 调研治理 | `🟠 初步结论` | `🟡 开发中` | 已定义证据等级、充分性标准、覆盖审查、已知缺口和更新触发器 | 动态主题需持续核验，证据不足项需补真实平台/设备实验 |
| `GOV-001` | [调研与实现中心](project-board.md) | 协作治理 | `✅ 已决策` | `✅ 已完成` | [PR #3](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/3) 已合入 `main`；统一看板、模板和 Git Skill 条件式同步已生效 | 完全无人触发的同步仍需后续 GitHub Action |

## 4. 架构与 Agent

| ID | 优先级 | 调研问题 | 调研状态 | 实现状态 | 负责人 | 交付与验收 |
| --- | --- | --- | --- | --- | --- | --- |
| `AGENT-001` | P0 | 自研循环、ReAct、Planner-Executor、Graph Workflow 如何选择 | `✅ 已决策` | `🧱 局部实现` | Zhuofan Xie | [PR #12](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/12) 整合原子 Run 持久化、八节点崩溃恢复、Checkpoint 身份校验、人工处置状态和写工具幂等保护，并通过 186 项后端回归；待强制超时、审批过期、Outbox 和费用预算 |
| `AGENT-002` | P1 | 单 Agent、多 Agent 和确定性工作流的使用边界 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 场景边界、通信成本、调试与评测方案 |
| `JOB-001` | P0 | 通用任务状态机、取消、重试、恢复与通知如何设计 | `🟠 初步结论` | `🧱 局部实现` | Zhuofan Xie | [PR #8](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/8) 已合并空间照片失败任务复用原始图片的一键重试、原子抢占防重复执行、Web 恢复入口和 API 测试；待抽象为通用 Job、取消、进程级恢复、退避和通知策略 |
| `EVAL-001` | P1 | 如何评价规划正确率、工具调用成功率和任务完成率 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 基准任务集、指标、回归测试入口 |

## 5. 外部控制与结果回传

| ID | 优先级 | 调研问题 | 调研状态 | 实现状态 | 负责人 | 交付与验收 |
| --- | --- | --- | --- | --- | --- | --- |
| `CHANNEL-001` | P0 | 飞书长连接、权限、白名单、幂等和文本回复 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | [PR #6](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/6) 已合并配置 UI、安全凭证、SQLite 去重、Open ID 白名单、keepalive、自动重连状态、出站重试和任务回收；待真实断网/休眠/恢复实验与指标 |
| `CHANNEL-002` | P1 | 图片、文件、视频、卡片、进度和失败如何回传 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | [PR #6](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/6)、[PR #8](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/8)、[PR #11](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/11) 与 [PR #12](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/12) 已覆盖基础媒体、预览关联、空间/风格任务重试、owner/chat 隔离、Viewer、持久化风格草稿、多选相册、富文本图文、JPG/PNG/WebP 文件输入与描述持续补充；待真实手机飞书回归、通用 Presenter、Outbox 和视频降级 |
| `CHANNEL-003` | P2 | 企业微信、公众号、小程序如何接入 | `🟠 初步结论` | `⬜ 未开始` | 待领取 | 官方路径对比、主体要求、成本与限制 |
| `CHANNEL-004` | P1 | Web 控制台如何同步显示手机端收发消息 | `✅ 已决策` | `🟡 开发中` | Zhuofan Xie | 2026-07-29 已将主页重构为图文对话工作台；[PR #8](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/8) 已合并飞书图片预览、卡片语义化展示和按 `chat_id` 删除只读镜像；待真机验收、群聊 sender 边界、隐私保留策略和统一 Message 迁移 |
| `PREVIEW-001` | P1 | 手机如何安全预览空间照片、GLB、PLY 和 3DGS | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 已实现独立 `8766` 只读 Viewer、HMAC 链接、文件白名单、触控/陀螺仪、手机尺寸适配、TryCloudflare 自动重连与飞书“刷新预览链接”；开发期 Token 最长 7 天。上线前仍需固定域名 Named Tunnel、可撤销分享记录、访问审计、弱网与 iOS/Android 真机测试，GLB/3DGS 未接入 |
| `REMOTE-001` | P1 | 局域网、隧道、中继和远程唤醒如何选择 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | [PR #6](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/6) 已合并显式 opt-in 的 TryCloudflare HTTPS 测试隧道，仅转发签名 Viewer 并动态回传公网地址；待用户确认私人媒体外发后真机验证，固定域名仍需认证、撤销、限流、审计与中继 |
| `OUTBOX-001` | P1 | 渠道回复如何持久化、重试、去重和进入失败队列 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | Outbox Schema、退避策略、死信与断网恢复实验 |

## 6. 记忆、能力库与数据

| ID | 优先级 | 调研问题 | 调研状态 | 实现状态 | 负责人 | 交付与验收 |
| --- | --- | --- | --- | --- | --- | --- |
| `MEMORY-001` | P0 | 会话、偏好、任务和资产记忆如何分层 | `✅ 已决策` | `✅ 已完成` | Xianggang Ma | Xianggang Ma 实现类型、范围、时效、否定与取代、证据链、字段加密、project 贯通、显式跨渠道身份映射、结构化滚动摘要和记忆管理 API/UI；经 [PR #12](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/12) 合入，[2026-08-28 硬验收](acceptance/context-memory-hard-acceptance-2026-08-27.md) 9/9 通过，2026-08-31 仓库级回归 186 passed |
| `MESSAGE-001` | P0 | Web、飞书与未来微信如何共享统一消息模型 | `🟠 初步结论` | `🧱 局部实现` | Zhuofan Xie | Web Conversation/Message 已以 SQLite 为唯一数据源；[PR #8](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/8) 已合并飞书 `channel_events` 的 `message_id` 与受控 `media_url` 增量；待正式增加 Attachment、ChannelIdentity、Reply/引用关系并迁入统一消息表 |
| `MEMORY-002` | P1 | FTS、向量检索、摘要和上下文压缩如何组合 | `✅ 已决策` | `✅ 已完成` | Xianggang Ma | Xianggang Ma 实现结构化/词法/本地向量混合召回、证据加权、真实模型结构化滚动摘要、完整请求 Token 硬门禁和 old/new shadow 自动切换；经 [PR #12](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/12) 合入，[硬验收](acceptance/context-memory-hard-acceptance-2026-08-27.md) 覆盖 500/1000/2000 轮与 128-owner 隔离 |
| `CAP-001` | P0 | Capability Manifest、权限和调用接口如何定义 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 工具库已接入图片个性化，运行时可查询 Manifest；新增 [Capability 接入指南](guides/capability-integration.md)；待将空间照片等旧工具补齐 Manifest、角色权限和通用安装器 |
| `CAP-002` | P1 | 模型安装、升级、卸载、依赖隔离和哈希校验 | `🟠 初步结论` | `🧱 局部实现` | Xianggang Ma | 围绕 Xianggang Ma 实现的图片风格化能力，已增加跨平台管理器、固定源码/API 归档回退、隔离环境、Fake 链路部署、自动启动、Windows NVIDIA 许可门禁、macOS MPS 工程准备、安全远程 Provider 配置与[迁移文档](guides/photo-style-deployment.md)；待 MPS/第二台 Windows 实机验收、可恢复卸载、签名制品、SBOM 与通用 Capability Installer |
| `DATA-001` | P1 | 用户、任务、资产和派生关系的数据模型 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 2026-08-26 新增外部身份、个人工作区、设备节点和审计表，沿用既有 owner key 保持数据兼容；见[身份、工作区与节点绑定](architecture/identity-workspace-device-binding.md)。待正式迁移工具、生命周期、备份恢复和多设备路由 |

## 7. LLM 与模型路由

| ID | 优先级 | 调研问题 | 调研状态 | 实现状态 | 负责人 | 交付与验收 |
| --- | --- | --- | --- | --- | --- | --- |
| `LLM-001` | P0 | DeepSeek、Qwen、GLM、OpenAI 等模型的 Tool Calling 和视觉能力 | `🟡 调研中` | `🟡 开发中` | Zhuofan Xie | 已实现普通问答与 Tool Calling 双验证、原子启用、基础问答降级和可观察运行状态；当前分支增加兼容 `image_url` 的视觉问答、连续看图追问、生成工具意图隔离和瞬时图片上下文，待增加视觉连接测试并按真实供应商测量成功率、延迟、价格、上下文和隐私 |
| `LLM-002` | P1 | 云端 LLM、本地小模型和规则执行如何自动路由 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 路由规则、降级策略、离线模式和成本实验 |
| `LLM-003` | P1 | Prompt、工具 Schema 和上下文如何版本化与回归测试 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 版本格式、评测工具、失败样本库 |

## 8. 空间照片与 3D

| ID | 优先级 | 调研问题 | 调研状态 | 实现状态 | 负责人 | 交付与验收 |
| --- | --- | --- | --- | --- | --- | --- |
| `SPATIAL-001` | P1 | 深度、主体分割和背景补全模型如何选择 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 已接入 Apple Vision → BiRefNet → 深度蒙版的跨平台主体分割链路、质量门控和降级告警；待 Windows 真机权重推理、统一图集、延迟、内存、功耗与许可矩阵 |
| `SPATIAL-REMOTE-001` | P1 | 空间照片如何迁移到 Windows/远程 GPU | `🟠 初步结论` | `⬜ 未开始` | 待领取 | Windows 本机 CUDA 设备选择已具备；远程尚缺 `SpatialSceneProvider`、异步 Job、资产结果契约、鉴权、幂等、哈希和回传对账，不得复用未鉴权脚本执行 |
| `SPATIAL-002` | P1 | LDI、MPI、Mesh、单图 3DGS 的质量与成本边界 | `🧪 待实验` | `🧱 局部实现` | 待领取 | 多路线原型、伪影分析、ADR |
| `SPATIAL-003` | P1 | Web、手机与桌面 Viewer 如何分级渲染 | `🟠 初步结论` | `🧱 局部实现` | 待领取 | 帧率、内存、发热与降级策略实测 |
| `SPATIAL-004` | P2 | 场景适用性和生成质量如何自动判断 | `🟠 初步结论` | `🧱 局部实现` | Zhuofan Xie | 已记录蒙版面积、连通性、质量分和降级告警，并按质量降低推荐视差；待失败样本集和人工标注评测 |
| `SPATIAL-005` | P2 | Flux-GS 多视角 3D 场景如何作为独立能力接入 Agent 与飞书 | `🟠 初步结论` | `🧱 局部实现` | Zuheng Zhao | [PR #17](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/17) 完成适配，[PR #18](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/18) 修正为单一飞书菜单并明确 Flux-GS 只覆盖多视角训练后半段。待单图多视角/相机前置链路、Linux NVIDIA 实机训练、跨节点上传、完成通知与许可确认 |
| `STYLE-001` | P2 | 图片个性化如何在 CPU 预览、SDXL 本机与远端 Provider 间切换 | `🟠 初步结论` | `👀 评审中` | Xianggang Ma | Xianggang Ma 实现图片风格化主链路、风格预设与质量增强；Zhuofan Xie 负责本地部署工程化和 M4 MPS 实机兼容。2026-09-15 [PR #20](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/20) 已合并显式风格预设和质量增强，[PR #22](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/22) 修复 IP-Adapter 的 MPS 兼容；[PR #23](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/23) 增加 Web 即时提交锁、活动任务反馈、`Idempotency-Key` 重放、服务端同键并发互斥和 payload fingerprint 冲突校验。待完成本 PR 评审、M4 真实用户图质量/性能/功耗验收、Windows GPU 主观质量验收、真实手机联调，以及多 API 进程下的数据库幂等表 |

## 9. 虚拟试衣与数字人

| ID | 优先级 | 调研问题 | 调研状态 | 实现状态 | 负责人 | 交付与验收 |
| --- | --- | --- | --- | --- | --- | --- |
| `VTON-001` | P2 | 2D 虚拟试衣模型如何选择 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 候选模型、人体与衣物一致性、显存、速度和许可评测 |
| `VTON-002` | P2 | 个人数字人、多姿态和身份一致性如何实现 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 输入规范、身份保持实验、隐私边界 |
| `VTON-003` | P2 | 2D 试衣结果如何扩展到多视角或 3D | `⬜ 未开始` | `⬜ 未开始` | 待领取 | NeRF、3DGS、Mesh 路线与数据需求对比 |

## 10. 虚拟宠物

| ID | 优先级 | 调研问题 | 调研状态 | 实现状态 | 负责人 | 交付与验收 |
| --- | --- | --- | --- | --- | --- | --- |
| `PET-001` | P2 | 单张宠物照片如何生成多视角或 3D 资产 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 毛发、外观一致性、拓扑与性能评测 |
| `PET-002` | P2 | 猫、狗、仓鼠等如何绑定骨骼和动作 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 动作库、自动绑定、物种映射和失败回退 |
| `PET-003` | P2 | 桌面宠物如何低功耗常驻运行 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 窗口方案、事件驱动渲染、CPU/GPU/功耗实测 |

## 11. 安全、性能与工程质量

| ID | 优先级 | 调研问题 | 调研状态 | 实现状态 | 负责人 | 交付与验收 |
| --- | --- | --- | --- | --- | --- | --- |
| `SEC-001` | P0 | 外部消息控制本地电脑的威胁模型与权限层 | `🟠 初步结论` | `🧱 局部实现` | Zhuofan Xie | 已有 Open ID 白名单、飞书身份绑定状态硬检查、Tool 风险等级、调用前 Schema、Interrupt 审批、owner 隔离和非幂等重放保护；Root 绑定 API 目前仅限本机来源。待正式威胁模型、用户门户与 Root 认证、资产 workspace 授权、审批过期和攻击测试 |
| `AUTH-001` | P0 | Web API、配置和资产如何认证并执行 owner 授权 | `🟠 初步结论` | `🧱 局部实现` | Zhuofan Xie | 已实现飞书外部身份、独立工作区、本机设备绑定、状态硬拦截与本机 Root 管理 UI/API；当前 Web 仍是全局 Root 视图。待飞书 OAuth 用户门户、Session、资源级授权、限流和跨电脑 Control Plane |
| `SEC-002` | P1 | Prompt Injection、恶意附件和能力安装如何防护 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 攻击样本、隔离策略、审计与应急流程 |
| `PERF-001` | P1 | 如何统一测量延迟、吞吐、内存、显存、功耗和温度 | `🟠 初步结论` | `🧱 局部实现` | Zhuofan Xie | [PR #15](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/15) 已合并[空间照片与飞书链路首轮基线](experiments/perf-001-spatial-feishu-baseline-2026-09-03.md)：20 次生成平均 5.030 s、P95 7.584 s、成功 20/20，23 条飞书入站至首条出站记录 P95 1191.1 ms；待标准 `.venv`、Windows NVIDIA、CPU-only、功耗、温度、并发和大样本复测 |
| `PERF-002` | P1 | 量化、缓存、按需加载和自适应降级如何落地 | `🟠 初步结论` | `🧱 局部实现` | 待领取 | 对照实验、质量损失和设备分级 |
| `QA-001` | P1 | 本地模型、外部平台和异步任务如何稳定测试 | `⬜ 未开始` | `🧱 局部实现` | 待领取 | Fake、录制回放、集成测试和失败注入方案 |
| `OPS-001` | P1 | 本地 Agent 如何开机启动、守护、升级和告警 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | [PR #15](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/15) 已合并统一 `deploy.py`、完整/轻量安装档位、真实模型许可门禁、空间模型固定快照准备、图片风格化 Provider 编排、doctor、备份恢复和完整迁移文档；30 项部署专项测试及前端 Build/SSR 通过。待 Windows/MPS 真实模型实机验收、守护、日志轮转和升级回滚 |

## 12. 知识体系与学习

| ID | 优先级 | 学习任务 | 调研状态 | 实现状态 | 负责人 | 交付与验收 |
| --- | --- | --- | --- | --- | --- | --- |
| `LEARN-001` | P0 | 建立分层知识体系、学习路径和超链接导航 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 总导航、四个分域知识地图、15 篇核心正文、实战路线、证据审查、术语表和写作模板；本地待评审 |
| `LEARN-AGENT-001` | P0 | 补充 Web Agent 基础、架构、调度和框架正文 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 已覆盖 Single/Multi-Agent、调度、Memory、Durable Execution、框架、协议、安全与评测；横评待做 |
| `LEARN-CHANNEL-001` | P0 | 补充外部控制和平台接入正文 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 已覆盖飞书、微信生态、Channel Gateway、媒体回传和可靠性；真实账号 PoC 待做 |
| `LEARN-MOBILE-001` | P1 | 补充 iOS、HarmonyOS 与 Android 部署正文 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 已覆盖系统后台边界、Push、部署拓扑和 Viewer；真机矩阵待测 |
| `LEARN-AI-001` | P1 | 补充 AI 功能、模型选型和端侧部署正文 | `⬜ 未开始` | `⬜ 未开始` | 待领取 | 空间照片、虚拟试衣、虚拟宠物、生成模型、3D、性能和许可 |
| `LEARN-OPS-001` | P1 | 补充可靠性、安全、运维和产品化正文 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 已覆盖可靠性、权限、可观测性、Capability、供应链、数据、性能、测试、运维和 UX；基线待测 |
| `LEARN-ROADMAP-001` | P0 | 建立从零学习到项目实现的连续阶段和验收门槛 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 已映射当前代码、阶段 0–9、交付物和任务 ID；阶段实现待推进 |
| `LEARN-RESEARCH-001` | P0 | 审查资料完整性、证据质量与时效更新机制 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | 已完成本轮覆盖审查，修正 MCP/Android 时效信息，登记微信/HarmonyOS/框架横评缺口 |
| `REPORT-001` | P0 | 如何用可验证结果、用户故事和清晰归属完成阶段汇报 | `🟠 初步结论` | `🟡 开发中` | Zhuofan Xie | [PR #15](https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent/pull/15) 已合并[阶段汇报初稿](reports/personal-digital-assistant-stage-report-2026-08.md)，完成 10 页主线、贡献边界、自动化门禁，并补入空间照片和飞书首轮性能基线；待真实手机 Viewer、Windows GPU、断网恢复、功耗数据和团队归属措辞确认 |

## 13. 每次更新必须填写

领取或推进任务时，至少更新以下信息：

- [ ] 负责人和开始日期；
- [ ] 调研状态与实现状态；
- [ ] 调研文档链接；
- [ ] 候选方案和主要证据；
- [ ] 实验记录及原始结果；
- [ ] ADR 或明确说明为何不需要 ADR；
- [ ] Issue、分支和 Pull Request；
- [ ] 验证结果、未解决问题与下一步。

任务只有同时满足以下条件才可以标记为完成：

- 调研结论可追溯；
- 关键假设经过验证；
- 技术决策及其代价已记录；
- 代码、测试和使用文档已合入；
- 本看板与相关索引已更新。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
