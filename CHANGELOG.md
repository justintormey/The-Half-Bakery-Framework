# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---
## [2.0.1] — 2026-04-17

### Summary

Patch release: Canonical issue ID refactor. All internal state, output filenames, process names, and agent prompts now use globally unique `owner/repo/number` keys (e.g. `justintormey/ledrdr/1`) instead of bare issue numbers. This eliminates cross-repo collisions that caused misdirected GitHub comments and duplicate agent dispatch when two repos shared the same issue number. Dashboard updated to parse canonical IDs. Agent artifacts routed to per-repo `.agent/` directories (gitignored).

---

### 🐛 Bug Fixes

#### Cross-repo issue collisions eliminated
When multiple repos had issues with the same number (e.g. `ledrdr#1` and `CougarCast#1`), the dispatcher used bare numbers as state keys — causing `state["running"]["1"]` collisions, misdirected GitHub comments, and agents dispatched multiple times per cycle.

**Root cause:** `state["running"]`, `state["pipeline_state"]`, `state["retry_queue"]`, output log filenames, and git branch names all used bare `issue_number` integers as keys.

**Fix:** Introduced `canonical_id(issue_repo, issue_number) → "owner/repo/number"` and `safe_id(cid) → "owner-repo-number"` helpers. `poll_board()` stamps `canonical_id` on every board item at the source. All downstream consumers use `canonical_id` as the state key and `safe_id` for filenames and branch names.

#### Duplicate dispatch within a single cycle fixed
A related bug caused the same issue to be dispatched multiple times in one cycle when the `running_issues` dedup set was built once before the loop and not updated mid-loop. Fixed by adding `running_issues.add(item["canonical_id"])` after each dispatch within the loop.

#### `cleanup_orphans` now correctly scopes branch deletion per repo
Previously, `cleanup_orphans` used a flat set of all running issue numbers to gate branch deletion — which could block deletion of `agent/1` in repo A because `repo/B#1` was running. Fixed by building a per-repo `allowed_nums` dict from running canonical IDs.

---

### ✨ Changes

#### Agent artifact directory standardized
Agent prompts now instruct agents to write all output artifacts (QA reports, research notes, engineering docs) to a `.agent/` directory within their target project repo. This directory is gitignored across all repos. Previously, artifacts accumulated in the dispatcher repo root as tracked files.

#### Dashboard updated for canonical IDs
- `dashboard/serve.py`: `/api/output/` endpoint accepts `safe_id` filenames (alphanumeric + dashes) instead of bare integers
- `dashboard/index.html`: Running agent cards parse `canonical_id` keys to display `#issueNumber` and link to the correct repo's GitHub issue page

#### Agent prompts enriched
Both `spawn_agent()` and `spawn_local_agent()` prompts now include:
- `**Issue:** owner/repo/number — title` (canonical reference)
- `**GitHub:** https://github.com/owner/repo/issues/N` (direct link)
- `**Artifacts:** Write to .agent/ in the target project repo`
- Explicit `--repo owner/repo` flag on all `gh issue` commands

---

### 🔗 Compatibility

This is a state-breaking change: any `state.json` with bare integer keys from v2.0.0 will not be recognized as running by v2.0.1. The recommended migration is to stop all in-flight agents (`launchctl stop com.halfbakery.dispatcher`), wipe `~/.half-bakery/state.json`, and restart. In-flight agents will still complete and commit their work; they simply won't be tracked for harvest.

[2.0.1]: https://github.com/youruser/The-Half-Bakery-Framework/compare/v2.0.0...v2.0.1

---
## [2.0.0] — 2026-04-15

### Summary

Major release: Smart Dispatcher v3 with evaluation gates, Skeptic agent, proactive work discovery, usage-aware scheduling, and local deployment module. This is a significant evolution from the v1.x "dispatch and hope" model to a verified, budget-aware, self-improving system.

---

