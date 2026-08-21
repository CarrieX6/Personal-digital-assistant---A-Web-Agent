# 数据治理、权限、安全与技术决策

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：原则已核对，项目政策与威胁模型待 ADR  
关联任务：`LEARN-OPS-001`、`SEC-001`、`SEC-002`

## 1. 数据清单先于隐私口号

建立数据登记：

| 数据 | 来源 | 用途 | 存储 | 外发 | 保留/删除 |
| --- | --- | --- | --- | --- | --- |
| 消息正文 | 飞书/微信 | 理解任务 | 会话库 | 可能到 LLM API | 用户策略 |
| 原始照片 | 用户 | 生成 | Asset Store | 本地或指定模型 API | 明示期限 |
| 深度/3D | 派生 | 预览/下载 | Asset Store | Viewer | 跟随父资产 |
| 偏好记忆 | 用户确认 | 个性化 | Memory DB | 按需入 Prompt | 可查看纠正 |
| Trace | 系统 | 排错/评测 | Observability | 默认不外发正文 | 短期 |
| Secret | 管理员 | API 认证 | Keychain/Secret Store | 只到目标服务 | 轮换/撤销 |

若不知道数据在哪里，就无法兑现导出、删除和“本地优先”。

## 2. 数据最小化

- Channel 只下载用户本次任务需要的附件；
- RAG 只取与当前问题相关片段；
- 云端模型只获得完成任务的必要数据；
- Trace 默认记录 hash/ID/长度，而不是完整内容；
- EXIF、位置和设备信息没有用途就移除；
- 临时文件自动过期；
- 不从对话推断敏感长期属性。

## 3. 所有权与隔离

每个 Conversation、Job、Run、Memory、Asset 都有 owner。授权查询必须同时包含
`owner_id`，不能先按可猜 ID 查询再判断。群聊资产需定义是群共享还是发件人私有；
默认私有更安全。

## 4. Prompt Injection 防线

模型不能可靠区分“可信指令”和“文档里的恶意文字”，所以用系统边界防护：

- instructions、用户输入、外部内容分区；
- 外部内容无权改变 Policy；
- 动态 Tool allowlist；
- Tool 端再次授权；
- 只读与写入工具分开；
- 高风险动作独立审批；
- 网络 egress 和文件路径限制；
- 输出渲染做 HTML/Markdown/URL 安全处理；
- 任务预算和 emergency stop。

OWASP 将 Prompt Injection 和 Excessive Agency 列为 LLM 应用主要风险，并建议减少
工具功能、权限和自治
([OWASP LLM Top 10](https://genai.owasp.org/llm-top-10/),
[Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/))。

## 5. Secret 管理

- UI 可填写 Key，但后端只返回“已配置/后四位”，不返回明文；
- macOS 优先 Keychain，服务器使用 Secret Manager；
- DB 中如必须存储，使用独立主密钥加密；
- 不进入 Git、Prompt、异常、Trace 和截图；
- 每供应商分开 Key；
- 支持替换、测试、撤销和最后使用时间；
- 日志过滤 `Authorization`、cookie、token、signed URL。

## 6. 审计与隐私日志

Audit Log 记录：

- 谁、从哪个 Channel；
- 在何时请求哪个 Capability/action；
- 作用于哪些 Asset；
- Policy/审批结果；
- 执行版本和结果状态；
- 对外发送到哪里。

审计不应成为第二份敏感内容仓库。使用 ID、分类和摘要，正文按需受限访问。

## 7. 威胁建模

至少分析：

- 外部攻击者伪造平台事件；
- 合法用户越权访问他人资产；
- 恶意附件攻击解码器；
- 间接 Prompt Injection；
- 恶意 Capability/MCP Server；
- 本地其他进程读取 Token；
- Viewer URL 泄露；
- 模型/API 供应商数据边界；
- 更新服务器或依赖被篡改；
- Agent 误删、误发和费用失控。

对每个威胁记录资产、入口、前置条件、影响、控制、剩余风险和验证实验。

## 8. 治理闭环

```text
问题 → Research → 候选 → Experiment → ADR → Implementation
     → Test/Release → Monitor → Re-evaluate
```

知识文档解释通用原理；Research 收集动态证据；Experiment 记录真实结果；ADR 才表示
项目决定。框架升级、平台规则、价格、许可、设备或目标变化时触发重新评估。

NIST AI RMF 提供自愿性的风险管理框架，Generative AI Profile 补充生成式 AI 风险
([NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework))。项目可用其
结构检查治理覆盖，但安全必须落到具体权限、测试和运行监控。

## 9. 发布门禁

- [ ] 数据流和外发清单；
- [ ] 用户/资产隔离测试；
- [ ] Prompt Injection 与 Tool 越权测试；
- [ ] Secret 扫描和日志脱敏；
- [ ] 依赖、模型来源、hash 和许可；
- [ ] 高风险审批与可逆操作；
- [ ] 导出、删除、备份、恢复；
- [ ] 事故停用 Capability/Key 的手册；
- [ ] ADR 与看板同步。

## 10. 来源

- [OWASP Top 10 for LLM Applications](https://genai.owasp.org/llm-top-10/)
- [OWASP LLM06:2025 Excessive Agency](https://genai.owasp.org/llmrisk/llm062025-excessive-agency/)
- [OWASP Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/)
- [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework)
- [NIST Generative AI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf)

返回[产品化工程知识地图](README.md)。

