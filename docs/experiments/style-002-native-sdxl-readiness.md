# `STYLE-EXP-002` 本机 SDXL Provider 准备与失败关闭

模板作者：**Zhuofan Xie**
实验作者：**Ma Xianggang**
实验日期：2026-08-13

| 字段 | 内容 |
| --- | --- |
| 任务 ID | `STYLE-001` |
| 实验 ID | `STYLE-EXP-002` |
| 关联调研 | [图片风格化 Skill 集成说明](../photo-style-transfer.md) |
| 负责人 | Ma Xianggang |
| Git Commit | 尚未提交；实验基线 `6b87915c05981c42a26b58ca7b440ed0260c3783` |

## 1. 目的与假设

- 要验证的问题：能否把参考仓库已经通过 8 GB 门禁的 SDXL + IP-Adapter 路径直接接入本项目，同时确保未接受许可证、缺模型、缺 CUDA 或缺依赖时失败关闭。
- 可证伪的假设：固定清单可复现 9.84 GiB 下载计划；真实 Provider 构造和状态查询不加载模型、不创建目录、不联网；只有通过许可证明、model lock、文件尺寸/哈希、质量门禁和 CUDA 预检后才能加载。
- 成功标准：模型/门禁合同、参数映射、SDXL 尺寸、环境选择和诊断接口自动测试通过；准备脚本能在无下载模式列出完整计划。
- 停止条件：需要代表用户接受许可证、下载模型，或发现运行时会隐式访问模型源。

## 2. 环境

| 项目 | 内容 |
| --- | --- |
| 设备 | NVIDIA GeForce RTX 4060 Laptop GPU，8188 MiB |
| 系统 | Microsoft Windows 10.0.26200.8875 |
| 电源模式 | 未采集；本实验不作本机推理性能结论 |
| 运行时 | Python 3.13.0、Node.js 24.14.1、pnpm 11.16.0 |
| 模型 | 固定清单：SDXL Base 1.0 fp16、IP-Adapter SDXL ViT-H、可选 LCM-LoRA；本轮未下载 |
| 依赖 | 默认 `.venv` 未安装 Torch/Diffusers GPU 栈；新增 `backend/requirements-gpu.txt` 作为显式可选依赖 |

## 3. 数据

- 数据集与版本：本轮不使用图片数据，不运行模型生成。
- 样本数量和选择规则：模型清单、参数与失败关闭合同；API 回归使用原有合成图。
- 输入尺寸和预处理：验证 64 像素倍数、最长边 768、像素预算 600,000 的尺寸映射。
- 隐私与授权：未使用个人图片、未下载权重、未代表用户接受许可证。
- 数据哈希或可复现方式：模型 revision、允许文件、尺寸和关键 SHA-256 固定在 `backend/config/photo-style-models.json`。

## 4. 变量与对照

| 类型 | 内容 |
| --- | --- |
| 自变量 | Provider 选择、质量档位、强度参数、模型目录/lock 是否存在、GPU 依赖是否可用 |
| 因变量 | Provider ready 状态、参数映射、是否允许加载、错误信息和自动测试结果 |
| 控制变量 | 参考提交 `c8e0b641f7f334faf73167211b5f0a8033e3b1dc`、固定 revision、运行时 `local_files_only` |
| 基线 | `local-preview` 与上一轮 `pic-style-http` 合同测试 |

## 5. 指标

- 质量：仅验证上游已接受门禁记录与本项目配置一致；不产生本地质量样本。
- 成功率：自动测试按用例通过数记录。
- 延迟与吞吐：未运行本机推理；不采集。
- 峰值内存/显存：本机未测；上游记录 6572 MiB，仅作为来源明确的参考值。
- 功耗、能耗与温度：未采集。
- API、存储或带宽成本：计划下载 10,561,842,988 字节（9.84 GiB），本轮实际下载 0 字节。

## 6. 执行步骤