### 🧠 Smart Evaluation (evaluator.py — NEW)
- 6-gate layered evaluation: output exists → summary block → git diff → scope match → test suite → optional LLM spot-check
- Zero tokens for first 5 gates; LLM gate only on retries
- Failed evaluations retry with failure context (max 2), then move to Review
- Column-specific gate configuration (Engineering gets all gates, Research/QA get lighter checks)
- Pipeline classification: issues auto-classified as bug/feature/research/architecture/chore/docs/polish with tailored pipelines

### 🔍 Skeptic Agent (agents/skeptic/ — NEW)
- Verification gate agent that trusts nothing and verifies everything
- Reads actual git diffs, runs tests, compares deliverables against issue requirements
- Can APPROVE (advance), REJECT (send back with feedback), or create new issues for gaps found
- Routes work to any column: Ready, Engineering, Research, Architecture, QA, Done
- Outputs structured ##VERDICT## block parsed by the dispatcher

### 🔎 Proactive Work Discovery (discoverer.py — NEW)
- Scans repos for TODO/FIXME comments, outdated deps, security vulnerabilities, quality gaps
- Vision-driven discovery: reads project-visions.md and generates issues for unstarted deliverables
- Interview questions: creates [Interview] issues for product owner decisions, routed to Review
- Orphan rescue: finds open GitHub issues not on the project board and adds them
- Board hygiene: fixes items with no status, moves closed items to Done
- Backlog → Ready promotion when queue is empty

### 💰 Usage Budgeting (budget.py, usage_tracker.py — NEW)
- Time-of-day scheduling: conservative during work hours, aggressive evenings/weekends
- 5-hour rolling window tracking from per-session token counts
- Weekly ceiling tracking with throttle and pause thresholds
- Per-agent model selection (Opus for complex work, Sonnet for review/docs)
- 429 rate limit detection from debug logs as emergency circuit breaker

### 🚀 Local Deployment (deployer.py — NEW)
- Replaces GitHub Actions deploy workflows with local S3 sync
- `.local/` directory pattern: PII, secrets, and deploy config stay gitignored
- Overlay system: merge local config into clean staging directory before deploy
- CloudFront invalidation after sync
- `deploy-targets.json` configuration for all projects

### 🔄 Pipeline & Routing
- Smart pipeline templates: bugs skip Docs, chores skip QA, features get full chain with Skeptic gates
- Skeptic verdict routing: agents can send work to any column based on review
- Pipeline state preservation: issues resume where they left off after Skeptic rerouting
- Epic/sub-issue support: epics go to Backlog, sub-issues dispatch independently
- Board pagination: handles projects with 100+ board items

### 📋 Agent Improvements
- All agent personas compressed ~50% (caveman-style input reduction)
- "Terse. No filler. Execute before explaining." output style
- Agents instructed to write ALL output in their project's repo, never in half-bakery
- Retry context injection: failed agents get specific feedback on what went wrong

### 🏗️ Infrastructure
- Configurable `--model` flag per agent type
- Local LLM provider with health check and automatic Claude fallback
- `--output-format json` for exact per-session token accounting
- Board hygiene runs every cycle: fixes orphans, closes done items, routes epics to Backlog

### Changed
- `column-routes.json`: Skeptic column added, Research/Architecture route to Skeptic instead of Review
- `dispatcher.json`: budget, evaluation, discovery, agent_models, providers config sections
- Dashboard: dynamic pipeline rendering, usage API endpoint

---


## [1.1.1] — 2026-04-08

### Summary

Documentation patch — remove three deprecated agent personas (marketing-expert, 3d-designer, ceo) from the public framework repo. These were removed from the active dispatch roster in the private working repo but were not cleaned up when v1.1.0 was published.

---

### 📝 Documentation Fixes

