# 分层、证据化与时态化 Agent 记忆

实现与文档作者：**Xianggang Ma**
更新日期：2026-08-27
关联任务：`MEMORY-001`、`MEMORY-002`

## 1. 结论

项目在原有 owner/thread 隔离、显式长期记忆和 LangGraph Checkpoint 基础上，采用
“工作记忆、短期会话记忆、语义长期记忆、情景记忆、程序性记忆”五层结构。长期
记忆不再按更新时间整表注入，而是先执行作用域与有效期过滤，再综合查询意图、词项、
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
- `retrieval_policy`：`always`、`explicit_only` 或 `never`，作为相关度评分之前的硬门禁；
- `valid_from/valid_to/status`：有效期与 active、superseded、archived 等状态；
- `topic_key/supersedes_id`：确定性识别的同主题更新关系；
- `last_accessed_at/access_count/utility_score`：召回和任务结果反馈；
- `metadata_json`：工具、任务或资产等受控扩展字段；复合陈述拆分后只保存原文哈希、
  原子序号和分组 ID，不在元数据中复制整段原文。
- `content_ciphertext/content_nonce/encryption_version/key_id`：AES-256-GCM 正文密文；
- `metadata_ciphertext/metadata_nonce`：用户可写扩展元数据密文；`normalized_content`
  在加密模式下保存 HMAC-SHA256 盲索引，只用于精确去重，不包含可逆正文。

`agent_memories` 现在保存可检索的 claim，而不是把“正文”和“证据”混为一个字段。
每次显式保存、消息强化、Run 派生或人工修订都会在 `agent_memory_evidence` 写入独立
evidence：`source_type/source`、`source_message_id/source_run_id`、`observed_at`、
`confidence`、内容哈希与加密证据摘录。相同 claim 被不同消息再次确认时保留同一个
memory ID，但增加证据记录；召回先验会使用有上限的 evidence support bonus，且上下文
只注入证据数量和来源引用，不把全部证据摘录重复塞入提示词。Root 可通过
`GET /api/memories/{memory_id}/evidence` 审计证据链。

`agent_memory_events` 只记录事件类型、内容哈希和非敏感元数据，不复制记忆正文。
永久删除一条记忆时同步删除其事件与使用记录，避免“逻辑删除后仍可恢复正文”。

## 4. 写入与更新

### 4.1 显式写入

用户发送“记住：……”或通过记忆中心创建记忆时立即保存。保存前拒绝 API Key、
访问令牌、密码和验证码特征。类型可以由用户选择，也可以使用确定性规则推断。

写入前会保守识别由逗号、分号或句号连接的稳定资料、偏好、任务状态和流程。只有每个
分句都能恢复为独立陈述时才拆分；例如“我叫林舟，住在北京，喜欢简洁回答”会形成
三条可分别召回和更新的记录。无法完整识别时保留原文单条写入，避免静默丢失语义。

`remember_many()` 在同一个 SQLite 连接和事务中完成全部原子记录、时态取代和审计事件；
任一分句失败会回滚整批写入。原子分组 ID 由 owner、来源证据和原始内容确定性生成，
相同请求重放返回同一组记录，不重复产生副作用。

长期记忆正文和 metadata 默认使用 AES-256-GCM 加密，每次写入使用独立随机 nonce，
AAD 绑定 owner、memory ID 和字段名。短期侧对会话标题、消息正文与 metadata、结构化
摘要，以及可恢复 Run 的请求、审批和响应 JSON 使用整对象加密；AAD 同时绑定记录类型、
owner 和记录 ID。调度所需的 role、status、channel、时间、外键和索引仍保持结构化明文。
密钥优先进入操作系统钥匙串；钥匙串不可用时才回退到权限受限的 `*.memory-key` 文件。
钥匙串服务名由稳定的 `AGENT_WORKSPACE_ID` 派生，不依赖仓库绝对路径，因此移动项目目录
不会生成一把无法解密旧数据库的新密钥。迁移或备份必须同时保留数据库与对应密钥；多套
需要完全隔离的实例应配置不同的工作区 ID。
数据库敏感字段只保存空占位、密文、nonce、版本和 key ID。

