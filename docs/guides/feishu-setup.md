# 飞书机器人配置与使用指南

作者：**Zhuofan Xie**  
更新日期：2026-07-29

这份指南面向希望通过飞书使用本机个人数字助手的成员。完成配置后，用户可以在飞书
私聊机器人，或在群聊中 `@机器人` 发送指令；指令由运行 Web Agent 的电脑执行，
结果返回原飞书会话。

> 当前 Web 控制台是本机 **Root 管理员视图**。管理员可以查看本机数据库中记录的
> Web 会话和已授权飞书会话。飞书普通用户只能在自己所在的飞书会话中看到消息，
> 但不应把 Web 控制台开放给普通用户。

## 1. 使用前确认

需要同时满足两层授权：

1. **飞书应用可用范围**：用户必须与企业自建应用处于同一飞书租户，并被加入应用
   版本的可用范围；
2. **本项目 Open ID 白名单**：用户的 `ou_...` Open ID 必须填写在 Web 控制台的
   “外部接入”设置里。

飞书企业自建应用主要供同一租户内成员使用；面向不同租户或外部用户需要另行设计
应用发布与外部共享方案。参见[飞书应用类型说明](https://open.feishu.cn/document/home/app-types-introduction/overview)。

准备条件：

- 一台持续运行本项目的电脑，并能访问公网；
- 飞书开放平台管理员或应用开发者权限；
- 已启动后端与 Web 控制台；
- 每位用户自己的飞书账号。

本项目采用飞书官方长连接：电脑主动连接飞书，不需要公网 IP、域名或反向代理，但
电脑必须能访问飞书服务。长连接只适用于企业自建应用，具体约束以
[长连接接收事件官方说明](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case)
为准。

## 2. 创建应用并启用机器人

1. 打开[飞书开放平台](https://open.feishu.cn/app)，创建“企业自建应用”；
2. 在“添加应用能力”中启用“机器人”；
3. 在“凭证与基础信息”中复制 `App ID` 和 `App Secret`；
4. 不要把 `App Secret` 发到群聊、截图、Issue 或提交到 Git。

机器人能力与使用范围可参考
[飞书机器人概述](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/bot-v3/bot-overview)。

## 3. 申请权限

在“权限管理”中按最小权限原则申请：

| 用途 | 需要的能力 |
| --- | --- |
| 接收用户消息 | 接收私聊或群聊中提及机器人的消息权限 |
| 回复文本和卡片 | `im:message:send_as_bot`，或控制台提示的等价消息发送权限 |
| 接收图片事件 | `im:resource`（获取与上传图片或文件资源） |
| 下载用户消息中的原图 | `im:message:readonly` 或 `im:message`；资源接口还需要 `im:resource` |

控制台通常显示中文权限名，而不是 scope。请搜索：

- **获取单聊、群组消息**：`im:message:readonly`；
- **获取与上传图片或文件资源**：`im:resource`。

“读取用户发给机器人的单聊消息”（`im:message.p2p_msg:readonly`）只允许接收私聊事件，
不能代替资源下载所需的 `im:message:readonly`。如果逐项搜索仍找不到，打开
“权限管理 → 批量导入/导出权限 → 导入”，以**应用身份（tenant）**导入：

```json
{
  "scopes": {
    "tenant": [
      "im:message.p2p_msg:readonly",
      "im:message.group_at_msg:readonly",
      "im:message:readonly",
      "im:message:send_as_bot",
      "im:resource"
    ],
    "user": []
  }
}
```

导入后必须创建并发布新版本；若租户要求审批，还需管理员批准新增权限。机器人已经
收到但下载失败的旧消息不会自动重放，请发布后发送一张新图片验证。

飞书会根据所订阅事件提示需要的具体权限。权限变更后通常还需要创建并发布新版本，
否则线上应用不一定生效。参见[权限管理说明](https://open.feishu.cn/document/server-docs/application-scope/introduction?lang=zh-CN)、
[回复消息接口](https://open.feishu.cn/document/server-docs/im-v1/message/reply?lang=zh-CN)和
[上传图片接口](https://open.feishu.cn/document/server-docs/im-v1/image/create?lang=zh-CN)。

## 4. 配置消息事件

进入“事件与回调 → 事件配置”：

1. 选择“使用长连接接收事件”；
2. 添加事件 `im.message.receive_v1`；
3. 保存配置。

这是机器人接收文字和图片消息所必需的事件。飞书推荐使用新版事件结构；配置与发布
方式参见[添加事件官方指南](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/subscription-event-case?lang=zh-CN)。

## 5. 配置卡片按钮回调（可选）

只有使用“功能卡片”的按钮或菜单时才需要这一步。纯文字、图片消息收发无需配置
卡片回调。

进入“事件与回调 → **回调配置**”，而不是“事件配置”：

1. 选择“使用长连接接收回调”；
2. 添加“卡片回传交互”，标识为 `card.action.trigger`；
3. 保存并重新发布应用版本。

如果搜索不到 `card.action.trigger`，先确认当前位置是“回调配置”。飞书将卡片交互
归类为回调，不是普通事件。参见
[卡片回调常见问题](https://open.feishu.cn/document/develop-a-card-interactive-bot/faqs)和
[接收并处理回调](https://open.feishu.cn/document/event-subscription-guide/callback-subscription/receive-and-handle-callbacks?lang=zh-CN)。

## 6. 发布版本并设置可用范围

1. 打开“版本管理与发布”，创建版本；
2. 把测试成员或部门加入“可用范围”；
3. 提交发布，并完成管理员审核；
4. 权限、事件或回调发生变化后，再发布一个新版本。

仅在 Web Agent 白名单里添加 Open ID 不够；用户还必须处于飞书应用可用范围。若
用户找不到机器人、无法把机器人加入群聊或事件完全没有到达，优先检查版本发布和
可用范围。参见[飞书消息常见问题](https://open.feishu.cn/document/server-docs/im-v1/faq)。

## 7. 在 Web Agent 中连接

1. 启动后端和 Web 控制台；
2. 打开首页右上角“外部接入”；
3. 选择“飞书（中国大陆）”；
4. 填写 `App ID` 和 `App Secret`；
5. 点击“测试凭证”；
6. 勾选“保存后启用长连接接入”并保存；
7. 等待状态显示“已连接”。

“测试凭证”成功只说明 App ID 和 App Secret 可以换取应用凭证，不代表事件、权限、
版本和可用范围都已正确配置。

## 8. 获取并授权用户 Open ID

1. 暂时保持 Open ID 白名单为空，保存并启用；
2. 用户私聊机器人发送任意文字；
3. 机器人会返回该用户的 `ou_...` Open ID，但不会执行指令；
4. Root 管理员把 Open ID 添加到“允许的用户 Open ID”，每行一个；
5. 保存后，用户再次发送消息完成验证。

Open ID 是应用维度身份标识，应复制机器人实际返回的值，不能用手机号、邮箱或显示
名称代替。

## 9. 私聊、群聊与图片验证

### 私聊

向机器人发送：

```text
现在几点？
```

预期结果：飞书收到回答，Web Root 控制台出现对应飞书会话的只读镜像。

### 群聊

1. 在 Web Agent 外部接入设置中勾选“允许群聊中已授权用户通过 @机器人 发出指令”；
2. 把机器人加入群聊；
3. 已加入 Open ID 白名单的用户发送 `@机器人 现在几点？`。

未 `@机器人`、未进入白名单或不在应用可用范围的成员不会触发执行。

### 图片

向机器人单独发送一张普通图片。当前项目会先尝试“消息资源”接口，再以独立图片接口
做兼容回退；成功后创建空间照片任务并返回静态封面和短时局域网 Viewer 链接。下载
收到的图片依赖消息资源接口，参见
[获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2?lang=zh-CN)。

飞书聊天气泡不能直接运行 Three.js，但手机浏览器可以打开机器人返回的签名 Viewer
链接。局域网模式使用 `http://192.168.x.x:8766/v/<签名>`；部分飞书内置浏览器会
限制明文 HTTP，此时可按[跨平台部署指南](deployment.md)显式启动临时 HTTPS Tunnel。
链接默认 12 小时过期，只能读取当前空间照片的白名单分层文件。

## 10. 常见故障

| 现象 | 优先检查 |
| --- | --- |
| 测试凭证超时 | 电脑网络、代理、防火墙、飞书域名连通性 |
| 测试成功但一直未连接 | 是否勾选启用、区域是否正确、长连接事件配置 |
| 已连接但机器人不回复 | `im.message.receive_v1`、应用版本、可用范围、Open ID 白名单 |
| 私聊返回 Open ID 但不执行 | 这是未授权保护；把 Open ID 加入白名单并保存 |
| 群聊没有反应 | 群聊开关、机器人是否入群、用户白名单、是否 `@机器人` |
| 能收文字但图片提示 `99991672` | 同时开通 `im:resource` 与 `im:message:readonly`（或 `im:message`），创建并发布新版本，再重新授权 |
| 卡片显示但按钮无效 | 在“回调配置”添加 `card.action.trigger`，不是“事件配置” |
| 机器人重复回复 | 检查本地去重数据库与服务是否重复启动 |

飞书事件处理存在超时和重试机制，因此处理程序必须尽快确认事件，并对同一事件做
幂等处理。参见[事件订阅概述](https://open.feishu.cn/document/server-docs/event-subscription-guide/overview?from=from_parent_docs)。

### 长连接当前加固与下一阶段

当前代码已显式启用 SDK 自动重连、30 秒 keepalive 检查、90 秒唤醒阈值、两次探测
失败判定、出站最多四次退避重试，并把 `reconnecting` 暴露给健康状态；重启连接时会
取消和回收本进程尚未完成的回调任务。生产化仍需完成持久化 Inbox/Outbox、带抖动的
指数退避、死信队列、断网重放、进程看门狗、连接/消息延迟指标和故障注入测试。只有
Outbox 能确保“任务完成但飞书暂时不可用”时，结果不会随进程退出丢失。

## 11. 数据可见性与安全边界

- Web 控制台是本机 Root 管理端，可查看所有被本机记录的渠道会话；
- 飞书普通用户不会获得 Web 控制台权限，只能看到自己参与的聊天；
- 群聊成员都能看到群内机器人回复，不适合处理私人数据；
- App Secret 与模型 API Key 只应保存在运行 Agent 的电脑；
- Open ID 白名单控制“谁可以触发执行”，不能代替飞书应用可用范围；
- 当前尚未实现按管理员角色分权、审计导出、加密消息正文和用户自助删除。

因此，现阶段适合受控团队内测，不应直接作为公开多租户服务。
