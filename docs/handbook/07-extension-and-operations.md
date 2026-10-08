# 7. 添加工具、测试与上线门槛

文档整理：**Zhuofan Xie** · 2026-10-08

[返回主目录](README.md)

## 7.1 先区别 Tool、Handler、Capability 与 Model

- Tool：让 Planner 看见的名称、用途、参数和风险声明。
- Handler：代码入口，把经过校验的参数交给业务服务。
- Capability：产品功能，含部署、状态、作者、权限和输出等信息，可包含多个 Tool。
- Model/Provider：实际计算/生成实现，可隔离进程或服务部署。

Depth Anything V2 是空间工具内部模型，不是一个知道如何鉴权的 Agent。
新增一个 Tool 也不会自动产生一个 Web 工作台或飞书菜单卡片。

## 7.2 从无副作用的小工具开始

以下为教学示例，尚未注册到本项目。不要把它列入已有工具数量。

在 [tools.py](../../backend/app/tools.py) 新增 handler：

```python
def echo_upper(arguments: dict[str, Any]) -> dict[str, Any]:
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ToolError("echo_upper requires non-empty text")
    return {"result": text.upper()}
```

在 `build_default_registry()` 的 `return registry` 之前注册：

```python
registry.register(
    ToolSpec(
        name="echo_upper",
        description="将输入英文文本转为大写，仅用于教学",
        parameters={
            "type": "object",
            "properties": {"text": {"type": "string", "maxLength": 200}},
            "required": ["text"],
            "additionalProperties": False,
        },
        handler=echo_upper,
        risk_level="read",
        requires_approval=False,
        idempotent=True,
    )
)
```

为什么只读起步：先验证参数和注册，不引入文件删除、外发与无法撤回的副作用。
Schema 校验与 handler 防御各有作用，不能只在 UI 校验。
DemoPlanner 不会自动理解新工具；开启真实 Tool Calling 模型，或写确定性测试验证。

## 7.3 为示例写一个确实会拒绝错误参数的测试

新建教学测试文件后：

```python
import pytest
from backend.app.tools import ToolError, build_default_registry

def test_echo_upper():
    registry = build_default_registry()
    registry.validate_call("echo_upper", {"text": "hello"})
    assert registry.execute("echo_upper", {"text": "hello"}) == {"result": "HELLO"}
    with pytest.raises(ToolError):
        registry.validate_call("echo_upper", {"text": 123})
    with pytest.raises(ToolError):
        registry.validate_call("echo_upper", {"text": "ok", "extra": True})
```

调用方必须先 validate 再 execute，单独调用 `execute()` 不会自动跑全部 policy。
教学示例不调用网络、不读取用户数据；模型工具选择还要另测，不能以单元测试替代 Planner 效果。

## 7.4 将真实模型变成完整可安装功能

按[能力接入指南](../guides/capability-integration.md)和
[媒体能力 SOP](../guides/feishu-media-capability-sop.md)推进：

| 交付项 | 你要做什么 | 为什么 |
| --- | --- | --- |
| 输入契约 | 受控 source/dataset ID、尺寸/类型/数量 | 避免任意路径和资源越权 |
| Capability/Tool 声明 | 版本、作者、风险、Schema、批准/幂等 | 产品与执行策略可审查 |
| Provider 契约 | health、能力/模型版本、create/status/result | 不把模型进程耦合进聊天回调 |
| 安装计划 | 平台、依赖、空间、权重、哈希、许可证、Smoke | 新电脑可复现，不依赖开发者私有缓存 |
| 异步任务 | 原子 Job/Asset、进度、失败、重试 | 图像耗时不阻塞用户反馈 |
| 结果 Presenter | 缩略图/文件/Viewer、渠道差异 | 用户能消费结果，而非得到服务器路径 |
| UI/菜单 | ToolLibrary、工作台、Feishu 菜单与回调 | 后端注册不会自动补齐所有前端入口 |
| 测试/记录 | Fake 契约＋真实模型＋真实渠道＋质量 | 区分工程可用和效果达标 |

模型下载执行固定白名单动作，不允许 LLM 生成任意 Shell。
涉及许可证或大下载量必须由本机所有者明确确认；非幂等/高风险外写需要审批。

## 7.5 幂等：创建与执行不是同一层

以风格化为例：

1. 前端同步锁，第一次点击立刻进入 submitting；
2. 同一提交复用 Idempotency-Key 和输入指纹；
3. 服务事务预留 Asset/Job；
4. worker 使用原子 claim 和 lease 获取执行权；
5. 完成/失败状态落库，重试保留合法输入。

源码：
[PhotoStyleStudio](../../app/components/PhotoStyleStudio.tsx) 的提交逻辑；
[AssetRepository](../../backend/app/assets.py) 的
`create_asset_and_job()`、`claim_job()`、`refresh_job_lease()`、
`claim_failed_job_retry()`；
[PhotoStyleService](../../backend/app/style_transfer.py)。

