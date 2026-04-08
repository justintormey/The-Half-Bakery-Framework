# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

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
