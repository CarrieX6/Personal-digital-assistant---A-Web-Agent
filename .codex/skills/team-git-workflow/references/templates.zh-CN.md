# 协作模板

作者：**Zhuofan Xie**

在创建 Pull Request、任务交接或记录冲突时使用。填写所有字段，并删除不适用章节。

## Pull Request

```markdown
## 改动摘要

- 修改了什么
- 为什么选择这个方案

## 任务与看板

- 任务 ID：`<TASK-ID 或不适用>`
- [ ] 已更新负责人、调研/实现状态和关联文档，或说明不适用原因

## 用户影响

- 可见行为与兼容性
- 本地模型下载、存储、权限或迁移影响

## 验证

- [ ] 后端测试：`<命令或不适用>`
- [ ] 前端 Lint：`<命令或不适用>`
- [ ] 前端测试：`<命令或不适用>`
- [ ] 手动检查：`<场景或不适用>`

## 安全与隐私

- [ ] 未包含 API Key、个人媒体、本地数据库、运行轨迹或模型权重
- [ ] 已记录新增的外部请求与数据存储

## 评审指引

- 建议首先查看：`<最重要的文件>`
- 重点关注：`<风险或设计决策>`

## 截图或预览

`UI 或生成媒体发生变化时附上。`

## 后续工作

- 待完成事项或 `无`
```

## 任务交接

```markdown
任务：
负责人：
分支：
最新提交：
Pull Request：
任务 ID：
看板状态：

已完成：
- ...

验证：
- 通过/跳过 — 命令与原因

潜在重叠：
- 文件、API、Schema 或 `无`

待决定事项与风险：
- ...

下一步：
- 接手成员需要执行的一项具体操作
```

## 冲突记录

```markdown
冲突来源：
- 当前分支：
- 合入分支：
- 冲突文件：

双方需要保留的意图：
- 当前分支：
- 合入分支：

解决方案：
- 最终行为：
- 兼容性影响：

验证：
- 命令与结果

后续：
- 需要的评审人或额外决策
```

## 命名示例

| 工作 | Codex 分支 | 人工分支 | 提交 |
| --- | --- | --- | --- |
| 飞书消息接入 | `codex/feature-feishu-channel` | `feature/feishu-channel` | `feat: add Feishu message adapter` |
| 资产权限修复 | `codex/fix-asset-authorization` | `fix/asset-authorization` | `fix: enforce asset ownership checks` |
| 记忆架构文档 | `codex/docs-memory-architecture` | `docs/memory-architecture` | `docs: explain memory storage boundaries` |
| Python 依赖更新 | `codex/chore-python-deps` | `chore/python-deps` | `chore: update backend dependencies` |
