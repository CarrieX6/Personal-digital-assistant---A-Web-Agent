# Flux-GS Capability 接入与部署

作者：**Zuheng Zhao（功能贡献）**、**Zhuofan Xie（主项目）**
集成日期：2026-09-10
对应任务：`SPATIAL-005`

## 1. 结论与当前状态

本项目已经接入 [Zuheng Zhao 的 Flux-GS Skill](https://github.com/zhaozuheng0726/Flux-gs-skill)
适配层，但没有把整套训练源码、WebGL 大文件、模型输出或 CUDA 编译产物复制进个人
数字助手仓库。主项目负责安全编排，独立 Flux-GS 服务负责 GPU 训练和 Web 发布：

```text
Web / 飞书自然语言
        ↓
LangGraph plan → policy → 人工审批
        ↓
validate_flux_gs_dataset / create_flux_gs_demo / get_flux_gs_job_status
        ↓
FluxGSService（dataset_id 转受控路径、owner、幂等键）
        ↓
FluxGSHttpProvider（X-Tenant-ID / X-Owner-ID / X-API-Key）
        ↓
独立 Linux + NVIDIA GPU Flux-GS 服务
        ↓
训练 comp.json → 静态 WebGL Viewer → demo_url
```

当前已完成：

- 三项声明式 Tool 和 Capability Manifest；
- `external_write + requires_approval + idempotent` 训练创建策略；
- 本机 Root 管理 API 与 Provider 健康检查；
- 任意路径改为受控 `dataset_id`，阻止 `../`、绝对路径和嵌套路径；
- 工具库按真实 Provider 状态显示“未部署 / 测试模式 / GPU 已就绪”；
- 飞书菜单、能力说明和 GPU 服务状态卡片；
- LangGraph 异步任务 handoff，避免在 Agent 循环内持续轮询；
- Preview Provider、HTTP 契约、路径拦截和本机 Root 边界测试。

尚未完成、不能对外宣称已经完成：

- 本项目环境尚未完成真实 Linux/NVIDIA Flux-GS 训练验收；
- 飞书不能直接上传大型 COLMAP 数据集 ZIP；
- PDA 与远程 GPU 不共享磁盘时，仍需对象存储或分片上传协议；
- 训练完成通知没有持久化 Outbox，主进程重启可能中断主动通知；
- 上游训练依赖含非商用条款，商业产品上线前必须完成法务与许可证确认。

## 2. 为什么采用独立 Provider

Flux-GS 不是单张图片的轻量推理。它需要多视角图片、COLMAP 相机/点云、CUDA 扩展、
NVIDIA GPU、训练任务和静态站点发布。若直接塞进 FastAPI/LangGraph 主进程，会产生：

- CUDA/PyTorch 与主项目 Python 依赖冲突；
- 长训练阻塞消息处理，飞书断线或 API 重启时难以恢复；
- macOS MPS 与 Windows CPU 环境无法提供相同训练能力；
- 权重、数据集、编译缓存和 Web 资产显著放大主仓库；
- 训练权限、GPU 配额和许可证边界无法独立治理。

因此主 Agent 只持有任务契约，GPU 服务可以部署在同机 Linux 工作站、Windows 的 Linux
GPU 容器/WSL 环境，或受控的远程 NVIDIA 节点。这个边界也让后续把最小线程池替换为
Celery、RQ、Slurm 或企业已有任务平台时，不必改 Agent Tool Schema。

## 3. 输入数据要求

`dataset_id=scene-a` 会被映射到受控根目录下的 `scene-a/`，不接受文件路径：

```text
<FLUX_GS_DATASET_ROOT>/scene-a/
  images/
    00001.jpg
    00002.jpg
    ...
  sparse/
    0/
      cameras.bin 或 cameras.txt
      images.bin  或 images.txt
      points3D.bin 或 points3D.txt
```

建议至少 8 张经过 COLMAP 去畸变和稀疏重建的图片。校验只证明目录结构可读，不证明
相机位姿正确、覆盖充分或最终重建质量达标。只有单张图片时，应使用现有 2.5D 空间
照片能力，不应伪装成 Flux-GS 多视角重建。

## 4. GPU 服务部署

真实训练以源仓库当前文档为准：

- [依赖与接入检查](https://github.com/zhaozuheng0726/Flux-gs-skill/blob/70bccee/docs/dependencies_zh.md)
- [自定义数据到 WebGL Demo](https://github.com/zhaozuheng0726/Flux-gs-skill/blob/70bccee/docs/demo_guide_zh.md)
- [最小 HTTP Provider](https://github.com/zhaozuheng0726/Flux-gs-skill/blob/70bccee/server/flux_gs_service.py)

基线环境为 Linux、Python 3.11、NVIDIA GPU、CUDA 12.x（源仓库推荐 12.6）和
`tmc3`。源文档建议显存 12 GB 以上；这只是部署建议，不是本项目实测结论。

```bash
git clone https://github.com/zhaozuheng0726/Flux-gs-skill.git
cd Flux-gs-skill
git checkout 70bccee

bash environment/setup_flux_gs_training.sh flux-gs
conda activate flux-gs
python -m pip install -r environment/requirements-flux-gs-service.txt

export FLUX_GS_API_KEY='<通过 Secret 管理器注入>'
export FLUX_GS_BASE_URL='https://3d.example.com/flux-gs'
bash scripts/start_flux_gs_service.sh
```

启动后先检查：

```bash
curl -fsS http://127.0.0.1:18100/health/ready
curl -fsS http://127.0.0.1:18100/health/provider
```

源仓库的最小 Provider 适合单机接入验证。生产环境必须补充持久队列、重启恢复、取消、
超时、GPU 配额、日志轮转、指标、审计和失败重试，不能把内存线程池当成生产队列。

## 5. 数据集根目录映射

Agent 侧和 GPU 服务侧必须能看到同一份相对数据集 ID：

```dotenv
# PDA 电脑看到的数据
FLUX_GS_DATASET_ROOT=/srv/pda/flux-gs-datasets

# GPU Provider 进程看到的同一挂载；同机时可与上面相同
FLUX_GS_PROVIDER_DATASET_ROOT=/mnt/pda-datasets
```

例如 `dataset_id=scene-a`：

```text
PDA 管理目录：/srv/pda/flux-gs-datasets/scene-a
GPU 请求路径：/mnt/pda-datasets/scene-a
```

远程 GPU 不能直接读取 Mac 本地路径。当前可用方案是受控 NFS/SMB 共享挂载；正式产品
更推荐“客户端分片上传 → 对象存储隔离前缀 → SHA-256 对账 → GPU 临时下载 → 生命周期
清理”，并让 Tool 继续只传 `dataset_id`，不要重新暴露路径和 URL。

## 6. 个人数字助手配置

在主项目 `.env` 配置：

```dotenv
FLUX_GS_PROVIDER=http
FLUX_GS_SERVICE_URL=http://127.0.0.1:18100
FLUX_GS_API_KEY=
FLUX_GS_TENANT_ID=personal-agent
FLUX_GS_TIMEOUT_SECONDS=30
FLUX_GS_DATASET_ROOT=backend/data/flux-gs-datasets
FLUX_GS_PROVIDER_DATASET_ROOT=backend/data/flux-gs-datasets
```

如果 GPU 服务在其他电脑，使用专用网络 HTTPS 地址，并通过系统 Secret、容器 Secret
或进程环境注入相同 API Key；不要把 Key 写进 Git、截图、飞书消息或 URL Query。

本地只验证适配链路时可临时设置：

```dotenv
FLUX_GS_PROVIDER=preview
```

Preview 只检查目录结构并返回 `production_quality=false`，不会训练模型，也不能作为
真实效果或性能验收证据。

## 7. Web、Agent 与飞书调用

### Web / Agent

管理员把数据集放入受控目录后，可以发送：

```text
生成 Flux-GS 3D 场景，dataset_id=scene-a
```

正常链路应先调用 `validate_flux_gs_dataset`，随后创建任务。创建训练是高成本外部写
操作，LangGraph 会进入等待审批状态；批准后执行账本使用幂等键避免重复创建。

Root API 仅允许本机来源：

```bash
curl -X POST http://127.0.0.1:8000/api/flux-gs/datasets/validate \
  -H 'Content-Type: application/json' \
  -d '{"dataset_id":"scene-a"}'

curl http://127.0.0.1:8000/api/flux-gs/provider
curl http://127.0.0.1:8000/api/flux-gs/jobs/<job-id>
```

### 飞书

1. 向机器人发送“菜单”；
2. 点击“Flux-GS 3D”；
3. 卡片展示 Provider 是否就绪、COLMAP 输入要求和调用格式；
4. 发送 `生成 Flux-GS，dataset_id=scene-a`；
5. 在审批入口确认 GPU 训练；
6. 任务完成后取得 `demo_url`，在手机浏览器打开 WebGL Viewer。

飞书卡片不是 3D 渲染容器，只负责入口、状态和链接。`demo_url` 必须是手机可访问的
HTTPS 地址；`127.0.0.1`、Mac 本地路径和仅 GPU 内网可见的 URL 都不能回传给手机。

## 8. 安全、许可与上线门禁

### 已实现边界

- Tool 只接受符合白名单正则的 `dataset_id`；
- 训练创建要求人工审批；
- owner、tenant、API Key 和幂等键透传到独立服务；
- Flux-GS 管理 API 只允许本机 Root 控制台；
- Provider 返回的数据集绝对路径不会回传给 Agent；
- 数据集、训练输出、权重、Key 和 CUDA 缓存都在 `.gitignore` 边界之外。

### 许可门禁

上游 `training/LICENSE` 是 Apache-2.0，但训练树还包含 Gaussian-Splatting 许可的 CUDA
子模块，例如
[simple-knn/LICENSE.md](https://github.com/zhaozuheng0726/Flux-gs-skill/blob/70bccee/training/submodules/simple-knn/LICENSE.md)
和
[diff-gaussian-rasterization_fluxgs/LICENSE.md](https://github.com/zhaozuheng0726/Flux-gs-skill/blob/70bccee/training/submodules/diff-gaussian-rasterization_fluxgs/LICENSE.md)。
这些条款把使用限制在研究/评估等非商业用途。主仓库因此只合入可分离的 HTTP 适配层，
不复制训练子模块。商业上线前必须逐项核对 Flux-GS 自身代码、数据、模型、CUDA 扩展、
TMC13/GPCC 和 Viewer 依赖的许可证并取得必要授权。

## 9. 验收清单

```bash
source .venv/bin/activate
pytest backend/tests/test_flux_gs.py
pytest backend/tests/test_api.py backend/tests/test_orchestration.py backend/tests/test_feishu.py
pnpm run lint
pnpm run test
pnpm run build
git diff --check
```

真实 GPU 验收还应固定数据集和版本，记录：COLMAP 校验成功率、排队/训练/发布耗时、
P50/P95、峰值显存、输出大小、Viewer 首屏时间/FPS、失败原因、重启恢复和重复请求是否
只产生一个任务。没有这些数据前，只能说“适配层已接入”，不能说“生产可用”。

## 10. 后续工程化优先级

1. `P0`：数据集上传协议、owner 隔离、哈希对账和恶意压缩包防护；
2. `P0`：GPU Job 持久队列、租约、取消、超时、重启恢复和幂等存储；
3. `P0`：许可清单、SBOM 和商业使用审批；
4. `P1`：飞书进度卡片、Outbox、失败重试和完成后主动回传；
5. `P1`：固定 HTTPS 域名、Viewer 鉴权、撤销、访问审计和移动端降级视频；
6. `P1`：Windows/远程 NVIDIA、并发、性能、显存和弱网实测。
