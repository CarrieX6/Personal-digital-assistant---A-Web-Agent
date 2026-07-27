# Collaboration Templates

作者：**Zhuofan Xie**

Use these templates when the core workflow calls for a pull request, handoff, or
conflict record. Fill every field and remove sections that do not apply.

## Pull Request

```markdown
## Summary

- What changed
- Why this approach was chosen

## User impact

- Visible behavior and compatibility
- Local model download, storage, permission, or migration impact

## Validation

- [ ] Backend tests: `<command or not applicable>`
- [ ] Frontend lint: `<command or not applicable>`
- [ ] Frontend tests: `<command or not applicable>`
- [ ] Manual check: `<scenario or not applicable>`

## Security and privacy

- [ ] No API keys, personal media, local databases, run traces, or model weights
- [ ] New external calls and stored data are documented

## Review guidance

- Start with: `<most important files>`
- Pay special attention to: `<risk or design decision>`

## Screenshots or preview

`Attach for UI or generated-media changes.`

## Follow-up

- Remaining work or `None`
```

## Task Handoff

```markdown
Task:
Owner:
Branch:
Latest commit:
Pull request:

Completed:
- ...

Validation:
- PASS/SKIPPED — command and reason

Potential overlaps:
- files, APIs, schemas, or `None`

Remaining decisions and risks:
- ...

Next action:
- one concrete action for the receiving teammate
```

## Conflict Record

```markdown
Conflict source:
- current branch:
- incoming branch:
- conflicting files:

Intent preserved from each side:
- current:
- incoming:

Resolution:
- chosen behavior:
- compatibility impact:

Validation:
- commands and outcomes

Follow-up:
- reviewer or additional decision required
```

## Naming Examples

| Work | Codex branch | Human branch | Commit |
| --- | --- | --- | --- |
| Feishu ingress | `codex/feature-feishu-channel` | `feature/feishu-channel` | `feat: add Feishu message adapter` |
| Asset access fix | `codex/fix-asset-authorization` | `fix/asset-authorization` | `fix: enforce asset ownership checks` |
| Architecture note | `codex/docs-memory-architecture` | `docs/memory-architecture` | `docs: explain memory storage boundaries` |
| Dependency update | `codex/chore-python-deps` | `chore/python-deps` | `chore: update backend dependencies` |
