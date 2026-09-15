# 实验记录

作者：**Zhuofan Xie**

本目录保存模型、Agent、平台接入、性能与能耗实验。实验记录必须关联
[调研与实现中心](../project-board.md)中的任务 ID，并使用
[实验模板](experiment-template.md)记录可复现信息。

## 实验索引

| 实验 ID | 任务 ID | 主题 | 日期 | 负责人 | 结论 | 文档 |
| --- | --- | --- | --- | --- | --- | --- |
| `STYLE-EXP-001` | `STYLE-001` | 图片风格化本地预览与工程链路 | 2026-08-12 | Xianggang Ma | API、Registry、Agent、Web/PC 与本地预览链路通过；真实 SDXL 质量待独立门禁 | [记录](style-001-local-preview.md) |
| `STYLE-EXP-002` | `STYLE-001` | 本机 SDXL Provider 准备与失败关闭 | 2026-08-13 | Xianggang Ma | 固定模型、许可、门禁、离线加载与诊断合同通过；模型未下载，GPU 生成待显式许可 | [记录](style-002-native-sdxl-readiness.md) |
| `STYLE-EXP-003` | `STYLE-001` | 本机 SDXL、IP-Adapter 与 LCM-LoRA GPU 冒烟 | 2026-08-13 | Xianggang Ma | 正式 CUDA 环境与两条真实推理路径通过 8 GB 工程冒烟；生产质量与稳定性仍待固定图集评测 | [记录](style-003-native-sdxl-gpu-smoke.md) |
| `PERF-EXP-001` | `PERF-001` | 空间照片与飞书链路性能基线 | 2026-09-03 | Zhuofan Xie | 20 次空间生成平均 5.030 s、P95 7.584 s；23 条飞书入站到首条出站记录 P95 1191.1 ms；仍需标准环境、真机和跨平台复测 | [记录](perf-001-spatial-feishu-baseline-2026-09-03.md) |

## 规则

- 不提交 API Key、个人图片、未授权数据或模型权重；
- 原始结果过大时记录生成方式、哈希和本地保存位置，不提交文件本体；
- 必须记录失败结果，不能只保留效果最好的样本；
- 上游报告与本项目实测分开；
- 性能结果必须注明设备、电源模式、运行时和模型版本。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
