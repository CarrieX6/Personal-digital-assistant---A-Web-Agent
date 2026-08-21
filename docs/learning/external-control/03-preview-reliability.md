# 多模态结果回传、3D 预览与可靠性

作者：**Zhuofan Xie**  
更新日期：2026-07-29  
成熟度：架构初稿，平台与真机实验待完成  
关联任务：`CHANNEL-002`、`PREVIEW-001`、`QA-001`

## 1. 聊天窗口不是 3D Runtime

聊天平台擅长文本、图片、短视频、文件、卡片和链接；不能假定它原生播放 GLB、PLY 或
3DGS。一个结果应同时生成“可立即理解的预览”和“完整可交互资产”：

```text
原始结果
  ├─ thumbnail.jpg
  ├─ preview.mp4 / preview.gif
  ├─ summary + metrics
  ├─ interactive viewer URL
  └─ source asset（GLB/PLY/3DGS package）
```

## 2. 格式与降级

| 结果 | 聊天内首选 | 交互形式 | 下载形式 |
| --- | --- | --- | --- |
| 空间照片 | JPG + MP4 视差预览 | Web Viewer | 原图、深度、资产包 |
| Mesh | Turntable MP4 | WebGL Viewer | GLB |
| Point/3DGS | MP4/GIF | 专用 Web Viewer | PLY/压缩包/约定格式 |
| 试衣 | 前后对比图 | Web Gallery/Viewer | PNG/MP4/GLB |
| 长任务 | 卡片状态 | Job 页面 | 最终文件 |

GIF 兼容性高但色彩、体积和帧率差；MP4 更适合动态预览；交互 Viewer 才负责触控、
陀螺仪和视角变化。

## 3. Viewer 安全

Viewer URL 应包含不可猜测的 Asset/Grant，不直接暴露本地路径。建议：

- HTTPS；
- 短时签名、只读、单一 Asset；
- 不在 URL query 放 API Key；
- CSP 限制脚本与外部资源；
- 资产 Content-Type、Magic Number 和大小校验；
- 禁止任意 URL 加载，防止 SSRF；
- 默认不允许目录列举；
- 支持撤销和访问日志；
- 原始高分辨率资产与预览资产分权。

公网 Viewer 可以部署在 Cloud Gateway，也可由受控 Tunnel 暂时代理本机；后者在电脑
离线时不可用。

## 4. 设备姿态与视差

空间照片 Viewer 可使用：

- 鼠标/触控拖拽；
- Pointer move 的轻微视差；
- 手机 Device Orientation（需用户授权和浏览器支持）；
- 自动慢速 Ken Burns/视差视频作为降级。

视角范围必须由深度、遮挡补全质量和裁剪边界限制。大幅移动会暴露单图不可见区域，
因此“能拖更远”不等于更自然；Viewer 应读取资产的安全视角范围。

## 5. 进度消息

不要每秒发一条聊天消息。内部可有细粒度事件，Channel 聚合为：

- 已接收；
- 正在排队；
- 正在处理（阶段名和估计，而非伪精确百分比）；
- 等待确认；
- 已完成；
- 失败（可重试/需要新输入）。

支持更新原卡片的平台优先更新；否则只在状态阶段变化时发送。

## 6. Outbox 与投递状态

`generated` 不等于 `delivered`。Outbox 保存：

- outbound ID 和目标；
- 内容/Asset 引用；
- platform idempotency key；
- attempt、next_retry_at；
- 平台响应 message ID；
- `pending/sending/sent/failed/expired`；
- 错误类别。

若上传媒体成功但发送消息失败，重试应复用有效媒体 key；若 key 过期才重新上传。

## 7. 大文件

- 聊天平台限制之前主动生成较小预览；
- 完整资产放 Asset Store；
- 支持断点/分片仅在存储和客户端都支持时启用；
- 下载链接有有效期和大小提示；
- 手机端不自动下载大模型或大 3D；
- 清理任务不能删除仍被消息/Job 引用的资产。

## 8. 故障与用户体验

| 故障 | 用户看到 | 系统动作 |
| --- | --- | --- |
| 本地电脑离线 | 已排队/设备离线 | 等待或让用户取消 |
| 模型失败 | 失败阶段和可操作建议 | 保存日志、允许换参数 |
| 预览转码失败 | 原始文件可下载 | 重试转码，不重跑模型 |
| Viewer 过期 | 链接已过期 | 认证后重新签发 |
| 平台限流 | 结果已生成，回传延迟 | Outbox 退避 |
| 连接中断 | 状态暂不可用 | 重连后从 Job 状态恢复 |

## 9. 验收实验

- 飞书图片、文件、卡片和 MP4 的真实限制；
- iPhone、华为手机、Android 浏览器打开 Viewer；
- 4G/5G/Wi-Fi 切换；
- 50 MB、200 MB 资产的预览与下载；
- 签名 URL 过期、撤销和越权；
- 本地电脑中途睡眠；
- Outbox 在超时、限流和“响应丢失但发送成功”下的查重。

## 10. 本项目当前实现与下一步

截至 2026-07-29：

- Agent 最终回答已由纯文本改为飞书富文本消息，Markdown 标题不再作为 `###` 原样
  显示；处理中状态仍使用短纯文本；
- 首次对话以及用户发送“菜单”时会返回 JSON 2.0 功能卡片；
- 按钮通过 `card.action.trigger` 长连接事件回到本机，当前已接入空间照片说明、个人
  资产、能力列表和当前时间；
- 飞书收发事件写入本机 SQLite，Web 控制台轮询展示最近 50 条，数据库最多保留 500
  条；
- 空间照片生成完成后，聊天内返回封面 JPG；交互 Viewer 尚未对手机开放。

手机端交互预览不能把 `http://127.0.0.1:3000` 或 `localhost` 发送给用户，因为它们在
手机上指向手机自身。目标链路应为：

```text
电脑生成空间资产
  → 为单个 Asset 签发短时只读 Grant
  → HTTPS Gateway 暴露 Viewer 与低分辨率纹理
  → 飞书卡片返回“打开空间效果”
  → 手机浏览器加载触控 Viewer
  → 链接过期或撤销后拒绝继续访问
```

推荐先实现同一 HTTPS Origin 下的 Viewer 与 `/api/shared-assets/*`，避免让手机页面
直接访问未鉴权的本地 API。飞书移动端若默认在内置容器打开，可在经过安全校验的链接
上增加官方参数 `lk_mobile_jump_to_browser=true` 请求跳转系统浏览器。电脑离线时，
Viewer 也应显示“设备离线”，并继续保留聊天内 JPG/MP4 降级预览。

## 11. 来源

- [飞书：发送消息](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create)
- [飞书：上传图片](https://open.feishu.cn/document/server-docs/im-v1/image/create)
- [飞书：搭建卡片内容](https://open.feishu.cn/document/feishu-cards/feishu-card-cardkit/build-card-content)
- [飞书：处理卡片回传交互](https://open.feishu.cn/document/uAjLw4CM/ukzMukzMukzM/feishu-cards/handle-card-callbacks)
- [飞书：配置网页在浏览器中打开](https://open.feishu.cn/document/uYjL24iN/uMTMuMTMuMTM/web-app-open-ability/configure-webpage-to-open-in-browser)
- [飞书：事件回调优化](https://open.feishu.cn/document/event-subscription-guide/event-subscriptions/event-callback-optimization-guide)
- [WebSocket RFC 6455](https://www.rfc-editor.org/rfc/rfc6455.html)

返回[外部控制与移动端知识地图](README.md)。