#### Stale agent references removed
The following agents were removed from the active roster (they were opinionated personas tied to a specific user's workflow, not general-purpose framework components). All references have been purged:

- **marketing-expert** — `agents/marketing-expert/` directory removed; Marketing column and keywords removed from `column-routes.json`; references removed from README
- **3d-designer** — `agents/3d-designer/` directory removed; 3D Design column and keywords removed from `column-routes.json`; references removed from README
- **ceo** — `agents/ceo/` directory removed; references removed from README (this agent was manual-only with no column)

#### Pipeline diagram corrected
README pipeline diagram now correctly shows:
```
Engineering ──> QA ──> Docs ──> Done     (default pipeline)
Research ──> Ready                        (human reviews, decides next)
Architecture ──> Ready                    (human reviews, decides next)
```
Previously incorrectly showed Research/Architecture routing to "Review" instead of "Ready".

#### Agent count updated
README now correctly states "Five specialists" (founding-engineer, qa, documentarian, research-analyst, architect) rather than "Eight specialists."

---

**Semver rationale:** PATCH — documentation-only correction. No API, behavior, or configuration changes. Removing the agent directories does not break existing deployments (users who had custom agents in these directories would need to keep their local copies, but the framework itself has no dependency on them).


## [1.1.0] — 2026-04-08

### Summary

Six weeks of real-world production use. The dispatch loop is stable and has processed dozens of issues end-to-end. This release captures all the lessons learned: critical launchd bug fixes, new features that emerged from actual usage, and hardening of the dispatcher's resilience.

---

### 🐛 Bug Fixes

#### launchd: Agents crashed silently on every dispatch
All dispatched agents were exiting immediately with zero output. Three independent root causes:

1. **Missing `AbandonProcessGroup`** — By default, launchd kills *all* child processes when the managed script exits. Since the dispatcher spawns agents and then exits, launchd was immediately killing every agent. Fix: `<key>AbandonProcessGroup</key><true/>` in the plist. **This is now documented in the plist template and README.**

2. **Missing `USER` environment variable** — Claude Code's OAuth session (Max subscription auth) uses the `USER` env var to locate credentials. launchd doesn't inherit this from the login session. Without it, every agent exited with "Not logged in." Fix: add `USER` to plist `EnvironmentVariables`.

3. **Missing `~/.local/bin` in PATH** — The `claude` binary lives at `~/.local/bin/claude` but that path wasn't in the launchd plist's `PATH`. Fix: prepend `~/.local/bin` to the plist PATH.

**Additional hardening:** `start_new_session=True` added to `subprocess.Popen` (agents get their own process group as defense-in-depth); `stdin=subprocess.DEVNULL` added (eliminates a 3-second "no stdin" warning from `claude --print`).

#### Cross-repo issue comments went to the wrong repo
When working on issues from repos other than the dispatcher's home repo, `gh_issue_comment` and `gh_issue_close` were using `config["github_repo"]` (always the dispatcher's repo). Comments and closes silently went to the wrong repo. Fix: extract `issue_repo` (from GraphQL `nameWithOwner`) when polling the board, and use it for all GitHub operations on that issue.

#### GraphQL queries hardcoded the repo owner
`get_project_fields()` and `poll_board()` had a hardcoded username in their GraphQL queries. This made the dispatcher fail silently for any user other than the original author. Fix: derive `owner = config["github_repo"].split("/")[0]` dynamically in both functions.

---

### ✨ New Features

#### Auto-derive Target Project
The `Target Project` custom field is no longer required on GitHub issues. The dispatcher now derives the target project from the issue's repository name (`nameWithOwner` from GraphQL), with an exact-match, case-insensitive, and nested-directory fallback chain. Manual `Target Project` field overrides still work.

#### Spanning Projects
Agents working on meta-projects (e.g., a dispatcher or orchestration repo) can now receive `--add-dir` access to all sibling project directories. Configure in `dispatcher.json`:
```json
"spanning_projects": ["your-meta-project"]
```
Agents working on spanning projects receive `--add-dir` flags for every git repo under `projects_root`, giving them cross-portfolio read/write access.

