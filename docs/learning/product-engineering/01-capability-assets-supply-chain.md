# Capability、资产与软件/模型供应链

作者：**Zhuofan Xie**  
更新日期：2026-07-28  
成熟度：架构初稿，Manifest 与安装实验待完成  
关联任务：`LEARN-OPS-001`、`CAP-001`、`CAP-002`

## 1. 概念边界

| 概念 | 本项目定义 |
| --- | --- |
| Tool | Agent 可调用的最小、带 Schema 的操作 |
| Capability | 可被用户理解和安装的一项完整功能，包含工具、模型、Workflow 和 UI 元数据 |
| Skill | 给 Agent 的工作方法/知识说明，不自动等于执行权限 |
| Model | 推理资产和运行配置 |
| MCP Server | 通过 MCP 暴露工具/资源的适配服务 |
| Plugin Package | 可安装交付单元 |
| Workflow | 确定性与 Agent 节点组成的执行过程 |

一个“空间照片 Capability”可包含上传校验 Tool、深度模型、LDI Workflow、Viewer
模板和导出 Tool；用户安装的是 Capability，而不是一串散落的 Python 包。

## 2. Manifest v1 建议

```yaml
id: com.example.spatial-photo
version: 0.1.0
author: Zhuofan Xie
entrypoint: package.module:create_capability
inputs:
  - image/jpeg
outputs:
  - image/jpeg
  - video/mp4
  - application/vnd.example.spatial-scene
runtime:
  python: ">=3.11,<3.13"
permissions:
  filesystem: [workspace-assets]
  network: [none]
resources:
  memory_mb: 4096
  gpu_memory_mb: 2048
risk_level: L1
license:
  code: Apache-2.0
artifacts:
  - url: ...
    sha256: ...
```

真实 Schema 还需定义：平台、架构、模型许可证、依赖锁文件、进度协议、错误码、
卸载清理、兼容范围和签名。

## 3. 安装生命周期

```text
Discover → Review → Download → Verify → Install → Smoke Test
        → Enable → Upgrade/Rollback → Disable → Uninstall → Data Cleanup
```

每一步都可失败。必须区分：

- 卸载代码/模型；
- 删除由 Capability 生成的用户资产；
- 删除用户记忆与配置；
- 撤销凭证。

后面三项不能因卸载自动发生，需向用户明确说明。

## 4. 隔离

- 每个 Capability 使用锁定依赖和隔离环境；
- 默认无网络、无任意文件系统和无 shell；
- 文件以 Asset ID 传入，不给整个主目录；
- GPU 使用经 Scheduler 分配；
- 外部模型服务使用短期、最小权限凭证；
- MCP/插件进程的输出视为不可信；
- 安装和运行使用不同权限。

容器不是桌面端唯一答案；macOS 本地 GPU 可能需要原生进程。即便不能完全容器化，
也要做到独立用户目录、明确 allowlist、资源限制和进程退出清理。

## 5. 软件与模型供应链

至少记录：

- 源仓库、release/tag/commit；
- 下载 URL 和 SHA-256；
- 代码、权重、数据与 API 许可证；
- Python/Node/系统依赖；
- 构建者与构建参数；
- 已知漏洞和撤销状态；
- 模型卡、预期用途、限制和评测；
- 本项目验证设备和结果。

SPDX 是 ISO/IEC 5962:2021 国际开放标准，可表示 SBOM 与相关供应链信息
([SPDX Specifications](https://spdx.dev/use/specifications/))。CycloneDX 1.7 的对象
模型可表示软件、服务、依赖、ML 模型、来源和漏洞，其 ML-BOM 专门记录模型、数据集
和训练配置
([CycloneDX Overview](https://cyclonedx.org/specification/overview/),
[ML-BOM](https://cyclonedx.org/capabilities/mlbom/))。

SLSA 是用于描述并逐级改善供应链安全的规范，包含构建来源与验证
([SLSA v1.2](https://slsa.dev/spec/v1.2/))；Sigstore Cosign 可对容器和普通 Blob
签名/验证
([Sigstore](https://docs.sigstore.dev/cosign/signing/signing_with_blobs/))。

首期不必一次实现所有标准，但至少要有 hash、来源、许可和可复现的锁文件。

## 6. 模型卡不是许可证

Hugging Face 官方建议模型卡记录预期用途、限制、训练数据、参数和评测，并可在
metadata 中声明 license
([Hugging Face Model Cards](https://huggingface.co/docs/hub/model-cards))。

检查顺序：

1. 仓库 LICENSE/模型权重许可原文；
2. 模型卡；
3. 基础模型和训练数据条款；
4. 商业使用、再分发、衍生、命名和地域限制；
5. API 服务条款；
6. 无明确许可则不默认可商用或再分发。

## 7. 多模态资产管线

上传后：

1. 流式写临时隔离区并计算 hash；
2. 检查大小、MIME、Magic Number、像素/时长；
3. 安全解码并移除不必要元数据；
4. 写入 Asset Store；
5. 生成缩略图/预览；
6. 派生资产记录 parent 和 pipeline version；
7. 按所有权生成访问授权；
8. 到期清理未引用临时资产。

3DGS 等格式尚未完全统一时，项目应定义版本化 Bundle：manifest、点/高斯数据、相机、
缩略图、Viewer 兼容版本和许可证。

## 8. 升级与回滚

- 安装新版本不覆盖旧环境；
- 先做 hash、Schema 和 smoke test；
- 新 Job 才切流；
- 进行中 Job 保留原版本；
- Manifest/DB migration 可向前和可恢复；
- 模型缓存按内容寻址；
- 安全撤销可禁用某 hash；
- 回滚不删除用户资产。

## 9. 来源

- [MCP Architecture](https://modelcontextprotocol.io/docs/learn/architecture)
- [SPDX Specifications](https://spdx.dev/use/specifications/)
- [CycloneDX Specification Overview](https://cyclonedx.org/specification/overview/)
- [CycloneDX ML-BOM](https://cyclonedx.org/capabilities/mlbom/)
- [SLSA v1.2](https://slsa.dev/spec/v1.2/)
- [Sigstore：Signing Blobs](https://docs.sigstore.dev/cosign/signing/signing_with_blobs/)
- [Hugging Face Model Cards](https://huggingface.co/docs/hub/model-cards)

返回[产品化工程知识地图](README.md)。

