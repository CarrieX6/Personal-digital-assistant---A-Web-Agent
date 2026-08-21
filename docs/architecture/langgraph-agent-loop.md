# LangGraph 循环 Agent 设计

作者：**Zhuofan Xie**  
更新日期：2026-07-29

## 结论

项目已在 2026-07-29 从一次性流程：

```text
START → plan → tools → answer → END
```

升级为第一版受控循环：

```text
接收任务 → 规划 → 策略检查 → 执行一个工具 → 观察结果
                  ↑                         ↓
                  └──── 继续 / 重规划 ← 决策节点
                                          ├── 完成
                                          └── 安全失败
```

当前已经实现单工具执行、名称与参数 Schema 检查、最大工具步数、最大重规划次数、
最大连续错误、总运行时间、LangGraph `recursion_limit`、SQLite Checkpointer 和
安全失败状态。高风险 Tool 可通过 `interrupt()` 暂停，在 Web Root 或飞书批准/拒绝
后恢复；持久化执行账本会复用完成结果，并阻止状态不明的非幂等副作用自动重试。
模型通过原生 Tool Calling 表达“继续调用工具”或“返回最终文本”。

尚未实现工具执行的强制终止、Token/费用预算、审批过期、通用 Outbox，以及结构化
`clarify` 决策。

**需要手写边界条件。** 模型适合做语义判断，但停止条件、权限、预算、超时、参数校验
和副作用审批必须由确定性代码控制。LangGraph 提供图、条件边、持久化和中断机制，
不会替开发者自动定义产品的安全边界。

## 1. 模型判断和代码判断如何分工

### 交给模型，但要求受约束输出

- 用户目标是否已经完成；
- 下一步应使用哪个已允许工具；
- 工具结果是否足以回答；
- 是否需要重规划；
- 缺少什么用户信息；
- 最终自然语言回答如何组织。

当前第一版复用兼容供应商的原生 Tool Calling：返回 `tool_calls` 代表继续，返回文本
代表完成或说明能力边界。后续需要区分“完成、澄清、审批”时，再升级为受限结构：

```python
class NextDecision(BaseModel):
    action: Literal["continue", "replan", "clarify", "complete"]
    reason: str
    next_goal: str | None = None
    final_answer: str | None = None
```

### 必须由代码确定

- 最大循环步数、最大重规划次数和最大连续错误次数；
- 工具是否存在，参数是否符合 Schema；
- 当前用户、渠道和会话是否有权调用工具或访问资产；
- 单步超时、总运行时、Token、费用、附件大小和并发预算；
- 发送消息、删除文件、付款等副作用是否需要人工批准；
- 错误可否重试、退避时间和幂等键；
- 哪些状态是不可逆终态；
- 禁止模型绕过的内容与数据策略。

即使模型返回 `continue`，代码只要检测到预算耗尽、权限不足或终态，也必须覆盖模型
决策并安全结束。

## 2. 当前状态

```python
class AgentGraphState(TypedDict):
    run_id: str
    message: str
    execution_context: dict[str, str]
    plan: dict
    pending_calls: list[dict]
    round_observations: list[dict]
    observations: list[dict]
    steps: list[dict]
    answer: str
    status: Literal[
        "running", "waiting_approval", "completed", "failed"
    ]
    step_count: int
    replan_count: int
    consecutive_errors: int
    started_at: float
    last_error: str | None
    approval_required: bool
    approval: dict | None
```

预算和身份必须进入状态或运行配置，不能只写在提示词中。`thread_id` 用于恢复同一条
运行；`owner_id + channel + thread_id` 用于项目自身的数据隔离。

LangGraph Checkpointer 会在图执行的每个 super-step 保存 checkpoint，并通过
`thread_id` 查找和恢复状态。参见
[LangGraph Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)。

## 3. 节点职责

| 节点 | 责任 | 状态 |
| --- | --- | --- |
| `plan` | 生成首轮工具计划或直接回答 | 已实现 |
| `policy` | 校验工具、Schema、步数和运行时间 | 已实现 |
| `execute_tool` | 一次只执行一个工具并捕获安全错误 | 已实现 |
| `observe` | 记录结果、连续错误和下一路由 | 已实现 |
| `decide` | 把本轮结果交给模型，完成或生成下一轮工具 | 已实现 |
| `finalize` | 输出完成回答 | 已实现 |
| `fail` | 输出稳定的安全停止原因 | 已实现 |
| `approval` | 高风险副作用暂停、批准/拒绝并恢复 | 已实现 |

一次只执行一个工具很重要：它让每次工具执行后都能重新检查授权和预算，也便于暂停、
重试和恢复。

## 4. 条件边与边界伪代码

