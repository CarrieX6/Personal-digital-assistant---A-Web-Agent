---
name: team-git-workflow
description: Coordinate safe multi-person Git and GitHub work in the Personal Digital Assistant repository. Use when starting or claiming a task, creating a branch, inspecting or staging changes, committing, synchronizing with main, pushing, opening or reviewing a pull request, resolving conflicts, handing work to a teammate, or preparing a release. Also use when several people or agents may edit the repository concurrently.
---

# Team Git Workflow

作者：**Zhuofan Xie**

Keep `main` runnable, preserve other contributors' work, and make every published
change reviewable. Treat local edits, credentials, personal media, generated
assets, and model files as data that must not be discarded or exposed.

## Establish Context

1. Run `git status -sb`, `git branch --show-current`, `git remote -v`, and
   `git log -5 --oneline`.
2. Inspect both `git diff` and `git diff --cached` before editing or staging.
3. Identify the task owner, intended scope, current branch, upstream branch, and
   files likely to overlap with another contributor.
4. Resolve any referenced issue or pull request before changing code.
5. Treat every pre-existing modification as user-owned. Do not overwrite,
   discard, stage, commit, or stash it unless it belongs to the stated task.
6. Stop and report the collision when another contributor is actively changing
   the same lines or when safe ownership cannot be established.

## Choose the Workflow

- Use one task, one owner, one branch, and one pull request.
- For Codex-created branches, use `codex/<type>-<short-slug>`.
- For human-created branches, prefer `feature/<slug>`, `fix/<slug>`,
  `docs/<slug>`, or `chore/<slug>`.
- Work directly on `main` only when the user explicitly requests it and accepts
  the loss of pull-request review.
- Create a draft pull request when work is useful to share but not ready to
  merge.
- Never mix unrelated cleanup into a feature or fix.

## Start a Task

When the worktree has no unrelated local changes:

```bash
git fetch origin --prune
git switch main
git pull --ff-only
git switch -c codex/<type>-<short-slug>
```

When the worktree is dirty, first determine which changes belong to the current
task. Do not switch branches, stash, or clean files merely to obtain a clean
status. Ask for direction only if the existing state prevents safe progress.

Record task ownership in the related issue or pull request when one exists.
Before broad refactors, announce the intended modules so teammates can avoid the
same files.

## Implement Safely

- Keep commits small and cohesive, but do not create meaningless micro-commits.
- Maintain backward-compatible interfaces unless the task authorizes a breaking
  change.
- Add or update the smallest relevant tests and documentation.
- Register new assistant abilities in the project's Capability Registry and
  describe local model, storage, permission, and download requirements.
- Do not commit API keys, `.env` files, personal images, settings, databases,
  run traces, generated assets, model weights, or machine-specific caches.
- Never print secret values while checking files. Inspect names, ignore rules,
  and staged diffs instead.

The repository must continue to ignore at least:

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

## Validate Before Commit

Inspect the exact proposed change:

```bash
git diff --check
git diff --stat
git diff
git diff --cached
git status --short
```

Run checks proportional to the change. For application changes, prefer:

```bash
.venv/bin/python -m pytest backend/tests -q
pnpm run lint
pnpm run test
```

For a documentation- or Skill-only change, run the relevant validator and
`git diff --check`. Report every check that was skipped and why.

Before staging, search tracked and untracked filenames for private artifacts and
inspect the candidate diff for credential-shaped content without echoing secret
values.

## Stage and Commit

1. Stage explicit paths with `git add <path>...`.
2. Use `git add -A` only after confirming the entire worktree is in scope.
3. Re-run `git diff --cached --stat` and `git diff --cached`.
4. Use Conventional Commit subjects:
   `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, or `chore:`.
5. Include generated attribution only when the repository owner requests it.
6. Confirm `git status -sb` and the new commit after committing.

Do not amend a published commit or rewrite a teammate's history without explicit
agreement.

## Synchronize and Publish

1. Run `git fetch origin --prune`.
2. If only the current owner has used the branch, rebase it onto `origin/main`.
3. If other people share the branch, merge `origin/main` instead of rewriting
   their commits.
4. Resolve conflicts file by file by understanding both sides. Never solve a
   conflict by blindly choosing “ours” or “theirs.”
5. Re-run affected checks after synchronization.
6. Push the branch with an upstream:
   `git push -u origin <branch>`.
7. Never force-push `main`. Use `--force-with-lease` only on a privately owned
   feature branch, for a clear reason, and with authorization.

Open a draft pull request by default. Mark it ready only when implementation,
tests, documentation, and reviewer guidance are complete. Load
`references/collaboration-templates.md` when creating or reviewing a pull
request, handing off work, or resolving a non-trivial conflict.

## Review and Merge Boundary

- Summarize what changed, why, user impact, validation, risks, screenshots for
  UI changes, and any model download or data migration.
- Request review from a teammate who understands the affected area.
- Address actionable feedback in new commits and rerun relevant checks.
- Do not dismiss review threads or merge the pull request unless the user has
  authorized that external action.
- Prefer squash merge for a noisy branch and regular merge when its commit
  history communicates meaningful stages.
- Delete a remote branch only after merge and only when no teammate still uses
  it.

## Handle Conflicts and Recovery

- Fetch first and inspect the divergence graph.
- Preserve both contributors' intent, then rebuild the smallest coherent
  result.
- Run `git diff --check` and all affected tests after resolution.
- Never use `git reset --hard`, `git checkout -- <path>`, recursive deletion,
  blind stash deletion, or branch deletion as a shortcut.
- If credentials were committed, stop publishing, revoke or rotate them, and
  coordinate history cleanup. Removing the latest copy alone is insufficient.

## Hand Off

End with:

- task and owner;
- branch and latest commit;
- pull request URL and state;
- completed and skipped checks;
- files or interfaces likely to conflict;
- remaining decisions, risks, migrations, and model or data dependencies;
- exact next action for the teammate.

Do not claim publication, review, merge, or passing checks without confirming
the actual state.
