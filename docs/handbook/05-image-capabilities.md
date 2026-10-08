# 5. 空间照片、风格化与功能库

文档整理：**Zhuofan Xie** · 2026-10-08

图片风格化主要实现：**Xianggang Ma**；Flux-GS 原始功能：**Zuheng Zhao**

[返回主目录](README.md)

## 5.1 空间照片先做一张可解释的测试图

安装空间模型后，在工具库打开空间照片，选择一张自己有权使用、主体和背景明显的图片。
先用小图完成测试，再测试横图、竖图、毛发、多主体和遮挡。观察 Job 状态与蒙版，
不要只看原图是否“变好看”。

```text
安全解码/重编码 → 相对深度
→ 主体蒙版 → 前景 Alpha + 补全背景
→ WebP/PNG + scene.json → Web/手机视差预览
```

## 5.2 深度模型与分割模型各自解决什么

| 模块 | 当前实现 | 为什么需要 |
| --- | --- | --- |
| 深度 | Depth Anything V2 Small | 估计图内远近；不是主体实例分类 |
| 完整主体 | macOS 14+ Apple Vision / BiRefNet | 避免猫的头、身体和尾巴因深度差被切到不同层 |
| 降级 | 深度阈值蒙版 | 分割不可用时仍可输出，但必须承认完整主体保护变弱 |
| 后处理 | 形态滤波、软 Alpha、遮挡区扩展、邻域填充/平滑 | 降低边缘孔洞与移动露底 |
| 交付 | 双层图像与元数据 | 降低浏览器渲染成本，不要求真实几何重建 |

源码：
[assets.py](../../backend/app/assets.py) 的 `DepthAnythingV2Estimator.estimate()`、
`build_layered_scene()`、`_nearest_background_fill()`；
[spatial_segmentation.py](../../backend/app/spatial_segmentation.py) 的
`AutoForegroundSegmenter.segment()`、`MacOSVisionForegroundSegmenter`、
`BiRefNetForegroundSegmenter`。

auto 依次尝试当前平台可用的原生分割、BiRefNet，再深度降级。
显式选 vision/birefnet 时，该模型失败会抛错，而不是总静默降级。
可以在 `.env` 设置 `SPATIAL_FOREGROUND_SEGMENTER=birefnet` 验证对应路径，
修改后重启；模型文件/依赖必须先安装。

