# Agent 框架现状、优缺点与选型方法

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：官方能力已核对，项目横评待实验  
关联任务：`LEARN-AGENT-001`、`AGENT-001`

> 版本基线：2026-07-28。框架变化快，采用前必须重新核对发行版、许可和迁移指南。

## 1. 先选抽象层，不先选品牌

框架大致分为：

1. Model/Tool SDK：封装模型、Schema、Tool Calling；
2. Agent Runtime：管理 Agent Loop、Handoff、Session；
3. Graph/Workflow：显式状态、边、Checkpoint、HITL；
4. Multi-Agent Runtime：消息、Supervisor、分布式 Agent；
5. Durable Workflow Engine：跨失败的长期执行；
6. UI/协议层：Agent 与前端、工具或其他 Agent 的互操作。

项目可以组合不同层，不必把一个框架用于所有问题。

## 2. 当前主要候选

### 自研 Loop + Pydantic/FastAPI

优势：依赖少、行为透明、适合现有 `AgentRunner` 和小团队；缺点：持久化、HITL、
Trace、并发工具与协议适配都要自己维护。适合先做最小闭环和建立基准。

### LangChain v1

`create_agent` 是 v1 的标准 Agent API，底层使用 LangGraph；Middleware 用于动态
Prompt、工具选择、摘要、guardrail 和 HITL
([LangChain v1](https://docs.langchain.com/oss/python/releases/langchain-v1))。

优势：模型/工具生态广、起步快；缺点：抽象和依赖较多，上游版本迁移需管理。建议
只引入确实减少重复代码的组件。

### LangGraph v1

核心是 State、Node、Edge、Persistence、Interrupt 和 Subgraph；v1 继续把 durable
execution、checkpoint、streaming、HITL 作为一等能力
([LangGraph v1](https://docs.langchain.com/oss/python/releases/langgraph-v1))。

优势：显式图、恢复、审批和复杂路由；缺点：简单 Agent 会显得重，State Schema 和
图升级需要纪律。适合本项目中“对话 → 审批 → 异步任务 → 回传”的长流程实验。

### OpenAI Agents SDK

提供 Agent、Runner、Tools、Handoffs、Guardrails、Sessions 和 Tracing
([OpenAI Agents SDK](https://openai.github.io/openai-agents-python/agents/))。
优势：概念精简、OpenAI 模型整合和 Trace 直接；缺点：跨供应商与本地模型能力需按
实际适配器验证，不能因 SDK 名称假定完全兼容。

### Pydantic AI

强调 Python 类型、依赖注入、结构化输出、模型无关、OpenTelemetry、MCP、HITL、
Evals 和 Durable Execution
([Pydantic AI](https://pydantic.dev/docs/ai/overview/))。
优势：与 FastAPI/Pydantic 心智模型一致、类型和测试友好；缺点：功能快速演进，团队
需要控制版本。它是原大纲遗漏的重点候选。

### Google Agent Development Kit（ADK）

ADK 支持 Python、TypeScript、Go、Java/Kotlin，覆盖 Agent、Graph Workflow、
Multi-Agent、Sessions/Memory、MCP、A2A、评测和部署
([Google ADK](https://adk.dev/agents/))。
优势：多语言、显式确定性/非确定性组合、协议和评测覆盖完整；缺点：Google Cloud
相关能力较强，但本地和非 Google 模型路径仍需实测。它是原大纲遗漏的重点候选。

### Microsoft Agent Framework

截至 2026-07，Microsoft 官方称它是 Semantic Kernel 与 AutoGen 的直接后继，提供
Agents、Harness 和 Graph Workflows，含 MCP、Checkpoint、HITL 和多提供商支持
([Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/))。

因此新项目不应只比较旧 Semantic Kernel 与 AutoGen；应把 Microsoft Agent Framework
作为主候选，并把二者当作现有生态/迁移来源。其成熟度、Python 包版本和非 Azure
使用体验仍需实验。

### AutoGen

AgentChat 适合快速构建会话式单/多 Agent；Core 提供基于 Actor 模型的事件驱动运行时
([AutoGen Core](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/index.html))。
优势：多 Agent 研究与消息模型成熟；缺点：官方已指向 Microsoft Agent Framework
作为后继，新项目要评估迁移风险，而不是只看历史热度。

### CrewAI

官方把能力分为 Agents、Crews 和 Flows；Flows 支持状态、持久化和长流程恢复
([CrewAI Docs](https://docs.crewai.com/))。优势是角色/团队表达直观；缺点是对本项目
单 Agent + Capability 的早期阶段可能抽象过高，需用固定任务衡量实际收益。

### 其他候选

- LlamaIndex Workflows：知识/RAG 密集应用值得评估；
- Temporal/DBOS/Restate/Prefect：属于可靠执行层，不应和 Agent SDK 一对一比较；
- Mastra 等 TypeScript 框架：只有前后端统一 TypeScript 成为项目约束时再纳入。

## 3. 能力矩阵（文档级，不代表项目实测）

| 候选 | 最适合的起点 | 状态/HITL | Multi-Agent | 当前主要风险 |
| --- | --- | --- | --- | --- |
| 自研 | 最小 Loop、强控制 | 自研 | 自研 | 重复造基础设施 |
| LangChain | 通用 Agent 集成 | 依赖 LangGraph | 可组合 | 抽象/升级 |
| LangGraph | 有状态长流程 | 强 | 强 | 学习与图版本 |
| OpenAI Agents SDK | OpenAI-first Agent | Sessions/guardrails | Handoff | 跨供应商需验证 |
| Pydantic AI | 类型安全 Python | Durable 集成/HITL | 有模式 | 快速演进 |
| Google ADK | 多语言完整 Runtime | Graph/HITL | 强 | 项目适配待测 |
| Microsoft Agent Framework | Microsoft 生态及新项目横评 | Checkpoint/HITL | 强 | 新框架成熟度 |
| AutoGen | 消息驱动多 Agent | 有 | 强 | 后继迁移 |
| CrewAI | 角色团队 + Flow | 有 | 强 | 早期可能过重 |

## 4. 为什么不能用“Star 数/宣传 Demo”选型

统一 PoC 必须验证：

- DeepSeek/Qwen/GLM/OpenAI 与本地兼容路径；
- 工具 Schema、并行调用和错误恢复；
- SQLite 或可接受的持久层；
- 飞书审批后恢复；
- 进程重启后 Job/Run 状态；
- Trace 导出与敏感数据控制；
- 依赖体积、冷启动、升级和许可证；
- 团队可理解性。

## 5. 推荐决策门槛

首轮只保留三类实现：

1. 自研基线；
2. LangGraph 或 Pydantic AI 中一个 Python-native 候选；
3. Google ADK 或 Microsoft Agent Framework 中一个完整平台候选。

用同一批任务和故障注入测试后写 ADR。未跑基准前，不在知识文档中写“最终采用”。

## 6. 来源

- [LangChain v1](https://docs.langchain.com/oss/python/releases/langchain-v1)
- [LangGraph v1](https://docs.langchain.com/oss/python/releases/langgraph-v1)
- [OpenAI Agents SDK](https://openai.github.io/openai-agents-python/agents/)
- [Pydantic AI](https://pydantic.dev/docs/ai/overview/)
- [Google ADK](https://adk.dev/agents/)
- [Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/)
- [AutoGen](https://microsoft.github.io/autogen/stable/index.html)
- [CrewAI](https://docs.crewai.com/)

返回[Web Agent 知识地图](README.md)。

