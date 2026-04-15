# The Half Bakery Framework

**Your ideas are half-baked. Let the agents finish cooking.**

Half Bakery turns GitHub Issues into autonomous [Claude](https://claude.ai) agent work sessions. Write a ticket, drag it to "Ready," and walk away. A Python dispatcher picks it up, routes it to the right specialist agent, and runs it through a pipeline — engineering, QA, docs — until it's done.

No orchestration framework. No token-burning coordination layer. Just a cron job, some git worktrees, and Claude doing the actual work.

```
  You (human)                    The Bakery (agents)
  ┌──────────┐                   ┌──────────────────────┐
  │ Write an │    drag to        │  dispatcher.py       │
  │  issue   │ ──"Ready"──────> │  (runs every 5 min)  │
  │          │                   │                      │
  └──────────┘                   │  1. Pick up ticket   │
       │                         │  2. Create worktree  │
       │                         │  3. Spawn Claude     │
       │                         │  4. Merge when done  │
       │                         │  5. Advance pipeline │
       │                         └──────────────────────┘
       │                                    │
       │    ┌───────────────────────────────┘
       │    │
       v    v
  ┌──────────────────────────────────────────────────┐
  │  Engineering ──> QA ──> Docs ──> Done            │
  │     🔧           🔍       📝       ✅              │
  └──────────────────────────────────────────────────┘
```

## Why This Exists

Most multi-agent systems burn tokens on coordination — agents polling queues, reading board state, deciding what to do next. That's expensive busywork.

Half Bakery takes a different approach: **deterministic dispatch, stateless agents.** A ~1300-line Python script handles all the boring stuff (polling, routing, state tracking, merges). Agents receive exactly two things: who they are and what to do. No wasted tokens.

The whole thing runs on a Claude Max subscription. No API keys, no per-token billing, no infrastructure beyond your laptop and a launchd timer.

## Quick Start

### Prerequisites

- macOS (uses launchd for scheduling)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) with an active Max subscription
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated
- Python 3.10+

### 1. Clone and configure

```bash
git clone https://github.com/youruser/half-bakery-framework.git
cd half-bakery-framework

# Create runtime directories
mkdir -p ~/.half-bakery/{output,logs,worktrees,cache}
```

Edit `config/dispatcher.json`:
```json
{
  "max_concurrent": 3,
  "agent_timeout_minutes": 45,
  "projects_root": "~/projects",
  "agents_root": "~/half-bakery-framework/agents",
  "github_repo": "your-username/your-repo",
  "github_project_number": 1,
  "state_dir": "~/.half-bakery",
  "claude_permission_mode": "bypassPermissions",
  "spanning_projects": []
}
```

> **Note:** Use `bypassPermissions` — agents are headless (`--print` mode) and cannot respond to permission prompts. Safety comes from git worktree isolation and timeouts, not the sandbox.

### 2. Set up your GitHub Projects board

Create a [GitHub Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects) board (v2) on your repo with these columns:

| Column | Purpose |
|--------|---------|
| **Backlog** | Raw ideas. Dispatcher ignores these. |
| **Ready** | Triaged and ready. Dispatcher auto-routes to the right agent. |
| **Research** | Research Analyst investigates. |
| **Architecture** | Architect designs the approach. |
| **Engineering** | Founding Engineer builds it. |
| **QA** | QA agent reviews and tests. |
| **Docs** | Documentarian updates docs. |
| **Review** | Needs human attention. Dispatcher ignores. |
| **Done** | Complete. Issue gets closed. |

The dispatcher **auto-derives the target project** from the issue's repository name — no custom fields needed.

### 3. Test it

```bash
# Validate your setup (checks claude binary, config paths, gh CLI)
python3 scripts/dispatcher.py --dry-run

# Run the dispatcher manually
python3 scripts/dispatcher.py

# Create a test issue and drag to Ready, then run again
```

### 4. Install the timer

```bash
# Copy and edit the plist — update paths and environment variables (see below)
cp launchd/com.halfbakery.dispatcher.plist ~/Library/LaunchAgents/
vim ~/Library/LaunchAgents/com.halfbakery.dispatcher.plist

# Install
launchctl load ~/Library/LaunchAgents/com.halfbakery.dispatcher.plist
launchctl start com.halfbakery.dispatcher

# Now it runs every 5 minutes automatically
```

### launchd Plist Requirements

The plist **must** include these environment variables and settings. Without them, agents will crash silently:

