# 从零到可运行个人数字助手：学习与实战路线

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：路线已审查，阶段实现待推进  
关联任务：`LEARN-001`、`AGENT-001`、`CHANNEL-001`、`SEC-001`

本文把知识库中的“学什么”连接到“改哪些代码、如何验收”。它不是另一份技术
大纲：每个阶段都必须产生可运行交付物、测试证据或明确的研究结论，完成后才进入
下一阶段。

## 1. 使用方式

每个阶段遵循同一个循环：

```text
阅读最少前置 → 画清输入输出 → 实现最小切片
            → 自动测试 → 人工链路验证 → 记录证据/缺口
            → 需要选型时做 Experiment 和 ADR
```

不要先把所有框架、协议和模型学完。先完成当前阶段的验收，再按遇到的问题回到
知识章节深入阅读。

## 2. 当前基线：已经有什么，不能误认为有什么

| 模块 | 当前事实 | 仍缺少 |
| --- | --- | --- |
| Web UI | 已有 Agent 控制台、Root 待审批队列、服务端会话历史、供应商设置、图片上传、空间照片 Viewer | 身份登录、审批历史和记忆管理 UI |
| LLM Adapter | `backend/app/llm.py` 可调用 OpenAI-compatible Chat Completions | 多供应商能力探测、流式事件、统一错误分类和回归评测 |
| Agent | `backend/app/orchestration.py` 已实现 LangGraph 单工具循环、Policy、observe/replan、硬预算、Interrupt、人工审批、持久化 Run 和幂等账本 | 运行中恢复、强制超时、审批过期、Outbox 和费用预算 |
| Tool Registry | `backend/app/tools.py` 可注册 Tool、导出并在调用前校验基础 Schema | 角色权限、超时、取消、副作用等级和版本 |
| Job/Asset | `backend/app/assets.py` 支持空间照片专用任务和本地资产 | 通用持久队列、重启恢复、Outbox 和资产授权 |
| 设置与 Secret | `backend/app/settings.py` 支持 UI 配置和系统钥匙串/加密文件 | 多 Provider 独立凭证、轮换、审计和能力探测 |
| Channel Gateway | 已有飞书长连接、白名单、去重、文本/单图/卡片 MVP | 统一 Adapter、统一消息、跨渠道身份绑定、Outbox 和多媒体回传 |
| Memory | 已有 owner/channel/thread 隔离的 SQLite 会话、消息 API 和显式长期记忆 | 飞书消息统一迁移、摘要、检索、导出、加密与保留策略 |

这张表以当前代码为准。文档里的目标架构和“框架支持某能力”不等于项目已经实现。

## 3. 阶段 0：能独立运行和定位当前项目

### 学习

- [项目工程基础](foundations/00-project-engineering.md)；
- 根目录 [README](../../README.md) 的启动、测试和数据目录；
- [系统架构](../architecture/system-architecture.md) 的当前代码映射。

### 实践

1. 启动 FastAPI 和前端；
2. 在无 API Key 的演示模式完成一次文本工具调用；
3. 上传一张测试图并创建空间照片 Job；
4. 运行后端测试、前端 lint 和前端测试；
5. 能用 `run_id`、`job_id`、日志和浏览器网络面板定位一次请求。

### 验收

- 新环境可以按照 README 从零启动；
- 三组测试命令通过，或把环境相关失败记录为可复现 Issue；
- 能指出数据、配置、Secret、Trace 和生成资产分别存在哪里；
- 没有把个人图片、Key、数据库或模型权重加入 Git。

## 4. 阶段 1：把 Tool Contract 做成可靠边界

### 学习

- [LLM 应用与安全基础](foundations/03-llm-security-basics.md)；
- [MCP、A2A、AG-UI、评测与可观测性](web-agent/04-protocols-evaluation-security.md)
  中的 Capability/Tool Contract。

### 实现

- 为 Tool 增加版本、输入/输出 Schema、风险等级、超时和幂等声明；
- 在 Tool 执行端进行统一 Schema 验证，不能只信模型；
- 统一 `ToolError`：参数错误、权限拒绝、超时、暂时失败和永久失败；
- 用 Fake Tool 覆盖成功、无效参数、异常、超时和重复调用。

### 验收

- 模型给出未知字段、越界值或未知 Tool 时安全失败；
- Tool 不接受任意本地路径，只接受受控 Asset ID；
- Tool Contract 有 Contract Test；
- 交付物关联 `CAP-001`、`SEC-001`。

## 5. 阶段 2：实现可测试的 Agent Loop

### 学习

- [Agent Loop、架构模式与 Single/Multi-Agent](web-agent/01-agent-loop-architectures.md)；
- [Agent 框架现状、优缺点与选型方法](web-agent/03-frameworks.md)。

### 实现