### 4.2 时态更新

位置和明确的个人属性使用 `topic_key` 识别同一事实槽位。新事实写入时，旧记录进入
`superseded`，`valid_to` 指向新事实的生效时间，新记录保留 `supersedes_id`。不会
覆盖旧事实，因此可以解释当前值和历史值。

姓名、住址、生日、年龄、职业和联系方式使用独立 profile 槽位。启动迁移会按确定性
规则重新识别旧版 `fact` 中的个人资料，但不改写正文或来源；例如“我叫林舟”会迁移为
`profile/profile:name`，从而可在新的 thread 中按身份意图召回。

### 4.3 情景写入

包含工具调用或失败的 Agent Run 在终态后形成一条情景记忆，包括任务摘要、工具名、
结果与状态。普通闲聊不自动形成情景记忆。可能包含凭证特征的 Run 不写入派生记忆。

### 4.4 会话摘要

会话默认保留最近二十条未摘要消息，并同时受 `AGENT_RECENT_HISTORY_TOKENS` 约束；任一
条件先达到都会触发滚动压缩。真实模型启用时，主路径通过强制
`save_session_summary` Tool Schema 合并旧状态和新增消息，输出摘要、决策、开放事项、
已完成动作、有效假设、产物引用、阻塞项和下一目标；字段集合、类型、条数和总 Token
预算均在写库前再次校验。模型不可用、拒绝结构化调用或输出校验失败时，才进入标记为
`fallback:extractive-v2` 的本地确定性兜底，不再把正则抽取伪装成主压缩机制。兜底优先
保护已有摘要；模型主路径另有只覆盖明确决定、待办和阻塞项的确定性遗漏保护层，它不生成
主摘要。一次最多压缩 240 条原始消息，积压消息继续分块，避免摘要请求自身逼近模型窗口。

模型调用在 SQLite 写锁外执行；写回前重新核对摘要版本、覆盖消息范围和消息内容指纹，
并使用 `last_message_id` 条件更新防止并发摘要覆盖新状态。`session-summary-v3` 为每个状态
项保存原始 message ID、role 和内容 SHA-256；读取时可逐项验证 owner/thread 归属、消息存在
及 hash。数据库记录 provider、schema 版本、fallback reason 和
`covered_from_message_id`，便于观测降级与恢复来源。摘要作为非可信 user message 中的
JSON 注入，不会成为 system message，也不会删除原始消息。

摘要与召回默认使用 `auto` rollout：同一不可变输入先运行 legacy/candidate 并记录输入 hash、
版本、差异指标和通过位；累计至少 20 个样本且通过率不低于 0.95 前只服务 legacy，达标后
才自动切换 candidate。`shadow` 模式只比较、永不切换。

## 5. 召回与上下文装配

召回步骤：

1. 先按 owner、scope、status 和有效期过滤，再执行 `retrieval_policy` 硬门禁；`never`
   永不进入模型上下文，`explicit_only` 只有在明确询问对应资料槽位、记忆类型或全部
   已保存记忆时才允许进入候选；private/sensitive 默认使用 `explicit_only`；
2. 默认加密模式先按结构化字段取得 owner 范围内的受控候选，在内存中解密，再使用
   中文双字词、英文词项、少量领域同义词和受控查询意图评分；持久化 FTS 会被删除，
   避免索引旁路泄漏正文；
3. 身份、姓名、位置、年龄、生日、职业、联系方式、偏好、待办、流程和历史任务等
   明确意图可以召回对应类型或 profile 槽位，即使问法与正文没有字面重合；窄槽位查询
   不召回其他个人资料，宽泛身份查询默认排除 `sensitive` 记忆；新会话中的简单问候只
   允许引入 `normal` 级别的姓名或身份槽位，用于安全的基础个性化；
