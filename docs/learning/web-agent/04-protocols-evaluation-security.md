# MCP、A2A、AG-UI、评测与可观测性

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：已核对，协议采用待 ADR  
关联任务：`LEARN-AGENT-001`、`CAP-001`、`EVAL-001`、`SEC-002`

## 1. 三类协议解决三个不同边界

| 边界 | 协议 | 主要对象 |
| --- | --- | --- |
| Agent ↔ Tool/Data | MCP | tools、resources、prompts |
| Agent ↔ Agent | A2A | Agent Card、message、task、artifact |
| Agent ↔ User Interface | AG-UI | run、message、tool、state、UI events |

它们不是相互替代关系，也不是项目第一天必须全部实现。

## 2. MCP

MCP 使用 Host–Client–Server 架构：Host 管理一个或多个 Client，Client 与 Server
建立会话，Server 暴露能力
([MCP Architecture](https://modelcontextprotocol.io/docs/learn/architecture))。
本文核对的正式协议修订为 `2025-11-25`；实现时应固定 revision，并在升级前核对
授权、传输和 Schema 变化
([MCP Specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25))。

MCP 适合把已有 Capability 暴露为可发现工具，但协议不会自动提供：

- 本项目用户与资源授权；
- Tool 是否安全；
- 安装包签名与沙箱；
- 高风险动作审批；
- GPU 资源调度；
- Job 的产品状态和资产生命周期。

建议：先定义内部 Capability Manifest 与稳定 Tool Contract；需要跨 Agent 客户端
复用时再增加 MCP Adapter，避免业务逻辑只存在 MCP Server 内。

## 3. A2A

A2A 是独立 Agent 系统的互操作协议。最新官方规范使用 Agent Card 描述能力和安全
要求，以 Message 发起交互，以有状态 Task 管理长任务，以 Artifact 表达结果，并支持
流式和 Push Notification
([A2A Specification](https://a2a-protocol.org/latest/specification/))。

它适合未来“手机/云端 Agent 把任务委派给电脑端 Agent 节点”，但首期同一进程内的
模块调用不需要 A2A。引入时要解决：

- Agent Card 的可信发现和版本；
- 每个远端 Agent 的认证、授权和租户隔离；
- Artifact 的短期访问 URL；
- Task 取消、超时和状态映射；
- 远端 Agent 不应看到本地全部记忆或工具。

## 4. AG-UI

AG-UI 是 Agent 与用户前端之间的开放、事件驱动协议，可传递 Run 生命周期、文本、
Tool Call、状态快照/增量和自定义事件
([AG-UI Overview](https://docs.ag-ui.com/))。

它可作为未来 Web Console/Companion App 的参考，但飞书/微信本身有各自消息协议，
因此需要：

```text
内部 AgentEvent
  ├─ AG-UI Adapter → Web/Companion UI
  ├─ Feishu Adapter → 文本/卡片/图片/链接
  └─ WeChat Adapter → 平台支持的消息/链接/文件
```

不要把原始 Chain of Thought 作为 UI 事件；只显示可验证的计划、工具状态、进度和
结果。

## 5. Capability/Tool Contract

建议字段：

- 稳定 `capability_id` 和 semver；
- 输入/输出 JSON Schema；
- 同步/异步执行模式；
- 资源和平台要求；
- 风险级别和所需权限；
- 是否幂等、idempotency key；
- timeout、取消和进度事件；
- 错误码与是否可重试；
- 资产 MIME、大小和生命周期；
- 作者、来源、许可证、hash/signature。

Tool 描述用于帮助模型选择；Policy 使用机器字段授权。两者不能混为自然语言。

## 6. 评测：结果与轨迹都要测

Google ADK 官方将 Agent 评测拆为轨迹/工具使用和最终响应，并指出 LLM 的概率性使
传统确定性断言不足
([ADK Evaluation](https://adk.dev/evaluate/))。

项目指标分层：

### 决策与轨迹

- 意图/Capability 路由准确率；
- 正确工具选择率；
- 参数 Schema 一次通过率；
- 不必要工具调用数；
- 预算/停止条件命中；
- 高风险动作审批覆盖率。

### 最终任务

- 任务完成率；
- 结果是否可打开；
- 用户修改次数；
- 事实性与来源；
- 生成质量的专用指标（路线 E 后续定义）。

### 系统

- P50/P95 延迟、Token、API 成本；
- Queue wait、Worker time；
- 恢复、取消、重试成功率；
- 内存、显存、功率和总能量。

LLM-as-Judge 只能作为一个评估器：Judge 模型、Prompt 和顺序偏差要版本化，并用人工
标注校准。

## 7. 可观测性

一个用户请求至少关联：

```text
channel_event → request → agent_run → model_call/tool_call
                              └──────→ job → asset → outbound_message
```

Trace 记录时间关系；Metric 用于聚合趋势；Log 用于离散诊断。OpenTelemetry 已有
GenAI 相关语义约定，但相关字段仍在演进，项目应封装内部字段并固定版本
([OpenTelemetry GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/))。

默认不记录 API Key、完整私密消息、原图和模型隐藏推理。对可选 Prompt/Response
采样需用户知情、脱敏和保留期。

## 8. Agent 安全测试

至少覆盖：

- 直接和间接 Prompt Injection；
- 工具参数越权；
- 子 Agent 权限继承；
- 恶意 MCP/A2A Server；
- Tool Result 注入；
- SSRF、路径穿越、命令注入；
- 大文件/解压炸弹；
- 重放审批；
- 无限循环和费用耗尽；
- 断线/重试造成重复外发。

OWASP 2026 Agentic Top 10 已把 Goal Hijack、Tool Misuse、Identity/Privilege Abuse、
Agentic Supply Chain、Unexpected Code Execution、Memory/Context Poisoning、Cascading
Failures 等列为专门风险类别
([OWASP Agentic Top 10](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/))。

## 9. 项目采用顺序建议

1. 内部 Capability Contract；
2. 内部统一 AgentEvent；
3. 飞书 Adapter；
4. Web Console 可按需采用 AG-UI；
5. 第三方 Tool 生态需要时增加 MCP；
6. 出现真正独立远端 Agent 节点后增加 A2A。

## 10. 来源

- [MCP Architecture](https://modelcontextprotocol.io/docs/learn/architecture)
- [MCP Specification 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25)
- [A2A Specification](https://a2a-protocol.org/latest/specification/)
- [AG-UI Overview](https://docs.ag-ui.com/)
- [Google ADK Evaluation](https://adk.dev/evaluate/)
- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [OWASP Agentic Top 10 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)

返回[Web Agent 知识地图](README.md)。
