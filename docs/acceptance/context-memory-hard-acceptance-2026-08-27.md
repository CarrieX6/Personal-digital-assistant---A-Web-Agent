# Agent 上下文与记忆硬验收报告（2026-08-28）

记忆功能实现负责人：**Xianggang Ma**

## 结论

按既定九项硬标准复验，当前实现 **9/9 通过**。结论只覆盖本仓库当前 Agent、SQLite
持久层、Web/飞书入口和已注册写工具；以后新增渠道、模型或有副作用工具时，必须重新执行
相同门禁，不能继承本报告结论。

## 逐项证据

| 编号 | 结果 | 实现与直接证据 |
|---|---|---|
| 1. 500～2000 轮早期事实召回 | 通过 | `glm-4.6v` 真实模型分别完成 500、1000、2000 轮递归压缩；三次均召回首轮验收代号，同时保留 SQLite 决定、崩溃矩阵待办、最近 20 条消息和有效 provenance。证据见 `evidence/real-model-context-500-1000.json` 中 500 轮 session、`evidence/real-model-context-1000.json`、`evidence/real-model-context-2000.json`。 |
| 2. 否定、改口、时间变化、同名任务 | 通过 | claim 层使用 owner/project/entity/predicate 的盲索引键、显式 polarity、valid_from/valid_to、status 与 supersedes_id；否定写 active tombstone，改口关闭旧版本，同名任务按 project scope 隔离。对应 correction、negation、temporal fact、same-task/project 测试通过。claim 明文不复制到公开 metadata。 |
| 3. 多项目、多线程、跨渠道身份连续性 | 通过 | `project_id` 已贯穿 thread、run、checkpoint、tool context、memory context 与查询；线程不能换 project 重开。Web/飞书入口先解析显式 verified identity link，未提供解析器时使用渠道限定 alias，不自动合并身份。项目召回、线程冲突、verified alias 跨渠道连续性及其他 owner 隔离测试通过。 |
| 4. 每个图节点前后崩溃恢复 | 通过 | plan、policy、approval、execute_tool、observe、decide、finalize、fail 八节点统一包装 before/after failpoint；16 个边界均在独立子进程中 `os._exit(91)`，重开 SQLite 后恢复到规范化 completed/failed 状态，矩阵 16/16 通过。 |
| 5. 非幂等副作用不重复 | 通过 | 工具账本先持久化稳定 idempotency key；不支持下游幂等的写操作在 post-effect/pre-commit 窗口恢复为 `ambiguous_side_effect`，绝不自动重发。当前真实写适配器空间照片与图片风格化均透传幂等键，并用 owner+key 确定性派生 asset/job（风格化同时派生 seed）；直接重放测试各只产生一个资产/任务。 |
| 6. 实际发送请求不越窗 | 通过 | 上下文默认窗口增至 98,304，输出预留 16,384。每次 HTTP 调用前对最终完整 JSON（messages、tools、tool choice、视觉引用和输出预留）做 ASCII 序列化字节上界门禁，越界在 transport 前拒绝；摘要原始消息最多 240 条/请求并继续分块。真实 500/1000/2000 轮最终请求分别为 8,483/15,749/7,706 输入上界，加 16,384 输出预留，均小于 98,304。 |
| 7. 每个摘要决定/任务可追溯 | 通过 | `session-summary-v3` 为每个非空 summary/decision/open_loop/blocker 等项保存 source message IDs、role 与 SHA-256；写入时只接受同 owner/thread 的已存在消息并重算 hash。三次真实模型报告的 `provenance_valid` 均为 true；存储重开和篡改检测测试通过。 |
| 8. owner 泄漏为 0 | 通过 | 128-owner 对抗召回矩阵 leakage=0；memory/thread/run/asset API 和 checkpoint 均验证 owner；语义相似度不能绕过 owner、scope、project、status、有效期及 retrieval_policy 硬过滤。 |
| 9. 新旧机制先 shadow 后切换 | 通过 | 默认 `auto` 对同一输入同时运行 legacy/candidate，写入不可变输入 hash、版本、通过位与指标；达到 20 个样本且通过率不低于 0.95 前只服务旧路径，达标后才自动服务结构化摘要/hybrid retrieval。`shadow` 模式永不切换。摘要和召回均有“首样本保持 baseline、达标后切换”测试。 |

## 真实模型结果

| 轮数 | Provider | 摘要调用批次 | 早期事实 | 决定 | 待办 | Provenance | 完整请求上界 |
|---:|---|---:|---|---|---|---|---:|
| 500 | `llm-tool-schema:glm-4.6v` | 3 | 通过 | 通过 | 通过 | 通过 | 8,483 + 16,384 / 98,304 |
| 1000 | `llm-tool-schema:glm-4.6v` | 5 | 通过 | 通过 | 通过 | 通过 | 15,749 + 16,384 / 98,304 |
| 2000 | `llm-tool-schema:glm-4.6v` | 10 | 通过 | 通过 | 通过 | 通过 | 7,706 + 16,384 / 98,304 |

一次 1000 轮运行曾因模型返回无效 tool_calls 而 fallback，随后暴露出下一批摘要越窗和早期
事实丢失。失败原始报告保留在 `evidence/real-model-context-500-1000.json`。修复包括：单次摘要
源消息硬上限与连续分块、fallback 优先保护既有摘要、显式决定/待办/阻塞安全层，以及严格的
逐消息完成态匹配。修复后的 1000 轮报告为 `evidence/real-model-context-1000.json`。

## 回归记录

以下是 2026-08-28 专项验收时的历史记录：

- 上下文/记忆/编排/Token/API/飞书相关套件：143 passed；
- 自动 shadow 门禁与记忆层：41 passed；
- 最终摘要故障链最小回归：3 passed；
- 两个真实写适配器幂等重放：空间照片与图片风格化分别通过；
- 八节点 before/after 独立进程崩溃矩阵：16 passed；
- 真实模型 500、1000、2000 轮：全部通过。

依赖库仅报告既有弃用警告。按要求未继续运行与本次上下文/记忆变更无关的测试。

### 2026-08-31 合并回归

在把分层记忆分支整合到最新 `main`（包含飞书媒体、空间照片、图片风格化、身份与
workspace 隔离）后，重新执行仓库级回归：

- 后端全量测试：186 passed，3 个既有依赖弃用警告；
- 前端生产构建：通过；
- 前端服务端渲染测试：1 passed；
- 合并冲突、补丁空白和 Python 编译检查：通过；
- 额外覆盖稳定 workspace 密钥标识与旧路径 keyring 密钥迁移，避免移动仓库后历史记忆无法解密。

本节只证明本次合并没有破坏已覆盖功能；真实手机飞书、断网恢复、Windows GPU 和固定域名
Viewer 仍需按项目看板执行设备级验收。

## 运行约束

1. 生产环境保持 `AGENT_SESSION_SUMMARY_MODE=auto`、
   `AGENT_MEMORY_RETRIEVAL_MODE=auto`；直接指定 `structured`/`hybrid` 会显式绕过 rollout
   门禁，只允许专项验收或已审批迁移使用。
2. 新模型必须配置准确的窗口与输出预留；最终请求硬门禁不可关闭。
3. 新增任何 `local_write`/`external_write` 工具时，必须声明真实幂等能力：要么透传稳定
   幂等键并补 post-effect 崩溃测试，要么标记非 retry-safe，让 ambiguous 状态转人工核对。
4. identity link 只能由已认证绑定流程写入；不得按姓名、邮箱文本或相似内容自动合并 owner。
