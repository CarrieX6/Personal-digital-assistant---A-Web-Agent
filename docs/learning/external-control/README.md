# 外部控制与移动端知识地图

作者：**Zhuofan Xie**
更新日期：2026-07-28
状态：核心架构已核对；微信/HarmonyOS 深度证据、平台与真机 PoC 待完成

本分域覆盖手机通过飞书、微信等渠道控制本地电脑 Agent 的通信、身份、安全、网络、
移动端限制和结果回传。

## 正文导航

1. [Channel Gateway、飞书与微信生态接入](01-channel-gateway-feishu-wechat.md)
2. [网络部署、iOS、HarmonyOS 与 Android 边界](02-deployment-mobile.md)
3. [多模态结果回传、3D 预览与可靠性](03-preview-reliability.md)
4. [身份、认证、授权与设备绑定](04-identity-auth.md)

以下知识地图继续作为实现与调研查漏清单。

若要直接实现首个 PoC，按总知识库的
[阶段 5：飞书文本闭环](../implementation-roadmap.md#8-阶段-5飞书文本闭环)
阅读；本页其余部分用于查漏，不要求一次掌握。

## 1. 渠道通信基础

### 1.1 接入方式

- Webhook；
- WebSocket；
- 长连接；
- Long Polling；
- Bot API；
- Callback API；
- Push Notification。

### 1.2 事件处理

- Event ID 和 Message ID；
- 可选 CloudEvents 外层信封；
- 验签；
- 幂等；
- 重试；
- 乱序；
- ACK；
- Callback 超时；
- 异步任务移交。

### 1.3 消息与媒体

- Text；
- Rich Text；
- Card；
- Image；
- File；
- Audio；
- Video；
- Location；
- Button Action。

## 2. 身份、认证与权限

- App、Tenant、User 和 Conversation；
- OAuth；
- App Token 和 Tenant Token；
- 用户绑定；
- 白名单；
- 群聊与私聊；
- Capability 权限；
- 高风险审批；
- Token 加密和轮换；
- 审计。

## 3. 统一 Channel Gateway

### 3.1 数据模型

- `InboundMessage`；
- `OutboundMessage`；
- Attachment；
- Conversation Context；
- Result Package；
- Error 和 Retry Metadata。

### 3.2 Channel Adapter

- Receive；
- Verify；
- Normalize；
- Download Attachment；
- Send Text/Card/Media；
- Update Progress；
- 平台能力探测；
- 降级策略。

### 3.3 与其他模块边界

- Identity & Policy；
- Agent Orchestrator；
- Job Queue；
- Asset Store；
- Preview Service；
- Audit Log。

## 4. 飞书

### 4.1 应用形态

- 企业自建应用；
- Bot；
- 长连接；
- Webhook；
- 应用身份和用户身份。

### 4.2 能力大纲

- 权限申请；
- 消息事件；
- 图片和文件下载；
- 消息回复；
- 卡片；
- 卡片动作；
- 媒体上传；
- 限流；
- 事件重试；
- 多实例消费。

### 4.3 部署问题

- 本地长连接常驻；
- 代理和网络稳定性；
- 电脑睡眠；
- Secret 存储；
- 断线恢复；
- 多用户和多租户；
- 调试环境与正式环境。

## 5. 微信生态

### 5.1 路径拆分

- 普通个人微信；
- 企业微信自建应用；
- 微信公众号；
- 微信小程序；
- 微信开放平台。

### 5.2 研究维度

- 官方公开能力；
- 企业主体；
- 公网 HTTPS；
- 审核；
- 消息类型；
- 回调限制；
- 媒体限制；
- 用户身份；
- 成本；
- 账号与合规风险。

### 5.3 明确排除

- 非公开登录协议；
- Hook 客户端；
- 模拟点击；
- 长期托管个人微信登录。

## 6. 部署拓扑

### 6.1 聊天平台直连本地 Agent

```text
手机聊天客户端 → 平台云端 → 本地电脑长连接 Agent
```

### 6.2 云端 Gateway + 本地执行节点

```text
手机 → 平台 → Cloud Gateway → 安全通道 → 本地 Agent
```

### 6.3 Companion App

```text
手机 App → 局域网或安全隧道 → 本地 Agent
```

### 6.4 全手机端与混合部署

- Agent 全部在手机端；
- 云端 Agent + 本地模型节点；
- 手机 Router + 电脑 Capability；
- 离线与在线降级。

### 6.5 对比维度

- 公网 IP；
- 域名与证书；
- NAT；
- 隐私；
- 可用性；
- 成本；
- 延迟；
- 维护；
- 离线能力；
- 唤醒。

## 7. 网络、隧道与远程唤醒

- NAT、CGNAT；
- Reverse Proxy；
- Outbound Tunnel；
- Relay；
- VPN 与 Zero Trust；
- 短时签名 URL；
- 局域网模式；
- 电脑睡眠；
- Wake-on-LAN；
- 远程健康检查；
- 网络切换和重连；
- 离线消息积压。

## 8. iOS、HarmonyOS 与 Android

### 8.1 手机只作为聊天入口

- 不运行自定义后台 Agent；
- 依赖现有聊天客户端；
- Push；
- 图片上传；
- 链接和文件打开。

### 8.2 手机作为 Viewer / Companion App

- App Sandbox；
- 后台任务；
- Deep Link / Universal Link；
- 局域网权限；
- 相册和文件；
- WebView；
- WebGL / WebGPU；
- 设备姿态；
- Push 和审批。

### 8.3 手机运行 Agent 或模型

- Core ML、Metal 与 Apple Silicon；
- HarmonyOS 端侧 AI 能力；
- Android LiteRT/TensorFlow Lite 与 GPU delegate；NNAPI 仅作为既有项目迁移背景；
- 模型量化；
- 内存和存储；
- 发热和电池；
- 动态下载；
- 应用商店审核；
- 后台存活。

### 8.4 平台对比

- iOS；
- HarmonyOS；
- Android；
- 厂商系统差异；
- 开放能力；
- 限制与降级；
- 需要官方资料核对的动态项。

## 9. 结果回传与多模态预览

- 文本摘要；
- 图片；
- MP4；
- GIF 降级；
- 文件；
- GLB / PLY / 3DGS；
- Turntable Video；
- 空间照片演示；
- 签名 Viewer；
- 触控和设备姿态；
- 过期、撤销和访问日志；
- 大文件和平台限制。

## 10. 可靠性、安全与运维

- 重复消息；
- 幂等；
- 限流；
- Retry 和 Dead Letter；
- 断线恢复；
- Token 过期；
- Secret 轮换；
- 未授权用户；
- 恶意附件；
- Prompt Injection；
- 服务守护；
- 日志与告警；
- 版本升级；
- 平台 API 变化。

## 11. 必做实验

- 飞书文本 Echo；
- 重复事件；
- 图片附件；
- 异步任务进度；
- 结果图片和视频；
- Viewer 签名链接；
- 断网与重连；
- 电脑重启恢复；
- iPhone 访问；
- HarmonyOS 手机访问；
- 不支持 3D 时的视频降级。

返回[知识库总导航](../README.md)。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