先在现有自研基线上实现：

```text
model → tool calls → validate/policy → execute
      → observations → model → … → final/stop
```

- 设置最大步数、总时间、Token/费用和工具调用预算；
- 支持一个 Tool 的失败被模型观察，但不允许无限重试；
- Trace 记录模型调用、Tool、错误分类和停止原因；
- 使用 Fake Model 测确定性轨迹，再用真实供应商做兼容性实验。

### 验收

- 至少覆盖零工具、单工具、多轮工具、工具失败、达到预算五种路径；
- 真实 Provider 结论进入 `LLM-001` 实验，不写成框架固有事实；
- 此时再决定继续自研，还是对比 LangGraph/Pydantic AI；
- 交付物关联 `AGENT-001`、`LLM-001`、`EVAL-001`。

## 6. 阶段 3：会话状态与最小记忆

### 学习

- [可靠性、任务状态与数据存储](foundations/02-reliability-data.md)；
- [Agent 调度、Memory 与 Durable Execution](web-agent/02-orchestration-memory-durable.md)。

### 实现

- 继续把现有 `agent_threads`、`agent_messages` 扩展为完整的 User、ChannelIdentity、Conversation、Run、Message 模型；
- 区分当前 Run 状态、会话历史、任务记录和长期偏好；
- 长期偏好只在明确规则或用户确认后写入；
- 提供查看、纠正、删除和导出；
- 先使用 SQL/FTS；只有固定数据集证明需要时再增加向量检索。

### 验收

- 重启后可恢复会话索引，但不会自动重放副作用；
- 不同用户无法读取彼此消息、资产或记忆；
- 删除和导出流程有测试；
- 交付物关联 `MEMORY-001`、`DATA-001`。

## 7. 阶段 4：通用可恢复 Job 与 Outbox

### 学习

- [可靠性、任务状态与数据存储](foundations/02-reliability-data.md)；
- [多模态结果回传、3D 预览与可靠性](external-control/03-preview-reliability.md)。

### 实现

- 把空间照片专用任务抽象为通用 Job 状态机；
- 持久化 Job、Attempt、Progress、Result Asset 和 Outbox；
- Worker 使用租约/心跳，支持取消、超时、重试和崩溃恢复；
- 副作用通过幂等键和 Outbox 投递；
- 先用 SQLite 单机实现，确认并发需求后再评估外部队列。

### 验收

- 杀掉 Worker 后重启，不产生两个结果或重复外发；
- 取消、不可重试失败、重试耗尽都有明确终态；
- UI 可以从持久状态恢复进度；
- 交付物关联 `JOB-001`、`QA-001`。

## 8. 阶段 5：飞书文本闭环

### 学习

- [Channel Gateway、飞书与微信生态接入](external-control/01-channel-gateway-feishu-wechat.md)；
- [身份、认证、授权与设备绑定](external-control/04-identity-auth.md)；
- [网络部署与移动端边界](external-control/02-deployment-mobile.md)。

### 实现

- 定义 `InboundMessage`、`OutboundMessage` 和 `ChannelAdapter`；
- 使用飞书企业自建应用长连接接收文本事件；
- 验证应用配置，绑定允许的用户，按平台事件 ID 去重；
- 快速 ACK，把执行移交给 Job/Agent；
- 通过 Outbox 返回文本结果和可追踪错误码。

当前进度（2026-07-29）：已用独立 `lark-channel-sdk` 完成长连接本地实现，包含 UI
配置、安全凭证、Open ID 白名单、群聊 @ 策略、SQLite 持久去重、后台 Agent 调用和
文本回复。当前仍直接回复飞书，尚未抽象持久 Outbox；真实账号、应用权限、断线重连
和手机端端到端验收未完成。

### 验收

- 手机飞书发送文本，电脑执行，结果返回同一会话；
- 重复事件只创建一个 Run/Job；
- 非绑定用户和无权限群聊不能调用；
- 断网重连后状态可解释；
- 交付物关联 `CHANNEL-001`、`SEC-001`。

## 9. 阶段 6：图片、空间照片与 3D 结果回传

### 学习

- [多模态结果回传、3D 预览与可靠性](external-control/03-preview-reliability.md)；
- [Capability、资产与软件/模型供应链](product-engineering/01-capability-assets-supply-chain.md)。

### 实现

- 下载用户附件到隔离区，校验类型、尺寸、解码和所有权；
- 调用现有空间照片 Capability；
- 回传封面图或演示视频，并提供短时、只读、单资产 Viewer URL；
- Viewer 不可直接访问磁盘路径；链接可撤销、有访问日志；
- 3D 不可用时降级为视频、图片或文件。

