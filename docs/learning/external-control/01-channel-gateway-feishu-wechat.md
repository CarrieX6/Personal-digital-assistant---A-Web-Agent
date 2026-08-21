# Channel Gateway、飞书与微信生态接入

作者：**Zhuofan Xie**  
更新日期：2026-07-29  
成熟度：飞书官方资料及真实文本连接已核对；单图媒体链路待实测；微信生态为入口级核对  
关联任务：`LEARN-CHANNEL-001`、`CHANNEL-001`、`CHANNEL-003`

## 1. 外部聊天控制的标准链路

```text
聊天平台
  → Channel Adapter（连接、验签、解析）
  → Identity & Policy（绑定、授权、审批）
  → Agent Orchestrator（意图、工具、状态）
  → Job/Capability Worker（真正执行）
  → Asset/Preview（结果）
  → Outbox → Channel Adapter → 聊天平台
```

Channel Adapter 不应直接执行模型或 shell。它负责平台差异，核心 Agent 只看到统一消息。

## 2. 统一消息模型

### `InboundMessage`

- channel、tenant/app；
- platform event/message/conversation/user ID；
- normalized user ID；
- text、mentions、locale；
- attachments（仅元数据和受控下载引用）；
- reply/thread context；
- received_at、signature metadata；
- deduplication key。

### `OutboundMessage`

- target conversation/user；
- kind：text/card/image/file/link/progress；
- content/asset ID；
- reply_to；
- idempotency key；
- fallback；
- expiry；
- delivery state。

平台原始 JSON 要保留受限、短期副本供排错，但业务代码不要依赖其任意字段。

如果后续 Channel、Cloud Gateway 和本地 Worker 都通过事件通信，可参考 CloudEvents
1.0 的 `id`、`source`、`specversion`、`type`、`time` 和数据内容字段作为外层信封，
再把 `InboundMessage` 放入 `data`。CloudEvents 统一事件元数据，不会替代平台验签、
用户绑定和业务幂等
([CloudEvents Specification 1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md))。
首个单进程飞书 PoC 不必为了采用标准而先引入事件总线。

## 3. Event 接收的正确顺序

1. 验证平台身份、时间戳/签名；
2. 检查事件 ID 是否重复；
3. 解析最小必要字段；
4. 写入 Inbox/Job；
5. 在平台时限内 ACK；
6. 后台下载附件、运行 Agent；
7. 结果写 Outbox 并重试发送。

