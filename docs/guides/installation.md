# 从 GitHub 下载到一键运行

作者：**Zhuofan Xie**

图片风格化功能实现：**Xianggang Ma**

更新日期：2026-09-15

对应任务：`CAP-002`、`CAP-003`、`OPS-001`

## 1. 最短路径

本项目把安装分为两层：先启动轻量 Web Agent，再由本机“设置与模型安装”逐项安装模型。
这样即使 SDXL 下载或 GPU 适配失败，用户仍可进入图形界面查看原因、复制修复命令并重试。

### macOS

克隆仓库后运行：

```bash
./scripts/quickstart.sh --install-system-deps
```

也可以在 Finder 中双击 `WebAgent.command`。脚本会先说明将要发生的操作并请求确认，然后：

1. 检测 Python 3.11–3.13、Node.js 22.13+ 和 Git；
2. 在 Apple Silicon macOS 上缺失时调用本机 Homebrew 安装；
3. 创建项目 `.venv`，安装锁定的前后端依赖；
4. 启动 `http://localhost:3000/` 与 Agent API；
5. 模型和第三方许可证留到设置中心单独确认。

只查看诊断、不安装：

```bash
./scripts/bootstrap.sh --plan
```

如果 Homebrew 本身不存在，脚本不会执行网上复制来的安装管道；它会提示用户打开
[Homebrew 官方网站](https://brew.sh/)完成安装后重试。

### Windows 10/11

解压发布包后双击 `WebAgent-Windows.cmd`，或在 PowerShell 中运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\quickstart.ps1 --install-system-deps
```

脚本使用 Windows 自带的 `winget` 安装 Python 3.12、Node.js LTS 和 Git。安装后若当前
PowerShell 还看不到新 PATH，按提示重新打开 PowerShell 并再次运行即可。NVIDIA 模型仍需
在设置中心检查驱动、CUDA/WSL2、显存、磁盘和许可证，基础启动成功不等于 GPU 功能通过。

## 2. GitHub 安装包

仓库新增 `Build install packages` 工作流。维护者发布 `v*` 标签时会生成并附加：

- `personal-digital-assistant-<version>-macos-linux.tar.gz`；
- `personal-digital-assistant-<version>-windows.zip`；
- `SHA256SUMS`。

安装包只包含 Git 已跟踪的代码、文档、启动器和示例配置，不包含 `.env`、API Key、SQLite、
个人图片、生成资产、`.venv`、模型权重或 `.capabilities`。也可以在 GitHub Actions 中手动
运行工作流生成开发测试包。发布前可在本机复现：

```bash
python3 scripts/package_release.py --version dev
```

这是一阶段的“可审计源码安装包”，不是已签名的 macOS `.app` 或 Windows MSIX。真正做到
无终端、自动升级和系统托盘常驻，还需要 Apple Developer ID/Notarization、Windows 代码
签名、安装器升级通道与回滚机制。

## 3. 设置中心如何处理缺失环境

进入“设置与模型安装”后，“这台设备 → 启动环境”逐项显示：

- 当前检测版本和项目最低要求；
- 该依赖影响 Web、API 还是 Git 更新；
- 当前系统对应的修复命令；
- 一键复制命令和官方安装说明；
- 修复后的“重新检测”入口。

安装模型时采用“计划 → 许可 → 下载 → 校验 → 真实 smoke → 重启/质量验收”的状态机，
只运行仓库内固定白名单动作，不接受 LLM 生成的 Shell。模型安装记录和脱敏日志写入本机
SQLite；页面刷新不会丢失，服务重启后未完成任务会进入可重试失败态。

## 4. Apple Silicon 图片风格化

当前 M4/16GB 路径为：

```text
内容图 + 1–3 张风格参考图
  → SDXL img2img 保留内容结构
  → IP-Adapter SDXL ViT-H 注入风格图特征
  → 640/768 像素档位、FP16、VAE tiling/slicing
  → PyTorch MPS + attention slicing
  → 单任务隔离 Worker，生成后退出并回收统一内存
```

固定权重包括 SDXL Base 1.0、IP-Adapter SDXL ViT-H 与关闭默认使用的 LCM-LoRA 预览组件，
合计约 9.84GiB。先查看计划：

```bash
./scripts/photo-style.sh plan-real
./scripts/photo-style.sh prepare-macos-mps
```

第二条会安装 Diffusers/Accelerate/PEFT 并完成 MPS 预检，但没有明确许可参数时会在模型下载
前停止。本人阅读输出中的许可证后，才执行：

```bash
./scripts/photo-style.sh prepare-macos-mps --accept-model-licenses
```

完整命令会下载固定 revision、校验文件大小与 SHA-256、写入本机 model lock、切换
`PHOTO_STYLE_PROVIDER=sdxl-local`，并用非个人合成图运行一次真实 MPS smoke。结果和本机验证
记录放在被 Git 忽略的 `backend/models/photo-style/` 下。此 smoke 只能证明工程链路可运行，
不会把 `production_quality` 自动改为 true；仍需用统一真实图集完成质量、连续十次稳定性、
P50/P95、统一内存峰值、swap、功耗和人工伪影检查。

## 5. 常见问题

### Node.js 已安装但仍显示缺失

Homebrew 的 `node@22` 可能是独立版本目录。重新打开终端，或先运行：

```bash
export PATH="$(/opt/homebrew/bin/brew --prefix node@22)/bin:$PATH"
corepack pnpm --version
```

### SDXL 下载中断

保留 `backend/models/photo-style/.cache/` 后重复原安装命令，Hugging Face 下载器会复用已经
完成的文件。不要删除已完成权重，也不要把模型目录提交到 Git。

### M4 16GB 内存不足

关闭其他重型应用，先使用 `preview` 档位。项目默认最长边 640/768、单并发、VAE
tiling/slicing、MPS attention slicing，并在每个任务后退出隔离 Worker。若仍然 OOM，不应
通过关闭 PyTorch 内存保护强行运行，应记录输入尺寸和系统内存后降级到 NVIDIA 节点或更小
模型；模型路线变化必须重新做质量对照。

### 为什么 Docker 不能替代 Mac MPS

Docker Desktop 容器不能把 Apple MPS 当作 CUDA GPU 暴露给现有 PyTorch 推理链路。因此
macOS 使用原生 arm64 Python/MPS 子进程；Windows/Linux NVIDIA 才适合使用 CUDA 容器。

## 6. 官方依据

- [Diffusers：Apple Silicon MPS](https://huggingface.co/docs/diffusers/optimization/mps)
- [Diffusers：IP-Adapter 与 SDXL img2img](https://huggingface.co/docs/diffusers/using-diffusers/ip_adapter)
- [SDXL Base 1.0 模型卡与 OpenRAIL++ 许可证](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0)
- [IP-Adapter 模型卡与 Apache-2.0 许可证](https://huggingface.co/h94/IP-Adapter)
- [Python 下载](https://www.python.org/downloads/)
- [Node.js 下载](https://nodejs.org/en/download)
- [Git 下载](https://git-scm.com/downloads)