模型原理来源：
[Depth Anything V2 官方仓库](https://github.com/DepthAnything/Depth-Anything-V2)、
[BiRefNet 官方仓库](https://github.com/ZhengPeng7/BiRefNet)。
本项目接入预训练权重，没有为当前空间照片功能另行训练深度/分割网络。

## 5.3 额外处理具体是什么

深度输出插值到图像尺寸，按百分位归一化为灰度；灰度不是以米为单位的真实距离。
解码检查上传字节、像素数量，最长边缩至 1600px，并重编码，不保留原始 EXIF。

主体分割提供软蒙版。后处理补小孔、适度羽化，前景以 RGBA 保存；
主体区域扩展后在背景中填补。当前 `_nearest_background_fill()` 是低分辨率邻域颜色传播与平滑，
不是扩散式 Inpainting，也无法恢复主体背后的真实场景。

因此大视角会暴露模糊、拉伸或不存在的背面。优化应该用边缘、主体完整率、
遮挡露底与视角范围评测，而不只是把视差幅度继续加大。

## 5.4 生成哪些文件，怎样检查

`SpatialSceneService._process()` 更新 Job，生成：

```text
source.webp
depth.png
foreground-mask.png
foreground.webp
background.webp
scene.json
```

`scene.json` 记录尺寸、文件名、模型、分割质量/警告、推荐强度和表示版本。
`segmentation_quality` 是工程启发式质量检查，不是标注数据测出的 mIoU。

不要直接公开整个资产目录。通过资产 API 或限定文件集的签名 Viewer 访问。
测试对应 [test_spatial_segmentation.py](../../backend/tests/test_spatial_segmentation.py)、
[test_api.py](../../backend/tests/test_api.py) 的空间管线/非法上传/owner 隔离测试。

## 5.5 两套预览实现必须分别理解

| 入口 | 实现 | 真实效果 |
| --- | --- | --- |
| Web 工作台 | [SpatialViewer.tsx](../../app/components/SpatialViewer.tsx)：Three.js，两平面分设 Z，指针目标平滑驱动 camera.position | 小范围透视与前后景相对移动 |
| 手机签名链接 | [lan_viewer.py](../../backend/app/lan_viewer.py)：HTML/CSS，两张分层图，触摸/设备姿态驱动 transform | 轻量 2.5D 视差，不依赖完整 Web 应用 |

当前 Web 几何是平面，不是按每像素深度生成连续网格；
深度图提供分层降级/调试与资产数据，但不能说“每个深度像素已变成独立三维点”。

手机端 deviceorientation 读取姿态变化，校准、限幅和平滑后映射为前后景不同位移；
不是把图片变成完整可旋转模型。系统权限/安全上下文不可用时可触摸拖动。
两个入口使用同一资产，但渲染器和输入机制不同，测试不能只覆盖其中一个。

Web 降低功耗的措施包含 low-power 提示、像素比上限、运动停止后停止动画帧；
这些是工程措施，尚无整机能耗前后对照，不应给出“省电百分比”。

## 5.6 图片风格化的真实调用路径

```text
内容图 + 1–3 张参考图 + 参数
→ PhotoStyleService 创建 Job
→ 独立 Provider（SDXL + IP-Adapter）
→ 本机隔离 worker 推理
→ result.webp / 元数据
→ Web 对照 + 飞书结果图/文件
```

SDXL 负责生成，IP-Adapter 用参考图条件引导；输出并不天然保持人物身份或结构完全不变。
需要按模式、参数、样本和 seed 验证，不使用“完整复刻任意风格”的承诺。

源码：
[style_transfer.py](../../backend/app/style_transfer.py) 的 `PhotoStyleService`、
[sdxl_style_provider.py](../../backend/app/sdxl_style_provider.py)、
[sdxl_worker.py](../../backend/app/sdxl_worker.py)、
[部署管理器](../../scripts/manage_photo_style.py)。

操作：

1. 设置中心确认真实模型已部署、Provider 健康；
2. 选择内容图与参考图，查看其角色；
3. 提交后立即显示任务，等待结果；
4. 查看实际 Provider/模型与输出，不把 local-preview 当 SDXL；
5. 测试快速重复点击：同一提交应复用任务；修改输入后应创建新的任务；
6. 对结果做人工盲评和失败样本记录，Fake 契约测试不代替质量验收。

专门部署和显存/MPS 限制见
[部署指南](../guides/photo-style-deployment.md)、
[MPS 验收模板](../experiments/style-004-macos-mps-validation.md)。

## 5.7 Flux-GS 为什么只有一个产品功能入口

产品功能“3D 建模”可以包含数据校验、训练提交、状态查询等内部工具，
不必把每个生命周期步骤变成单独的菜单功能。
当前需要受控 COLMAP 多视角数据集与独立 Provider；不是给任意单张图就完成完整 3DGS。

查看 [flux_gs.py](../../backend/app/flux_gs.py) 和
[部署/许可门禁](../guides/flux-gs-capability.md)。GPU、模型/依赖与第三方许可不满足时应明确不可用，
不能自动转成低质量图片后仍标成 3D 成功。

## 5.8 性能怎么看

已有 [PERF-001](../experiments/perf-001-spatial-feishu-baseline-2026-09-03.md)：
Apple M4、9 张图/20 次受控生成；其中热启动 19 次均值 4.783s、P95 7.408s，
20/20 完成。这是历史小样本本机基线，不是所有设备 SLA。

分层/补全/编码平均 3.860s，约占热启动总耗时 80.7%，所以优化优先级应由阶段计时决定。
RSS 和 MPS driver allocation 存在统一内存重叠，不相加；尚未测能耗。
首次生成、模型下载、进程启动、热推理是不同口径。

练习：挑一张失败图，分别判断问题来自深度、蒙版、背景补全、渲染还是链接。
不要把所有问题统称为“模型不行”。

[下一章：飞书与 Viewer](06-feishu-and-viewer.md) · [返回主目录](README.md)
