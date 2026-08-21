# 工程与 AI 基础知识地图

作者：**Zhuofan Xie**
更新日期：2026-07-28
状态：核心正文已核对，项目实验待补

本分域提供理解 Agent、外部控制和端侧部署所需的基础，不展开与项目无关的完整
计算机课程。

## 正文导航

0. [项目工程基础：环境、依赖、配置、测试与调试](00-project-engineering.md)
1. [Web、网络与异步编程基础](01-web-async.md)
2. [可靠性、任务状态与数据存储](02-reliability-data.md)
3. [LLM 应用与安全基础](03-llm-security-basics.md)

以下知识地图保留为查漏清单；正文中的“项目建议”不是已经完成的 ADR，“待实验”也
不代表已在本项目设备上验证。

零基础读者先完成第 0 章的启动和测试，再按知识库
[实战路线](../implementation-roadmap.md)进入具体阶段。下方大纲无需一次读完。

## 1. Web 与网络

### 1.1 HTTP

- Request、Response、Header、Body 和状态码；
- REST、RPC 与 Streaming；
- HTTPS、TLS 与证书；
- 超时、重试、连接池和代理；
- 文件上传、分片和下载；
- Server-Sent Events。

### 1.2 长连接

- WebSocket；
- Long Polling；
- Heartbeat；
- 断线检测与重连；
- 正向连接与反向连接；
- 移动网络切换。

### 1.3 网络部署

- DNS、域名和证书；
- NAT、CGNAT 和端口转发；
- Reverse Proxy；
- Tunnel 与 Relay；
- 局域网发现；
- 防火墙和零信任访问。

## 2. Python 与异步编程

### 2.1 类型与数据模型

- Type Hint；
- Dataclass；
- Pydantic；
- JSON Schema；
- 输入验证和错误模型。

### 2.2 并发模型

- Thread、Process 和 Async IO；
- Event Loop；
- Coroutine、Task 和 Future；
- 锁、队列和背压；
- CPU 密集与 IO 密集任务；
- GPU 任务和工作进程。

### 2.3 Web 服务

- FastAPI；
- Middleware；
- Dependency Injection；
- Streaming Response；
- 生命周期和优雅退出；
- 测试客户端。

## 3. 分布式系统

### 3.1 消息语义

- At-most-once、At-least-once 和 Exactly-once；
- 幂等键；
- 重复、乱序、延迟和丢失；
- Event ID、Message ID、Run ID 和 Job ID。

### 3.2 可靠性模式

- Retry 与 Exponential Backoff；
- Timeout；
- Circuit Breaker；
- Dead Letter Queue；
- Outbox Pattern；
- Saga 与补偿操作；
- Eventual Consistency。

### 3.3 状态与并发

- 状态机；
- 事务；
- 乐观锁和悲观锁；
- 并发更新；
- 分布式锁的边界；
- Side Effect 与重复执行。

## 4. 数据与存储

### 4.1 关系数据库

- SQLite 与 PostgreSQL；
- Schema、Index 和 Migration；
- Transaction；
- 备份与恢复；
- 多用户隔离。

### 4.2 搜索与向量

- Full-Text Search；
- Embedding；
- Vector Store；
- Hybrid Search；
- Reranker；
- 召回率与精确率。

### 4.3 文件和对象资产

- 文件哈希；
- Metadata；
- 派生关系；
- 临时文件；
- 生命周期；
- 大文件和对象存储。

## 5. AI 与 LLM 基础

### 5.1 模型交互

- Token；
- Context Window；
- System、User、Assistant 和 Tool Message；
- Sampling；
- Streaming；
- 多模态输入。

### 5.2 Embedding、RAG 与 Fine-tuning

- 三者解决的问题；
- 适用边界；
- 数据需求；
- 隐私与成本；
- 项目中何时不需要训练。

### 5.3 推理与部署

- 云端 API 与本地模型；
- CPU、MPS、CUDA 和 NPU；
- 精度与量化；
- 延迟、吞吐、内存和显存；
- 预热、缓存和批处理。

## 6. 安全基础

- Authentication 与 Authorization；
- OAuth、JWT、API Key 和 Secret；
- 最小权限；
- 输入验证；
- 命令注入、SSRF 和路径穿越；
- 加密存储和密钥轮换；
- 审计；
- 软件供应链。

## 7. 完成标准

- [ ] 每个二级主题有独立正文或明确合并理由；
- [ ] 每篇正文包含与本项目的对应关系；
- [ ] 分布式系统部分提供重复消息和任务恢复实验；
- [ ] 安全部分关联项目威胁模型；
- [ ] 性能部分统一项目测量术语。

返回[知识库总导航](../README.md)。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
