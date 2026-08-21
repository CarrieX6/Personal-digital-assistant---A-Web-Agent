# 可靠性、任务状态与数据存储

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：已核对，项目 Schema 待实验  
关联任务：`LEARN-001`、`JOB-001`、`MEMORY-001`、`DATA-001`

## 1. 从“消息”到“任务”

聊天平台的一条消息可能被重复推送，网络响应也可能丢失。系统不能用“收到一次 HTTP
请求”代表“业务只执行一次”。建议保存四类标识：

| 标识 | 回答的问题 |
| --- | --- |
| `event_id` | 平台事件是否已经接收过 |
| `message_id` | 用户看见的消息是哪一条 |
| `run_id` | 一次 Agent 推理/编排过程是什么 |
| `job_id` | 一个可持久化、可恢复的后台任务是什么 |

典型处理：

1. 以 `(channel, tenant, event_id)` 建唯一约束；
2. 首次事件创建 Job，重复事件返回同一结果或 ACK；
3. Job Worker 领取任务时使用原子状态迁移；
4. 回传消息也保存平台响应 ID，失败后可安全重试。

## 2. 投递语义与幂等

- At-most-once：可能丢，不重复；
- At-least-once：尽量不丢，但可能重复；
- Exactly-once：通常只能在受控边界内通过事务、去重和幂等效果逼近，不能把它当作
  跨聊天平台、数据库和 GPU Worker 的天然保证。

幂等不是“接口名称相同”，而是同一业务请求重复执行时不会产生额外副作用。可用：

- 客户端生成或平台提供的 idempotency key；
- 数据库唯一约束；
- 状态机的 Compare-and-Set；
- 输出按内容哈希去重；
- 将发送、支付、删除等副作用记录为单独步骤。

## 3. Job 状态机

建议基线：

```text
queued → running → succeeded
   │        ├──→ retry_wait → queued
   │        ├──→ waiting_approval → running
   │        └──→ failed
   └───────────→ cancelled
```

规则：

- 状态转换由代码白名单控制，不能任意写字符串；
- `cancel_requested` 与最终 `cancelled` 分开；
- Worker lease 有过期时间，进程崩溃后任务可重新领取；
- 每一步保存 attempt、错误类别、开始/结束时间；
- 已发生副作用的步骤重试前先查询执行结果；
- 用户可见状态与内部步骤状态分开。

## 4. Timeout、Retry、Backoff 与 Circuit Breaker

重试前先分类：

- 可重试：连接中断、限流、临时 5xx、Worker 崩溃；
- 不可重试：参数无效、权限不足、文件格式不支持；
- 需要人工：支付、覆盖文件、发送公开内容、冲突决策。

指数退避应加入随机抖动；总尝试次数和总时长都要有限制。Circuit Breaker 用于上游
持续失败时快速拒绝或降级，避免所有任务同时压垮依赖。Dead Letter Queue 不是
“垃圾桶”，而是保存超过自动恢复范围、等待诊断和人工处理的任务。

## 5. 事务、Outbox 与补偿

PostgreSQL 事务把多个步骤作为一个 all-or-nothing 操作；中间状态对其他事务不可见
([PostgreSQL Transactions](https://www.postgresql.org/docs/current/tutorial-transactions.html))。
但数据库事务不能覆盖“数据库写入 + 调用聊天平台 API”这两个系统。

Outbox Pattern 的思路是：在同一数据库事务里写业务状态和待发送事件，再由独立
Publisher 投递。这样避免“数据库已成功、通知未发送”后没有恢复线索。

对于已经发生、无法回滚的外部动作，使用补偿而不是幻想分布式回滚。例如：

- 文件已上传：补偿是删除或标记过期；
- 消息已发送：补偿可能是追加更正，而不是物理撤回；
- GPU 资产已生成：任务取消后可延迟清理。

## 6. SQLite 与 PostgreSQL 的项目边界

### SQLite

适合单机、个人用户、早期原型。WAL 模式允许读写更好地并发，但仍有单写者约束，
并且 WAL 文件与数据库文件需要一起正确管理
([SQLite WAL](https://sqlite.org/wal.html))。

建议用于：

- 单机用户、会话、任务和资产元数据；
- 本地 Outbox；
- 小规模 FTS；
- 配置和审计索引。

不建议把多个网络节点直接共享同一个 SQLite 文件。

### PostgreSQL

适合 Cloud Gateway、多实例 Worker、多租户和更强的并发与运维需求。迁移条件应由
实际并发、可用性和部署拓扑决定，而不是因为“PostgreSQL 更专业”就提前引入。

## 7. 记忆不是一张聊天记录表

建议分层：

| 层 | 内容 | 生命周期 | 是否自动进入 Prompt |
| --- | --- | --- | --- |
| Conversation | 原始消息、附件引用 | 按用户策略保留 | 当前窗口的一部分 |
| Working State | 当前 Run 的计划、变量、审批 | Run 结束后归档 | 是 |
| User Profile | 明确偏好、设备、允许的能力 | 长期，可查看/纠正 | 按需 |
| Episodic | 已完成任务与结果摘要 | 长期、可删除 | 检索后 |
| Knowledge | 文档片段、来源、版本 | 跟随资料生命周期 | 检索后 |
| Audit | 谁在何时调用什么能力 | 按安全策略 | 否 |

禁止把模型自行推断的敏感属性直接写成长期事实。长期记忆写入应有来源、置信度、
更新时间、用途和删除入口。

## 8. FTS、向量检索与混合检索

- FTS 擅长精确词、ID、文件名和专有名词；
- Embedding 检索擅长语义相近表达；
- Hybrid Search 把两种召回合并；
- Reranker 对候选片段重新排序；
- 摘要减少上下文，但可能丢失细节，应保留原文引用。

RAG 最初提出把参数化生成模型与可检索的非参数记忆结合，用于知识密集任务
([Lewis et al., NeurIPS 2020](https://papers.neurips.cc/paper/2020/hash/6b493230205f780e1bc26945df7481e5-Abstract.html))。
它并不保证答案正确；检索失败、切块错误、旧资料和生成误读仍需分别评测。

## 9. 资产数据模型

多模态结果不要只存路径。建议 Asset 至少包含：

- `asset_id`、owner、MIME、size、hash；
- 原始文件名与安全化后的存储名；
- 来源、父资产和派生类型；
- 创建任务、模型/流程版本；
- 缩略图和预览版本；
- 保留期限、删除状态；
- 访问策略和下载审计。

文件类型应同时检查扩展名、MIME 和内容 Magic Number；解码放在隔离进程，并限制
像素、时长、压缩比和解压后大小。

## 10. 待实验项

- SQLite WAL 下 Channel、Agent、Worker 并发写入；
- Worker 崩溃后的 lease 回收；
- 同一飞书事件重复 10 次是否只生成一个 Job；
- 回传 API 超时但实际成功时能否查重；
- FTS5、向量和混合检索的召回、延迟与磁盘占用。

## 11. 来源

- [PostgreSQL：Transactions](https://www.postgresql.org/docs/current/tutorial-transactions.html)
- [SQLite：Write-Ahead Logging](https://sqlite.org/wal.html)
- [RAG 原始论文，NeurIPS 2020](https://papers.neurips.cc/paper/2020/hash/6b493230205f780e1bc26945df7481e5-Abstract.html)

返回[工程与 AI 基础知识地图](README.md)。

