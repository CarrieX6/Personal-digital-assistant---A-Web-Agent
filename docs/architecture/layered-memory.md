# 分层、证据化与时态化 Agent 记忆

作者：**Zhuofan Xie**
更新日期：2026-08-24
关联任务：`MEMORY-001`、`MEMORY-002`

## 1. 结论

项目在原有 owner/thread 隔离、显式长期记忆和 LangGraph Checkpoint 基础上，采用
“工作记忆、短期会话记忆、语义长期记忆、情景记忆、程序性记忆”五层结构。长期
记忆不再按更新时间整表注入，而是先执行作用域与有效期过滤，再综合关键词、FTS、
时间、重要度、可信度和历史效用进行召回。

所有记忆仍是非可信上下文，不能覆盖系统策略；程序性记忆只有在用户明确保存或管理
端确认后才能生效。原始消息与任务 Run 保持事实来源，摘要和情景记忆是可删除的派生
数据。

## 2. 分层边界

| 层级 | 内容 | 生命周期 | 当前存储 |
| --- | --- | --- | --- |
| 工作记忆 | 当前计划、工具观察、审批、预算 | 单个 Run | LangGraph State/Checkpoint |
| 短期会话 | 最近消息、滚动摘要、决策、未完成事项 | 单个 thread | `agent_messages`、`agent_session_summaries` |
| 语义长期 | 资料、偏好、事实、任务状态、资产关系 | 跨会话 | `agent_memories` |
| 情景记忆 | 历史任务、工具、结果和失败经验 | 跨会话 | `agent_memories(type=episode)` |
| 程序性记忆 | 用户确认的操作习惯和流程 | 跨会话 | `agent_memories(type=procedure)` |

Checkpoint 只负责恢复图执行，不能作为产品会话或长期记忆的查询接口。

## 3. 数据契约

每条长期记忆包含：

- `memory_type`：profile、preference、fact、task_state、episode、procedure、
  asset_relation；
- `scope/scope_id`：user、channel、thread 或 project；
- `source_message_id/source_run_id/source`：来源证据；
- `confidence/importance/sensitivity`：可信度、重要度与敏感级别；
- `valid_from/valid_to/status`：有效期与 active、superseded、archived 等状态；
- `topic_key/supersedes_id`：确定性识别的同主题更新关系；
- `last_accessed_at/access_count/utility_score`：召回和任务结果反馈；
- `metadata_json`：工具、任务或资产等受控扩展字段。

`agent_memory_events` 只记录事件类型、内容哈希和非敏感元数据，不复制记忆正文。
永久删除一条记忆时同步删除其 FTS、事件与使用记录，避免“逻辑删除后仍可恢复正文”。

## 4. 写入与更新

### 4.1 显式写入

用户发送“记住：……”或通过记忆中心创建记忆时立即保存。保存前拒绝 API Key、
访问令牌、密码和验证码特征。类型可以由用户选择，也可以使用确定性规则推断。

### 4.2 时态更新

位置和明确的个人属性使用 `topic_key` 识别同一事实槽位。新事实写入时，旧记录进入
`superseded`，`valid_to` 指向新事实的生效时间，新记录保留 `supersedes_id`。不会
覆盖旧事实，因此可以解释当前值和历史值。

### 4.3 情景写入

包含工具调用或失败的 Agent Run 在终态后形成一条情景记忆，包括任务摘要、工具名、
结果与状态。普通闲聊不自动形成情景记忆。可能包含凭证特征的 Run 不写入派生记忆。

### 4.4 会话摘要

会话保留最近五条未摘要消息；更早消息按本地确定性规则生成 extractive summary，
同时提取带“决定、确认、选择”的决策和带“下一步、尚未、还需”的开放事项。该摘要
不会成为 system message，也不会删除原始消息。

## 5. 召回与上下文装配

召回步骤：

1. 先按 owner、scope、status 和有效期过滤；
2. 使用 FTS5、中文双字词、英文词项和少量领域同义词生成候选；
3. 非空请求必须存在词项、子串或 FTS 命中，无关记忆即使重要度较高也不注入，
   避免把无关敏感信息发送给模型；
4. 综合词项重合、FTS 命中、时间衰减、重要度、可信度、效用和作用域打分；
5. 去除重复内容，最多向上下文交付配置数量的记录；
6. 只为真正进入上下文的记忆记录本次 Run、排名和使用次数，不记录提示词原文。

上下文预算优先保证系统策略、工具 Schema 和当前请求。剩余预算为会话摘要和长期记忆
保留独立份额，再放入最近原始消息，避免短期历史完全挤掉高价值长期记忆。最终顺序：

```text
可信附件元数据
→ 非可信会话摘要 / 决策 / 开放事项
→ 非可信长期记忆证据
→ 最近原始消息
→ 当前用户请求
```

## 6. 结果反馈

被注入某个 Run 的记忆写入 `agent_memory_usage`。Run 完成时小幅提高其效用，失败时
降低效用；分值只影响排序，不会自动删除或改变记忆正文。此机制是第一版可解释反馈，
后续应结合用户纠正、工具成功率和对照实验调整权重，不能把“Run 完成”直接等同于
“记忆正确”。

## 7. 用户控制与 API

Web 记忆中心支持新增、搜索、按类型筛选、编辑重要度、归档、永久删除和 JSON 导出。
API：

- `GET/POST /api/memories`；
- `PUT/DELETE /api/memories/{memory_id}`；
- `GET /api/memories/export`。

当前 Web 产品仍是本机 Root 视图，接口使用 `local` owner。飞书用户继续通过自然语言
管理自己的 owner-scoped 记忆；统一身份模型与远程记忆管理依赖 `AUTH-001` 和
`MESSAGE-001`，本任务不扩大未认证 Web API 的可见范围。

## 8. 兼容迁移与降级

- 启动时使用增量 `ALTER TABLE` 为旧 `agent_memories` 补列，原字符串记忆迁移为
  active fact，并保留原 ID、正文、来源和时间；
- 支持 FTS5 时自动重建本地索引；SQLite 未编译 FTS5 时退化为结构化与词项评分；
- 不引入必须下载的 Embedding 模型。后续可以在固定评测证明收益后增加本地
  Embedding Provider，而不改变 MemoryRecord 和上下文接口；
- 删除会话不会删除长期记忆、任务资产或飞书原消息；清空长期记忆会删除对应索引和
  使用记录。

## 9. 验证与后续实验

当前自动测试覆盖：旧库迁移、类型化写入、时态取代、跨 owner 隔离、敏感信息拒绝、
会话摘要、混合召回、使用反馈、情景记忆和 CRUD/导出 API。
2026-08-24 在 Windows 开发环境完成分支级验证：后端完整测试集 83 项通过，前端正式
构建与服务端渲染测试通过；文件私有权限断言仅在可暴露 POSIX mode bits 的平台检查
`0600`，Windows 仍由应用层执行加密存储与受限文件创建逻辑。

后续使用固定数据集评估：Recall@K、知识更新、时间推理、跨会话推理、错误前提拒答、
跨 owner 泄漏、上下文 Token、P95 召回延迟和任务成功率。Embedding、LLM 后台摘要、
动态记忆建链和自主程序记忆均必须位于功能开关后，并通过当前基线对照后才进入默认
路径。

参考：

- [LangGraph Memory](https://docs.langchain.com/oss/python/concepts/memory)
- [LongMemEval](https://arxiv.org/abs/2410.10813)
- [LongMemEval-V2](https://arxiv.org/abs/2605.12493)
- [A-MEM](https://arxiv.org/abs/2502.12110)

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
