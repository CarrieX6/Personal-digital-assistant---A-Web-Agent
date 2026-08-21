# Web、网络与异步编程基础

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：已核对  
关联任务：`LEARN-001`、`LEARN-AGENT-001`、`LEARN-CHANNEL-001`

## 1. 为什么 Agent 项目必须先懂这些

个人数字助手不是“一个模型加一个聊天框”，而是一组跨网络、跨进程、持续运行的
服务：聊天平台送来事件，Gateway 验证并标准化，Agent 调用模型和工具，后台任务
生成资产，最后再把结果推回聊天窗口。链路中的大部分时间都在等待网络、磁盘、
数据库或模型服务，所以 I/O 并发、超时、取消和恢复比单次算法速度更先决定可用性。

## 2. HTTP API 的最小心智模型

HTTP 是请求—响应协议。项目中至少要区分：

- 方法与语义：`GET` 读取，`POST` 创建或触发，`PUT/PATCH` 更新，`DELETE` 删除；
- Header：认证、内容类型、追踪 ID、幂等键等控制信息；
- Body：JSON、表单或二进制内容；
- 状态码：成功、客户端错误、服务端错误；
- 超时：连接、读取、写入、连接池等待应分别限制；
- 重试：只重试可恢复错误，并确认操作幂等；
- 流式返回：用于逐字输出和任务事件，不等于任务已经持久化。

[OpenAPI Specification](https://spec.openapis.org/oas/latest.html) 是与编程语言无关的
HTTP API 描述标准，可用于生成文档、客户端和测试。对本项目而言，Agent Tool 和
Capability 的 HTTP 接口应先有机器可读 Schema，再写调用代码。

### REST、RPC 与异步任务

| 形式 | 适用问题 | 主要代价 |
| --- | --- | --- |
| REST 资源 API | 用户、会话、资产、任务的增删改查 | 长任务不能占住请求等待 |
| RPC/动作 API | `generate_spatial_scene` 这类明确动作 | 容易把状态和错误语义藏在动作名里 |
| 流式 API | Token、进度、日志、局部结果 | 断线后的恢复和补发要另行设计 |
| Job API | 秒级以上、可取消、可恢复的生成任务 | 需要任务表、队列和状态机 |

项目建议：聊天事件到达后快速创建 Job 并 ACK；执行与回传在后台完成。不要让飞书
回调或普通 HTTP 请求一直等待 GPU 任务。

## 3. WebSocket、SSE、Webhook 与长轮询

WebSocket 在握手后建立双向消息通道；RFC 6455 将其定义为独立的、基于 TCP 的协议，
其 HTTP 关系主要在初始 Upgrade 握手
([RFC 6455](https://www.rfc-editor.org/rfc/rfc6455.html))。

| 技术 | 方向 | 连接形态 | 典型用途 |
| --- | --- | --- | --- |
| Webhook | 平台 → 服务端 | 每个事件一个 HTTP 请求 | 公网回调、卡片动作 |
| SSE | 服务端 → 客户端 | 单向持久 HTTP 流 | 文本和 Agent 事件流 |
| WebSocket | 双向 | 持久连接 | 飞书长连接、实时交互 |
| Long Polling | 近似服务端推送 | 请求挂起后重建 | 无长连接能力时的兼容方案 |

关键点：

1. 长连接不是可靠队列。断线期间的消息是否重放取决于平台协议。
2. Heartbeat 只说明连接仍可通信，不说明业务任务成功。
3. 移动网络、代理、睡眠和 Wi-Fi/蜂窝切换都会让连接失效。
4. 重连必须使用退避与抖动，不能形成“重连风暴”。
5. 事件要有平台事件 ID，并在本地做去重。

## 4. HTTPS、认证与代理边界

公网入口必须使用 HTTPS。TLS 解决传输机密性和服务端身份验证，但不会自动解决：

- 请求是否来自预期平台；
- 当前平台用户是否绑定本地用户；
- 用户是否有调用某个 Capability 的权限；
- 附件是否安全；
- Agent 是否应该执行高风险动作。

因此链路应按顺序处理：TLS → 平台验签/Token → 重放保护 → 用户绑定 → Capability
授权 → 参数校验 → 审批。不要把 API Key 放在前端、聊天消息或日志中。

## 5. Python `asyncio`

Python 官方将 `asyncio` 定义为使用 `async/await` 编写并发代码的库，适合
I/O-bound 和高层网络代码，并提供 Task、Queue、同步、子进程和网络 API
([Python asyncio](https://docs.python.org/3/library/asyncio.html))。

### 并发不等于并行

- 并发：等待 I/O 时让事件循环推进其他任务；
- 并行：多个 CPU/GPU 执行单元同时计算；
- `async def` 中直接运行深度估计或视频编码，仍会阻塞事件循环；
- GPU 推理、图像处理和阻塞 SDK 应放到进程/线程池或独立 Worker。

FastAPI 官方建议：支持 `await` 的 I/O 库放在 `async def` 中；阻塞库可以用普通
`def`，由框架在合适的执行环境中运行
([FastAPI async/await](https://fastapi.tiangolo.com/async/))。

### 必须掌握的控制面

- `asyncio.TaskGroup`：结构化地管理一组并发任务；
- timeout：为外部请求、工具和整条 Run 设置不同上限；
- cancellation：把取消向子任务和工具传播；
- bounded queue：限制在途任务，形成背压；
- semaphore：限制模型/API/GPU 并发；
- graceful shutdown：停止接收、等待或取消任务、写回状态、关闭连接；
- `asyncio.create_subprocess_exec`：避免拼接 shell 字符串执行外部程序。

Python 官方明确指出，阻塞代码可通过 executor 移出事件循环线程
([Developing with asyncio](https://docs.python.org/3/library/asyncio-dev.html))。

## 6. 推荐的进程边界

```text
API / Channel 进程
  ├─ 接收、验签、标准化、ACK
  ├─ 创建 Job
  └─ 推送轻量状态

Agent Worker
  ├─ 调用 LLM
  ├─ 选择和校验工具
  └─ 生成工作流状态

GPU / Media Worker
  ├─ 深度、3D、试衣等推理
  ├─ 转码、缩略图
  └─ 写入 Asset Store
```

这是项目建议，不是某个框架的强制结构。其价值是把低延迟事件接收与高延迟生成任务
隔离，并允许分别设置并发和资源预算。

## 7. 最小实践清单

- [ ] 每个外部请求都有 timeout；
- [ ] 每个 Job 有 `job_id`、状态和取消入口；
- [ ] Event Loop 中不直接运行 GPU/CPU 重任务；
- [ ] 队列有容量上限；
- [ ] 重试有次数、退避和错误分类；
- [ ] 服务关闭时不会把进行中任务错误标记为成功；
- [ ] 请求、Run、Job 和 Asset 可用 Trace ID 关联。

## 8. 来源

- [Python `asyncio` 官方文档](https://docs.python.org/3/library/asyncio.html)
- [Python：Developing with asyncio](https://docs.python.org/3/library/asyncio-dev.html)
- [FastAPI：Concurrency and async/await](https://fastapi.tiangolo.com/async/)
- [OpenAPI Specification](https://spec.openapis.org/oas/latest.html)
- [RFC 6455：The WebSocket Protocol](https://www.rfc-editor.org/rfc/rfc6455.html)

返回[工程与 AI 基础知识地图](README.md)。

