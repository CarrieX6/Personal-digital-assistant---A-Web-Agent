# 文档与代码复核记录

文档整理：**Zhuofan Xie** · 2026-10-08

任务：DOCS-REPRO-001

[返回主目录](README.md)

## 基线与范围

- GitHub main 与本地 origin/main 的提交均为 `29e9865b23f0ea1c5ba02511c5fc7ec82da2ae23`。
- 使用独立文档工作区/分支 `codex/docs-beginner-handbook`，未修改应用执行代码、个人数据或模型。
- 上一轮 PPT 及其他工作区修改未加入本次提交。
- 本轮补全工程手册、入口导航及已有架构/空间模型文档；不是复制个人学习白皮书。
- 图片风格化/分层记忆主要贡献者 Xianggang Ma；Flux-GS 原始贡献者 Zuheng Zhao。

## 修正的重要不一致

| 原文容易引发的误解 | 复核后的实际边界 |
| --- | --- |
| 分割网络未使用 | Apple Vision/BiRefNet 已接入，深度阈值仅为降级 |
| Memory 导出/加密待开发 | claim/evidence、加密、单条 CRUD/导出和混合检索已存在 |
| Preview/Installer 尚无代码 | 专用签名 Viewer、安装中心和 deploy.py 已存在 |
| 异步提交立即返回待开发 | 图像 Job 创建后的确定性 async_handoff 已存在 |
| 所有 Web 入口都支持幂等 | 风格化读取请求头；直接空间 HTTP 入口未转发该 key |
| 120 秒必然终止任何执行 | 节点间合作式检查，单工具/进程强制取消待补 |
| 所有预览都用 Three.js | Web 为双平面相机；手机专用链接为 CSS 分层位移 |
| Run 完成等于图片已收到 | Run、Job、Asset、渠道交付需要分别验证 |
| 模型缓存意味着完整迁移 | 环境、权重、数据和密钥需要分别迁移/验收 |
| 工具筛选等于完整授权 | 原生 Tool Call 仍需本轮允许集合与角色/能力硬门禁 |
| JSON 格式就保证日志脱敏 | 格式化器保留消息/异常；授权渠道摘要也可能含个人内容 |

## 本轮检查

- 相对链接/代码围栏：使用 [check_docs.py](../../scripts/check_docs.py) 检查 19 份文档，
  238 个相对文件链接无缺失，围栏无未闭合。此检查不证明所有标题锚点或外链可达。
- 检查器测试：[8 项标准库单元测试](../../scripts/tests/test_check_docs.py)全部通过，
  覆盖有效/缺失链接、围栏、外链跳过、尖括号格式等；不依赖应用环境。
- 源码符号：AST 检查 14 个源码文件中的 94 个主要类/函数定义，均存在；
  另人工对照关键 API、环境参数及前端入口。符号存在不等于所有路径均已集成验收。
- 教学示例：3 段 Python 语法检查通过；隔离载入现有 Registry/Schema 实现，
  示例工具正常执行且 4 种非法参数被拒绝。未加载完整应用，不等于集成测试通过；
  教学工具未注册为项目新功能。
- Git：git diff --check 通过，只提交本次工程文档、检查器与其测试。
- 后端专项：尝试运行 P0、Token、Orchestration、Context 四份测试文件。
  已有 Python 3.12 环境超过 5 分钟无 pytest 结果；采样仍在导入堆栈，主动停止后退出 143。
  本轮记为**未完成**，不是测试通过，也没有据此认定应用功能失败；未修改应用环境。

可独立重跑的文档检查：

```bash
python3 scripts/check_docs.py docs/handbook README.md docs/README.md \
  docs/architecture/system-architecture.md docs/architecture/langgraph-agent-loop.md \
  docs/architecture/layered-memory.md docs/architecture/p0-reliability-and-operations.md \
  docs/guides/installation.md docs/guides/complete-local-deployment.md \
  docs/spatial-scene-device-deployment.md docs/project-board.md
python3 -m unittest discover -s scripts/tests -p test_check_docs.py -v
git diff --check
```

## 外部资料与核验限制

- LangGraph Graph API、Persistence、Interrupts：官方页面作为机制参考；项目事实由源码核对。
- Depth Anything V2、BiRefNet：作者官方仓库作为模型任务和集成参考，不臆造额外训练或效果指标。
- MDN DeviceOrientationEvent.requestPermission：解释安全上下文/用户动作，未做所有手机浏览器实测。
- Python sqlite3：说明事务/连接并发边界，不将当前版本文档的新 API 自动套入本项目环境。
- 飞书官方资源下载/上传页面本轮未取得完整可读权限正文；保留官方入口并要求按租户实际 scope、
  发布授权及错误码核验，不声称已经重新确认全部最小权限集合。

## 未执行与不应宣称完成

未重新下载 SDXL/深度/分割权重；未发送真实飞书消息；未在新电脑、Windows/NVIDIA/Spark
或手机浏览器做完整部署/生成/倾斜验收；未重测历史性能、Token 降幅或质量盲评。

历史性能只引用 [PERF-001](../experiments/perf-001-spatial-feishu-baseline-2026-09-03.md) 的设备、
样本数、冷/热启动及本机入站/出站定义。历史专项证据不能当作本轮全量回归结果。

## 升级后如何维护

1. 核对 main 提交、工具注册和 API 契约。
2. 先更新手册当前功能矩阵，再改对应章节与专题文档。
3. 删除过期的“待开发/已完成”表述，保留真实限制和贡献归属。
4. 运行相对链接检查及受影响测试；把真实模型/平台验收单独登记。
5. 按 Git Skill 同步看板、PR 和合并状态，不因写了文档就宣称实现完成。
