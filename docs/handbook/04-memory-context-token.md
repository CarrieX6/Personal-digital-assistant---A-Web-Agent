# 4. 数据、记忆、上下文与 Token

文档整理：**Zhuofan Xie** · 2026-10-08

分层记忆主要实现：**Xianggang Ma**

[返回主目录](README.md)

## 4.1 保存、召回和送给模型是三件事

聊天数据库可以保存大量消息，但每轮不应全部塞进模型。模型没有自动访问本机 SQLite：
应用先查询，筛选和压缩，再构造本轮请求。

| 层 | 内容 | 用途/源码 |
| --- | --- | --- |
| 原始会话 | 用户/助手消息、附件引用、Run | SQLiteMemoryStore.append_message / create_run |
| 短期摘要 | 稳定资料、决策、未完成项、资产引用、来源范围 | _refresh_session_summary / get_session_summary |
| 长期 claim | 用户偏好、事实、任务、情景与程序性记忆 | remember / remember_many / search_memories |
| evidence | claim 的来源消息/Run、时间和证据 | list_memory_evidence |
| 工作状态 | pending_calls、observations、预算、审批 | LangGraph State/Checkpoint |
| 资产/任务 | 输入、派生结果、Job 状态 | AssetRepository / SpatialSceneService |

Checkpoint 用于恢复执行，不是产品聊天数据库，也不是长期事实库。

## 4.2 数据在哪里

由 [main.py](../../backend/app/main.py) 和
[AgentRunner.__init__](../../backend/app/agent.py) 可找到默认目录：

```text
backend/data/
  agent_memory.sqlite3       会话、Run、摘要、长期 claim/evidence 等
  agent_checkpoints.sqlite3  图状态和工具执行账本
  token_usage.sqlite3        模型调用计数与耗时
  assets/                    结果文件目录（以当前服务布局为准）
  source-images/             暂存图片
  viewer-secret.key          Viewer 签名密钥
  其他渠道/身份/安装状态库、非敏感配置及加密文件
```

不要用本列表猜全部文件名；默认常量、服务构造路径和部署备份代码才是来源。
数据库、个人媒体和密钥均不提交 Git。

## 4.3 新手可验证的记忆实验

1. 在会话 A 发送：“记住：我偏好简洁中文回答。”
2. 在记忆中心确认条目、类型与来源。
3. 新建会话 B，问“我偏好什么样的回答？”
4. 清空会话 B，再问；解释为何用户级长期偏好可以仍然存在。
5. 在记忆中心删除该条；不要仅删除一段聊天就认为所有派生记忆都删除。
6. 飞书另一个用户不应召回这个 Web/其他 owner 的记忆；隔离需要自动化测试，不能只看 UI。

源码：
[memory.py](../../backend/app/memory.py) 的 `context()`、`remember_many()`、
`search_memories()`、`clear_thread()`、`delete_memory()`；
[MemoryManagerDialog](../../app/components/MemoryManagerDialog.tsx)。

“原子 claim”是一条可单独更新的陈述。例如姓名、所在地、偏好可以分开。
`remember_many()` 在一笔事务内写入可识别的原子项；不能完整识别时保留原文，
避免强行切句丢语义。重复 claim 可以增加 evidence，不等于把相同正文重复保存很多次。

## 4.4 记忆召回不是把整个表倒入 Prompt

`search_memories()` 先按 owner、作用域、时间有效性和 retrieval_policy 过滤，再做检索排序：
词法/槽位/语义候选、RRF 融合与 MMR 多样性选择，结合证据和使用反馈。

- RRF：合并不同检索通道的排序；不是训练一个新的大模型。
- MMR：在相关性和候选间差异之间取舍，减少重复注入。
- `explicit_only / never`：先决定是否允许进入候选，不应被“相似度高”覆盖。
- `superseded` 等时态状态：旧偏好保留审计关系，默认不作为当前事实注入。

实现见 [memory.py](../../backend/app/memory.py) 的 `_semantic_scores()`、
`_mmr_select_memories()`、`_retrieval_policy_allows()`，以及
[retrieval.py](../../backend/app/retrieval.py)、
[Transformer Embedding](../../backend/app/transformer_embedding.py)。

