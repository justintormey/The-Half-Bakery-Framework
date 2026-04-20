# Contributing to Half Bakery

## Commit Message Format

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <short description>
```

**Types:**

| Type | When to use |
|------|-------------|
| `feat` | New capability or agent behavior |
| `fix` | Bug fix |
| `refactor` | Code restructure with no behavior change |
| `docs` | Documentation only |
| `chore` | Maintenance (deps, config, tooling) |

**Common scopes:**

| Scope | Covers |
|-------|--------|
| `dispatcher` | `scripts/dispatcher.py` and dispatch loop |
| `evaluator` | `scripts/evaluator.py`, LLM gate logic |
| `budget` | Budget tracking, spend logging |
| `config` | `config/*.json`, schema, column routes |
| `docs` | Docs, READMEs, agent AGENTS.md files |

Examples:
```
feat(dispatcher): add canonical_id stamping to poll_board()
fix(evaluator): fail-closed on LLM gate errors
refactor(dispatcher): extract paginate_project_items() helper
docs(skeptic): document Data Lifecycle Audit rule
chore(config): update projects_root to ~/PROJECTS
```

---

## One-Commit-Per-Fix Discipline

Each commit must be **self-contained and bisectable**:

- It builds and runs correctly in isolation
- `git revert <sha>` undoes exactly the change it describes
- It does not bundle unrelated cleanup with a functional change
- Commit message references the GitHub issue: `(#123)` or `fixes #123`

### When to bundle vs. split

**Bundle** when fixes share a root cause and reverting one without the other would leave the codebase broken. Write a commit message that names the shared root cause.

**Split** when:
- Changes are in different subsystems
- One could be cherry-picked without the other
- The description would need "and" to cover both

When in doubt, split.

---

## Skeptic's Data Lifecycle Audit

Any PR that modifies a **persisted data shape** — `state.json`, cached JSON, GitHub project fields/labels, config files, or any dict that survives across runs — **must pass the Skeptic's Data Lifecycle Audit before merge**.

Full rule: [`agents/skeptic/AGENTS.md` — Data Lifecycle Audit section](agents/skeptic/AGENTS.md)

The three required checks:

1. **Other writers** — grep every place the structure is written. A new field is safe only if every writer populates it, or every reader tolerates its absence.
2. **Live data** — check what is actually stored now (`cat ~/.half-bakery/state.json`, read real project items). Do existing entries match the new reader's assumptions?
3. **Migration** — any schema tightening (new required field, renamed key, split structure) requires an idempotent migration that runs on every load.

**Missing any one of these three = REJECT.**

Precedent: on 2026-04-17 a PR added `pipeline` / `pipeline_index` to `state["pipeline_state"][cid]` but left 11 legacy counter-only entries unhandled. The reader crashed every cycle and blocked the whole fleet for hours.

---

## State and Migration Discipline

Any schema change to `state.json` or any other persisted structure requires:

1. **All writers aligned** — every code path that writes the structure must be updated in the same PR
2. **Live-data audit** — verify that existing entries in production state are compatible with the new reader assumptions before the PR merges
3. **Idempotent migration in `migrate_state()`** — the migration must be safe to run multiple times and must handle entries created before the change

A schema change that ships without all three is a breaking change waiting to happen.
