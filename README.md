# Half Bakery

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

Half Bakery takes a different approach: **deterministic dispatch, stateless agents.** A ~700-line Python script handles all the boring stuff (polling, routing, state tracking, merges). Agents receive exactly two things: who they are and what to do. No wasted tokens.

The whole thing runs on a Claude Max subscription. No API keys, no per-token billing, no infrastructure beyond your laptop and a launchd timer.

## Quick Start

### Prerequisites

- macOS (uses launchd for scheduling)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) with an active Max subscription
- [GitHub CLI](https://cli.github.com/) (`gh`) authenticated
- Python 3.10+

### 1. Clone and configure

```bash
git clone https://github.com/justintormey/half-bakery-framework.git
cd half-bakery-framework

# Create runtime directories
mkdir -p ~/.half-bakery/{output,logs,worktrees,cache}
```

Edit `config/dispatcher.json`:
```json
{
  "max_concurrent": 3,
  "agent_timeout_minutes": 30,
  "projects_root": "~/projects",
  "agents_root": "~/half-bakery-framework/agents",
  "github_repo": "your-username/your-repo",
  "github_project_number": 1,
  "state_dir": "~/.half-bakery",
  "claude_permission_mode": "acceptEdits"
}
```

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
| **Marketing** | Marketing Expert writes content. |
| **3D Design** | 3D Designer creates assets. |
| **Review** | Needs human attention. Dispatcher ignores. |
| **Done** | Complete. Issue gets closed. |

Add a custom text field called **Target Project** — this tells the dispatcher which repo to work in.

### 3. Test it

```bash
# Run the dispatcher manually first
python3 scripts/dispatcher.py

# Create a test issue, set Target Project, drag to Ready
# Run the dispatcher again and watch it work
```

### 4. Install the timer (optional)

```bash
# Edit the plist with your paths first
vim launchd/com.halfbakery.dispatcher.plist

# Install
cp launchd/com.halfbakery.dispatcher.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.halfbakery.dispatcher.plist
launchctl start com.halfbakery.dispatcher

# Now it runs every 5 minutes automatically
```

## How the Pipeline Works

When an issue lands in the **Ready** column, the dispatcher scans the title and body for keywords and routes it:

| Keywords in issue | Routes to |
|-------------------|-----------|
| bug, fix, error, broken, crash | Engineering |
| research, investigate, explore, analyze | Research |
| blog, post, announce, social | Marketing |
| design, architecture, RFC | Architecture |
| 3D, model, mesh, render, blender | 3D Design |
| *(anything else)* | Engineering |

You can always override by dragging the issue to any column manually.

After each agent finishes, the dispatcher merges the work and advances to the next stage:

```
Engineering ──> QA ──> Docs ──> Done     (default pipeline)
Research ──> Ready                        (human decides next)
Architecture ──> Ready                    (human decides next)
Marketing ──> Done
3D Design ──> QA ──> Docs ──> Done
```

## The Agents

Eight specialists, each with a persona and clear boundaries:

| Agent | What it does | Vibe |
|-------|-------------|------|
| **founding-engineer** | Writes code, ships features, fixes bugs | "Move fast, commit often" |
| **qa** | Reviews code, checks security, enforces conventions | "No ship without my sign-off" |
| **documentarian** | Maintains project history and docs | "If it's not documented, it didn't happen" |
| **research-analyst** | Investigates questions, produces structured analysis | "Here are the facts and my recommendation" |
| **architect** | Designs systems, writes RFCs | "Let's think about this before we build it" |
| **marketing-expert** | Creates blog posts, social content, announcements | "Ship the narrative" |
| **3d-designer** | Creates and prepares 3D-printable models | "Will it print?" |
| **ceo** | Strategic triage and decision-making (manual only) | "What do we stop?" |

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

## Configuration

### `config/dispatcher.json`

| Key | What it does | Default |
|-----|-------------|---------|
| `max_concurrent` | Max agents running at once | `3` |
| `agent_timeout_minutes` | Kill agents after this long | `30` |
| `projects_root` | Parent directory of your project repos | `~/projects` |
| `agents_root` | Path to this repo's `agents/` directory | (set during setup) |
| `github_repo` | Which repo to poll for issues | (set during setup) |
| `github_project_number` | Which Projects board to use | `1` |
| `claude_permission_mode` | Permission mode for Claude sessions | `acceptEdits` |

### `config/column-routes.json`

Maps board columns to agent types and defines the pipeline. Edit this to customize your workflow — add columns, change the pipeline order, or wire up new agents.

## Managing the Dispatcher

```bash
# Trigger a cycle now
launchctl start com.halfbakery.dispatcher

# Pause the service
launchctl stop com.halfbakery.dispatcher

# Watch what's happening
tail -f ~/.half-bakery/logs/dispatcher.log

# See running agents
cat ~/.half-bakery/state.json

# Uninstall
launchctl unload ~/Library/LaunchAgents/com.halfbakery.dispatcher.plist
```

## What Happens When Things Go Wrong

| Situation | What the dispatcher does |
|-----------|------------------------|
| Agent times out (>30 min) | Kills the process, moves issue to Review, posts a comment |
| Agent crashes | Detects dead PID, moves to Review, preserves output for debugging |
| Agent is blocked | Detects `BLOCKED:` in output, moves to Review, posts the blocker |
| Merge conflict | Moves to Review, preserves the agent's branch for manual resolution |
| Dispatcher itself crashes | Lock file prevents zombie runs. Agents keep running independently. Next cycle cleans up. |

Everything that needs human attention ends up in the **Review** column with a comment explaining what happened.

## Cost Model

This runs entirely on a **Claude Max subscription** ($100-200/month). No API keys, no per-token billing.

Things to know:
- 3 concurrent agents will eat through your rate-limit window faster than single sessions
- If you're hitting rate limits, drop `max_concurrent` to 2 or 1
- The 30-minute timeout is your budget safety net — adjust as needed
- Research and Architecture agents tend to be cheaper (shorter sessions) than Engineering

## File Layout

```
half-bakery-framework/
  agents/                    Agent personas (the "who")
    founding-engineer/
      AGENTS.md              Instructions and boundaries
      HEARTBEAT.md           Execution checklist
    qa/ ...
    ceo/ ...
  config/
    column-routes.json       Pipeline routing rules
    dispatcher.json          Runtime configuration
  scripts/
    dispatcher.py            The dispatcher (~700 lines of Python)
  launchd/
    com.halfbakery.dispatcher.plist

~/.half-bakery/              Runtime state (created automatically)
  state.json                 Running agents + PIDs
  worktrees/                 One git worktree per active agent
  output/                    Agent stdout logs
  logs/                      Dispatcher logs
  cache/                     Cached GitHub Projects field IDs
```

## Design Decisions

**Why not use an existing orchestrator?** We evaluated 12+ tools (March 2026). Every multi-agent framework we found either burns tokens on coordination (the thing we're trying to avoid) or requires infrastructure beyond a laptop. The closest match was Claude Code Agent Farm, but it generates work internally rather than reading from an external ticket source.

**Why git worktrees?** Universal consensus across the multi-agent ecosystem. Every serious orchestrator isolates agents this way. It prevents concurrent agents from stepping on each other's files while keeping them in the same git repo.

**Why launchd?** It's already on your Mac. No Docker, no Kubernetes, no cloud functions. The dispatcher is stateless and crash-safe — if it dies, the next 5-minute cycle picks up where it left off.

**Why GitHub Issues?** You probably already use them. The issue body IS the task specification. Comments become the communication log. The Projects board IS the kanban. No new tools to learn.

## Contributing

PRs welcome. The dispatcher is a single Python file with no dependencies beyond the standard library + `gh` CLI. Agent personas are just markdown.

## License

MIT
