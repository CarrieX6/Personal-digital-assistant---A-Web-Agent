# 文档中心

作者：**Zhuofan Xie**

这里存放项目的架构设计、平台调研、学习笔记与具体功能技术文档。文档服务于团队
协作和技术决策，不存放 API Key、个人数据、模型权重或生成资产。

> 开始新任务、领取调研或查看整体进度时，先进入
> [调研与实现中心](project-board.md)。它是项目研究与开发状态的统一入口。

## 目录

### 项目管理

- [调研与实现中心](project-board.md)：全部调研、实验、决策和实现任务的优先级、
  状态、负责人、依赖与验收标准。
- [调研与决策流程](research/process.md)：从问题登记、资料调研、实验到 ADR 和
  代码实现的标准流程。
- [实验记录](experiments/README.md)：模型、Agent、平台和性能实验索引。
- [技术决策记录](decisions/README.md)：已经生效或被替代的 ADR 索引。

### 阶段汇报与审计

- [个人数字助手项目阶段汇报（2026-08）](reports/personal-digital-assistant-stage-report-2026-08.md)：
  面向公司和团队评审的 10 页结果型汇报、故事主线、贡献边界、数据证据与下一阶段。
- [阶段汇报索引](reports/README.md)：汇报文档的归属和证据要求。

### 架构设计

- [系统架构](architecture/system-architecture.md)：最终目标、模块边界、消息链路、
  数据模型、安全边界和分阶段路线图。
- [LangGraph 循环 Agent 设计](architecture/langgraph-agent-loop.md)：已实现的循环
  节点与代码硬边界，以及待完成的人工确认、异步恢复和幂等控制。
- [飞书身份、个人工作区与电脑节点绑定](architecture/identity-workspace-device-binding.md)：
  一个外部账号一个隔离工作区、Root 管理、本机节点绑定与多电脑 Control Plane 演进。
- [分层长短期记忆设计](architecture/layered-memory.md)：工作记忆、滚动摘要、
  类型化长期记忆、混合检索、时间有效性、证据追踪与结果反馈闭环。

### 配置指南

- [Web Agent 单机完整节点架构与迁移方案](guides/full-node-migration.md)：目标电脑同时
  承载 Web、Agent、飞书、数据、Viewer 和隔离模型 Provider；包含安装状态机、DGX
  Spark ARM64 门禁、备份恢复与真实验收。
- [个人数字助手完整本地部署与迁移手册](guides/complete-local-deployment.md)：从 GitHub
  克隆到一键安装、真实模型、Web/Agent/功能库/飞书配置、加密数据迁移和完整验收。
- [飞书机器人配置与使用指南](guides/feishu-setup.md)：企业自建应用、权限、长连接、
  Open ID、私聊、群聊、图片、卡片回调、Root 可见性与故障排查。
- [macOS / Windows 本地部署](guides/deployment.md)：原生一键安装、启动、GPU 边界与
  局域网签名 Viewer。
- [图片风格化独立服务部署与迁移](guides/photo-style-deployment.md)：跨平台部署管理器、
  Fake 链路测试、macOS MPS、Windows NVIDIA、远程 GPU、自动启动、迁移和验收。
- [Flux-GS Capability 接入与部署](guides/flux-gs-capability.md)：受控数据集 ID、独立
  Linux/NVIDIA GPU 服务、Agent 工具、飞书卡片、WebGL 结果和非商用许可门禁。
- [macOS MPS 真实风格化验收模板](experiments/style-004-macos-mps-validation.md)：固定样本、
  十次稳定性、统一内存、功耗、fallback 和质量门禁。
- [新功能 / Capability 接入指南](guides/capability-integration.md)：服务、Provider、
  Tool Manifest、渠道适配、测试和旧分支迁移约定。
- [飞书图片与 2.5D 能力接入 SOP](guides/feishu-media-capability-sop.md)：多图角色
  收集、异步 Job、空间 Viewer、风格化结果图、Outbox、新电脑安装和发布验收流程。

### 平台与技术调研

- [调研模板](research/research-template.md)：问题、候选方案、证据、过程、结论和
  实现待办模板。
- [外部聊天控制调研](research/external-chat-control.md)：飞书、微信与企业微信的
  接入方式，以及图片、空间照片和 3D 资产的回传策略。
- [Apple 空间场景技术路线核对](apple-spatial-scene-research.md)：Apple 公开能力、
  2.5D、LDI 与 3DGS 路线。
- [空间照片端侧部署与资产格式](spatial-scene-device-deployment.md)：当前网络模型、
  图像处理、渲染流程、训练需求、性能和部署格式。
- [图片风格化 Skill 集成说明](photo-style-transfer.md)：Provider 中立接口、Web/PC
  工作台、Capability Manifest、存储权限和真实模型接入要求。

### 学习笔记

- [个人数字助手知识库](learning/README.md)：从工程基础、Web Agent、外部控制到
  产品化工程的分层知识体系、正文和学习路线。
- [从零到可运行个人数字助手](learning/implementation-roadmap.md)：按当前代码基线
  分阶段学习、实现、测试和验收的主路线。
- [调研证据与文档维护方法](learning/research-quality.md)：证据等级、当前覆盖审查、
  已知缺口、时效性核验和更新触发器。
- [项目工程基础](learning/foundations/00-project-engineering.md)：环境、依赖、配置、
  Secret、测试、调试和 Git 协作前置。
- [Web Agent 知识地图](learning/web-agent/README.md)：Tool Calling、Agent 架构、
  Single/Multi-Agent、调度、LangChain、LangGraph、Memory、安全与评测。
- [外部控制知识地图](learning/external-control/README.md)：飞书、微信、Channel
  Gateway、远程网络、iOS、HarmonyOS、Android 和结果预览。
- [身份、认证、授权与设备绑定](learning/external-control/04-identity-auth.md)：事件
  真实性、OAuth/OIDC/PKCE、账号绑定、设备凭证和资源授权。
- [产品化工程知识地图](learning/product-engineering/README.md)：Capability、资产、
  供应链、数据治理、安全、性能、能耗、测试、运维和 Human-Agent UX。
- [AI 功能与模型知识地图](learning/ai-capabilities/README.md)：空间照片、虚拟试衣、
  虚拟宠物、图像视频生成、3D 和端侧模型选型。

### 团队协作

- [多人 Git 协作 Skill](../.codex/skills/team-git-workflow/SKILL.md)：任务领取、
  分支与提交、同步、Pull Request、Review、冲突处理、敏感文件检查和交接规范；
  默认中文并支持切换英文（[中文说明](../.codex/skills/team-git-workflow/references/workflow.zh-CN.md) /
  [English](../.codex/skills/team-git-workflow/references/workflow.en.md)）。

## 文档规范

每篇文档建议包含：

1. 作者和更新日期；
2. 问题背景与目标；
3. 结论先行；
4. 事实、工程推断与未验证假设分开表述；
5. 方案比较、风险和决策；
6. 官方资料或论文链接；
7. 与当前代码的对应关系。

调研结论变化时更新原文，不为同一问题创建多个互相冲突的版本。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
