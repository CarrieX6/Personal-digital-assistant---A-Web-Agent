# 网络部署、iOS、HarmonyOS 与 Android 边界

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：官方平台原则已核对，设备实测待完成  
关联任务：`LEARN-MOBILE-001`、`REMOTE-001`

## 1. 先决定手机承担什么角色

| 角色 | 手机做什么 | 推荐度 |
| --- | --- | --- |
| 聊天入口 | 使用飞书/企业微信，发送命令和查看结果 | 首期推荐 |
| Companion/Viewer | 登录、审批、3D Viewer、设备姿态 | 第二阶段 |
| Agent Router | 少量本地规则、离线队列、连接电脑 | 有明确需求再做 |
| 完整 Agent/模型节点 | 常驻连接、推理、工具执行 | 受后台/功耗限制，不作为主架构 |

苹果和华为手机并非“无法开发”，但第三方应用受到沙箱、生命周期、权限和后台调度约束。
因此系统级常驻 Agent 不应成为链路成功的前提。

## 2. 三种部署拓扑

### A. 平台长连接直达本地

```text
手机聊天 App → 平台云 → 本地电脑出站长连接
```

优点：最少组件、数据直接到电脑；缺点：电脑睡眠/离线时不可执行，消息积压能力依赖
平台。适合飞书 PoC。

### B. Cloud Gateway + 本地 Worker

```text
手机 → 平台 → Cloud Gateway → 出站安全通道 → 本地 Worker
```

优点：公网回调、离线队列、统一身份和多设备；缺点：增加服务器、费用、隐私和运维。
Gateway 不应保存不必要的原始照片；可只存加密 Job 和短期 Asset。

### C. Companion App 直连

局域网内可通过 HTTPS/WebSocket 发现本机服务；远程场景需 VPN/Zero Trust Relay。
不要在家用路由器上直接暴露 Agent 管理端口。

## 3. NAT、Tunnel 与 Relay

- NAT/CGNAT 常使手机无法直接访问家中电脑；
- 出站 Tunnel/Relay 让本地电脑主动建立连接，减少入站配置；
- Tunnel 只解决连接，不解决用户身份和 Capability 授权；
- 公网 Viewer 需要短时签名 URL、HTTPS、访问日志和撤销；
- 远程唤醒受硬件、路由器、睡眠模式和网络条件影响，不能承诺必达。

如果没有离线排队和多设备需求，优先使用飞书长连接，不必先购买公网服务器。

## 4. iOS 后台限制

Apple 要求按任务类型选择后台机制：短时收尾、后台 `URLSession`、`BGAppRefreshTask`、
`BGProcessingTask` 或 Push；调度时间由系统决定，后台执行并非无限常驻
([Apple Background Strategies](https://developer.apple.com/documentation/BackgroundTasks/choosing-background-strategies-for-your-app))。

因此：

- 不依赖 iOS App 永久维持自定义 WebSocket；
- 上传/下载用系统后台传输；
- Push 只用于通知和触发有限工作；
- 重计算尽量在电脑或云端；
- Companion App 打开后恢复 Run 状态；
- Universal Link/Deep Link 打开特定 Job/Viewer，而不是把访问凭证写入 URL。

## 5. Android 后台限制

Android 官方建议多数持久后台任务使用 WorkManager；它支持跨应用重启/设备重启的
调度、约束和任务链。Foreground Service 必须展示通知，并且启动受系统规则约束
([Android Background Tasks](https://developer.android.com/develop/background-work/background-tasks),
[WorkManager](https://developer.android.com/develop/background-work/background-tasks/persistent))。

因此 Android 比 iOS 开放不等于可随意常驻：

- 用户可见的持续任务用 Foreground Service；
- 可延迟工作用 WorkManager；
- 实时聊天仍优先依赖平台 Push；
- 厂商省电策略需要真实设备测试；
- 长推理要处理温控、前台通知和中断。

### Android 端侧推理路线更新

NNAPI 已在 Android 15 弃用。Android 官方迁移指南建议从 NNAPI 迁移到可更新的
TensorFlow Lite/LiteRT 路径，并可按任务评估 GPU delegate；因此新项目不应把 NNAPI
作为默认长期接口
([Android NNAPI Migration Guide](https://developer.android.com/ndk/guides/neuralnetworks/migration-guide))。

具体模型、量化和 delegate 仍属于路线 E 的后续评测；这里仅确定平台 API 的维护
边界。最终性能必须在目标 SoC、系统版本和功耗条件下实测。

## 6. HarmonyOS / 华为设备

HarmonyOS 也有应用沙箱、后台任务和系统资源调度。华为 Push Kit 支持 HarmonyOS、
Android、iOS 和 Web，并通过系统通道推送
([Huawei Push Kit](https://developer.huawei.com/consumer/cn/hms/huawei-pushkit/))。

当前文档只核对了 Push Kit 入口，尚未完成特定 HarmonyOS 版本的后台任务、Deep
Link、Viewer 和端侧推理官方资料矩阵。因此以下内容是架构原则，不是“已完成华为
真机适配”的结论。

设计原则：

- 使用官方 Push/后台任务能力，不把非官方保活作为架构基础；
- API 版本和设备形态能力应在 Manifest/设备探测中声明；
- 端侧模型/渲染需在目标 HarmonyOS 版本与真机上测；
- 手机只作聊天入口时，系统差异主要由飞书/微信客户端承担；
- 自建 Companion App 时，再分别适配权限、后台、Deep Link 和 Viewer。

## 7. 电脑睡眠与常驻服务

本地 Agent 的真实可用性取决于电脑：

- 服务是否开机启动；
- 进程是否被守护；
- 用户退出后是否继续；
- 睡眠是否断开网络/GPU；
- 模型加载是否可恢复；
- 磁盘是否有空间；
- 更新失败能否回滚。

应区分：

- “服务进程崩溃”：守护进程可拉起；
- “电脑睡眠”：通常不能靠应用进程自行恢复；
- “电脑关机”：需用户开机或独立远程唤醒基础设施；
- “网络断开”：事件可能积压在平台/Gateway，恢复后补做。

## 8. 安全基线

- 所有远程连接均由本地发起或经认证的 HTTPS；
- 不公开 SQLite、模型服务或调试端口；
- 设备注册使用一次性代码和可撤销凭证；
- 不同设备独立凭证；
- Channel User 与本地 User 显式绑定；
- 高风险操作二次确认；
- Viewer URL 短期、只读、单 Asset；
- 支持远程吊销设备。

## 9. 设备实验矩阵

每个平台至少测试：前台、后台、锁屏、网络切换、弱网、低电量、重启、App 被杀、
大文件上传、Viewer 15 分钟、连续审批。记录成功率、恢复时间、功率、温度和系统版本。

## 10. 来源

- [Apple：Choosing Background Strategies](https://developer.apple.com/documentation/BackgroundTasks/choosing-background-strategies-for-your-app)
- [Android：Background Tasks Overview](https://developer.android.com/develop/background-work/background-tasks)
- [Android：Persistent Work / WorkManager](https://developer.android.com/develop/background-work/background-tasks/persistent)
- [Android：NNAPI Migration Guide](https://developer.android.com/ndk/guides/neuralnetworks/migration-guide)
- [Huawei Push Kit](https://developer.huawei.com/consumer/cn/hms/huawei-pushkit/)

返回[外部控制与移动端知识地图](README.md)。