可以使用 `add_conditional_edges`，也可以让节点返回 `Command(goto=...)`。两者都是
显式路由，参见
[LangGraph Graph API](https://docs.langchain.com/oss/python/langgraph/graph-api)。

```python
def route_after_observe(state: AgentGraphState) -> str:
    # 代码硬边界，优先级高于模型
    if state["status"] in {"completed", "failed"}:
        return state["status"]
    if state["step_count"] >= MAX_STEPS:
        return "budget_exhausted"
    if state["replan_count"] >= MAX_REPLANS:
        return "budget_exhausted"
    if state["consecutive_errors"] >= MAX_ERRORS:
        return "failed"
    if elapsed_seconds(state) >= MAX_RUNTIME_SECONDS:
        return "timed_out"

    # 仅在硬边界通过后使用模型的结构化语义决策
    decision = validate_decision(state["next_decision"])
    return {
        "continue": "policy",
        "replan": "plan",
        "clarify": "clarify",
        "complete": "finalize",
    }[decision.action]
```

编译图时再设置 LangGraph 的 `recursion_limit`，作为最后一道保险；业务仍需保留自己的
步数和预算计数，因为 `recursion_limit` 不能表达费用、权限或错误策略。

## 5. 人工确认与继续执行

需要用户补充信息或批准高风险操作时，在节点中调用 `interrupt(payload)`。恢复时必须
使用原 `thread_id` 并传入 `Command(resume=...)`。LangGraph 会从中断节点开头重新
执行，因此中断前的副作用必须幂等，或移动到中断之后。

参见[LangGraph Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)。

典型审批负载：

```python
interrupt({
    "type": "approval_required",
    "tool": "send_external_message",
    "summary": "将图片发送到飞书群 oc_xxx",
    "risk": "群成员都可见",
})
```

当前实现将 Run 元数据保存在 `agent_runs`，批准或拒绝必须映射回同一个
`run_id + thread_id`。Web Root 提供全渠道待审批队列；飞书用户可回复
`批准 <run_id>` 或 `拒绝 <run_id>`。重启后使用同一 Checkpointer 恢复，重复批准
返回已保存结果，不会再次执行工具。

## 6. 实施状态与下一步

已经完成：

1. 扩展 `AgentGraphState`，加入身份、状态、计数器、当前工具和错误；
2. 把一次执行全部调用的 `_tools` 拆成单次 `execute_tool`；
3. 新增确定性的 `policy` 和 `observe`；
4. 新增 `decide`，建立 `observe → decide → policy/finalize` 循环；
5. 将策略、观察和决策写入 Web 可恢复的 Trace；
6. 增加 `interrupt`、持久化 Run、Web/飞书审批和跨重启恢复；
7. 增加工具风险/幂等声明与 SQLite 执行账本；
8. 增加重规划、预算耗尽、参数拒绝、审批隔离、重复批准和 checkpoint 重载测试。

下一步：

1. 为缺少信息的任务增加结构化 `clarify` 中断；
2. 增加审批过期、一次性 nonce 和更完整的审计字段；
3. 增加 Outbox、失败队列和跨渠道可靠通知；
4. 增加真正可中断的工具超时、Token/费用和并发预算；
5. 增加进程在工具执行中崩溃和越权资产攻击测试。

第一版不应直接实现开放式无限 Agent。建议默认：

```text
MAX_STEPS = 4
MAX_REPLANS = 3
MAX_CONSECUTIVE_ERRORS = 2
MAX_RUNTIME_SECONDS = 120
```

这些是工程起始值，不是通用最佳值；应通过真实任务集测量成功率、时延和费用后调整。

## 7. 与 Single Agent / Multi-Agent 的关系

循环并不要求 Multi-Agent。当前阶段应先完成一个带工具、记忆、硬边界和人工确认的
Single Agent。只有当任务存在稳定、可验证的角色边界，例如“研究 → 实现 → 审核”，
且单 Agent 的上下文或权限确实成为瓶颈时，再把某个节点替换为子图或独立 Agent。

LangGraph 官方把 workflow 区分为预定代码路径，把 agent 描述为动态决定过程和工具
使用；两者可以组合。参见
[Workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)和
[LangGraph Overview](https://docs.langchain.com/oss/python/langgraph/overview)。

## 8. 验收标准

- [x] 工具结果不足时能再次规划，而不是直接生成看似完成的回答；
- [x] 达到工具步数或重规划上限后确定结束，模型无法继续循环；
- [x] 重启 Checkpointer 后能读取同一 `thread_id` 的已保存状态；
- [x] 重启进程后能恢复等待审批的任务；
- [x] 相同 Run 的副作用工具重放不会重复执行；
- [ ] 重启进程后能恢复正在执行中的异步任务；
- 越权访问在工具执行前被阻止；
- [x] Web 和飞书能处理等待批准、完成和失败；
- [ ] Web 和飞书能处理结构化等待补充；
- [x] 每条运行可追溯策略检查、模型决策、工具结果和耗时。