4. 其他非空请求仍必须存在有效词项或子串命中，无关记忆即使重要度较高也不
   注入，避免把无关敏感信息发送给模型；
5. 综合意图、词项重合、时间衰减、重要度、可信度、证据支持数、效用和作用域打分；
6. 去除重复内容，最多向上下文交付配置数量的记录；
7. 只为真正进入上下文的记忆记录本次 Run、排名和使用次数，不记录提示词原文。

启用 `AGENT_MEMORY_SEMANTIC_ENABLED` 后，召回升级为混合 v2。owner、scope、status、
有效期和 `retrieval_policy` 仍先执行硬过滤；通过门禁的候选分别进入槽位意图、词法和
本地语义三路排名，使用 RRF 消除分数尺度差异，再把时效、重要度、可信度、效用和
作用域作为先验重排，最后用 MMR 抑制近重复证据。纯语义候选只有达到
`AGENT_MEMORY_SEMANTIC_THRESHOLD` 才能进入融合，语义相似度不能绕过敏感资料门禁。

零下载基线 Provider 使用确定性的本地哈希向量，组合字符特征与可审计概念映射。
生产语义 Provider 可切换为 `ibm-granite/granite-embedding-97m-multilingual-r2`：服务只从
显式准备的本地目录加载固定 revision，设置 `local_files_only=True`、
`trust_remote_code=False`，采用 CLS Pooling、L2 归一化和 384 维向量。Query 与 Document
编码接口保持分离；当前 Granite 不添加前缀，后续更换指令模型时无需修改召回核心。

本地模型目录必须带 `embedding-manifest.json`，记录 revision、文件大小、SHA-256、
Pooling、最大长度、分块重叠和安全加载策略。清单指纹与有效运行参数进入 model ID；模型、
权重、Pooling 或截断参数变化都会让旧向量自动失效。超过运行时长度的文本按 Token 分块，
块向量按有效 Token 数加权聚合并再次归一化。

向量存入 `agent_memory_embeddings`，包含 model ID、维度、HMAC 内容指纹、密文、nonce、
版本和 key ID；不建立明文 ANN。模型推理在 SQLite 写锁外完成，写入前校验记忆快照仍为
当前版本，避免并发更新重新插入旧向量。首次查询按 owner 对缺失或过期向量懒生成；批量
回填只处理 `always` 策略，`explicit_only` 保持按明确意图懒生成，`never` 永不生成。
记忆更新、取代、归档和删除会使旧向量立即失效。查询时只解密已通过硬门禁的 owner 候选
并在内存计算余弦相似度。可选 Provider 失败时自动退回结构化与词法召回；将
`AGENT_MEMORY_EMBEDDING_REQUIRED=true` 后则在启动时加载自检并对错误快速失败。
`AGENT_MEMORY_SEMANTIC_THRESHOLD` 留空时，Hash 基线默认 0.24，Transformer 默认 0.80；
后者来自当前固定评测集的误召回校准，扩充业务数据后仍需重新评测阈值。

模型准备和历史回填均为显式运维动作，应用运行时不会下载权重：

```powershell
python backend/scripts/prepare_memory_embedding_model.py --download --smoke-test

$env:AGENT_MEMORY_SEMANTIC_ENABLED = "true"
$env:AGENT_MEMORY_EMBEDDING_PROVIDER = "transformer"
$env:AGENT_MEMORY_EMBEDDING_MODEL_PATH = "backend/models/embeddings/granite-embedding-97m-multilingual-r2"

python backend/scripts/backfill_memory_embeddings.py `
  --model-path backend/models/embeddings/granite-embedding-97m-multilingual-r2 `
  --all-owners
```

上下文预算优先保证系统策略、工具 Schema 和当前请求。剩余预算为会话摘要和长期记忆
保留独立份额，再放入最近原始消息，避免短期历史完全挤掉高价值长期记忆。预算指标
分别记录当前请求、摘要、长期记忆、原始历史、被丢弃条数和被截断条数；无法完整放入
的最近超大消息会保留带省略号的前缀，而不是整条静默消失。超长当前请求会为已召回的
摘要和长期记忆保留受控预算，并同时保留请求首尾，避免丢失末尾的最终指令。最终顺序：