没有安装语义模型时存在降级路径；必须看运行状态，不要仅凭架构图断言正在用真实 Embedding。
本章机制说明来自代码；完整数据契约见[分层记忆](../architecture/layered-memory.md)。

## 4.5 一轮上下文怎样拼接

[ContextBuilder.build()](../../backend/app/context.py) 接收本轮消息、工具 Schema、
附件元数据、近期对话、摘要和召回记忆，做预算及去重；Planner 将结果转成模型请求。

```text
系统约束 + 已选工具 Schema + 受控附件元数据
+ 非可信的结构化会话摘要
+ 已过滤的非可信长期记忆
+ 仍需保留的近期角色消息
+ 本轮用户消息
+ 预留输出容量
```

这不是“把所有文本拼成一个 system prompt”：
用户历史和记忆不能提升为系统指令，owner 也不能靠 Prompt 约定实现。
摘要覆盖过的原始消息、重复记忆会被去重，避免一条信息多次占容量。

默认预算不是所有供应商的真实上下文窗口。换模型时根据其契约调整：
`AGENT_CONTEXT_TOKEN_BUDGET`、`AGENT_OUTPUT_TOKEN_RESERVE`、
`AGENT_RECENT_HISTORY_TOKENS`、`AGENT_SESSION_SUMMARY_TOKENS`、
`AGENT_LONG_TERM_MEMORY_TOKENS`。

## 4.6 长会话如何压缩，压错怎么办

`SQLiteMemoryStore._refresh_session_summary()` 按 Token 压力维护滚动摘要；
真实 Planner 可用 Schema 约束摘要，结构中保留关键信息和 provenance。
无可用摘要模型时，抽取式兜底仍会显式记录，不伪装成模型摘要。

压缩不是删掉所有旧消息：原始事件是事实来源，摘要是可重建的派生数据。
检查 `verify_session_summary_provenance()` 与
[test_context.py](../../backend/tests/test_context.py) 的预算、来源、去重测试。

常见错误和改法：

| 错误 | 原因 | 处理 |
| --- | --- | --- |
| 记不住早期约束 | 只截最近 N 条 | 保留结构化约束/未完成项；评测早期事实召回 |
| 同一事实反复出现 | 摘要和原文都注入 | 使用来源覆盖范围去重 |
| 新旧偏好冲突 | 只按更新时间拼接 | 时态替代关系＋当前有效过滤 |
| 摘要“批准”了危险操作 | 将摘要当可信策略 | 审批走代码状态，不以摘要文本为证据 |
| 压缩引入幻觉 | 摘要过度推断 | 来源核对、约束字段、回退/重建，不静默接受 |

## 4.7 Token 三个口径

1. 本地 Tokenizer：用于预算和裁剪，有真实本地模型与显式 fallback。
2. 发出前的保守门禁：完整请求体加输出/视觉预留；不是精确供应商计费器。
3. 供应商 usage：调用后用于计数；缺失时记录 estimate，不能冒充 actual。

[Tokenization](../../backend/app/tokenization.py)：
`get_default_token_counter()`、`enforce_request_token_gate()`；
[TokenUsageStore](../../backend/app/token_usage.py)：
`record()`、`summary()`、`reserve_token_budget()`；
[llm.py](../../backend/app/llm.py)：HTTP 前检查和调用后记录。

可在本机查询：

```bash
curl -fsS http://127.0.0.1:8000/api/usage/tokens
```

账本保存计数、阶段、owner/run/thread、模型和耗时，不保存完整 Prompt、图片 Base64 或 Key。
`AGENT_RUN_TOKEN_BUDGET` 为可选 Run 预算；未启用时不能说系统已强制每 Run 配额。

## 4.8 如何证明节省而不损伤效果

固定同一任务集对比：全量工具/历史基线 vs 工具筛选、去重摘要和异步移交。
同时报告成功率、早期约束保留率、Token/Run、P95、估算占比和质量，不只报 Token 降幅。
目前没有可复核的统一 Token 节省百分比；价格/币种版本未建立时也不报“省了多少元”。

[下一章：图像能力](05-image-capabilities.md) · [返回主目录](README.md)
