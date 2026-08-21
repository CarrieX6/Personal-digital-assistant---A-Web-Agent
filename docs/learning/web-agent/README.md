# Web Agent 知识地图

作者：**Zhuofan Xie**
更新日期：2026-07-28
状态：核心正文已核对，框架横评与 ADR 待完成

本分域覆盖从最小 Tool Calling Loop 到有状态 Graph、Agent 调度和 Multi-Agent 的
完整知识层级。

## 正文导航

1. [Agent Loop、架构模式与 Single/Multi-Agent](01-agent-loop-architectures.md)
2. [Agent 调度、Memory 与 Durable Execution](02-orchestration-memory-durable.md)
3. [Agent 框架现状、优缺点与选型方法](03-frameworks.md)
4. [MCP、A2A、AG-UI、评测与可观测性](04-protocols-evaluation-security.md)

本次补充了原大纲缺少的 Google ADK、Pydantic AI、Microsoft Agent Framework、A2A
和 AG-UI。以下章节继续作为知识检查清单。

实践顺序以[从零到可运行个人数字助手](../implementation-roadmap.md)的阶段 1–4
为准。本页是概念地图，不表示必须先学习所有框架后才能修改 Agent。

## 1. LLM 应用基础

### 1.1 消息与上下文

- Chat Completion 消息模型；
- System Prompt；
- Context Window；
- Token 预算；
- Streaming；
- 多模态消息。

### 1.2 Structured Output

- JSON Mode；
- JSON Schema；
- 类型校验；
- Schema 演进；
- 无效输出恢复；
- 供应商兼容性。

### 1.3 Tool Calling

- 工具选择；
- 参数生成；
- 工具结果回传；
- 多轮工具调用；
- 并行工具调用；
- 错误、超时和取消。

## 2. Agent Loop

### 2.1 最小循环

- Observe、Decide、Act；
- Tool Result；
- Stop Condition；
- 最大步数；
- Token 和时间预算。

### 2.2 ReAct 与反思

- Reasoning + Acting；
- Self-reflection；
- Reviewer；
- 可能的收益；
- 幻觉、循环和成本问题。

### 2.3 确定性工作流边界

- 什么问题不需要 Agent；
- 规则、状态机和 Agent 如何组合；
- 失败时如何回退；
- 何时禁止自由规划。

## 3. Agent 架构模式

### 3.1 Tool Calling Agent

- 适用场景；
- 优缺点；
- 复杂度上限；
- 与当前 `AgentRunner` 的关系。

### 3.2 Plan-and-Execute

- Planner；
- Executor；
- Plan Store；
- Replan；
- 计划过时和错误传播。

### 3.3 Router

- 意图识别；
- Tool、Capability 和 Workflow 路由；
- Model Router；
- 规则与模型混合路由。

### 3.4 Graph / State Machine

- State、Node 和 Edge；
- Conditional Edge；
- Checkpoint；
- Interrupt；
- Human-in-the-loop；
- Durable Execution。

### 3.5 Event-driven Agent

- Domain Event；
- Event Bus；
- 异步任务；
- 结果通知；
- 背压和事件重放。

## 4. Single Agent

### 4.1 Single Agent + Tools

- 简单性；
- 上下文集中；
- 工具数量增长；
- 长任务限制；
- 调试和恢复。

### 4.2 Single Agent + Skills

- 动态 Skill 加载；
- Progressive Disclosure；
- Skill 组合；
- 版本与权限；
- 与 Capability 的区别。

### 4.3 Single Agent + Graph

- 单 Agent 决策；
- 确定性流程控制；
- 适合本项目早期的原因与待验证假设；
- 与纯 Agent Loop 的实验对比。

## 5. Multi-Agent

### 5.1 Supervisor

- 任务分派；
- 子 Agent 生命周期；
- 聚合与验证；
- 递归委派限制。

### 5.2 Planner–Executor–Reviewer

- 职责分离；
- 中间协议；
- 反馈循环；
- Token 和延迟成本。

### 5.3 Sequential 与 Parallel

- 流水线；
- 并行调研；
- 依赖和同步；
- 冲突结果处理。

### 5.4 Debate 与 Blackboard

- 多方案讨论；
- Shared Memory；
- 写入冲突；
- 终止条件；
- 结论仲裁。

### 5.5 Single 与 Multi 的决策边界

- 任务复杂度；
- 可并行性；
- 专业分工；
- 成本；
- 一致性；
- 安全和权限；
- 统一评测方法。

## 6. Agent 调度

### 6.1 请求调度

- 多用户队列；
- 优先级；
- 配额与限流；
- 公平性；
- 背压。

### 6.2 工作流调度

- Task DAG；
- 串行与并行；
- 依赖；
- 重试和补偿；
- 暂停、取消和恢复。