```text
可信附件元数据（视觉原图仅作为当前请求的瞬时输入，不进入检查点或记忆）
→ 非可信会话摘要 / 决策 / 开放事项 / 完成项 / 假设 / 产物 / 阻塞 / 下一目标
→ 非可信长期记忆证据
→ 最近原始消息
→ 当前用户请求
```

滚动摘要覆盖到的消息不会再作为原始历史重复注入。当前图片问答可在最近原始会话窗口
内复用上一组仍有效的暂存图片进行连续追问；图片二进制不进入摘要、长期记忆、Run
Trace 或 LangGraph Checkpoint，摘要只保留用户与助手的文本语义。

装配前还会对当前请求、会话摘要、结构化状态、最近消息和长期记忆执行确定性跨层去重；
已经在更近证据中完整出现的长期记忆不再重复注入。存在相关度分数时，长期记忆按“相关
价值/Token 成本”选择，使紧张预算优先容纳短而高价值的证据，而不是被单条长记忆占满。

Token 预算不再固定用 UTF-8 字节数估算。`AGENT_TOKENIZER_BACKEND=auto` 默认只读加载
项目内 Embedding 模型的 Hugging Face Tokenizer（不加载权重、不联网、不执行远程代码），
并在预算指标中记录 tokenizer ID；只有本地 tokenizer 不可用时才显式回退
`heuristic:utf8-bytes-v1`。生产环境可设为 `transformers`，使缺失 tokenizer 直接启动失败。
所有消息、JSON Tool Schema、摘要和首尾保留截断均使用同一个 TokenCounter。

## 6. 结果反馈

被注入某个 Run 的记忆写入 `agent_memory_usage`。Run 完成时小幅提高其效用，失败时
降低效用；分值只影响排序，不会自动删除或改变记忆正文。此机制是第一版可解释反馈，
后续应结合用户纠正、工具成功率和对照实验调整权重，不能把“Run 完成”直接等同于
“记忆正确”。

## 7. 用户控制与 API

Web 记忆中心支持原子化新增、搜索、按类型筛选、编辑重要度与召回方式、归档、永久
删除和 JSON 导出。
API：

- `GET/POST /api/memories`；
- `POST /api/memories/atomic`（兼容保留原单条创建接口）；
- `PUT/DELETE /api/memories/{memory_id}`；
- `GET /api/memories/{memory_id}/evidence`；
- `GET /api/memories/export`。

当前 Web 产品仍是本机 Root 视图，接口使用 `local` owner。飞书用户继续通过自然语言
管理自己的 owner-scoped 记忆。`agent_identity_links` 已提供经外部认证后写入的显式
身份映射基础，但记忆层不会自行合并不同渠道 owner；调用方必须先完成账号绑定并主动
选择解析后的 subject。远程记忆管理仍依赖 `AUTH-001` 和 `MESSAGE-001`，本任务不扩大
未认证 Web API 的可见范围。

## 8. 兼容迁移与降级

- 启动时使用增量 `ALTER TABLE` 为旧 `agent_memories` 补列，原字符串记忆迁移为
  active fact，并保留原 ID、正文、来源和时间；缺少证据的旧 claim 会生成一条
  `legacy_claim` evidence，不改写 claim；
- 旧 private/sensitive 记录首次增加 `retrieval_policy` 列时迁移为 `explicit_only`，
  normal 记录保持 `always`；旧会话摘要新增结构化字段时使用空值兼容，不重写旧摘要；
- 启动时先完成旧类型和 profile 槽位识别，再在单事务中加密旧正文与 metadata、替换
  标准化明文为盲索引并删除旧 FTS；同一迁移还会加密旧会话标题、消息、摘要和 Run
  载荷，随后执行 WAL 截断和 `VACUUM` 清理可回收页；
