# 多人 Git 协作流程

作者：**Zhuofan Xie**

保持 `main` 分支始终可运行，保护其他贡献者的工作，并确保所有发布的改动都可以被
审查。将本地修改、凭证、个人媒体、生成资产和模型文件视为不得丢失或泄露的数据。

## 建立上下文

1. 运行 `git status -sb`、`git branch --show-current`、`git remote -v`
   和 `git log -5 --oneline`。
2. 编辑或暂存前检查 `git diff` 和 `git diff --cached`。
3. 确认任务负责人、任务范围、当前分支、上游分支，以及可能与他人重叠的文件。
4. 如果任务引用了 Issue 或 Pull Request，修改代码前先读取并确认其范围。
5. 将所有已有修改视为用户或其他成员所有。除非它属于当前任务，否则不得覆盖、
   丢弃、暂存、提交或 stash。
6. 如果其他成员正在修改相同代码，或无法安全判断所有权，停止写入并报告冲突。

## 选择协作方式

- 一个任务对应一个负责人、一个分支和一个 Pull Request。
- Codex 创建的分支使用 `codex/<type>-<short-slug>`。
- 人工创建的分支优先使用 `feature/<slug>`、`fix/<slug>`、`docs/<slug>`
  或 `chore/<slug>`。
- 只有用户明确要求并接受失去 PR 评审时，才直接修改 `main`。
- 工作已经可以分享但尚未准备合并时，创建草稿 PR。
- 不把无关清理混入功能或修复任务。

## 开始任务

工作区没有无关修改时：

```bash
git fetch origin --prune
git switch main
git pull --ff-only
git switch -c codex/<type>-<short-slug>
```

工作区存在修改时，先判断哪些修改属于当前任务。不要为了得到干净状态而直接切换
分支、stash 或清理文件。只有现有状态确实阻止安全推进时才请求用户决定。

存在关联 Issue 或 PR 时，在其中记录任务负责人。进行大范围重构前说明计划修改的
模块，避免团队成员同时修改相同文件。

## 安全实现

- 保持提交小而完整，不创建没有独立意义的碎片提交。
- 除非任务明确允许破坏性变更，否则保持接口向后兼容。
- 添加或更新最小范围的相关测试和文档。
- 新增助手能力时，将其注册到项目的 Capability Registry，并说明本地模型、
  存储、权限和下载要求。
- 不提交 API Key、`.env`、个人图片、配置、数据库、运行轨迹、生成资产、
  模型权重或机器缓存。
- 检查敏感文件时不输出密钥值，只检查文件名、忽略规则和待提交差异。

仓库至少必须继续忽略：

```text
.env*
.venv/
backend/data/settings.json
backend/data/assets/
backend/data/source-images/
backend/data/*.sqlite3
backend/data/runs.jsonl
backend/data/*.enc
backend/data/.secret_master_key
```

## 提交前验证

检查准确的待提交范围：

```bash
git diff --check
git diff --stat
git diff
git diff --cached
git status --short
```

按照改动风险运行验证。应用代码变更优先运行：

```bash
.venv/bin/python -m pytest backend/tests -q
pnpm run lint
pnpm run test
```

只有文档或 Skill 变更时，运行对应验证器和 `git diff --check`。说明所有跳过的
检查及原因。

暂存前检查已跟踪和未跟踪文件名中是否存在个人数据，并检查待提交差异是否具有凭证
特征，但不要在输出中显示密钥值。

## 暂存与提交

1. 使用 `git add <path>...` 明确暂存文件。
2. 只有确认整个工作区都属于当前任务时，才使用 `git add -A`。
3. 再次运行 `git diff --cached --stat` 和 `git diff --cached`。
4. 使用 Conventional Commits：`feat:`、`fix:`、`docs:`、`test:`、
   `refactor:` 或 `chore:`。
5. 只有仓库所有者要求时才加入生成工具的署名。
6. 提交后确认 `git status -sb` 和新提交。

未经明确同意，不修改已经发布的提交，也不重写其他成员的历史。

## 同步与发布

1. 运行 `git fetch origin --prune`。
2. 只有当前负责人使用该分支时，将其 rebase 到 `origin/main`。
3. 多人共享分支时合并 `origin/main`，不要改写他人的提交。
4. 理解冲突双方意图后逐个文件解决，不盲目选择 “ours” 或 “theirs”。
5. 同步后重新运行受影响的检查。
6. 使用 `git push -u origin <branch>` 推送并设置上游。
7. 不强制推送 `main`。只有私人持有的功能分支确有需要且已获授权时，才使用
   `--force-with-lease`。

默认创建草稿 PR。只有实现、测试、文档和评审说明完整后，才标记为可评审状态。

## 评审与合并边界

- 说明改动内容、原因、用户影响、验证结果、风险、UI 截图，以及模型下载或数据迁移。
- 请求熟悉相关模块的团队成员评审。
- 使用新提交处理有效反馈，并重新运行相关检查。
- 未经用户授权，不关闭评审线程或合并 PR。
- 分支历史杂乱时优先 squash merge；提交历史具有清晰阶段意义时使用普通 merge。
- 仅在合并完成且没有成员继续使用时删除远端分支。

## 冲突与恢复

- 先 fetch，再检查分支分叉关系。
- 保留双方意图，重建最小且一致的结果。
- 解决后运行 `git diff --check` 和所有受影响测试。
- 不使用 `git reset --hard`、`git checkout -- <path>`、递归删除、
  盲目删除 stash 或删除分支作为捷径。
- 如果凭证已被提交，停止发布、撤销或轮换凭证，并协调清理历史；仅删除最新文件
  不足以消除泄漏。

## 工作交接

结束时提供：

- 任务和负责人；
- 分支与最新提交；
- PR 链接和状态；
- 已完成和跳过的检查；
- 可能冲突的文件或接口；
- 未解决决策、风险、迁移、模型与数据依赖；
- 接手成员需要执行的下一项具体操作。

未确认真实状态时，不得声称已经发布、通过评审、完成合并或通过检查。
