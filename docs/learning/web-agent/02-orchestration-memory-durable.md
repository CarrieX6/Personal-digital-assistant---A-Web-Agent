# Agent 调度、Memory 与 Durable Execution

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：已核对，项目实现待实验  
关联任务：`LEARN-AGENT-001`、`JOB-001`、`MEMORY-001`

## 1. “调度”至少有五层

| 层 | 决定什么 | 典型机制 |
| --- | --- | --- |
| Request | 谁先获得服务 | 队列、优先级、配额、限流 |
| Workflow | 步骤如何运行 | DAG、状态机、重试、补偿 |
| Agent | 哪个 Agent 接手 | Router、Supervisor、Handoff |
| Model/Tool | 用哪个模型或能力 | 规则、能力标签、健康与权限 |
| Resource | 在哪台设备执行 | CPU/GPU/NPU、显存、温度、电量 |

把这五层都塞给一个 LLM 判断，会让故障无法定位。优先使用确定性调度；只有语义路由
才需要模型参与。

## 2. 任务与资源调度

每个 Job 应声明：

- capability/version；
- 输入资产和输出类型；
- 优先级、deadline、最大尝试；
- CPU/GPU、内存/显存、磁盘和网络预算；
- 可取消、可恢复、是否需要独占 GPU；
- 数据驻留和模型来源约束。

Scheduler 先过滤不满足硬约束的 Worker，再按队列、公平性、缓存命中和负载选择。
温度、电量与功耗属于资源策略，不应让模型自由解释。

## 3. Short-term State 与 Long-term Memory

- State：当前 Run 为完成任务必须保存的事实和控制信息；
- Checkpoint：State 在某个安全边界的持久化快照；
- Conversation History：消息记录；
- Long-term Memory：跨会话保留的偏好、事件或知识；
- RAG Index：可检索资料，不等于用户记忆。

写入长期记忆的门槛应高于把内容留在当前对话。每条记忆需要 owner、来源、时间、
置信度、用途、敏感级别、过期/删除策略。模型生成摘要只是派生数据，原始来源仍应可
追溯。

## 4. Context Engineering

每轮上下文建议按顺序构建：

1. 不变安全策略；
2. 当前用户和 Channel 的授权上下文；
3. 当前 Job/Run 状态；
4. 与意图匹配的少量工具；
5. 当前对话窗口；
6. 检索到且标注来源的记忆/知识；
7. 可用 Token 预算和压缩策略。

LangChain v1 的 Middleware 支持动态 Prompt、摘要、选择性工具访问、状态和
Human-in-the-loop；这说明“上下文工程”已成为框架的一等控制点
([LangChain v1](https://docs.langchain.com/oss/python/releases/langchain-v1))。

## 5. 什么是 Durable Execution

Durable Execution 的目标是在进程崩溃、网络失败或等待人工时保留进度并继续。它不
意味着所有副作用天然 exactly-once。

LangGraph 的 Interrupt 会通过持久层保存图状态，等待外部输入后恢复；官方特别提醒，
恢复时节点会从头执行，因此 Interrupt 之前的副作用必须幂等
([LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts))。

Temporal 通过 Event History 记录 Workflow 进展并在失败后 replay；Workflow 代码需
满足确定性约束，外部副作用放在 Activity 中
([Temporal](https://docs.temporal.io/temporal))。

### Checkpoint 不等于完整任务系统

Checkpoint 解决图状态恢复；产品仍需要：

- Job 列表和用户可见状态；
- 队列、Worker lease 与优先级；
- 资源调度；
- 大文件/资产存储；
- 取消和清理；
- 通知 Outbox；
- 版本迁移；
- 审计和权限。

## 6. LangGraph 与工作流引擎的边界

| 需求 | 轻量 DB + Worker | LangGraph | Temporal 等工作流引擎 |
| --- | --- | --- | --- |
| 单机 MVP | 最简单 | 可用 | 通常过重 |
| LLM 图与 HITL | 需自研 | 强项 | 需自行封装 Agent |
| 跨服务长任务 | 需大量工程 | 取决于部署 | 强项 |
| 调度与运营能力 | 自研 | 部分 | 较完整 |
| 学习成本 | 低到中 | 中 | 高 |

Pydantic AI 当前文档也提供 Temporal、DBOS、Prefect、Restate、Airflow 等 Durable
Execution 集成，说明 Agent 框架和可靠工作流引擎是可以组合的两层
([Pydantic AI Durable Execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/))。

## 7. Human-in-the-loop

HITL 不是弹窗，而是可恢复协议：

1. 系统生成审批对象：动作、参数、风险、预览、过期时间；
2. Checkpoint 保存到 `waiting_approval`；
3. 用户从飞书卡片或 Web UI 同意、拒绝或修改；
4. 服务验证审批人、Run ID、版本和签名；
5. 恢复前重新授权；
6. 参数改变则创建新审批；
7. 审批和执行分别写审计。

## 8. 失败与升级

- 节点必须标注 pure/side-effecting；
- Side effect 使用 idempotency key；
- Graph/Workflow 保存 definition version；
- 新版本不能盲目恢复旧 Checkpoint；
- 大对象只存 Asset ID，不写入状态 JSON；
- 超过恢复窗口的 Job 进入人工处理；
- 对无法安全恢复的任务明确失败并允许用户重开。

## 9. 项目分阶段路线

1. SQLite Job 表 + 有界 Worker + Outbox；
2. Agent State 与生成 Job 分离；
3. 为审批和模型调用加 Checkpoint；
4. 用断电/杀进程实验验证恢复；
5. 只有跨设备、多 Worker 或复杂长流程出现明确需求，再评估 LangGraph/Temporal。

## 10. 来源

- [LangChain v1 Release Notes](https://docs.langchain.com/oss/python/releases/langchain-v1)
- [LangGraph v1](https://docs.langchain.com/oss/python/releases/langgraph-v1)
- [LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)
- [Temporal Durable Execution](https://docs.temporal.io/temporal)
- [Temporal Workflow Execution](https://docs.temporal.io/workflow-execution)
- [Pydantic AI Durable Execution](https://pydantic.dev/docs/ai/capabilities/durable_execution/overview/)

返回[Web Agent 知识地图](README.md)。

