# Agent Loop、架构模式与 Single/Multi-Agent

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：已核对，项目选型待 ADR  
关联任务：`LEARN-AGENT-001`、`AGENT-001`、`AGENT-002`

## 1. 什么才算 Agent

本知识库采用工程定义：Agent 是由模型驱动、能基于当前状态选择工具或动作、观察结果
并继续迭代直到停止条件的运行系统。单次模型回复、固定流水线和聊天 UI 本身不等于
Agent。

LangChain v1 将 Agent 描述为“模型在循环中选择工具，直到输出最终答案或达到停止
条件”，其 `create_agent` 底层是 LangGraph 图运行时
([LangChain Agents](https://docs.langchain.com/oss/python/langchain/agents))。

## 2. 最小 Agent Loop

```text
输入 → 构建上下文 → 调用模型
                    ├─ final → 校验并返回
                    └─ tool call
                         ↓
             授权 → 参数校验 → 执行
                         ↓
                   tool result ──┘
```

必须由代码控制：

- 最大模型轮次、最大工具次数、总时间和 Token 预算；
- Tool Call Schema 与权限；
- 工具 timeout、取消和重试；
- 终止条件；
- 同一副作用是否已经发生；
- 全链路 Trace；
- 高风险动作的人工审批。

模型可以“建议下一步”，但不能决定自己的权限和资源上限。

## 3. ReAct

ReAct 论文把推理与行动交错：模型根据观察更新计划并调用外部环境。论文在特定 QA、
事实核验和交互基准上报告了相对基线的改进
([Yao et al., ICLR 2023](https://arxiv.org/abs/2210.03629))。这是一种方法和实验结果，
不是对所有 Agent 任务的保证。

工程上不要要求或保存模型的私有 Chain of Thought。可记录的是：

- 简短、面向用户的计划；
- 已选择的工具和参数摘要；
- 工具结果与错误；
- 状态转移；
- 可审计的决策理由标签。

## 4. 何时不该用 Agent

若流程、输入和成功条件可明确编码，优先普通函数或 Workflow。Microsoft Agent
Framework 官方也明确建议：能用函数处理的任务就不要使用 AI Agent，并区分开放式
任务适合 Agent、步骤明确的流程适合 Workflow
([Microsoft Agent Framework](https://learn.microsoft.com/en-us/agent-framework/overview/))。

| 场景 | 首选 |
| --- | --- |
| 上传图 → 校验 → 深度 → LDI → 导出 | 确定性 Workflow |
| 用户自然语言选择功能和参数 | Agent/Router |
| 发送结果前权限和附件检查 | 确定性 Policy |
| 多来源开放式调研 | Agent，必要时多 Agent |
| 删除、覆盖、支付 | 确定性审批流程 |

本项目应让 Agent 负责“理解意图、补足参数、选择已授权 Capability”；具体生成管线、
文件处理和安全策略由确定性代码执行。

## 5. 常见架构模式

### Tool-calling Agent

一个 Agent 直接选择工具。优点是最简单、上下文集中、易做 MVP；缺点是工具多时选择
困难，长任务恢复和权限隔离需要额外实现。

### Router

先按规则或小模型把请求分到 Capability/Workflow，再由目标流程处理。路由输出必须是
封闭枚举并可回退到澄清。适合本项目首期的空间照片、试衣、宠物和资料问答入口。

### Plan-and-Execute

Planner 生成计划，Executor 执行，必要时 Replan。适合长任务，但计划可能过时，错误
会沿步骤传播。计划应持久化成结构化步骤，并限制模型可修改的字段。

### Graph / State Machine

节点代表模型、工具或普通函数，边代表控制流，Checkpoint 保存状态，Interrupt 等待
人类。适合长任务、审批和恢复，但会增加 Schema、迁移和图版本复杂度。

### Event-driven Agent

Channel、Agent、Worker 和通知服务通过事件协作。扩展和隔离好，但必须解决重复、
乱序、背压、事件版本和可观测性。

## 6. Single Agent

单 Agent 的优势：

- 一个上下文与一套权限更容易理解；
- Trace 简单；
- Token 和延迟较低；
- 失败责任明确；
- 更适合小团队先做可用闭环。

扩展方式依次应是：增加确定性工具 → 动态筛选工具 → 拆 Workflow → 最后再考虑多个
自治 Agent，而不是一开始按“岗位名称”创建很多 Agent。

## 7. Multi-Agent

Multi-Agent 只有在职责、上下文、权限或并行性确实需要隔离时才有价值：

- Supervisor：分派给专业 Agent 并聚合；
- Handoff：当前 Agent 把控制权交给另一个 Agent；
- Planner–Executor–Reviewer：生成、执行、验证分离；
- Parallel workers：独立子问题并发处理；
- Debate：多个候选互评；
- Blackboard：共享任务板逐步写入。

AutoGen Core 使用 Agent Runtime 和异步消息支持事件驱动、分布式多 Agent 系统
([AutoGen Core](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/index.html))；
Google ADK 也建议当指令可靠性、上下文或代码模块化出现瓶颈时，再从单 Agent 演进为
Workflow
([Google ADK Agents](https://adk.dev/agents/))。

### Multi-Agent 的隐性成本

- 每次交接增加 Token、延迟和故障点；
- 权限可能被链式放大；
- 共享记忆可能写入冲突；
- 多个模型可能一起犯同一种错；
- 结果仲裁仍需要规则、模型或人；
- 评测要覆盖路由、轨迹和最终结果。

多 Agent Debate 在一些论文任务中提升了推理或事实性，但结论依赖模型、任务和评测
设置，不能直接当作项目收益
([Du et al., 2023](https://arxiv.org/abs/2305.14325))。

## 8. 本项目的渐进式建议

这是待 ADR 的工程建议：

1. 首期：Single Agent + 动态 Tool/Capability 筛选；
2. 空间照片等生成：确定性 Job Workflow；
3. 风险动作：Policy + Human-in-the-loop；
4. 长任务：持久化状态和恢复；
5. 只有调研、复杂内容生产等任务在基准上明确获益，才增加 Reviewer 或并行 Worker；
6. 不让子 Agent 继承 Supervisor 的全部凭证。

## 9. 选型实验

固定 30–50 个真实任务，对比：

- 自研 Single Agent Loop；
- Single Agent + Graph Workflow；
- Supervisor + 专业 Agent。

指标至少包括：任务完成率、正确工具率、参数正确率、平均轮次、P50/P95 延迟、Token、
恢复成功率、审批绕过率和人工修正次数。

## 10. 来源

- [LangChain v1 Agents](https://docs.langchain.com/oss/python/langchain/agents)
- [ReAct 原始论文](https://arxiv.org/abs/2210.03629)
- [Microsoft Agent Framework Overview](https://learn.microsoft.com/en-us/agent-framework/overview/)
- [Google ADK Agents](https://adk.dev/agents/)
- [AutoGen Core](https://microsoft.github.io/autogen/stable/user-guide/core-user-guide/index.html)
- [Multiagent Debate 论文](https://arxiv.org/abs/2305.14325)

返回[Web Agent 知识地图](README.md)。