```xml
<key>EnvironmentVariables</key>
<dict>
    <key>PATH</key>
    <!-- Must include ~/.local/bin where the claude binary lives -->
    <string>/Users/YOU/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    <key>HOME</key>
    <string>/Users/YOU</string>
    <key>USER</key>
    <!-- Required for Claude Max OAuth auth — without this, agents get "Not logged in" -->
    <string>YOUR_USERNAME</string>
</dict>
<!-- Required: launchd kills all child processes on parent exit by default.
     Since the dispatcher spawns agents and then exits, this MUST be true. -->
<key>AbandonProcessGroup</key>
<true/>
```

See `launchd/com.halfbakery.dispatcher.plist` for the full template.

## How the Pipeline Works

When an issue lands in the **Ready** column, the dispatcher scans the title and body for keywords and routes it:

| Keywords in issue | Routes to |
|-------------------|-----------|
| bug, fix, error, broken, crash | Engineering |
| research, investigate, explore, analyze | Research |
| design, architecture, RFC | Architecture |
| *(anything else)* | Engineering |

You can always override by dragging the issue to any column manually.

After each agent finishes, the dispatcher merges the work and advances to the next stage:

```
Engineering ──> QA ──> Docs ──> Done     (default pipeline)
Research ──> Ready                        (human reviews, decides next)
Architecture ──> Ready                    (human reviews, decides next)
```

## The Agents

Five specialists, each with a persona and clear boundaries:

| Agent | What it does | Vibe |
|-------|-------------|------|
| **founding-engineer** | Writes code, ships features, fixes bugs | "Move fast, commit often" |
| **qa** | Reviews code, checks security, enforces conventions | "No ship without my sign-off" |
| **documentarian** | Maintains project history and docs | "If it's not documented, it didn't happen" |
| **research-analyst** | Investigates questions, produces structured analysis | "Here are the facts and my recommendation" |
| **architect** | Designs systems, writes RFCs | "Let's think about this before we build it" |

Each agent gets:
- A persona file (`AGENTS.md`) — who they are and how they think
- An execution checklist (`HEARTBEAT.md`) — what to do every time
- An isolated git worktree — their own sandbox, no file conflicts

Each agent does NOT get:
- Board state or awareness of other agents
- Coordination responsibilities
- Access to the main branch

### Customizing agents

Agents are just markdown files. Edit `agents/{type}/AGENTS.md` to change behavior, add domain knowledge, or adjust boundaries. Add new agents by creating a new directory and updating `config/column-routes.json`.

## Spanning Projects

If an agent needs to work across multiple repos (e.g., the dispatcher itself needs access to all sibling projects), add the project name to `spanning_projects` in `dispatcher.json`:

```json
"spanning_projects": ["my-meta-project"]
```

Agents working on spanning projects receive `--add-dir` flags for all git repos under `projects_root`, giving them read/write access to the entire project portfolio.

## Epic / Sub-Issue Support

The dispatcher natively handles GitHub's sub-issues feature. Epics (issues with sub-issues) are skipped during dispatch — they're containers. Sub-issues dispatch normally with enriched context:

- **Parent Epic description** — the agent knows the bigger goal
- **Sibling awareness** — the agent sees other sub-issues (their status, not their work)
- **Auto-close** — when all sub-issues complete, the Epic is closed automatically

No configuration needed — detection is structural (issue has sub-issues = Epic).

## Dashboard

A local browser dashboard for monitoring the dispatcher. Zero external dependencies — Python stdlib HTTP server + vanilla HTML/CSS/JS.

```bash
# Launch the dashboard (opens browser automatically)
./dashboard/run

# Or run on a custom port
python3 dashboard/serve.py 8888
```

Shows running agents, activity feed, project inventory, and pipeline visualization.

## Configuration

### `config/dispatcher.json`

| Key | What it does | Recommended |
|-----|-------------|---------|
| `max_concurrent` | Max agents running at once | `3` |
| `agent_timeout_minutes` | Kill agents after this long | `45` |
| `projects_root` | Parent directory of your project repos | `~/projects` |
| `agents_root` | Path to this repo's `agents/` directory | *(set during setup)* |
| `github_repo` | Which repo to poll for issues | *(set during setup)* |
| `github_project_number` | Which Projects board to use | `1` |
| `claude_permission_mode` | Permission mode for Claude sessions | `bypassPermissions` |
| `spanning_projects` | Repos whose agents get cross-repo access | `[]` |