“同一图片”不必永远只生成一次：不同参数/seed、明确重新生成是不同业务操作。
同一个 key 携带不同输入也不该返回不相关旧结果。

边界：空间照片 Agent/飞书路径可传执行/消息幂等键；当前直接
`POST /api/spatial-scenes` 没有把 HTTP Idempotency-Key 传入服务。
不能据已有测试宣称所有 Web 入口都具备完整跨进程防重。
工具执行账本与图像 Job claim 是不同组件，也不能把前者自动推断为通用分布式 exactly-once。

## 7.6 重启、中断、重试分别处理什么

| 场景 | 当前处理 | 进一步生产化 |
| --- | --- | --- |
| 未启动 queued 空间任务 | 启动时重新提交 | 定时对账、容量控制 |
| running 租约过期 | 标记中断并保留输入供用户重试 | 心跳/强制取消/安全恢复规则 |
| Agent 可恢复 Checkpoint | reconcile 分类，Root 继续/终止 | 限制并发恢复，持续审计 |
| 非幂等动作结果不明 | 阻止盲目重放，人工核对 | 外部系统对账/业务事务协议 |
| 结果产生但未发送 | 渠道有界发送重试 | 持久化 Outbox/退避/死信 |
| 电脑断网/休眠 | 可观察连接状态，恢复依赖进程在线 | 守护/开机启动/在线健康 |

通过界面“重试”复用原始输入，不通过删数据库强行恢复。
模型失败、任务失败和发送失败应分别记录，避免为发送失败再次昂贵推理。

## 7.7 多用户和批量不是一个无限 asyncio.gather

当前 Orchestrator 在进程内使用锁，图调用不是已经实现高吞吐并行；
空间生成默认单 worker，持久化 claim 防同一 Job 多实例竞争。
两者是基础正确性，不是全局 GPU 显存调度器或按用户公平队列。

上线方案应再明确：

- owner 限流与队列长度；单用户批量占用上限；
- GPU 全局容量/显存预算，跨功能避免同时加载挤爆内存；
- 按用户公平调度、优先级和等待预估；
- 取消/退避/超时、队列持久化及恢复规则；
- SQLite 写竞争与多实例压测，达到需求后再考虑服务型数据库/队列。

SQLite 事务有助于原子记录，但 WAL 不表示写入无限并行；
机制参考 [Python sqlite3](https://docs.python.org/3/library/sqlite3.html)。
不用为了“生产化”立刻加入 Redis/Kubernetes，先用目标并发和实测瓶颈决定。

## 7.8 本机可执行的分层验证

```bash
# 不触发真实模型下载或飞书发送的专项测试
.venv/bin/python -m pytest backend/tests/test_p0_reliability.py backend/tests/test_token_usage.py -q
.venv/bin/python -m pytest backend/tests/test_context.py -q
.venv/bin/python -m pytest backend/tests/test_orchestration.py -q

# 本机服务启动后查看运行面
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/api/ops/metrics
```

完整后端测试：`.venv/bin/python -m pytest backend/tests -q`。
前端：`pnpm run lint`、`pnpm run test`。
真实图像 Smoke、飞书权限/回传、手机预览和质量验收单独进行。
本轮执行了哪些检查见[复核记录](validation-2026-10-08.md)，不把教程命令写成已经跑过。

metrics 是进程聚合 JSON；进程重启后的长期监控/告警系统仍需接入。
ready 当前包含配置/存储检查，飞书为可选状态，不证明全部模型能推理。

指标只记录聚合值，不等于所有日志都安全。
[JsonLogFormatter](../../backend/app/observability.py) 会保留调用点的消息及异常文本，
它本身不是自动脱敏器；Run/工具轨迹也可能含参数或输出。共享排障材料前须去除凭证、个人
内容和可访问资产的签名 URL，生产环境还需统一采集过滤、访问控制及保留周期。

## 7.9 上线前不要跳过的门槛

1. Root Web/API 不能直接对普通用户/公网开放；先做认证、授权与受控代理。
2. 图片输入、用户/会话/资产越权和提示注入要有负例测试；筛选工具清单之外仍需硬授权门禁。
3. 从手机入站到用户看到最终结果做完整链路测量。
4. 固定样本分别评测性能和视觉质量，报告 P50/P95、成功率及资源峰值。
5. 状态持久化、重复提交、断网、休眠、进程崩溃与通知重发做故障矩阵。
6. 数据保留/删除/导出、备份密钥、模型/依赖版本与许可可审计。
7. Token 优化同时保留任务质量；没有价格版本不承诺费用节省。
8. 真实设备验证过才写“兼容/部署完成”，不能用 Fake 替代。

## 7.10 推荐的练习完成标准

你能画出请求/Run/Job/Asset/渠道交付的区别；
能注册与测试一个只读工具；
能解释为何采用 LangGraph 但不把全部问题交给 LLM；
能找出一次失败属于输入、模型、队列、授权、发送还是 Viewer；
最后再给出一份有证据的新增能力接入 PR。

[返回主目录](README.md) · [返回项目文档中心](../README.md)
