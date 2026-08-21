# 术语表

作者：**Zhuofan Xie**
更新日期：2026-07-28
状态：核心定义已补充

本文件统一跨文档术语。定义以本项目的工程语境为准；框架专有含义以对应官方文档
为准。

## Agent 与工作流

| 术语 | 定义 |
| --- | --- |
| Agent | 由模型驱动、能基于状态选择工具/动作并迭代到停止条件的运行系统 |
| Agent Loop | 模型决策、工具执行、观察结果、继续或停止的控制循环 |
| ReAct | 交错使用 reasoning 与 acting 的方法，来源见 [ReAct 论文](https://arxiv.org/abs/2210.03629) |
| Planner | 把目标分解为结构化步骤的组件 |
| Executor | 执行计划步骤、工具或 Workflow 的组件 |
| Supervisor | 给多个 Agent/Worker 分配任务并聚合结果的协调者 |
| Single Agent | 一个主要模型决策主体配合多个工具/Workflow |
| Multi-Agent | 多个具有独立角色、上下文或权限的 Agent 通过协议协作 |
| Workflow | 明确步骤、控制流、错误与状态的可执行过程 |
| Graph | 以 Node 和 Edge 表示执行与路由的 Workflow |
| State | 当前 Run 继续执行所需的结构化数据 |
| Checkpoint | 在恢复边界持久化的 State 快照 |
| Durable Execution | 进程/网络失败或等待人工后仍能从记录进度恢复的执行机制 |
| Human-in-the-loop | 在审批、补充输入或验证点暂停并由人决定后恢复 |

## 工具与能力

| 术语 | 定义 |
| --- | --- |
| Tool Calling | 模型按工具 Schema 提出名称和参数，由应用验证、授权并执行 |
| Structured Output | 按 JSON Schema/类型返回可验证结构的模型输出 |
| JSON Schema | 描述和验证 JSON 结构的标准化 Schema |
| Skill | 给 Agent 的可复用方法、知识或工作说明，不自动授予执行权限 |
| Tool | Agent 可调用的最小、带输入输出契约的操作 |
| Capability | 面向用户的一项完整功能，可包含 Tool、Model、Workflow、UI 与资产 |
| Capability Registry | 保存可用 Capability 版本、Schema、权限和健康状态的注册表 |
| MCP | Agent/Host 与工具和数据服务互操作的 Model Context Protocol |
| A2A | 独立 Agent 系统之间发现、委派 Task 和交换 Artifact 的协议 |
| AG-UI | Agent 后端与用户界面之间传递 Run、Message、Tool 和 State 事件的协议 |
| Plugin | 可安装、校验、升级和卸载的能力交付包 |

## Context、Memory 与知识

| 术语 | 定义 |
| --- | --- |
| Context | 单次模型调用实际可见的消息、工具、资料和状态 |
| Context Engineering | 在预算、权限和可信度约束下选择、组织和压缩当前 Context |
| Working Memory | 当前 Run 使用的短期状态 |
| Conversation Memory | 会话消息及其摘要 |
| Episodic Memory | 已发生任务/事件的可检索记录 |
| Semantic Memory | 用户偏好、实体关系或知识事实 |
| Procedural Memory | 可复用流程、Skill 或操作方法 |
| Embedding | 把内容映射为向量表示，用于相似检索等任务 |
| RAG | 检索外部资料后把相关上下文提供给生成模型；原始定义见 [NeurIPS 2020](https://papers.neurips.cc/paper/2020/hash/6b493230205f780e1bc26945df7481e5-Abstract.html) |
| Reranker | 对第一阶段召回候选重新评分排序的组件 |
| Fine-tuning | 使用任务数据更新模型参数或适配器，而非运行时检索知识 |

## 调度与可靠性

| 术语 | 定义 |
| --- | --- |
| Request Scheduling | 多用户请求的排队、优先级、配额和公平性 |
| Workflow Scheduling | Workflow 步骤的依赖、并行、重试、暂停和恢复 |
| Agent Scheduling | 选择 Agent、Handoff 或 Supervisor/Worker 的过程 |
| Model Routing | 按能力、成本、延迟、隐私和健康选择模型 |
| Resource Scheduling | 分配 CPU/GPU/NPU、内存、显存、电量和温度预算 |
| Idempotency | 同一业务操作重复提交不会产生额外副作用 |
| Retry | 对判定为暂时性失败的操作再次尝试 |
| Backoff | 重试间隔逐步增大，通常加入随机抖动 |
| Dead Letter Queue | 超过自动恢复范围、等待诊断/人工处理的任务集合 |
| Eventual Consistency | 多个状态副本允许短暂不一致，最终收敛 |
| Side Effect | 改变外部世界的动作，如发消息、写文件、扣费 |
| Compensation | 对已发生、无法事务回滚的副作用执行补救动作 |

## 外部渠道与网络

| 术语 | 定义 |
| --- | --- |
| Webhook | 平台发生事件后向预设 HTTPS 地址发送请求 |
| WebSocket | 初始握手后建立的双向消息通道，见 [RFC 6455](https://www.rfc-editor.org/rfc/rfc6455.html) |
| Long Polling | 请求在服务端等待事件后返回，客户端随后重新请求 |
| Channel Adapter | 负责某聊天平台验签、解析、媒体和发送的适配层 |
| Channel Gateway | 汇聚多个 Adapter、身份、Outbox 和统一消息模型的服务 |
| OAuth | 用户授权客户端访问受保护资源的协议族 |
| Tenant | 平台或系统中的组织/租户隔离边界 |
| NAT | 在内部与外部网络地址之间转换 |
| CGNAT | 运营商级 NAT，通常使终端没有可直接入站访问的公网地址 |
| Reverse Proxy | 代表后端服务接收入站请求并转发 |
| Tunnel | 通过已建立的连接承载另一条逻辑连接 |
| Relay | 两端不能直连时转发流量的中继服务 |
| Deep Link | 打开 App 内指定页面/状态的链接机制 |
| Universal Link | 与网站域名关联并可安全打开 iOS App 的链接 |

## 多模态与空间内容

| 术语 | 定义 |
| --- | --- |
| Asset | 有所有者、类型、hash、生命周期和访问策略的文件/对象 |
| Derived Asset | 由另一个 Asset 和版本化 Pipeline 生成的资产 |
| Depth | 图像像素对应的相对或绝对深度表示 |
| Mask | 表示前景、主体或类别区域的像素级选择 |
| LDI | Layered Depth Image，同一视线可保存多层颜色与深度 |
| MPI | Multiplane Image，把场景表示为一组带透明度的深度平面 |
| Mesh | 顶点、边/面及材质组成的表面几何 |
| NeRF | 用神经网络表示辐射场并进行新视角合成的方法族 |
| 3DGS | 3D Gaussian Splatting，以三维高斯基元表示并渲染场景的方法族 |
| GLB | glTF 的二进制容器格式，常用于传输 Mesh/场景 |
| PLY | 可存储点云或 Mesh 属性的多边形文件格式 |
| Viewer | 在 Web/移动/桌面解析、渲染和交互预览资产的客户端 |

## 评测、性能与安全

| 术语 | 定义 |
| --- | --- |
| Trace | 一次端到端请求的关联执行轨迹 |
| Span | Trace 中一个有开始、结束、属性和状态的操作 |
| Run | 一次 Agent/Workflow 执行实例 |
| OpenTelemetry | 采集和导出 Trace、Metric、Log 的开放可观测性标准生态 |
| LLM-as-Judge | 用模型按量表评价结果；需人工校准并控制 Judge 偏差 |
| Latency | 完成请求/阶段的时间，通常报告 P50/P95 |
| Throughput | 单位时间完成的请求、Token、帧或资产量 |
| Quantization | 用更低位宽表示权重/激活以降低存储和计算，质量需实测 |
| Prompt Injection | 不可信输入诱导模型改变目标、规则或工具行为 |
| SSRF | 服务端被诱导访问非预期 URL/内部网络 |
| Least Privilege | 只授予完成任务所需的最小功能、资源和时长权限 |
| Audit Log | 记录主体、动作、资源、审批和结果的不可抵赖审计线索 |
| SBOM | Software Bill of Materials，软件组件与依赖清单 |
| ML-BOM | 模型、数据集、配置和依赖等 AI/ML 供应链清单 |

返回[知识库总导航](README.md)。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
