# `STYLE-EXP-003` 本机 SDXL、IP-Adapter 与 LCM-LoRA GPU 冒烟

模板作者：**Zhuofan Xie**
实验作者：**Ma Xianggang**
实验日期：2026-08-13；内存生命周期复验：2026-08-20

| 字段 | 内容 |
| --- | --- |
| 任务 ID | `STYLE-001` |
| 实验 ID | `STYLE-EXP-003` |
| 关联调研 | [图片风格化 Skill 集成说明](../photo-style-transfer.md) |
| 负责人 | Ma Xianggang |
| Git Commit | 尚未提交；实验基线 `6b87915c05981c42a26b58ca7b440ed0260c3783` |

## 1. 目的与假设

- 要验证的问题：在操作者明确接受三项许可证后，固定 revision 的 SDXL Base 1.0、IP-Adapter SDXL ViT-H 和 LCM-LoRA 能否在项目正式 `.venv` 与 8 GB RTX 4060 Laptop GPU 上离线加载并生成图片。
- 可证伪的假设：基础调度器与 LCM 预览均能在 8188 MiB 显存设备完成 640 × 384 合成样本生成，峰值预留显存不超过物理显存，输出保留内容结构并呈现参考图的颜色和纹理特征。
- 成功标准：三组固定文件全部通过尺寸与 SHA-256 校验；正式 CUDA 环境自检通过；基础路径和 LCM 路径各成功一次；峰值预留显存低于 8188 MiB；输出人工检查无空白、崩坏或明显结构丢失。
- 停止条件：权重校验失败、运行时联网取模型、CUDA 不可用、OOM，或任一路径无法生成有效图片。

## 2. 环境

| 项目 | 内容 |
| --- | --- |
| 设备 | NVIDIA GeForce RTX 4060 Laptop GPU，8188 MiB |
| 系统 | Microsoft Windows 10.0.26200.8875 |
| 电源模式 | 接电状态未单独固化；不据此作跨设备性能结论 |
| 运行时 | Python 3.13.0、PyTorch 2.11.0+cu128、Torchvision 0.26.0+cu128、NVIDIA Driver 592.00 |
| 模型 | SDXL Base 1.0 fp16 `462165984030d82259a11f4367a4eed129e94a7b`；IP-Adapter `018e402774aeeddd60609b4ecdb7e298259dc729`；LCM-LoRA `a18548dd4956b174ec5b0d78d340c8dae0a129cd` |
| 依赖 | Diffusers 0.35.2、Transformers 4.57.6、Accelerate 1.14.0、PEFT 0.19.1、Safetensors 0.8.0；`pip check` 无冲突 |

## 3. 数据

- 数据集与版本：参考仓库脚本 `scripts/create_synthetic_benchmark_pair.py` 生成的确定性合成内容图和风格图。
- 样本数量和选择规则：一对无个人信息的合成图，同时用于基础调度器和 LCM 对照。
- 输入尺寸和预处理：内容图与风格图由上游固定脚本生成；Provider 按预览档缩放为 640 × 384、64 像素倍数并归一化为 RGB。
- 隐私与授权：不含个人图片；操作者在本任务中明确接受 SDXL、IP-Adapter、LCM-LoRA 三项许可证；权重和接受证明仅保存在本机忽略目录。
- 数据哈希或可复现方式：内容图 SHA-256 `22c442fd44c82a702b615d14e2a5ec784d04a9a4a8dee8a1fd0bcb1c8da768c9`；风格图 SHA-256 `2c358e6be1982d4c06b166e348212ba0dac29eb201e6a3da55f7b2090885d902`。

## 4. 变量与对照

| 类型 | 内容 |
| --- | --- |
| 自变量 | 基础 DPMSolverMultistepScheduler 与启用 LCM-LoRA 的 LCMScheduler |
| 因变量 | 是否生成有效图片、生成阶段耗时、峰值预留显存、输出 SHA-256、人工结构/风格观察 |
| 控制变量 | 同一输入对、`preview`、`preserve_layout`、seed 1701、style/content/detail 强度 0.7/0.8/0.7、fp16、模型 CPU offload、VAE tiling、单并发 |
| 基线 | `STYLE-EXP-002` 的失败关闭合同；上游 8 GB 工程门禁仅作来源明确的参考，不算本机结果 |

## 5. 指标

- 质量：人工检查主体、地形、建筑和构图是否保留，以及参考图颜色/纹理是否进入结果；本轮不使用个人验收图，不声明生产质量。
- 成功率：两条正式推理路径各执行两次，按完成并写出有效 PNG 计数；最终代码复跑覆盖门禁元数据更新。
- 延迟与吞吐：脚本记录单张生成阶段 wall time；不包含首次模型加载和 SHA-256 校验；单并发，不报告吞吐。
- 峰值内存/显存：`torch.cuda.max_memory_reserved()`，每条路径记录 MiB。
- 功耗、能耗与温度：未连续采样；生成结束后的 `nvidia-smi` 快照为 47 °C、约 19.91 W，不代表运行峰值。
- API、存储或带宽成本：模型固定下载 10,561,842,988 字节（9.84 GiB）；无云 API 成本。

