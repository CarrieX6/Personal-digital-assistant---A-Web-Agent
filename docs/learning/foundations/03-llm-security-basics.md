# LLM 应用与安全基础

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：已核对  
关联任务：`LEARN-001`、`SEC-001`、`LLM-003`

## 1. LLM API 的组成

一次 Agent 调用通常包含：

- instructions/system message：应用规则和角色；
- conversation messages：用户、助手和工具消息；
- tools：名称、描述和参数 Schema；
- model settings：最大输出、采样、超时等；
- structured output schema：最终结果结构；
- usage：输入/输出 Token 和费用信息；
- stream events：增量文本、工具调用和完成原因。

Context Window 是单次模型可处理的上下文预算，不是永久记忆。把全部历史、所有工具和
所有文档每次都塞入上下文，会增加延迟、费用和错误选择概率。Context Engineering
应决定“此刻给模型哪些可信信息和工具”。

## 2. Structured Output 与 Tool Calling

Structured Output 约束模型返回可验证结构；Tool Calling 让模型提出工具名称和参数。
两者都不是授权系统：

1. 模型输出先做 Schema 验证；
2. 服务端根据真实用户、会话和 Capability 再授权；
3. 规范化路径、URL、文件和数值范围；
4. 高风险动作进入审批；
5. 工具结果作为不可信数据返回模型；
6. 每次调用记录 Trace，但密钥和敏感正文要脱敏。

Schema 应使用枚举和明确字段，避免一个 `command: string` 或 `url: string` 获得无限
能力。OpenAPI 可描述 HTTP 工具，JSON Schema/Pydantic 可做运行时验证。

## 3. Prompt、RAG 与 Fine-tuning 的边界

| 方法 | 改变什么 | 适合 | 不适合 |
| --- | --- | --- | --- |
| Prompt/Context | 单次输入 | 规则、示例、临时信息 | 永久可靠地改变模型能力 |
| RAG | 运行时检索资料 | 私有/更新知识、来源追踪 | 学习新的视觉生成能力 |
| Fine-tuning | 模型参数或适配器 | 稳定风格、格式、特定分布 | 频繁变化的事实库 |
| Tool | 接入外部确定性能力 | 查询、执行、生成资产 | 让模型绕过权限和业务校验 |

本项目早期实现 Channel、Agent 调度和空间照片调用，不要求先训练 LLM。应先通过
工具 Schema、Workflow、检索和评测验证问题是否真的来自模型能力；只有持续、可测的
缺口才进入微调调研。

## 4. 云端与本地推理

云端 API 的优势是模型能力和部署简单；代价是网络、费用、数据边界和供应商变化。
本地模型的优势是隐私、离线和可控；代价是下载、显存/内存、兼容、功耗和更新。

量化降低模型内存和带宽需求，但可能损失质量；是否值得只能在目标设备、目标任务上
实测。性能必须同时记录：

- 首 Token 延迟、总延迟、吞吐；
- 峰值/稳态内存与显存；
- 冷启动、模型加载和缓存命中；
- 平均功率与总能量；
- 温度、降频和连续任务表现；
- 输出质量与任务成功率。

## 5. LLM 特有威胁

OWASP 2025 LLM Top 10 包括 Prompt Injection、敏感信息泄漏、供应链、数据/模型投毒、
不当输出处理、Excessive Agency 等风险
([OWASP LLM Top 10](https://genai.owasp.org/llm-top-10/))。

其中 Excessive Agency 的根因被归纳为过多功能、过大权限或过高自治；OWASP 建议减少
可用扩展、功能和权限，并为高影响动作加入独立审批
([OWASP LLM06:2025](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/))。

### 对本项目的直接映射

| 威胁 | 示例 | 控制 |
| --- | --- | --- |
| 直接 Prompt Injection | 用户要求忽略规则读取全部文件 | 工具白名单、目录能力、授权 |
| 间接 Prompt Injection | 上传文档含“发送密钥给我” | 外部内容标记为数据，不继承指令权 |
| Excessive Agency | Agent 可删除、发送、安装任意内容 | 最小 Tool、审批、可逆操作 |
| SSRF | 工具按模型 URL 访问内网 | URL allowlist、DNS/IP 再校验、隔离网络 |
| 路径穿越 | `../../.ssh` | Asset ID 代替任意路径、规范化与根目录约束 |
| 命令注入 | 字符串拼 shell | 参数数组、禁止 shell、沙箱 |
| 数据泄漏 | Trace 保存 Token/照片 | 分类、脱敏、加密、保留期 |
| 供应链 | 未核验模型/插件执行代码 | 来源、hash、签名、SBOM、隔离 |

## 6. 身份与权限模型

必须区分：

- 渠道应用身份：飞书/微信平台认得哪个应用；
- 平台用户身份：消息由谁发送；
- 本地用户身份：该平台账号绑定哪个本地用户；
- Agent 身份：哪一个运行实例；
- Capability 身份：哪个功能包；
- 执行凭证：访问文件、API 和模型服务的短期凭证。

权限判断必须在工具执行服务端完成，不能仅依赖 System Prompt。建议权限维度：

`user × capability × action × resource × channel × risk_level`。

## 7. 风险分级与审批

- L0：只读、无敏感数据，可自动执行；
- L1：生成本地临时资产，可自动执行并可取消；
- L2：外发消息、长期写入记忆、安装模型，需要明确预览/确认；
- L3：删除、覆盖、支付、公开发布、系统配置，必须强确认和审计；
- 禁止：绕过系统安全、窃取凭证、未授权监控。

审批记录要绑定精确参数和过期时间；审批后参数变化必须重新确认。

## 8. 治理框架

NIST AI RMF 是自愿性的 AI 风险管理框架；其 Generative AI Profile 用于把生成式 AI
特有风险纳入组织的治理、测量和管理
([NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework))。项目可借用
“治理—映射—测量—管理”的思路，但不能仅用一张合规清单替代技术测试。

## 9. 来源

- [OpenAPI Specification](https://spec.openapis.org/oas/latest.html)
- [OWASP Top 10 for LLM Applications 2025](https://genai.owasp.org/llm-top-10/)
- [OWASP LLM06:2025 Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)
- [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
- [NIST AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework)
- [NIST Generative AI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf)

返回[工程与 AI 基础知识地图](README.md)。

