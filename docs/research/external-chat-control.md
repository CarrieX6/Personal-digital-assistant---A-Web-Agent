# 外部聊天控制与生成结果回传调研

作者：**Zhuofan Xie**  
更新日期：2026-07-27

## 1. 结论

第一条实现链路应选择**飞书企业自建应用 + 官方 SDK 长连接**。

原因：

- 家中电脑主动建立 WebSocket 长连接，不需要公网 IP、域名或内网穿透；
- 官方 SDK 已处理连接鉴权和加密传输；
- 可以接收机器人消息事件并调用消息 API 回复；
- 消息卡片适合展示任务状态、按钮和结果链接；
- 与“电脑常驻 Agent、手机远程下指令”的产品形态吻合。

微信方向不建议从普通个人微信开始。普通个人微信没有适合此项目的稳定公开 Bot
接口，模拟登录和非公开协议存在账号与维护风险。第二渠道优先考虑企业微信自建应用
或微信公众号，并复用统一的 Channel Adapter。

## 2. 飞书接入

### 2.1 推荐模式

使用企业自建应用，启用机器人能力并订阅 `im.message.receive_v1`。本地 Python 进程
通过飞书 SDK 建立长连接：

```text
飞书云端
   ⇅ WebSocket
本地 FeishuChannelAdapter
   → 验证用户和消息幂等
   → AgentRunner
   → 本地工具 / 任务队列
   → 飞书消息或卡片
```

官方长连接模式的关键约束：

- 本地设备需要能够访问公网；
- 只支持企业自建应用；
- 消息回调需要快速完成，耗时生成必须放入异步队列；
- 多个长连接实例是集群消费，不是每个实例都收到同一事件。

### 2.2 首版权限最小集

具体权限名称应以创建应用时的飞书开发者后台为准。首版只申请：

- 接收用户发给机器人的单聊消息；
- 以应用身份发送单聊/群聊消息；
- 读取用户明确发送给机器人的图片或文件；
- 上传并发送生成后的图片或文件。

不申请通讯录写入、云文档写入等无关权限。

### 2.3 回调处理

回调只做轻量工作：

1. 验证事件和应用身份；
2. 使用事件 ID / 消息 ID 幂等；
3. 检查用户白名单；
4. 保存必要附件到受控临时资产；
5. 创建 Agent Run 或异步任务；
6. 立即返回并发送“已接收”状态；
7. 后台任务完成后主动更新卡片或发送新消息。

### 2.4 结果卡片

建议卡片字段：

```text
任务名称
状态：排队 / 运行 / 完成 / 失败
进度与当前阶段
输入资产摘要
结果缩略图
[打开交互预览] [下载资产] [取消任务]
```

卡片按钮只携带不可猜测的任务 ID，不携带本地路径或 API Key。

## 3. 微信与企业微信

### 3.1 普通个人微信

不采用以下方式：

- 模拟桌面客户端点击；
- Hook 微信进程；
- 非公开登录协议；
- 扫码后长期托管个人账号。

这些方式不是稳定产品接口，容易随客户端升级失效，也可能产生账号安全问题。

### 3.2 可评估的官方路径

| 路径 | 适用场景 | 主要限制 |
| --- | --- | --- |
| 企业微信自建应用 | 团队内部使用、身份明确 | 需要企业主体和应用配置 |
| 微信公众号 | 用户通过公众号发送消息 | 回调需要公网 HTTPS 服务 |
| 微信小程序 | 控制面板和 Viewer | 不是纯聊天机器人，需要发布审核 |

实现时由 `WeComChannelAdapter` 或 `OfficialAccountChannelAdapter` 完成平台差异，
Agent、任务、记忆和资产层不感知具体渠道。

## 4. 空间照片和 3D 结果如何在聊天中预览

### 4.1 聊天内直接展示

聊天客户端通常不允许在普通消息中运行任意 Three.js/WebGL，因此不能把当前
交互式 Viewer 直接嵌入普通消息。应该生成渠道友好的派生结果：

- 封面图；
- 左右轻移的 3–5 秒 MP4 演示；
- 低分辨率 GIF（仅作兼容降级）；
- 资产名称、模型、尺寸、生成耗时；
- 打开 Viewer 的按钮。

视频优先于 GIF：画质更好，通常体积更小，移动端解码能耗也更低。

### 4.2 交互式预览

交互式预览继续复用当前 Web Viewer，但增加：

- 独立的只读资产路由；
- 短时签名 Token；
- HTTPS；
- 移动端触摸和设备姿态控制；
- 资源按需加载；
- 2D 静态回退；
- 到期、撤销和访问日志。

远程打开 Viewer 有三种部署方式：

| 方式 | 优点 | 缺点 |
| --- | --- | --- |
| 同一局域网访问电脑 | 数据不经过公网中继 | 离开家庭网络不可用 |
| 认证隧道/反向代理 | 不需要路由器端口转发 | 依赖第三方网络服务 |
| 云端只存加密预览 | 随时可访问 | 资产离开本机，成本和隐私更复杂 |

首版建议使用局域网或经过认证的短时隧道，并明确提示用户当前数据路径。

### 4.3 3DGS / GLB / PLY

消息内发送：

- turntable MP4；
- 压缩封面；
- 文件大小、格式和设备要求；
- Web Viewer 链接；
- 原始资产文件（平台大小限制允许时）。

Viewer 根据设备能力选择 3DGS 数量、分辨率和帧率，低端手机自动回退到视频。

## 5. 记忆与渠道身份

不要用昵称作为身份主键。使用：

```text
local_user_id
channel
channel_tenant_id
channel_user_id
conversation_id
```

同一个人绑定多个渠道时，必须在可信设备上完成一次显式绑定。记忆写入区分：

- 用户明确要求记住；
- 系统为了任务恢复保存；
- Agent 推断但尚未确认。

第三类默认不进入长期记忆。

## 6. 最小可行开发任务

1. 定义 `ChannelAdapter`、`InboundMessage`、`OutboundMessage`；
2. 新增 `backend/app/channels/feishu.py`；
3. 增加飞书凭据的加密配置；
4. 实现长连接、用户白名单和幂等表；
5. 把消息交给当前 `AgentRunner`；
6. 文本回复闭环；
7. 图片附件转本地 `source_image_id`；
8. 空间照片任务进度卡片；
9. 导出 MP4 演示和签名 Viewer 链接；
10. 再抽象企业微信适配器。

## 7. 官方资料

- 飞书开放平台，使用长连接接收事件：  
  https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case
- 飞书开放平台，事件概述：  
  https://open.feishu.cn/document/server-docs/event-subscription-guide/overview
- 飞书开放平台，消息概述：  
  https://open.feishu.cn/document/server-docs/im-v1/introduction
- 飞书开放平台，消息常见问题：  
  https://open.feishu.cn/document/server-docs/im-v1/faq
- 飞书开放平台，使用自定义机器人发送卡片：  
  https://open.feishu.cn/document/feishu-cards/quick-start/send-message-cards-with-custom-bot
- 企业微信开发者中心：  
  https://developer.work.weixin.qq.com/document/
- 微信开放文档：  
  https://developers.weixin.qq.com/doc/

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
