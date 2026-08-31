# `STYLE-EXP-004` macOS MPS 真实风格化验收模板

作者：**Zhuofan Xie**  
状态：`待执行`  
创建日期：2026-08-31

> 本文是实验协议，不是实验结果。未填写原始数据和证据前，不得把 MPS Provider 标为
> `production_quality=true`。

## 1. 目标

验证固定 revision 的 SDXL Base 1.0、IP-Adapter SDXL ViT-H 与可选 LCM-LoRA，能否在
Apple Silicon 的 PyTorch MPS 后端完成可重复的图片风格化，并量化质量、延迟、统一内存、
swap、功耗、温度、fallback 和任务后回收。

## 2. 固定环境

| 字段 | 待填写 |
| --- | --- |
| Git commit |  |
| macOS / 芯片 |  |
| 统一内存 / 可用磁盘 |  |
| Python / Torch / Diffusers / Transformers |  |
| 模型 lock SHA-256 |  |
| `PYTORCH_ENABLE_MPS_FALLBACK` |  |
| 电源模式 / 其他负载 |  |

## 3. 准备

```bash
./scripts/photo-style.sh doctor
./scripts/photo-style.sh plan-real
./scripts/photo-style.sh prepare-macos-mps --accept-model-licenses
python backend/scripts/smoke_photo_style_sdxl.py \
  --accelerator mps --content <content.png> --style <style.png> \
  --output <result.png> --quality preview --seed 1701
```

许可证接受只代表允许本机准备固定权重，不代表质量通过。测试图片必须有明确使用权；第一轮
优先使用项目生成的非个人合成图。

## 4. 用例矩阵

| 用例 | 质量 | 尺寸 | LCM | 次数 | 成功标准 |
| --- | --- | --- | --- | ---: | --- |
| 冷启动 | preview | 640 长边 | 关 | 1 | 输出非黑图、无 NaN、任务完成 |
| 热运行 | preview | 640 长边 | 关 | 10 | 10/10 完成，记录 P50/P95 |
| 标准质量 | standard | 768 长边 | 关 | 10 | 10/10 完成，无明显结构破坏 |
| LCM 对照 | preview | 640 长边 | 开 | 10 | 与基础预览对照质量和耗时 |
| 回收 | preview | 640 长边 | 关 | 3 | 子进程退出，主 Agent 不保留模型内存 |

固定 seed、内容图、风格图和业务参数；保存输入和输出 SHA-256。不要只挑成功图片。

## 5. 必须采集的指标

- 冷加载时间、纯生成时间、端到端时间、P50/P95；
- `torch.mps.driver_allocated_memory()`、进程 RSS、系统 memory pressure 和 swap；
- `powermetrics` 或等价工具采集的整机功耗与温度时间序列；
- CPU fallback 次数或日志、OOM、崩溃、黑图、NaN、重试次数；
- 主体一致性、构图保持、风格强度、额外肢体、文字/水印迁移等人工盲评；
- 连续十次后的质量漂移和任务退出后的内存回收时间。

## 6. 停止条件

- 系统持续进入高 memory pressure 或明显 swap 抖动；
- 任意黑图、NaN、不可恢复 OOM 或进程崩溃；
- fallback 令 P95 超过产品预算；
- 主体身份、布局或安全检查不能达到既定阈值；
- 权重、许可证、输入或运行时版本无法追溯。

## 7. 结论模板

| 维度 | 结果 | 证据路径 |
| --- | --- | --- |
| 工程可运行 | 待填写 |  |
| 十次稳定性 | 待填写 |  |
| P50 / P95 | 待填写 |  |
| 峰值统一内存 / swap | 待填写 |  |
| 功耗 / 温度 | 待填写 |  |
| 人工质量 | 待填写 |  |
| 是否允许 `production_quality=true` | 否，待评审 |  |

即使工程通过，也要单独评审产品质量；Windows CUDA 的历史门禁不能代替 MPS 结果。

## 8. 官方参考

- [Diffusers MPS 优化](https://huggingface.co/docs/diffusers/main/optimization/mps)
- [PyTorch MPS backend](https://docs.pytorch.org/docs/stable/notes/mps.html)
- [PyTorch MPS 环境变量](https://docs.pytorch.org/docs/stable/mps_environment_variables.html)
- [Diffusers IP-Adapter](https://huggingface.co/docs/diffusers/using-diffusers/ip_adapter)