## 6. 执行步骤

```powershell
python -m pip install torch==2.11.0 torchvision==0.26.0 `
  --index-url https://download.pytorch.org/whl/cu128
python -m pip install -r backend/requirements-gpu.txt
python -m pip check

python backend/scripts/prepare_photo_style_models.py `
  --download --accept-model-licenses --verify-checksums
python backend/scripts/prepare_photo_style_models.py --verify-checksums

python backend/scripts/smoke_photo_style_sdxl.py `
  --content backend/models/photo-style/smoke-inputs/content.png `
  --style backend/models/photo-style/smoke-inputs/style.png `
  --output backend/models/photo-style/smoke-results/formal-sdxl-ip-adapter-fp16-preview.png `
  --quality preview --seed 1701 --verify-model-hashes

python backend/scripts/smoke_photo_style_sdxl.py `
  --content backend/models/photo-style/smoke-inputs/content.png `
  --style backend/models/photo-style/smoke-inputs/style.png `
  --output backend/models/photo-style/smoke-results/formal-sdxl-ip-adapter-lcm-preview.png `
  --quality preview --seed 1701 --enable-lcm-preview --verify-model-hashes
```

## 7. 原始结果

| 方案/配置 | 质量 | 延迟 | 内存/显存 | 功耗/温度 | 成功率 | 备注 |
| --- | --- | --- | --- | --- | --- | --- |
| 固定模型准备 | 26/26 允许文件与关键权重哈希通过 | 下载约 93 分钟；复验只读本地文件 | 9.84 GiB 模型目录 | 未采集 | 3/3 组件 | model lock SHA-256 `92a4b65f1421c281c4b006cdbb01645f298a727278180833cd0ae79cc99054b3` |
| SDXL + IP-Adapter 基础预览 | 内容结构保留，参考颜色/纹理可见，人工冒烟通过 | 最终代码 8.247 秒；初次 13.267 秒 | 6278 MiB | 未连续采样 | 2/2 | 12 步，两次输出同一 SHA-256 `9791c90819baa0c7e21cdd6af7dbfe45591402d4e9e4a17234404535b7f5bd9f` |
| SDXL + IP-Adapter + LCM-LoRA | 内容结构保留，参考颜色/纹理可见，人工冒烟通过 | 最终代码 8.048 秒；初次 9.070 秒 | 6502 MiB | 未连续采样 | 2/2 | 6 步，两次输出同一 SHA-256 `823a1a9a47f54c55b7744cf7e851cd5b550cffd14aca7612303216eb11a916d4` |
| API 进程内加载后删除对象 | 生成成功 | 单次任务 | 任务后工作集 8920 MiB、私有提交 11505 MiB | 未采集 | 1/1 生成；内存门禁失败 | `loaded=false`、缓存为空，但 Windows/PyTorch 原生分配器没有归还工作集 |
| 单次隔离 SDXL 子进程 | 生成成功、中文进度正常 | 单次任务 | 全部 Python 峰值工作集 10421 MiB；API 主进程峰值 116 MiB、完成时 77–78 MiB | 未采集 | 3/3 | 子进程退出后不存在模型进程；Provider `loaded=false`、`unload_count=1` |
| 隔离 CUDA 状态预检 | `ready=true` | 单次查询 | API 主进程查询前 70 MiB、查询后 72 MiB | 未采集 | 1/1 | Torch 只在短生命周期预检进程导入 |

原始日志或大文件：

- 本地位置或生成方式：`backend/models/photo-style/` 下的固定模型、`model-lock.json`、`smoke-inputs/` 与 `smoke-results/`。
- SHA-256：模型清单指纹 `87cb610364aa8a5000c587ec2d11fa350dc120caa33e9c525f21cf388d675808`；输入、lock 与输出哈希见上文。
- 未提交原因：模型权重、Hugging Face 缓存、机器生成的 lock、测试输入和生成结果均为本地大文件或运行产物，受 `.gitignore` 保护。

## 8. 失败样本与异常