#### Dashboard
A local browser dashboard for monitoring the dispatcher at a glance. Zero external dependencies — Python stdlib HTTP server + vanilla HTML/CSS/JS.
```bash
./dashboard/run
```
Shows running agents, activity feed, project inventory, and pipeline visualization.

#### Epic / Sub-Issue Support
Native support for GitHub's sub-issues feature. Epics (issues with sub-issues attached) are automatically detected and skipped during dispatch — they're containers, not work items. Sub-issues dispatch normally with enriched context:
- Parent Epic title and description are injected into the agent's assignment
- Sibling sub-issue list (number, title, state) is visible to each agent — for context only
- When all sub-issues complete, the parent Epic is auto-closed

Detection is structural: if an issue has sub-issues, it's an Epic. Zero configuration changes needed.

#### Dry-Run Mode
```bash
python3 scripts/dispatcher.py --dry-run
```
Simulates a full dispatch cycle without spawning any agents, posting comments, or moving issues. Useful for validating configuration and testing routing before going live.

#### Startup Validation
`validate_environment()` runs at startup and fails fast if:
- The `claude` binary is not found
- Required config files are missing
- `projects_root` or `agents_root` don't exist
- The `gh` CLI is not in PATH

This surfaces misconfigurations immediately rather than letting them fail silently mid-cycle.

#### Orphan Cleanup
Stale worktree directories and `agent/*` git branches that are no longer tracked in `state.json` are automatically pruned at the start of each dispatcher cycle.

#### GraphQL Retry Logic
All GraphQL calls now retry up to 3 times with a 2-second backoff before failing. Improves resilience against transient GitHub API errors.

#### Structured Agent Output Parsing
Agents that output a `##SUMMARY##...##END##` block get a clean, formatted issue comment with the key fields (what was done, files changed, commits, follow-up needed). Agents without the block fall back to truncated raw output. Format:
```
##SUMMARY##
DONE: <one sentence>
FILES: <comma-separated list>
COMMITS: <SHAs or "none">
FOLLOWUP: <issues to create, or "none">
##END##
```

---

### ⚠️ Configuration Changes

#### `claude_permission_mode`: `acceptEdits` → `bypassPermissions`
Agents are headless (`--print` mode) and cannot respond to permission prompts. Any sandbox prompt causes a hang or silent exit. `bypassPermissions` is now the recommended value. Safety is provided by git worktree isolation and the timeout kill switch, not the sandbox.

#### New config keys
- `spanning_projects` (array, default `[]`) — list of project names whose agents get cross-portfolio `--add-dir` access
- `agent_timeout_minutes` default raised from 30 → 45 to accommodate longer agent sessions

---

### 🔒 Security

- Fixed: GraphQL queries no longer hardcode the repo owner
- Fixed: Issue comments now correctly target the issue's originating repo, not always the dispatcher's home repo
- No secrets, credentials, or PII in the codebase — the `github_repo` config field is user-supplied at setup time

---

### 📁 New Files

- `dashboard/serve.py` — Python stdlib HTTP server
- `dashboard/index.html` — Single-page monitoring UI
- `dashboard/run` — Launcher script

---

## [1.0.0] — 2026-03-31

### Initial release

Core dispatcher: polls GitHub Projects board, auto-routes issues by keyword, spawns Claude CLI agents in isolated git worktrees, harvests output, merges branches, posts issue comments, advances pipeline.

**Components:**
- `scripts/dispatcher.py` — the dispatcher
- `agents/` — 8 agent personas (founding-engineer, qa, documentarian, research-analyst, architect, marketing-expert, 3d-designer, ceo)
- `config/dispatcher.json` + `config/column-routes.json` — pipeline configuration
- `launchd/com.halfbakery.dispatcher.plist` — macOS scheduling

[2.0.0]: https://github.com/youruser/The-Half-Bakery-Framework/compare/v1.1.1...v2.0.0
