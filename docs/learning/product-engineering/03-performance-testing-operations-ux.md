# 性能、能耗、测试、运维与 Human-Agent UX

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：方法初稿，基线实验待完成  
关联任务：`LEARN-OPS-001`、`PERF-001`、`QA-001`

## 1. 性能不能只报“用了几秒”

端到端延迟拆分：

```text
Channel delivery
+ Gateway queue
+ Agent/model
+ Job queue
+ Model load
+ Inference
+ Post-process/export
+ Media upload
+ Result delivery
```

统一记录 P50/P95，而不是只挑最快一次。生成任务还要记录：

- 冷/热启动；
- 峰值和稳态 RAM/VRAM；
- GPU/CPU 利用；
- 平均功率（W）；
- 总能量（Wh/J）；
- 温度与降频；
- 输出质量；
- 设备、系统、依赖、模型和参数版本。

功率更低但耗时翻倍，可能让总能量更高；因此 Power 与 Energy 必须同时看。

## 2. 优化顺序

1. 避免不必要工作：缓存、去重、按需生成；
2. 降低输入：合理分辨率、帧数、上下文和工具数；
3. 控制并发：GPU 一次跑合适数量；
4. 复用：模型常驻、连接池、编译缓存；
5. 选择更小模型或量化；
6. 批处理；
7. 硬件专用后端；
8. 降级输出。

每个优化都要测质量。量化、降分辨率和缩短上下文不应只看速度。

## 3. 低功耗策略

- 事件驱动，不轮询；
- 无交互时 3D Viewer 降帧或停止渲染；
- 缩略图和预览只生成一次；
- GPU Worker 有并发上限和空闲卸载策略；
- 插电/电池、温度和前后台状态纳入 Scheduler；
- 大模型按需加载，频繁使用模型延迟卸载；
- 手机只渲染预览，重计算放电脑；
- 后台任务使用系统推荐调度。

Android 官方提醒选择错误的后台 API 会伤害资源效率和电池，并推荐按场景选择
WorkManager、前台服务或专用 API
([Android Background Work](https://developer.android.com/develop/background-work))；
Apple 也建议把重任务调度到系统选择的低活动时段
([Apple Background Strategies](https://developer.apple.com/documentation/BackgroundTasks/choosing-background-strategies-for-your-app))。

## 4. 测试金字塔

### 确定性测试

- Unit：Schema、Policy、状态迁移、路由规则；
- Contract：Channel、Tool、Capability、模型适配器；
- Integration：DB + Queue + Worker + Asset；
- E2E：真实飞书测试租户到结果回传。

### Agent/模型测试

- Fake Model 返回固定 Tool Calls；
- Fake Tool 注入超时、错误和恶意输出；
- Record/Replay 用于供应商响应回归（注意脱敏和许可）；
- 固定评测集测轨迹和最终结果；
- 多次运行报告分布；
- 人工基准校准 LLM Judge。

### 故障注入

- 杀 Worker/重启 API；
- 断网和 DNS 失败；
- 平台 429/5xx；
- DB 锁/磁盘满；
- GPU OOM；
- 模型下载中断；
- 重复/乱序事件；
- 审批过期和重放；
- Viewer URL 泄露/越权。

## 5. 可观测性与隐私

每个 Span 记录：

- operation、duration、status；
- model/provider（不含 Key）；
- Tool/Capability/version；
- input/output size；
- Token/费用；
- queue wait；
- retry/error category；
- device/resource；
-关联的 run/job/asset ID。

OpenTelemetry 的 GenAI Semantic Conventions 正在演进，内部事件 Schema 应版本化并
通过 Adapter 导出
([OpenTelemetry GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/))。

## 6. 常驻服务

平台原生机制：

- macOS：`launchd`；
- Linux：`systemd`；
- Windows：Windows Service 或 Task Scheduler。

服务需要：

- 开机启动与用户级/系统级权限选择；
- health/readiness；
- crash backoff，避免无限拉起；
- graceful shutdown；
- 日志轮转和磁盘上限；
- 版本化配置迁移；
- 更新前备份、更新后 smoke test、失败回滚；
- 模型下载续传和 hash 校验；
- Queue/DB/Asset 一致备份；
- 一键停止和卸载。

## 7. Human-Agent UX

用户必须随时知道：

- 系统理解了什么；
- 要调用哪个功能、数据去哪里；
- 当前在排队、执行、审批还是失败；
- 是否可取消；
- 将产生什么结果、费用和风险；
- 哪些内容被记住；
- 如何纠正、删除和重试。

高风险确认应显示具体对象和后果，不能只写“是否继续”。错误信息分三层：

1. 用户可操作说明；
2. 支持用错误码/Run ID；
3. 开发者 Trace（受限）。

进度不要伪造精确百分比。模型无法估算时，显示阶段、已耗时和可取消状态。

## 8. 发布与运行指标

- SLO：事件接收成功率、任务完成率、结果投递率；
- 错误预算：允许多少失败再停止发布；
- Canary：小比例用户/设备先升级；
- Capability kill switch；
- 模型/provider 降级；
- 安全事件响应；
- 用户资产恢复演练；
- 依赖和许可重新核对日期。

## 9. 第一批基准

建议选一台 Apple Silicon Mac、一个 iPhone、一台华为手机：

- 文本 Tool Calling 50 任务；
- 空间照片 10 张不同类型图；
- 冷/热启动各 5 次；
- 并发 1/2/4；
- 断网/睡眠/重启；
- 飞书图片/文件/Viewer；
- 记录质量、延迟、能量、温度、恢复。

这是待执行实验方案，不是已有结果。

## 10. 来源

- [Android Background Work](https://developer.android.com/develop/background-work)
- [Apple Background Strategies](https://developer.apple.com/documentation/BackgroundTasks/choosing-background-strategies-for-your-app)
- [OpenTelemetry GenAI Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
- [Google ADK Evaluation](https://adk.dev/evaluate/)

返回[产品化工程知识地图](README.md)。