| 样本/阶段 | 现象 | 原因判断 | 是否可复现 | 处理 |
| --- | --- | --- | --- | --- |
| PyTorch wheel 首次下载 | pip 在约 0.8/2.8 GB 处报 `WinError 10054` | 长连接被远端重置，不是包或磁盘错误 | 网络相关 | 使用支持分段续传的下载器完成同一官方 wheel，并按官方索引 SHA-256 `6f367e62fd81b75cdf23ca4b75ced834d2db2cf98d1588ac935bde345de9de23` 校验后本地安装 |
| Diffusers `dtype` 别名试验 | 参数被 AutoPipeline 忽略，生成 153.411 秒，峰值预留 12114 MiB | Diffusers 0.35.2 的该 AutoPipeline 构造器仍读取 `torch_dtype` | 是 | 恢复 `torch_dtype=torch.float16` 并注明兼容原因；失败输出保留在忽略目录，不计入成功结果 |
| LCM 调度器初始化 | 上游 DPM 配置携带 `skip_prk_steps` 警告 | LCMScheduler 不消费该遗留字段 | 是 | 构造 LCM 配置前过滤该字段，新增单元测试 |
| LCM LoRA 加载 | 文本编码器报告部分 LoRA key 缺失，并明确标注可安全忽略 | 固定 LCM-LoRA 权重主要作用于 UNet，与当前 Diffusers 加载日志一致 | 是 | 保留警告；不将其伪装为无警告，输出与显存冒烟均通过 |
| 任务后仅执行 `del`、`gc.collect()` 与 `torch.cuda.empty_cache()` | Provider 状态已卸载，但 API 进程仍占约 8.9 GiB 工作集 | Windows/PyTorch 原生 CPU 分配器保留已释放张量的内存页 | 是 | 默认改用单次隔离模型进程；进程退出后由操作系统确定性回收，连续批处理才显式选择常驻模式 |
| 隔离 Worker 中文进度 | 首次浏览器实测出现替换字符 | Windows 管道编码与父进程 UTF-8 解码不一致 | 是 | Worker 固定 `PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`；真实任务复验所有阶段中文正常 |
| `pnpm run lint` 包管理器入口 | 在离线环境无法验证并切换 `packageManager` 声明的 pnpm 11.9.0，源码检查尚未启动即退出 | 本机 pnpm 版本管理器需要访问签名源，与图片风格化代码无关 | 是 | 直接调用 lockfile 已安装的 ESLint 9.39.4；全仓源码 lint 通过，忽略 `.test-tmp` 和本地模型运行产物 |

## 9. 分析

- 结果是否支持假设：支持工程可运行假设。两条路径均在 8188 MiB 设备内完成，峰值分别占物理显存约 76.7% 和 79.4%。
- 与上游报告的差异：上游 20 例门禁记录预览 P50 5.8114 秒、P95 6.3312 秒、峰值 6572 MiB；本机单样本 LCM 显存接近上游，生成时间更高。两者采样范围和运行时不同，不能直接作回归判断。
- 可能的测量偏差：每路径只有两次正式样本且存在冷/热状态差异；人工质量观察未盲测；生成时间不含模型加载；未持续采样功耗、温度与系统内存。
- 对质量、性能和工程成本的权衡：LCM 在本样本用更少步数降低生成阶段耗时，但峰值显存略高；CPU offload 适合 8 GB 显存，却增加首次加载和主存成本。个人桌面默认用隔离进程换取任务后低内存；连续任务可设置 `PHOTO_STYLE_UNLOAD_AFTER_GENERATION=false` 复用模型，以约 10 GiB 常驻内存换取加载延迟。

## 10. 结论与下一步

- 结论：真实 SDXL + IP-Adapter 与可选 LCM-LoRA 已在项目正式环境通过本机工程冒烟，离线模型、许可、哈希、8 GB 调度和诊断链路可用；`local_validation` 提升为 `engineering_smoke_passed`。
- 置信度：单样本工程可运行性中高；生产图像质量、长时间稳定性和跨设备性能低到中。
- 推荐方案：保留 `local-preview` 为默认安全路径；获授权的本机安装可显式启用 `sdxl-local`，并默认在单次隔离子进程运行；LCM 继续默认关闭，仅作为可选预览加速。
- 是否需要补充实验：需要。使用获授权的非个人固定图集完成质量盲评、至少十次稳定性、P50/P95、系统内存与连续功耗/温度采样后，才可把 `production_quality` 改为 `true`。
- 需要更新的调研、ADR 和实现任务：`STYLE-001` 保持“开发中”；当前仍沿用 Provider 中立与现有任务架构，无需新增 ADR。若改为常驻 GPU Worker 或默认生产 Provider，再单独立项决策。

回归验证：正式 Python 3.13 环境后端 `22 passed`、compileall 通过；ESLint 9.39.4
全仓源码检查通过；Vinext/Vite 生产构建通过；Node SSR `1 passed`。Provider 只读状态预检
返回 `ready=true`、`models_ready=true`、`dependencies_ready=true`、`cuda_available=true`、
`preflight_errors=[]`。

---

Copyright © 2026 Ma Xianggang. All rights reserved.