飞书官方要求长连接消息处理在 3 秒内完成，否则触发超时重推；Webhook 也要求尽快
返回，官方优化指南建议先返回成功并异步处理耗时工作
([飞书长连接](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case?lang=zh-CN),
[事件回调优化](https://open.feishu.cn/document/event-subscription-guide/event-subscriptions/event-callback-optimization-guide))。

## 4. 飞书接入方式

飞书事件订阅支持：

- 长连接：SDK 与平台建立 WebSocket 出站连接，本地只需可访问公网；
- Webhook：平台向开发者公网 HTTPS 地址发送 HTTP POST。

官方对企业自建应用推荐长连接；其无需公网 IP/域名，并封装鉴权、加密和验签
([飞书事件概述](https://open.feishu.cn/document/server-docs/event-subscription-guide/overview?from=from_parent_docs))。

### 为什么首期选择长连接

这是项目建议：

- 本地 Mac 无需开放入站端口；
- 避免先处理域名、证书和内网穿透；
- 适合个人电脑常驻 Worker；
- 可直接完成“手机飞书 → 本地 Agent”PoC。

限制：

- 仅企业自建应用；
- 电脑睡眠/离线时连接中断；
- 每应用连接数和集群消费有平台规则；
- 多实例是竞争消费，不是广播；
- 仍需本地去重、重连和健康检查。

### 当前 SDK 与项目实现

截至 2026-07-29，项目使用飞书官方独立 Python 包 `lark-channel-sdk` 1.2.x，而不是
旧 `lark-oapi` 包中的 `lark_oapi.ws.Client`。官方仓库说明新 Channel 能力进入独立
SDK，旧入口进入兼容维护阶段
([Channel SDK 快速开始](https://github.com/larksuite/channel-sdk-python/blob/main/docs/quickstart.md),
[lark-oapi Python SDK](https://github.com/larksuite/oapi-sdk-python))。

当前代码对应关系：

| 能力 | 实现 |
| --- | --- |
| 长连接生命周期 | FastAPI lifespan 启停 `FeishuChannel` |
| 平台安全 | SDK `SecurityConfig(mode="strict")` |
| 入口策略 | 私聊开放到本地策略层；群聊默认禁用，启用后必须 @机器人 |
| 用户授权 | 本地 Open ID 白名单；空白名单只返回用户 ID，不执行 Agent |
| 幂等 | SDK DedupStore + `channel.sqlite3` 消息原子占位 |
| 快速处理 | SDK 回调只校验、占位并创建后台任务 |
| 回复 | 回复原消息，并为每个阶段生成确定性 `uuid` |
| 单图输入 | 使用原消息 ID + image key 下载，复用本地图片校验并创建空间照片任务 |
| 图片输出 | 将本地封面上传为飞书图片并回复原消息 |
| 隐私 | SQLite 不保存消息正文；App Secret 存系统钥匙串或加密文件 |

这是单飞书文本 + 单图 MVP，还没有形成跨平台 `ChannelAdapter`、持久 Outbox、离线
补发或完整媒体 Result Presenter。自动测试已覆盖凭证不回显、持久去重、重复消息、
白名单、图片下载、空间任务和封面回传；真实飞书图片权限和手机端链路仍需实验。

### 最小权限与身份

只申请：

- 接收消息事件；
- 向指定用户/群发送消息；
- 下载用户明确发送的资源；
- 上传项目生成的图片/文件。

应用身份、用户身份和资源范围必须分开。接收到 `open_id` 不等于自动获得本地电脑全部
权限；首次使用需绑定，群聊默认只响应 @ 或显式命令。

### 媒体回传

飞书发送消息支持文本、富文本、卡片、图片、视频、音频和文件等类型
([飞书发送消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create))。
图片需先上传获得 `image_key`；官方接口列出格式、10 MB 大小及分辨率限制
([飞书上传图片](https://open.feishu.cn/document/server-docs/im-v1/image/create))。
这些限制可能更新，Adapter 应从配置读取并在上传前转码，而不是写死在 Agent Prompt。

## 5. 微信不是一条接入路线

需拆分：

| 路径 | 官方形态 | 适合度 |
| --- | --- | --- |
| 企业微信自建应用 | 企业内部消息、回调和应用 API | 适合团队/企业场景，需主体与配置 |
| 微信公众号 | 用户关注后通过公众号交互 | 面向外部用户，受消息与审核规则约束 |
| 微信小程序 | 自建移动 UI、上传、WebView/页面 | 适合 Viewer/参数表单，不是常驻后台 Agent |
| 微信开放平台 | 网站/移动应用登录与开放能力 | 解决授权和 App 互通，不等于个人微信 Bot |
| 普通个人微信号 | 无通用公开 Bot API | 不作为本项目正式接入路径 |

官方入口：

- [企业微信开发文档](https://developer.work.weixin.qq.com/document/)
- [微信公众平台开发文档](https://developers.weixin.qq.com/doc/offiaccount/)
- [微信小程序开发文档](https://developers.weixin.qq.com/miniprogram/dev/framework/)
- [微信开放平台](https://developers.weixin.qq.com/doc/oplatform/)

项目明确排除模拟个人微信协议、客户端 Hook、自动点击和长期托管个人登录。这是安全、
稳定性和账号合规决定，不是对其技术可行性的讨论。

## 6. 飞书与微信的渐进路线

1. 飞书企业自建应用 + 长连接：文本闭环；
2. 图片、文件、卡片进度与审批；
3. Cloud Gateway + 本地 Worker（需要离线排队时）；
4. 企业微信 PoC；
5. 公众号/小程序仅在目标用户和主体明确后评估；
6. 不为追求“在微信聊天”而采用非公开协议。

## 7. PoC 验收

- [ ] 白名单用户发文本可创建一个且仅一个 Job；
- [ ] 重复事件不会重复执行；
- [ ] 3 秒内 ACK，耗时任务异步；
- [ ] 断网后自动重连；
- [ ] 非白名单用户不能调用；
- [ ] 群聊不误触发；
- [ ] 图片能安全下载并生成 Asset；
- [ ] 结果能以图片/文件/链接回传；
- [ ] 高风险动作卡片审批后才恢复；
- [ ] Secret 不出现在日志和前端。

## 8. 来源

- [飞书：事件概述](https://open.feishu.cn/document/server-docs/event-subscription-guide/overview?from=from_parent_docs)
- [飞书：使用长连接接收事件](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case?lang=zh-CN)
- [飞书：事件回调优化指南](https://open.feishu.cn/document/event-subscription-guide/event-subscriptions/event-callback-optimization-guide)
- [飞书：发送消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create)
- [飞书：上传图片](https://open.feishu.cn/document/server-docs/im-v1/image/create)
- [飞书 Channel SDK：快速开始](https://github.com/larksuite/channel-sdk-python/blob/main/docs/quickstart.md)
- [飞书 Channel SDK：API 参考](https://github.com/larksuite/channel-sdk-python/blob/main/docs/reference.md)
- [飞书 Channel SDK：持久去重](https://github.com/larksuite/channel-sdk-python/blob/main/docs/dedup-architecture.md)
- [CloudEvents Specification 1.0.2](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md)
- [企业微信开发文档](https://developer.work.weixin.qq.com/document/)
- [微信公众平台开发文档](https://developers.weixin.qq.com/doc/offiaccount/)
- [微信小程序开发文档](https://developers.weixin.qq.com/miniprogram/dev/framework/)

微信部分当前只足以确定官方产品路径和项目排除项，尚不足以形成企业微信、公众号或
小程序的完整能力/限制矩阵；该缺口由 `CHANNEL-003` 继续调研。

返回[外部控制与移动端知识地图](README.md)。