```bash
python backend/scripts/prepare_photo_style_models.py --plan
python -m pytest backend/tests -q --basetemp .test-tmp -p no:cacheprovider
python -m compileall -q backend/app backend/scripts
pnpm run lint
node_modules/.bin/vinext build
node --test tests/rendered-html.test.mjs
```

只有操作者审阅三个许可证 URL 后，后续才允许执行：

```bash
python backend/scripts/prepare_photo_style_models.py \
  --download --accept-model-licenses --verify-checksums
```

## 7. 原始结果

| 方案/配置 | 质量 | 延迟 | 内存/显存 | 功耗/温度 | 成功率 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| 模型准备计划 | 固定 revision、许可 URL 与文件清单通过 | 不适用 | 计划 9.84 GiB 磁盘下载 | 未采集 | 3/3 组件 | 无网络、无写入模型目录 |
| Native Provider 合同 | 参数、尺寸、门禁、选择、失败关闭和 Diffusers 调用合同通过 | 未采集 | 无权重 Mock 记录 512 MiB | 未采集 | 6/6 新合同 | 真实预检为 `ready=false`；Mock 只验证调用结构，不代表真实显存 |
| 后端完整回归 | API/Agent/Registry/Provider 诊断通过 | 4.04 秒 | 未采集 | 未采集 | 21/21 | 一个既有 httpx 2 迁移提醒 |
| Web/PC 静态验证 | Provider 状态文案、lint、构建与 SSR 通过 | 未采集 | 未采集 | 未采集 | 3/3 | 构建仅有既有大 chunk 提示 |

原始日志或大文件：

- 本地位置或生成方式：命令行会话；模型目录不存在。
- SHA-256：模型清单指纹 `87cb610364aa8a5000c587ec2d11fa350dc120caa33e9c525f21cf388d675808`。
- 未提交原因：模型权重、Hugging Face 缓存、model lock 和生成样本均属于本地运行产物。

## 8. 失败样本与异常

| 样本/阶段 | 现象 | 原因判断 | 是否可复现 | 处理 |
| --- | --- | --- | --- | --- |
| `sdxl-local` 预检 | `ready=false` | 模型、model lock 与 GPU 可选依赖尚未准备 | 是 | 明确报告缺项，不创建目录、不联网、不回退为伪真实模型 |
| 准备脚本 `--download` 未附许可参数 | 下载前退出 | 未提供 `--accept-model-licenses` | 是 | 打印三项许可证 URL 和 9.84 GiB 计划后停止，实际下载 0 字节 |
| GPU 生成 | 未执行 | 尚未获得操作者对三个模型许可证的显式接受 | 是 | 停止在下载门禁之前 |

## 9. 分析

- 结果是否支持假设：支持。代码已具备真实加载与推理路径，同时当前环境按设计失败关闭。
- 与上游报告的差异：上游在对应固定配置上记录 20/20 工程用例、预览 P50 5.8114 秒、P95 6.3312 秒和 6572 MiB 峰值显存；这些不是本项目本轮实测。
- 可能的测量偏差：尚未验证当前 Python/CUDA/Diffusers 组合，也未验证本机图片质量和长时间稳定性。
- 对质量、性能和工程成本的权衡：模型 CPU offload 和单并发降低 8 GB OOM 风险，但增加主存占用和模型切换延迟；LCM 默认关闭，避免未经本地验证就改变质量路径。

## 10. 结论与下一步

- 结论：本机真实 SDXL Provider 的工程实现和安全准备链路已完成，尚不能声明本机模型运行验收完成。
- 置信度：离线/许可/合同边界高；本机生成质量与性能未测。
- 推荐方案：由操作者审阅许可证后运行准备脚本，再以获授权的非个人测试图执行 GPU smoke 和十次稳定性测试。
- 是否需要补充实验：需要；记录本机输出、质量观察、P50/P95、峰值显存、失败恢复与依赖版本。
- 需要更新的调研、ADR 和实现任务：`STYLE-001` 保持“开发中”；若本机 8 GB 路径无法满足稳定性，再评估独立 Worker 或较轻底座并新增 ADR。

---

Copyright © 2026 Ma Xianggang. All rights reserved.
