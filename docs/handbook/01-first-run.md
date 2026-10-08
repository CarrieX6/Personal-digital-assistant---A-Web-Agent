# 1. 启动、配置与首次验收

文档整理：**Zhuofan Xie** · 2026-10-08

[返回主目录](README.md)

## 1.1 你需要什么，以及各自的用途

| 前置 | 用途 | 不满足时怎么做 |
| --- | --- | --- |
| Git 和仓库读取权限 | 获取版本、协作更新 | 安装 Git；私有仓库先登录，不把 Token 写进克隆地址 |
| Python 3.11–3.13 | API、Agent 和模型服务 | 先运行安装预检，不随意用系统 Python 覆盖项目环境 |
| Node.js 22.13+ / pnpm | React/vinext 网页 | 从启动环境诊断查看版本和修复命令 |
| LLM 账户 | 真实问答/Tool Calling | 没有时先验证 Demo，不把 Demo 当模型效果 |
| 模型磁盘/算力 | 本地图像生成 | 在设置中心看预检；基础 Web 启动不要求 SDXL 已下载 |
| 飞书自建应用 | 手机指令和结果回传 | 第 6 章配置，先验证 Web 本地链路 |

当前依赖版本分别见 [package.json](../../package.json)、
[后端依赖](../../backend/requirements.txt)和[安装入口](../../scripts/bootstrap.sh)。
CPU/MPS/CUDA 是不同模型的计算设备，不是“运行网页必须有 GPU”。

## 1.2 第一步：只启动基础 Web Agent

macOS / Linux：

```bash
git clone https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent.git
cd Personal-digital-assistant---A-Web-Agent
git switch main
bash scripts/bootstrap.sh --plan
bash scripts/quickstart.sh --install-system-deps
```

Windows PowerShell：

```powershell
git clone https://github.com/CarrieX6/Personal-digital-assistant---A-Web-Agent.git
Set-Location Personal-digital-assistant---A-Web-Agent
git switch main
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap.ps1 --plan
.\scripts\quickstart.ps1 --install-system-deps
```

预检先看计划再执行。系统依赖自动安装仍受平台限制：例如 macOS 需要已有 Homebrew，
Windows 的 winget/PATH 变化可能要求重新打开终端。Linux 缺失依赖按诊断提示安装，不承诺所有发行版一键解决。

为什么使用 quickstart：它默认 `core + photo-style skip`，先让设置界面可用，再逐项安装模型。
不要把它和默认下载真实模型的 `setup.sh` 路径混淆。源码见
[quickstart.sh](../../scripts/quickstart.sh)、[quickstart.ps1](../../scripts/quickstart.ps1)和
[部署器](../../scripts/deploy.py)的 `main()`、`install()`、`start()`。

预期结果：

- Web：`http://localhost:3000/`；
- API：`http://127.0.0.1:8000/health`；
- API 交互说明：`http://127.0.0.1:8000/docs`；
- 首次没有 LLM Key 时可以出现规则演示模式，这是正常的。
- 服务启动窗口需保持运行；关闭进程、电脑睡眠会影响手机任务和 Viewer。

macOS/Linux 验证：

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/api/tools
```

PowerShell 用 `Invoke-RestMethod http://127.0.0.1:8000/health` 查看 JSON。
不需要把 8000 暴露给手机；飞书长连接由电脑主动连接平台。

## 1.3 第二步：先完成一次不依赖图像模型的调用

在 Web 新建会话，发送“现在几点？”或“统计文本：你好 Agent”。

观察：

1. 能看到用户消息、助手回答；
2. 展开执行轨迹，区分规划、策略、工具和最终回答；
3. 刷新页面，会话仍存在；
4. 新建另一会话，历史不会直接合在一起。

没有配置 LLM 时，`DemoPlanner` 只支持既定规则。它验证工程链路，不证明自然语言泛化。
可对照 [AgentRunner](../../backend/app/agent.py) 的 `run()` 和 `DemoPlanner.plan()`，
以及 [API 测试](../../backend/tests/test_api.py) 的 `test_analysis_runs_multiple_tools_and_writes_trace`。

## 1.4 第三步：启用真实 LLM

打开“模型设置”：

1. 选择供应商；
2. 输入该供应商提供的 Base URL、真实可用的模型名和 Key；
3. 先测试连接/能力，再保存并启用；
4. 确认运行状态不是“已保存但未启用”；
5. 分别测试普通问答、明确工具调用；看图问答再单独验证视觉能力。

为何分开测试：HTTP 能连通 ≠ 模型名正确 ≠ Tool Calling 支持 ≠ 视觉支持。
不要求空间照片深度网络与规划 LLM 来自同一供应商。

源码：
[设置界面](../../app/components/ModelSettingsDialog.tsx)；
[配置与 Secret](../../backend/app/settings.py)；
[LLM 请求](../../backend/app/llm.py)的 `OpenAICompatiblePlanner`；
[运行时切换](../../backend/app/main.py)的 `save_llm_settings()`、`test_llm_settings()`。

Secret 由系统钥匙串或加密文件保存；普通配置只保留非敏感设置。
加密文件回退也依赖本机密钥，不能把“加密”理解成复制一个 JSON 就能迁移 Secret。
配置失败时不要贴完整配置/请求头，提供脱敏状态和错误类型即可。

## 1.5 第四步：逐项安装本地能力

进入“设置与模型安装”，顺序为：

```text
重新检测 → 选择能力 → 查看计划/空间/版本 → 阅读许可证
→ 本机所有者确认 → 执行固定安装动作 → 查看日志 → 安装后检测
→ 真实样本 Smoke → 用户效果验收
```

基础启动完成后再装空间照片；图片风格化另按
[真实 SDXL 部署](../guides/photo-style-deployment.md)配置。Fake / local-preview 不能算完整风格化功能。

选择一次完整安装的 macOS 用户也可以在阅读许可后使用：

```bash
bash scripts/setup.sh --accept-model-licenses
bash scripts/start.sh
```

Windows 对应 `scripts/setup.ps1`、`scripts/start.ps1`。
参数代表你已经接受文档中列出的第三方许可，不是绕过许可审核。
跨平台与硬件限制详见[完整部署手册](../guides/complete-local-deployment.md)，尚无目标设备实测的配置不得写成已验收。

## 1.6 每个阶段单独验收

| 阶段 | 通过标志 | 不通过时先查 |
| --- | --- | --- |
| 基础 API | /health 返回 JSON | Python、端口、启动日志 |
| 网页 | 页面和 API 请求都成功 | 浏览器网络请求、API 地址、CORS |
| LLM | 真实回答和 Tool Calling 分别通过 | 是否启用、模型能力、Key/URL |
| 空间照片 | Job 完成且前后景产物可预览 | 模型下载、分割降级、Job 错误 |
| 风格化 | Provider 为真实模型且 result 可读 | Provider 健康、权重/许可、内存 |
| 飞书 | 文本、图片、结果回传三段都成功 | 白名单、应用发布、媒体权限 |
| 手机 Viewer | 链接可达且可拖动 | 地址/防火墙/隧道/签名有效期 |

`deploy.py doctor` 是诊断；`deploy.py check` 是完整能力验收。只安装 core 时完整 check
因图像模型未部署而不通过，并不等于基础 Web Agent 不能使用。

## 1.7 学习检查

- 为什么先装 core，而不是把 SDXL 的下载成功作为网页启动前提？
- 为什么看到“Key 已保存”还要检查“模型已启用”？
- 为什么从 GitHub 克隆代码不等于数据和模型迁移完成？

[下一章：请求与代码地图](02-request-and-code-map.md) · [返回主目录](README.md)