- 外部 Secret 管理的 32 字节密钥可通过 `rotate_encryption_key(new_key)` 轮换。所有长期
  和短期密文、claim evidence、metadata、语义向量及盲索引在同一 SQLite 事务内重加密，异常时数据库和当前进程
  密钥一起保持旧值；调用成功后才能更新部署 Secret。自动生成的系统钥匙串/文件密钥
  暂不允许在线轮换，避免数据库提交与密钥提供者更新之间出现不可恢复窗口；
- 只有显式关闭记忆加密时才允许使用 FTS5；默认加密路径不建立持久化全文索引；
- 旧版 `fact` 会在启动时按确定性规则补齐可识别的类型和 profile 槽位，原始 ID、正文、
  来源、有效期和审计记录不变；
- 不引入必须下载的 Embedding 模型。后续可以在固定评测证明收益后增加本地
  Embedding Provider，而不改变 MemoryRecord 和上下文接口；
- 删除会话不会删除长期记忆、任务资产或飞书原消息；清空长期记忆会删除对应索引和
  使用记录。

## 9. 验证与后续实验

当前自动测试覆盖：旧库迁移、原子化写入、类型化写入、时态取代、硬召回策略、跨
owner 隔离、跨 thread 身份召回、profile 槽位防误召回、敏感信息拒绝、结构化会话
状态、消息数与 Token 双阈值摘要、超大最近消息截取、混合召回、使用反馈、情景记忆、
长短期静态加密、旧短期表迁移、错误密钥失败和密钥轮换中断回滚，以及 CRUD/导出 API。

2026-08-28 的硬验收结果、真实模型 500/1000/2000 轮报告和故障修复记录见
`docs/acceptance/context-memory-hard-acceptance-2026-08-27.md`；不再在架构文档维护容易
失真的测试总数。

固定离线集位于 `backend/tests/fixtures/memory_eval_cases.json`，执行
`python backend/scripts/evaluate_memory.py` 输出 Recall@K、泄漏数量、拒召准确率、
P50/P95 本地召回延迟和逐例
结果，并在缺失预期记忆或发生泄漏时返回非零退出码。该基线不调用外部模型，可直接
加入 CI。

当前字段加密覆盖 `agent_memories`、`agent_memory_evidence`、`agent_threads.title`、`agent_messages`、
`agent_session_summaries`、`agent_runs` 的用户载荷和 `agent_memory_embeddings` 向量，
不覆盖 LangGraph Checkpoint、
`runs.jsonl` 执行轨迹、渠道消息镜像和用户主动导出的 JSON；生产部署仍应启用操作系统
磁盘加密、账号权限和日志保留策略。文件密钥降级只能防止“单独复制数据库”后直接读取，
不能抵抗数据库和密钥文件被同时复制。Checkpoint/轨迹信封加密、托管密钥的崩溃一致
轮换和大规模隐私保护 ANN 仍需后续实现。

后续使用固定数据集评估：Recall@K、知识更新、时间推理、跨会话推理、错误前提拒答、
跨 owner 泄漏、真实 Token 预算、摘要忠实度、状态恢复准确率、P95 召回延迟和任务成功率。
动态记忆建链和自主程序记忆仍必须位于功能开关后，并通过当前基线对照后才进入默认路径。

参考：

- [LangGraph Memory](https://docs.langchain.com/oss/python/concepts/memory)
- [LongMemEval](https://arxiv.org/abs/2410.10813)
- [LongMemEval-V2](https://arxiv.org/abs/2605.12493)
- [A-MEM](https://arxiv.org/abs/2502.12110)
- [OpenAI Responses API：对话状态与上下文管理](https://developers.openai.com/api/reference/cli/resources/responses/methods/create)
- [OpenAI 模型指南：结构化输出与 Prompt Caching](https://developers.openai.com/api/docs/guides/latest-model?model=gpt-5.5)

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