### 6.3 Agent 调度

- Single/Multi-Agent 选择；
- Agent 能力描述；
- Supervisor 路由；
- 并发上限；
- 结果聚合。

### 6.4 模型与工具调度

- 云端与本地；
- 文本与视觉；
- 质量、延迟和成本；
- 健康检查与降级；
- Tool/Capability 权限。

### 6.5 资源调度

- CPU、MPS、CUDA 和 NPU；
- 内存和显存预算；
- GPU 并发；
- 温度和电量；
- 设备能力分级。

## 7. LangChain

### 7.1 核心抽象

- Model；
- Message；
- Prompt；
- Tool；
- Structured Output；
- Runnable / LCEL；
- Retriever；
- Callback 和 Trace。

### 7.2 研究问题

- 哪些组件减少项目重复代码；
- 是否增加调试难度；
- 多供应商兼容性；
- 升级和破坏性变化；
- 是否只使用部分组件；
- 与自研 `AgentRunner` 的边界。

## 8. LangGraph

### 8.1 核心抽象

- State；
- Node；
- Edge；
- Reducer；
- Subgraph；
- Checkpoint；
- Persistence；
- Interrupt；
- Streaming；
- Human-in-the-loop。

### 8.2 研究问题

- 长任务恢复；
- 异步本地模型任务；
- 飞书审批恢复；
- SQLite Checkpoint；
- 多用户隔离；
- Graph 版本迁移；
- Multi-Agent Graph；
- 对简单任务的额外复杂度。

## 9. 框架选型

### 9.1 候选

- 完全自研 Agent Loop；
- 自研 Loop + LangChain 组件；
- LangGraph + 自研 Capability；
- LangChain + LangGraph；
- OpenAI Agents SDK；
- AutoGen；
- Semantic Kernel；
- CrewAI；
- LlamaIndex Workflows。
- Google Agent Development Kit（ADK）；
- Pydantic AI；
- Microsoft Agent Framework（Semantic Kernel / AutoGen 的官方后继）；
- Durable Workflow Engine（Temporal、DBOS、Restate、Prefect）；
- Agent 协议层（MCP、A2A、AG-UI）。

### 9.2 对比维度

- Tool Calling；
- State 和 Checkpoint；
- Human-in-the-loop；
- Multi-Agent；
- 异步和 Durable Execution；
- 可观测性；
- 多模型兼容；
- 代码复杂度；
- 供应商锁定；
- 社区与维护；
- 学习成本。

## 10. Context、Memory 与 RAG

### 10.1 Context Engineering

- Context 组成；
- 动态 Skill；
- 工具结果压缩；
- 长任务状态外置；
- Prompt 版本；
- 不同 Agent 的隔离。

### 10.2 Memory

- Working、Conversation、User、Task、Asset、Semantic 和 Procedural Memory；
- 写入、确认、检索和删除；
- 记忆污染；
- 多用户隔离；
- 生命周期。

### 10.3 RAG

- Chunking；
- Embedding；
- Retrieval；
- Rerank；
- Citation；
- Offline Evaluation；
- 与 Fine-tuning、Memory 和 Tool 的区别。

## 11. Durable Execution 与 Job Queue

- Run、Step、Tool Call 和 Job 状态；
- Checkpoint 与 Resume；
- Side Effect；
- 幂等；
- Retry、Timeout 和 Cancel；
- 进程重启；
- 任务版本迁移；
- 进度事件；
- 结果通知。

## 12. 工具契约、MCP 与 Capability

- Tool Schema；
- 输入输出和错误类型；
- 幂等、副作用和权限声明；
- MCP Client/Server；
- 本地与远程 MCP；
- 不可信 Server；
- Capability Manifest；
- Skill、Tool、Capability、Model、MCP 和 Plugin 的边界。

## 13. 安全与 Human-in-the-loop

- Prompt Injection；
- 间接注入；
- 工具越权；
- 附件和网页内容；
- 风险分级；
- 审批前后状态；
- 审批超时和重复点击；
- 审计与撤销。

## 14. 评测与可观测性

- Task Success；
- Tool Selection 和 Argument Accuracy；
- Recovery；
- Token、Latency 和 Cost；
- Trace、Span 和 Run；
- Structured Logging；
- OpenTelemetry；
- Record/Replay；
- Failure Injection；
- LLM-as-Judge 与人工评审。

## 15. 必做对照实验

使用同一个“聊天图片生成空间照片并回传结果”任务比较：

1. 当前自研 `AgentRunner`；
2. LangChain Agent；
3. LangGraph Single Agent；
4. LangGraph Supervisor Multi-Agent。

实验指标和正式结论进入项目实验记录与 ADR，不写在本大纲中。

返回[知识库总导航](../README.md)。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