### `config/column-routes.json`

Maps board columns to agent types and defines the pipeline. Edit this to customize your workflow — add columns, change the pipeline order, or wire up new agents.

## Managing the Dispatcher

```bash
# Trigger a cycle now
launchctl start com.halfbakery.dispatcher

# Pause the service
launchctl stop com.halfbakery.dispatcher

# Unload entirely
launchctl unload ~/Library/LaunchAgents/com.halfbakery.dispatcher.plist

# Check if running
launchctl list | grep halfbakery

# Watch what's happening
tail -f ~/.half-bakery/logs/dispatcher.log

# See running agents
cat ~/.half-bakery/state.json
```

## What Happens When Things Go Wrong

| Situation | What the dispatcher does |
|-----------|------------------------|
| Agent times out | Kills the process, moves issue to Review, posts a comment with elapsed time |
| Agent crashes | Detects dead PID, moves to Review, posts last output for debugging |
| Agent is blocked | Detects `##BLOCKED##` in output, moves to Review, posts the blocker reason |
| Merge conflict | Moves to Review, preserves the agent's branch for manual resolution |
| Dispatcher itself crashes | Lock file prevents zombie runs. Agents keep running independently. Next cycle cleans up. |
| Orphaned worktrees/branches | Cleaned up automatically at the start of each dispatcher cycle |

Everything that needs human attention ends up in the **Review** column with a comment explaining what happened.

## Blocker Protocol

If an agent can't complete its work, it outputs a line starting with `##BLOCKED##` followed by the reason. The dispatcher detects this, posts the blocker as an issue comment, and moves the issue to **Review** for human attention.

```
##BLOCKED## Cannot proceed — AWS credentials not configured in environment
```

## Cost Model

This runs entirely on a **Claude Max subscription** ($100-200/month). No API keys, no per-token billing.

Things to know:
- 3 concurrent agents will eat through your rate-limit window faster than single sessions
- If you're hitting rate limits, drop `max_concurrent` to 2 or 1
- The 45-minute timeout is your budget safety net — adjust as needed
- Research and Architecture agents tend to be cheaper (shorter sessions) than Engineering

## File Layout

```
half-bakery-framework/
  agents/                    Agent personas (the "who")
    founding-engineer/
      AGENTS.md              Instructions and boundaries
      HEARTBEAT.md           Execution checklist
    qa/ ...
    documentarian/ ...
    research-analyst/ ...
    architect/ ...
  config/
    column-routes.json       Pipeline routing rules
    dispatcher.json          Runtime configuration
  scripts/
    dispatcher.py            The dispatcher (~1300 lines of Python)
  dashboard/
    serve.py                 Python stdlib HTTP server
    index.html               Single-page monitoring UI
    run                      Launcher script (opens browser)
  launchd/
    com.halfbakery.dispatcher.plist

~/.half-bakery/              Runtime state (created automatically)
  state.json                 Running agents + PIDs
  dispatcher.lock            Single-instance lock
  worktrees/                 One git worktree per active agent
  output/                    Agent stdout logs
  logs/                      Dispatcher logs
  cache/                     Cached GitHub Projects field IDs
```

## Design Decisions

**Why not use an existing orchestrator?** We evaluated 12+ tools. Every multi-agent framework we found either burns tokens on coordination (the thing we're trying to avoid) or requires infrastructure beyond a laptop. The closest match was Claude Code Agent Farm, but it generates work internally rather than reading from an external ticket source.

**Why git worktrees?** Universal consensus across the multi-agent ecosystem. Every serious orchestrator isolates agents this way. It prevents concurrent agents from stepping on each other's files while keeping them in the same git repo.

**Why launchd?** It's already on your Mac. No Docker, no Kubernetes, no cloud functions. The dispatcher is stateless and crash-safe — if it dies, the next 5-minute cycle picks up where it left off.

**Why GitHub Issues?** You probably already use them. The issue body IS the task specification. Comments become the communication log. The Projects board IS the kanban. No new tools to learn.

**Why `bypassPermissions`?** Agents run in `--print` (headless) mode and cannot respond to interactive prompts. Any permission prompt causes a hang or exit. Safety is provided by git worktree isolation (agents can't touch main branch) and the timeout kill switch.

## Contributing

PRs welcome. The dispatcher is a single Python file with no dependencies beyond the standard library + `gh` CLI. Agent personas are just markdown.

## License

MIT