当前进度（2026-07-29）：已实现飞书单张图片资源下载，直接复用
`SpatialSceneService` 的 20MB、格式、像素、解码、EXIF 和缩放校验；图片消息会创建
空间照片任务，轮询本地状态，并在完成后返回文本与封面图。多图、文件/视频、任务
卡片、MP4 演示和带认证的移动端 Viewer 尚未实现。

### 验收

- 手机完成“上传图片 → 生成 → 预览/下载”全链路；
- 链接过期、越权、撤销和重复投递均有测试；
- 记录飞书格式、大小和超时的真实实验结果；
- 交付物关联 `CHANNEL-002`、`PREVIEW-001`。

## 10. 阶段 7：审批、安全与数据治理

### 学习

- [数据治理、权限、安全与技术决策](product-engineering/02-data-security-governance.md)；
- [LLM 应用与安全基础](foundations/03-llm-security-basics.md)。

### 实现

- 建立 `user × capability × action × resource × channel × risk` Policy；
- L2/L3 动作绑定精确参数、审批人、过期时间和一次性 nonce；
- 对附件、Tool Result、MCP Server 做不可信输入处理；
- 增加审计、Secret 脱敏、速率限制和 emergency stop；
- 完成威胁模型与攻击用例。

### 验收

- Prompt 无法改变 Tool 端权限；
- 修改审批后的参数会使审批失效；
- 间接 Prompt Injection、SSRF、路径穿越和重放测试通过；
- 交付物关联 `SEC-001`、`SEC-002`。

## 11. 阶段 8：Capability 功能库

### 学习

- [Capability、资产与软件/模型供应链](product-engineering/01-capability-assets-supply-chain.md)。

### 实现

- 定义 Manifest v1、兼容范围、权限和资源预算；
- 下载后校验来源、hash、许可和锁文件；
- 环境隔离、smoke test、启用、升级、回滚和卸载分步执行；
- 把现有空间照片迁移为第一项可管理 Capability；
- MCP 只作为可选 Adapter，内部业务边界不依赖 MCP。

### 验收

- 安装失败不破坏旧版本；
- 卸载功能不会未经确认删除用户资产；
- Capability 被禁用后不能再由 Agent 调用；
- 交付物关联 `CAP-001`、`CAP-002`。

## 12. 阶段 9：评测、可观测性、性能与发布

### 学习

- [性能、能耗、测试、运维与 Human-Agent UX](product-engineering/03-performance-testing-operations-ux.md)；
- [调研证据与文档维护方法](research-quality.md)。

### 实现

- 固定 Agent 任务集、攻击集、恢复集和多模态链路集；
- 用统一 ID 串起 channel event、run、tool、job、asset 和 outbound；
- 报告成功率、P50/P95、内存、功率、总能量、恢复率和结果质量；
- 增加常驻服务、健康检查、日志轮转、备份和回滚；
- 研究结论经 Experiment 后进入 ADR。

### 验收

- 新版本能与基线自动比较；
- 失败能定位到具体链路，不依赖暴露私密原文；
- 至少完成断网、重启、重复事件、GPU OOM 和磁盘不足演练；
- 交付物关联 `EVAL-001`、`PERF-001`、`QA-001`。

## 13. 何时学习 Multi-Agent、MCP、A2A 和新框架

- Single Agent 无法清晰隔离上下文、权限或并行专业任务时，再做 Multi-Agent 实验；
- 第三方工具需要跨客户端发现和调用时，再接 MCP；
- 出现独立、跨进程或跨设备 Agent 委派时，再接 A2A；
- Web/Companion UI 需要统一 Agent 事件协议时，再评估 AG-UI；
- 当前自研基线无法满足恢复、HITL 或维护成本时，再做框架迁移 ADR。

“学过协议”不构成引入理由；固定 PoC 在质量、复杂度或维护性上有可重复收益才构成。

## 14. 阶段完成记录

每个阶段在[调研与实现中心](../project-board.md)更新：

- 任务 ID、负责人和日期；
- 阅读材料和问题；
- 代码/测试入口；
- 实验原始数据；
- 已证实与未证实结论；
- ADR 或不需要 ADR 的理由；
- 下一阶段的阻塞项。

## 15. 来源与项目依据

- [当前项目 README](../../README.md)
- [个人数字助手系统架构](../architecture/system-architecture.md)
- [当前 Agent Runner](../../backend/app/agent.py)
- [当前 LLM Adapter](../../backend/app/llm.py)
- [当前 Tool Registry](../../backend/app/tools.py)
- [当前设置与 Secret 存储](../../backend/app/settings.py)
- [Agent Loop 与架构模式](web-agent/01-agent-loop-architectures.md)
- [Channel Gateway 与平台接入](external-control/01-channel-gateway-feishu-wechat.md)
- [数据、安全与技术决策](product-engineering/02-data-security-governance.md)

返回[知识库总导航](README.md)。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
