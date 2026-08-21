# 产品化工程知识地图

作者：**Zhuofan Xie**
更新日期：2026-07-28
状态：核心正文已建立，项目基线与实验待完成

本分域覆盖从 Agent Demo 到可长期使用的个人数字助手所需的能力分发、多模态资产、
数据治理、许可证、性能、用户体验和运维。

## 正文导航

1. [Capability、资产与软件/模型供应链](01-capability-assets-supply-chain.md)
2. [数据治理、权限、安全与技术决策](02-data-security-governance.md)
3. [性能、能耗、测试、运维与 Human-Agent UX](03-performance-testing-operations-ux.md)

以下知识地图继续作为产品化查漏清单。

产品化不是最后一次性补做。权限、幂等和资产所有权从首个外部渠道阶段就开始；安装、
性能与运维再按[实战路线](../implementation-roadmap.md)的阶段 7–9逐步加入。

## 1. Capability、Skill、MCP 与插件

### 1.1 概念边界

- Skill；
- Tool；
- Capability；
- Model；
- MCP Server；
- Plugin Package；
- Workflow。

### 1.2 Capability Manifest

- ID、Version 和 Author；
- Entrypoint；
- Input / Output；
- Runtime；
- Model；
- Permission；
- Resource Budget；
- License；
- Compatibility。

### 1.3 生命周期

- Discover；
- Install；
- Verify；
- Enable；
- Upgrade；
- Disable；
- Rollback；
- Uninstall；
- Data Cleanup。

### 1.4 隔离与供应链

- Python / Node 环境；
- 模型哈希；
- 数字签名；
- 可信来源；
- 网络权限；
- 文件权限；
- GPU 权限；
- 沙箱；
- 漏洞和撤销。

## 2. 多模态资产管线

- Text、Image、Audio、Video、PDF；
- Depth、Mask、Mesh、GLB、PLY 和 3DGS；
- MIME 与 Magic Number；
- EXIF；
- 解码和尺寸限制；
- Asset ID；
- 哈希去重；
- 派生关系；
- 缩略图；
- 转码；
- 临时文件；
- 生命周期；
- 删除和恢复；
- 大文件传输；
- Viewer。

## 3. 数据治理与隐私

- 用户、渠道和会话身份；
- 任务与资产所有权；
- 数据最小化；
- 同意和用途；
- 本地优先；
- 加密；
- 导出；
- 删除；
- 保留期限；
- 备份；
- 多用户隔离；
- 云端 API 数据策略；
- 审计。

## 4. 模型许可与软件供应链

- 代码许可证；
- 权重许可证；
- 训练数据风险；
- 商业使用；
- 衍生模型；
- API 条款；
- 第三方 SDK；
- SBOM；
- 依赖漏洞；
- 模型来源和哈希；
- 版本冻结；
- 替代与退出策略。

## 5. 性能、功耗与设备能力

### 5.1 指标

- Latency；
- Throughput；
- CPU；
- GPU / NPU；
- Memory / VRAM；
- Power；
- Energy；
- Temperature；
- Battery；
- Disk 和 Network。

### 5.2 优化手段

- Quantization；
- Distillation；
- Cache；
- Batch；
- Lazy Load；
- Resolution Scaling；
- Event-driven Render；
- Model Routing；
- Adaptive Degradation；
- 任务并发限制。

### 5.3 设备分级

- Apple Silicon；
- NVIDIA；
- CPU-only；
- iPhone / iPad；
- HarmonyOS；
- Android；
- Browser；
- 低端设备回退。

## 6. Human-Agent UX

- 任务状态；
- 可取消；
- 可修改参数；
- 进度；
- 审批；
- 风险提示；
- 结果预览；
- 错误解释；
- 恢复入口；
- 记忆查看和纠正；
- 本地/云端标识；
- 模型与成本提示；
- 无障碍和移动端体验。

## 7. 测试与质量

- Unit；
- Integration；
- Contract；
- End-to-End；
- Fake Model / Tool；
- Record / Replay；
- Failure Injection；
- Security Test；
- Model Evaluation；
- Visual Regression；
- Performance Regression；
- 移动端兼容测试。

## 8. 常驻服务与运维

- macOS `launchd`；
- Windows Service / Task Scheduler；
- Linux `systemd`；
- 开机启动；
- Watchdog；
- Health Check；
- 日志轮转；
- 指标和告警；
- 自动更新；
- 配置迁移；
- 数据备份；
- 回滚；
- 磁盘清理；
- 模型下载恢复；
- 电脑睡眠与唤醒；
- 事故响应。

## 9. 产品与技术治理

- Research；
- Experiment；
- ADR；
- Capability Review；
- 模型准入；
- 安全审批；
- Release；
- Deprecation；
- 文档更新；
- 看板同步；
- 重新评估条件。

## 10. 完成标准

- [ ] 每个模块形成项目基线；
- [ ] Capability 有清单、权限和卸载策略；
- [ ] 模型有许可、性能和隐私记录；
- [ ] 资产有生命周期和删除策略；
- [ ] 服务有安装、更新、恢复和卸载流程；
- [ ] 用户可以理解、控制和撤销 Agent 行为。

返回[知识库总导航](../README.md)。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
