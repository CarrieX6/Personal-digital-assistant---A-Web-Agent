# 文档中心

作者：**Zhuofan Xie**

这里存放项目的架构设计、平台调研、学习笔记与具体功能技术文档。文档服务于团队
协作和技术决策，不存放 API Key、个人数据、模型权重或生成资产。

## 目录

### 架构设计

- [系统架构](architecture/system-architecture.md)：最终目标、模块边界、消息链路、
  数据模型、安全边界和分阶段路线图。

### 平台与技术调研

- [外部聊天控制调研](research/external-chat-control.md)：飞书、微信与企业微信的
  接入方式，以及图片、空间照片和 3D 资产的回传策略。
- [Apple 空间场景技术路线核对](apple-spatial-scene-research.md)：Apple 公开能力、
  2.5D、LDI 与 3DGS 路线。
- [空间照片端侧部署与资产格式](spatial-scene-device-deployment.md)：当前网络模型、
  图像处理、渲染流程、训练需求、性能和部署格式。

### 学习笔记

- [学习笔记索引](learning/README.md)：团队知识记录规范、建议学习顺序与选题清单。

### 团队协作

- [多人 Git 协作 Skill](../.codex/skills/team-git-workflow/SKILL.md)：任务领取、
  分支与提交、同步、Pull Request、Review、冲突处理、敏感文件检查和交接规范。

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
