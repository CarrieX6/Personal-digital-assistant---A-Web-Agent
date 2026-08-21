# AI 功能与模型知识地图

作者：**Zhuofan Xie**
更新日期：2026-07-28
状态：仅大纲

本分域覆盖个人数字助手功能库中的视觉、3D、视频和端侧模型能力，并提供统一的模型
选型、评测和部署框架。

## 1. 模型选型方法

### 1.1 任务定义

- 用户目标；
- 输入和输出；
- 允许的失败；
- 质量档位；
- 实时、异步或离线；
- 云端、本地或手机端；
- 隐私等级。

### 1.2 候选模型信息

- 模型、版本和 checkpoint；
- 参数量和权重大小；
- 输入分辨率；
- 输出格式；
- 训练数据；
- 代码和权重许可；
- 维护状态；
- 硬件和依赖。

### 1.3 统一评价维度

- 视觉质量；
- 身份和结构一致性；
- 失败类型；
- Latency；
- Memory / VRAM；
- Power / Energy / Temperature；
- API 与存储成本；
- 离线能力；
- 集成和维护；
- 隐私；
- 商业使用。

## 2. 视觉基础能力

### 2.1 深度估计

- Relative 与 Metric Depth；
- Monocular Depth；
- 边界质量；
- 室内与室外；
- 人物、宠物和透明物体；
- Depth Anything、Depth Pro 等候选方向；
- 端侧量化。

### 2.2 分割、抠图与人体解析

- Semantic / Instance Segmentation；
- Matting；
- Human Parsing；
- Hair 和 Fur；
- Garment Mask；
- 交互式分割；
- 轻量端侧模型。

### 2.3 姿态与关键点

- Human Pose；
- Hand / Face Landmark；
- Animal Pose；
- 2D 与 3D Pose；
- 遮挡；
- 视频稳定性。

### 2.4 修复与生成

- Inpainting；
- Outpainting；
- Generative Fill；
- 遮挡背景补全；
- 内容安全；
- 身份和纹理一致性。

## 3. 空间照片与视角生成

### 3.1 2.5D 表示

- Depth Warp；
- Layered Depth Image；
- Multi-Plane Image；
- Foreground / Background Layer；
- Occlusion；
- Hole Filling。

### 3.2 几何表示

- Point Cloud；
- Mesh；
- Texture；
- Camera；
- GLB / glTF；
- PLY；
- USD / USDZ。

### 3.3 Neural Rendering

- NeRF；
- 3D Gaussian Splatting；
- Single-image 3DGS；
- Novel View Synthesis；
- Sparse View；
- Nearby View 与 Large-baseline View。

### 3.4 Viewer

- Three.js；
- WebGL / WebGPU；
- Camera Headbox；
- Device Orientation；
- Mobile Touch；
- Level of Detail；
- Video 降级；
- 功耗。

## 4. 虚拟试衣与数字人

### 4.1 2D Virtual Try-on

- Person Image；
- Garment Image；
- Pose；
- Human Parsing；
- Cloth Warping；
- Diffusion；
- 身份保持；
- 衣物纹理和文字保持；
- 遮挡和身体结构。

### 4.2 数字人

- Avatar；
- 多视角一致性；
- Face / Body Identity；
- Pose Control；
- 表情；
- Hair；
- 隐私和生物特征。

### 4.3 3D Virtual Try-on

- Body Mesh；
- Garment Mesh；
- Cloth Simulation；
- Rig；
- Skinning；
- Collision；
- Neural Garment；
- 2D 到 3D 过渡路线。

### 4.4 评价

- Identity；
- Garment Fidelity；
- Pose；
- Anatomy；
- Occlusion；
- Temporal Consistency；
- 用户隐私和授权。

## 5. 虚拟宠物

### 5.1 外观生成

- Single-image Multi-view；
- 3D Reconstruction；
- Fur；
- Species Consistency；
- Texture；
- 失败回退。

### 5.2 骨骼与动画

- Rigging；
- Skinning；
- Retargeting；
- Animal Skeleton；
- Procedural Animation；
- Motion Generation；
- Motion Library。

### 5.3 行为系统

- 猫、狗、仓鼠等物种行为；
- Idle；
- Sleep；
- Eat；
- Groom；
- Run Wheel；
- State Machine；
- Agent 行为与动画解耦。

### 5.4 桌面运行

- Transparent Window；
- Always-on-top；
- Click-through；
- Event-driven Render；
- CPU / GPU；
- Battery；
- 系统交互权限。

## 6. 图像编辑、材质与文字效果

- Image-to-Image；
- Style Transfer；
- Material Transfer；
- Plush、Ceramic、Metal、Balloon；
- 3D Text；
- Depth 与 Normal；
- Relighting；
- HDR / EDR；
- Liquid Glass；
- Reflection、Refraction 和 Translucency；
- SDF 与沉浸光；
- 结构和文字可读性保持。

## 7. 视频、动作与插帧

- Frame Interpolation；
- Optical Flow；
- Intermediate View；
- Motion Transfer；
- Image-to-Video；
- Camera Motion；
- Temporal Consistency；
- Artifact；
- Mobile Playback；
- 生成与播放能耗。

## 8. 文生图与图生图

- Text-to-Image；
- Image-to-Image；
- ControlNet 类控制；
- Reference Image；
- Identity Consistency；
- View Conversion；
- Virtual Dressing；
- Image-to-3D；
- Image-to-3DGS；
- Prompt 与参数模板；
- 内容安全和版权。

## 9. 端侧和移动端部署

- PyTorch / ONNX；
- Core ML；
- Metal；
- CUDA / TensorRT；
- Android / HarmonyOS 端侧运行时；
- Quantization；
- Distillation；
- Tiling；
- Memory Mapping；
- Lazy Load；
- Thermal Throttling；
- Model Cache；
- 设备能力探测；
- 云端降级。

## 10. 功能库集成

- Capability Manifest；
- 输入输出 Schema；
- Model Dependency；
- Download；
- Progress；
- Cancel；
- Cache；
- Asset Relationship；
- Preview；
- Permission；
- License；
- Uninstall。

## 11. 必做实验

- 深度模型统一图集；
- 分割和毛发边界；
- Inpainting 遮挡背景；
- LDI、Mesh 与 3DGS；
- 2D Virtual Try-on；
- 宠物多视角和 Rig；
- 手机 Viewer；
- CPU、MPS、CUDA 和移动端；
- 质量、性能、功耗、许可和隐私矩阵。

## 12. 完成标准

- [ ] 每项功能有候选模型清单；
- [ ] 每个候选有版本、许可和设备要求；
- [ ] 使用统一测试集和指标；
- [ ] 区分上游报告和本项目实测；
- [ ] 明确端侧、云端和降级路线；
- [ ] 形成 Capability 和 ADR。

返回[知识库总导航](../README.md)。

---

Copyright © 2026 Zhuofan Xie. All rights reserved.
