# 项目工程基础：环境、依赖、配置、测试与调试

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：项目基线已核对  
关联任务：`LEARN-001`、`QA-001`

本章只补齐从零参与本项目必须具备的工程心智模型。Git 命令细节遵循仓库内
[`team-git-workflow`](../../../.codex/skills/team-git-workflow/SKILL.md)，不在这里
复制一套容易冲突的流程。

## 1. 先认识当前技术栈

| 层 | 当前实现 | 主要职责 |
| --- | --- | --- |
| Web UI | React、TypeScript、Next/Vite 相关构建 | 控制台、设置、上传和 Viewer |
| API | FastAPI、Pydantic | HTTP 路由、Schema 和错误边界 |
| Agent | Python 自研 Runner | 规划、Tool 执行和 Trace |
| 网络 | HTTPX | 调用 OpenAI-compatible Provider |
| 数据 | SQLite、本地文件 | Job、Asset、配置和 Trace |
| AI/视觉 | PyTorch、Transformers、Pillow | 深度估计和图像处理 |
| 3D | Three.js | 浏览器端空间照片视差 |
| 测试 | pytest、Node test、ESLint | 后端、渲染和静态检查 |

精确版本范围以 `backend/requirements.txt`、`package.json` 和 `pnpm-lock.yaml` 为准，
README 中的文字只是入口，不能代替锁文件。

## 2. 可复现环境

Python 使用项目专用虚拟环境，Node 使用仓库声明的 pnpm 版本。Python 官方说明
`venv` 用于创建与基础安装隔离的环境
（[Python venv](https://docs.python.org/3/library/venv.html)）。

最小顺序：

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r backend/requirements.txt
pnpm install
```

工程规则：

- 不把 `.venv`、`node_modules`、模型权重和构建缓存提交到 Git；
- 不在未记录的全局 Python/Node 环境里“临时装好就算完成”；
- 修改依赖时同时更新声明和锁文件，并记录原因；
- 模型文件要记录来源、版本、hash 和许可，不混入普通软件依赖；
- 新成员必须能只依靠仓库文档复现启动和测试。

pnpm 的 lockfile 用于解析一致的依赖图；CI 和本地应优先使用锁定结果
（[pnpm Settings：lockfile](https://pnpm.io/settings#lockfile)）。

## 3. 配置不是代码，Secret 不是普通配置

| 类型 | 示例 | 存放原则 |
| --- | --- | --- |
| 非敏感默认 | timeout、功能开关、默认 Base URL | 版本化配置或代码默认 |
| 本机配置 | 当前 Provider、模型名 | 本地配置文件，不含明文 Key |
| Secret | API Key、Channel Secret、签名私钥 | 系统钥匙串或加密 Secret Store |
| 用户数据 | 消息、图片、资产、记忆 | 本地数据目录、权限和生命周期 |

当前 `backend/app/settings.py` 已将 LLM 配置与 API Key 分离，并优先使用系统钥匙串。
后续新增飞书、Viewer 或设备凭证时应复用这一原则，而不是添加新的明文 JSON。

`.env.example` 只能放变量名和安全示例，不能放真实凭证。日志、测试快照、错误响应、
截图和 Trace 也属于常见泄露入口。

## 4. 先定义 Contract，再连接模块

跨模块边界至少要有：

- 输入/输出 Schema；
- 稳定 ID 和版本；
- 错误类型与 HTTP 状态；
- timeout、取消、重试和幂等语义；
- 用户/资源所有权；
- 哪些字段可进入日志；
- 向后兼容和 migration 规则。

FastAPI 基于 OpenAPI 生成交互式 API 描述，Pydantic 负责数据验证；它们能减少接口
歧义，但业务授权、文件安全和幂等仍需显式实现
（[FastAPI First Steps](https://fastapi.tiangolo.com/tutorial/first-steps/),
[Pydantic Models](https://docs.pydantic.dev/latest/concepts/models/)）。

## 5. 测试层次

### 每次改动前后

```bash
pytest backend/tests
pnpm run lint
pnpm run test
```

pytest 会发现约定命名的测试并提供 fixture 等机制
（[pytest Get Started](https://docs.pytest.org/en/stable/getting-started.html)）。

### 测什么

- Unit：纯函数、Schema、Policy、状态迁移；
- Contract：Provider、Tool、Channel 和 Capability Adapter；
- Integration：API、SQLite、Queue、Worker、Asset；
- End-to-End：手机聊天到本地执行再回传；
- Evaluation：概率性 Agent/模型在固定数据集上的分布；
- Failure Injection：断网、重复事件、进程退出、GPU OOM、磁盘满。

真实 API 测试不能替代 Fake/Mock：前者验证兼容性，后者保证错误路径可重复。

## 6. 调试顺序

遇到“没有变化”或“加载失败”时，按链路定位：

1. 浏览器 Network 是否发出请求、状态码和响应 Schema 是否正确；
2. API 是否创建唯一 `run_id` 或 `job_id`；
3. Agent Trace 是否选择正确 Tool；
4. Job 状态和错误是否持久化；
5. Asset 文件、manifest 和 URL 是否一致；
6. Viewer 是否成功下载资产、解析并渲染；
7. 平台回传是否有 outbound/delivery 状态。

不要先通过反复重启掩盖状态错误。保存最小输入、ID、版本和错误类别，才能形成回归
测试。

## 7. Git 协作的最小原则

- 从看板领取任务并使用任务 ID；
- 从最新 `main` 创建独立功能分支；
- 只暂存本任务文件，提交信息说明“为什么”；
- 提交前运行相关测试和敏感文件检查；
- PR 关联研究、实验、ADR 和验收证据；
- 合并后删除远程分支、更新本地 `main`、删除本地分支；
- 本轮用户要求“只在本地”时，不 stage、commit 或 push。

具体命令、冲突处理和看板同步以项目 Git Skill 为唯一流程来源。

## 8. 阶段验收

- [ ] 新环境能按照 README 启动前后端；
- [ ] 能运行三组测试并理解每组覆盖范围；
- [ ] 能说明配置、Secret、数据和生成资产的边界；
- [ ] 能从 UI 请求追踪到 Run、Tool、Job 和 Asset；
- [ ] 能新增一个受 Schema 约束的 Tool 及其测试；
- [ ] 不把“本机能运行”误写成“跨设备/跨账号已验证”。

## 9. 来源

- [Python venv](https://docs.python.org/3/library/venv.html)
- [pnpm Settings：lockfile](https://pnpm.io/settings#lockfile)
- [FastAPI First Steps](https://fastapi.tiangolo.com/tutorial/first-steps/)
- [Pydantic Models](https://docs.pydantic.dev/latest/concepts/models/)
- [pytest Get Started](https://docs.pytest.org/en/stable/getting-started.html)
- [Git Reference](https://git-scm.com/docs)

返回[工程与 AI 基础知识地图](README.md)。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
