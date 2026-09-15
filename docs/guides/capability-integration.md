# 新功能 / Capability 接入指南

作者：**Zhuofan Xie**

## 目标与边界

新功能以独立 Capability 接入，不直接修改 Agent 循环、渠道主逻辑或共享资产表的
控制流。Agent 只通过 Tool Registry 看见稳定 Schema；Web 和飞书调用同一个服务层；
模型 Provider 可替换，任务、权限、资产和审计语义保持稳定。

```text
Web / Feishu attachment
        ↓
Capability API / Tool handler
        ↓
Domain service → durable Job → Provider
        ↓
Asset repository → Result presenter
```

## 最小目录

```text
backend/app/<capability>.py       # 参数、Service、Provider Protocol、Tool 注册
backend/app/<provider>.py         # 可选的模型/远端 Provider
backend/config/<capability>.json  # 固定模型、许可、哈希或门禁
app/components/<Studio>.tsx       # 可选 Web 工作台
backend/tests/test_<capability>.py
docs/<capability>.md
docs/experiments/<id>.md
```

## 接入步骤

1. 先定义输入角色、输出资产、风险等级、owner 边界和失败语义；图片角色不能依赖上传
   顺序猜测。
2. 用 Pydantic/JSON Schema 定义参数，限制数量、类型、范围、字节数和枚举；LLM 只看
   资产 ID 与元数据，不看私人图片字节。
3. 定义 Provider Protocol。默认 Provider 必须离线、可测试且明确质量边界；真实模型
   需固定版本、许可、哈希、设备/显存门禁和失败降级。
4. Service 负责校验、EXIF 清理、任务创建、状态更新、结果落盘和临时附件删除；不要
   把这些逻辑放到 React 或飞书 handler。
5. 用 `register_<capability>_tools()` 注册 ToolSpec，并提供 Capability Manifest：稳定
   ID、semver、作者、入口、Schema、本地模型、存储、权限和下载量。
6. 在 `create_app()` 组合根实例化一次 Service，注册工具并暴露最小 API；生命周期结束
   时关闭执行器/模型进程。
7. Web 工具卡按“已安装/未安装/待上线”显示真实状态；有专用交互时从工具库进入，不
   改成新的首页主流程。
8. 渠道只做附件提取、鉴权、幂等、进度与结果适配。复杂任务进入持久化 Job，不能在
   长连接回调内同步跑模型。
9. 更新 `docs/project-board.md`、功能文档、实验记录和 README；标明事实、未验证项和
   硬件基线，不能把预览 Provider 写成真实模型效果。
10. 在第二台干净电脑验证依赖、模型/Provider、密钥、网络、渠道和回滚；安装步骤不得
    依赖开发机缓存、权重或系统钥匙串。

图片、2.5D、视频或 3D 能力接入飞书时，继续执行
[飞书图片与 2.5D 能力接入 SOP](feishu-media-capability-sop.md)，不要在渠道 Handler
内增加新的模型分支或依赖图片顺序猜测角色。

远端训练型能力还必须把用户可见 `dataset_id` 与服务端路径分开；LLM、飞书和 Web
请求不得提交任意绝对路径。Flux-GS 的实现示例见
[Flux-GS Capability 接入与部署](flux-gs-capability.md)：主进程只注册工具和 Provider，
CUDA 训练留在隔离服务，创建任务使用人工审批与幂等键。

## 必须通过的验收

- Tool Schema 非法参数、owner 越权、重复消息和重复执行测试；
- API 创建、任务完成/失败、资产读取/删除和临时文件清理测试；
- Provider 合同测试，真实 GPU/远端 Provider 用显式 opt-in 冒烟测试；
- Web 构建、键盘/移动端交互、空状态和错误状态；
- 飞书/外部渠道用 Fake 做回归，并至少完成一次真实租户权限/断网/重试实验；
- 无模型权重、Key、用户图片、数据库或 Viewer Secret 进入 Git；
- `git diff --check`、后端测试、前端 lint/build 全部通过。

## 旧分支迁移策略

旧功能分支不要直接覆盖主分支热点文件。先保留分支历史做三方合并，对 Agent、Main、
Tool Registry、首页和共享模型冲突选择新版架构，再逐项把 Domain Service、Provider、
测试、配置和 UI 作为 Capability 适配回来。本次 `feature/pic-style` 就按此方式迁移：
复用原 Provider 与实验材料，但入口改为新版工具库、Manifest 和共享异步资产链路。
