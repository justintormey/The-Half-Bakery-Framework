#!/usr/bin/env python3
"""Half Bakery v2 Dispatcher — polls GitHub Projects and spawns Claude agents.

Runs every 5 minutes via launchd. Deterministic dispatch, stateless agents.
All coordination stays in this script; agents get only their persona + assignment.
"""

import argparse
import fcntl
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DRY_RUN = False

# ---------------------------------------------------------------------------
# Paths & configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_DIR = SCRIPT_DIR.parent
CONFIG_DIR = REPO_DIR / "config"

STATE_DIR = Path.home() / ".half-bakery"
STATE_FILE = STATE_DIR / "state.json"
LOCK_FILE = STATE_DIR / "dispatcher.lock"
CACHE_DIR = STATE_DIR / "cache"
FIELD_CACHE = CACHE_DIR / "project-fields.json"
OUTPUT_DIR = STATE_DIR / "output"
WORKTREE_DIR = STATE_DIR / "worktrees"
LOG_DIR = STATE_DIR / "logs"

# Resolve claude binary — launchd PATH is minimal, so check common locations
CLAUDE_BIN = (
    shutil.which("claude")
    or str(Path.home() / ".local" / "bin" / "claude")
)


def load_config():
    with open(CONFIG_DIR / "dispatcher.json") as f:
        cfg = json.load(f)
    # Expand ~ in paths
    for key in ("projects_root", "agents_root", "state_dir"):
        if key in cfg:
            cfg[key] = str(Path(cfg[key]).expanduser())
    return cfg


def load_routes():
    with open(CONFIG_DIR / "column-routes.json") as f:
        return json.load(f)


def resolve_project_dir(projects_root, repo_name):
    """Find the local directory for a GitHub repo name.

    Tries: exact match, case-insensitive match, nested subdirectories
    (e.g., parent-dir/my-repo for repo my-repo-public).
    Returns the path string or None.
    """
    root = Path(projects_root)

    # Exact match
    candidate = root / repo_name
    if candidate.is_dir() and (candidate / ".git").exists():
        return str(candidate)

    # Case-insensitive match (handles "Sankey Diagramer" vs "sankey-diagramer")
    repo_lower = repo_name.lower().replace("-", " ").replace("_", " ")
    for child in root.iterdir():
        if not child.is_dir():
            continue
        child_lower = child.name.lower().replace("-", " ").replace("_", " ")
        if child_lower == repo_lower and (child / ".git").exists():
            return str(child)

    # Check with/without common suffixes (-repo)
    stripped = repo_name.removesuffix("-repo")
    if stripped != repo_name:
        result = resolve_project_dir(projects_root, stripped)
        if result:
            return result

    # Nested: check subdirectories of each top-level dir
    # (e.g., parent-dir/my-repo/)
    for child in root.iterdir():
        if not child.is_dir() or (child / ".git").exists():
            continue  # skip git repos, only check non-repo parent dirs
        for sub in child.iterdir():
            if not sub.is_dir():
                continue
            sub_lower = sub.name.lower().replace("-", " ").replace("_", " ")
            if (sub_lower == repo_lower or sub.name == repo_name or sub.name == stripped):
                if (sub / ".git").exists():
                    return str(sub)

    return None


def get_sibling_projects(projects_root, exclude=None):
    """Return paths of git repos under projects_root, excluding named dirs."""
    exclude = exclude or []
    root = Path(projects_root)
    siblings = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in exclude:
            continue
        if (child / ".git").exists():
            siblings.append(str(child))
    return siblings


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "dispatcher.log"),
        ],
    )


log = logging.getLogger("dispatcher")

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"running": {}, "last_poll": None}


def save_state(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.rename(STATE_FILE)


# ---------------------------------------------------------------------------
# File locking
# ---------------------------------------------------------------------------

def acquire_lock():
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        log.info("Another dispatcher is running, exiting.")
        sys.exit(0)
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd


def release_lock(lock_fd):
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    lock_fd.close()
    LOCK_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Process utilities
# ---------------------------------------------------------------------------

def is_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def kill_process(pid):
    """SIGTERM, wait 10s, SIGKILL if still alive."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(10):
        time.sleep(1)
        if not is_pid_alive(pid):
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

def gh_graphql(query, jq_filter=None, retries=3):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    if jq_filter:
        cmd.extend(["--jq", jq_filter])

    for attempt in range(1, retries + 1):
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if jq_filter:
                return result.stdout.strip()
            return json.loads(result.stdout)

        log.warning("GraphQL attempt %d/%d failed: %s",
                    attempt, retries, result.stderr.strip())
        if attempt < retries:
            time.sleep(2)

    log.error("GraphQL failed after %d attempts", retries)
    return None


def gh_issue_comment(repo, issue_number, body):
    if DRY_RUN:
        log.info("[DRY RUN] Would comment on %s#%d", repo, issue_number)
        return
    subprocess.run(
        ["gh", "issue", "comment", str(issue_number),
         "--repo", repo, "--body", body],
        capture_output=True, text=True,
    )


def gh_issue_close(repo, issue_number):
    if DRY_RUN:
        log.info("[DRY RUN] Would close %s#%d", repo, issue_number)
        return
    subprocess.run(
        ["gh", "issue", "close", str(issue_number), "--repo", repo],
        capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# Epic / sub-issue helpers
# ---------------------------------------------------------------------------

def fetch_parent_sub_issues(repo, parent_number):
    """Fetch sub-issues of a parent issue not on the board.

    Used when a sub-issue's parent isn't in the board query results, so we
    need a targeted query to build the sibling list.
    """
    owner, name = repo.split("/", 1)
    query = f'''query {{
        repository(owner: "{owner}", name: "{name}") {{
            issue(number: {parent_number}) {{
                subIssues(first: 20) {{
                    nodes {{ number title state }}
                    totalCount
                }}
            }}
        }}
    }}'''
    data = gh_graphql(query)
    if not data:
        return []
    try:
        return data["data"]["repository"]["issue"]["subIssues"]["nodes"]
    except (KeyError, TypeError):
        return []


def fetch_epic_summary(repo, issue_number):
    """Query the sub-issue completion summary for an Epic.

    Returns a dict with keys completed/total/percent_completed, or None on error.
    """
    owner, name = repo.split("/", 1)
    query = f'''query {{
        repository(owner: "{owner}", name: "{name}") {{
            issue(number: {issue_number}) {{
                state
                subIssuesSummary {{ completed total percentCompleted }}
            }}
        }}
    }}'''
    data = gh_graphql(query)
    if not data:
        return None
    try:
        summary = data["data"]["repository"]["issue"]["subIssuesSummary"]
        return {
            "completed": summary["completed"],
            "total": summary["total"],
            "percent_completed": summary["percentCompleted"],
        }
    except (KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# GitHub Projects field discovery & caching
# ---------------------------------------------------------------------------

def get_project_fields(config):
    """Discover and cache GitHub Projects field IDs and option IDs."""
    if FIELD_CACHE.exists():
        with open(FIELD_CACHE) as f:
            cached = json.load(f)
        # Cache is valid for 24 hours
        if cached.get("fetched_at"):
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if (datetime.now(timezone.utc) - fetched).total_seconds() < 86400:
                return cached

    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]
    query = f'''query {{
        user(login: "{owner}") {{
            projectV2(number: {project_num}) {{
                id
                fields(first: 30) {{
                    nodes {{
                        ... on ProjectV2SingleSelectField {{
                            id
                            name
                            options {{ id name }}
                        }}
                        ... on ProjectV2Field {{
                            id
                            name
                        }}
                    }}
                }}
            }}
        }}
    }}'''
    data = gh_graphql(query)
    if not data:
        log.error("Failed to fetch project fields")
        return None

    project = data["data"]["user"]["projectV2"]
    fields = {}
    for node in project["fields"]["nodes"]:
        name = node.get("name")
        if not name:
            continue
        fields[name] = {"id": node["id"]}
        if "options" in node:
            fields[name]["options"] = {
                opt["name"]: opt["id"] for opt in node["options"]
            }

    result = {
        "project_id": project["id"],
        "fields": fields,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(FIELD_CACHE, "w") as f:
        json.dump(result, f, indent=2)
    return result


# ---------------------------------------------------------------------------
# Board operations
# ---------------------------------------------------------------------------

def poll_board(config, fields_cache, routes):
    """Query the board for dispatchable issues."""
    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]
    query = f'''query {{
        user(login: "{owner}") {{
            projectV2(number: {project_num}) {{
                items(first: 100) {{
                    nodes {{
                        id
                        content {{
                            ... on Issue {{
                                number
                                title
                                state
                                body
                                repository {{ name nameWithOwner }}
                                subIssues(first: 20) {{
                                    nodes {{ number title state }}
                                    totalCount
                                }}
                                subIssuesSummary {{ completed total percentCompleted }}
                                parent {{ number title state body repository {{ nameWithOwner }} }}
                            }}
                        }}
                        fieldValues(first: 15) {{
                            nodes {{
                                ... on ProjectV2ItemFieldSingleSelectValue {{
                                    name
                                    field {{ ... on ProjectV2SingleSelectField {{ name }} }}
                                }}
                                ... on ProjectV2ItemFieldTextValue {{
                                    text
                                    field {{ ... on ProjectV2Field {{ name }} }}
                                }}
                            }}
                        }}
                    }}
                }}
            }}
        }}
    }}'''
    data = gh_graphql(query)
    if not data:
        return []

    dispatchable_columns = set(routes["columns"].keys()) | {"Ready"}
    items = []
    for node in data["data"]["user"]["projectV2"]["items"]["nodes"]:
        content = node.get("content")
        if not content or content.get("state") != "OPEN":
            continue

        status = None
        for fv in node.get("fieldValues", {}).get("nodes", []):
            field = fv.get("field", {})
            field_name = field.get("name")
            if field_name == "Status" and "name" in fv:
                status = fv["name"]

        # Resolve target project and source repo from the issue's repository
        repo_info = content.get("repository", {})
        repo_name = repo_info.get("name")
        target_project = repo_name or "half-bakery"
        issue_repo = repo_info.get("nameWithOwner") or config["github_repo"]

        if status and status in dispatchable_columns:
            sub_issues_data = content.get("subIssues") or {}
            summary_data = content.get("subIssuesSummary") or {}
            parent_data = content.get("parent")
            # Normalize parent: include its repo if available
            parent = None
            if parent_data:
                parent = {
                    "number": parent_data["number"],
                    "title": parent_data["title"],
                    "state": parent_data["state"],
                    "body": parent_data.get("body", ""),
                    "repo": (parent_data.get("repository") or {}).get(
                        "nameWithOwner", issue_repo
                    ),
                }
            items.append({
                "item_id": node["id"],
                "issue_number": content["number"],
                "title": content["title"],
                "body": content.get("body", ""),
                "status": status,
                "target_project": target_project,
                "issue_repo": issue_repo,
                "sub_issues": sub_issues_data.get("nodes", []),
                "sub_issues_total": sub_issues_data.get("totalCount", 0),
                "sub_issues_completed": summary_data.get("completed", 0),
                "parent": parent,
            })

    return items


def move_issue_to_column(fields_cache, item_id, column_name):
    """Move a project item to a different Status column."""
    if DRY_RUN:
        log.info("[DRY RUN] Would move item %s to %s", item_id, column_name)
        return True
    status_field = fields_cache["fields"].get("Status", {})
    field_id = status_field.get("id")
    option_id = status_field.get("options", {}).get(column_name)
    project_id = fields_cache["project_id"]

    if not field_id or not option_id:
        log.error("Cannot move to column %s: field/option not found", column_name)
        return False

    query = f'''mutation {{
        updateProjectV2ItemFieldValue(input: {{
            projectId: "{project_id}"
            itemId: "{item_id}"
            fieldId: "{field_id}"
            value: {{ singleSelectOptionId: "{option_id}" }}
        }}) {{
            projectV2Item {{ id }}
        }}
    }}'''
    result = gh_graphql(query)
    return result is not None


# ---------------------------------------------------------------------------
# Auto-routing from Ready column
# ---------------------------------------------------------------------------

def auto_route(item, routes):
    """Use Claude to classify which pipeline column an issue belongs to."""
    columns = list(routes.get("columns", {}).keys())
    descriptions = {
        "Engineering": "Build features, fix bugs, write code, implement changes",
        "Research": "Investigate questions, evaluate options, produce analysis",
        "Architecture": "Design systems, write RFCs, plan technical approaches",
    }
    column_list = "\n".join(
        f"- {col}: {descriptions.get(col, routes['columns'][col].get('agent', ''))}"
        for col in columns
    )

    prompt = (
        f"You are a project issue router. Given the issue below, reply with ONLY "
        f"the column name (one of: {', '.join(columns)}). Nothing else.\n\n"
        f"Columns:\n{column_list}\n\n"
        f"Issue title: {item['title']}\n"
        f"Issue body: {item['body'][:500]}\n\n"
        f"Column:"
    )

    try:
        result = subprocess.run(
            [CLAUDE_BIN, "--print", "-p", prompt],
            capture_output=True, text=True, timeout=30,
            stdin=subprocess.DEVNULL,
        )
        answer = result.stdout.strip()
        # Extract just the column name (LLM might add extra text)
        for col in columns:
            if col.lower() in answer.lower():
                log.info("Auto-route classified issue #%d as %s",
                         item["issue_number"], col)
                return col
    except (subprocess.TimeoutExpired, OSError) as e:
        log.warning("Auto-route LLM call failed: %s, falling back to Engineering", e)

    return routes.get("default_route", "Engineering")


# ---------------------------------------------------------------------------
# Git worktree management
# ---------------------------------------------------------------------------

def create_worktree(project_dir, issue_number, repo_name=None):
    """Create a git worktree for this issue in the target project."""
    # Use repo_name-issue_number to avoid collisions across repos
    worktree_id = f"{repo_name}-{issue_number}" if repo_name else str(issue_number)
    worktree_path = WORKTREE_DIR / worktree_id
    branch_name = f"agent/{issue_number}"

    if worktree_path.exists():
        log.warning("Worktree already exists at %s, cleaning up", worktree_path)
        if not cleanup_worktree(project_dir, issue_number, repo_name):
            log.error("Cannot clean stale worktree at %s, skipping issue",
                      worktree_path)
            return None
        if worktree_path.exists():
            log.error("Worktree directory still exists after cleanup: %s",
                      worktree_path)
            return None

    # Clean up orphaned branch if it exists without a worktree
    branch_check = subprocess.run(
        ["git", "-C", project_dir, "rev-parse", "--verify", branch_name],
        capture_output=True, text=True,
    )
    if branch_check.returncode == 0:
        log.warning("Orphaned branch %s exists, deleting", branch_name)
        subprocess.run(
            ["git", "-C", project_dir, "branch", "-D", branch_name],
            capture_output=True, text=True,
        )

    result = subprocess.run(
        ["git", "-C", project_dir, "worktree", "add",
         str(worktree_path), "-b", branch_name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("Failed to create worktree: %s", result.stderr.strip())
        return None

    return str(worktree_path), branch_name


def merge_worktree(project_dir, branch_name):
    """Merge the agent branch back into the project's default branch.

    Returns (success: bool, error_reason: str or None).
    Pre-checks: aborts stale merges, verifies branch, stashes dirty tree.
    Post-failure: aborts failed merge to leave repo clean.
    """
    # Pre-check 1: Abort any in-progress merge
    merge_head = Path(project_dir) / ".git" / "MERGE_HEAD"
    if merge_head.exists():
        log.warning("Aborting stale merge in %s", project_dir)
        subprocess.run(
            ["git", "-C", project_dir, "merge", "--abort"],
            capture_output=True, text=True,
        )

    # Pre-check 2: Verify the branch exists
    result = subprocess.run(
        ["git", "-C", project_dir, "rev-parse", "--verify", branch_name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return False, f"Branch {branch_name} does not exist"

    # Pre-check 3: Stash dirty working tree if needed
    status_result = subprocess.run(
        ["git", "-C", project_dir, "status", "--porcelain"],
        capture_output=True, text=True,
    )
    had_dirty_tree = bool(status_result.stdout.strip())
    if had_dirty_tree:
        log.warning("Working tree dirty in %s, stashing before merge", project_dir)
        subprocess.run(
            ["git", "-C", project_dir, "stash", "push", "-m",
             f"dispatcher-auto-stash-before-merge-{branch_name}"],
            capture_output=True, text=True,
        )

    # Attempt the merge
    result = subprocess.run(
        ["git", "-C", project_dir, "merge", branch_name, "--no-edit"],
        capture_output=True, text=True,
    )

    if result.returncode != 0:
        error_msg = result.stderr.strip()
        log.error("Merge failed for %s: %s", branch_name, error_msg)
        # Abort the failed merge to restore clean state
        subprocess.run(
            ["git", "-C", project_dir, "merge", "--abort"],
            capture_output=True, text=True,
        )
        if had_dirty_tree:
            subprocess.run(
                ["git", "-C", project_dir, "stash", "pop"],
                capture_output=True, text=True,
            )
        return False, error_msg

    # Restore stashed changes after successful merge
    if had_dirty_tree:
        subprocess.run(
            ["git", "-C", project_dir, "stash", "pop"],
            capture_output=True, text=True,
        )

    return True, None


def cleanup_worktree(project_dir, issue_number, repo_name=None):
    """Remove the worktree and delete the branch. Always succeeds or returns False."""
    worktree_id = f"{repo_name}-{issue_number}" if repo_name else str(issue_number)
    worktree_path = WORKTREE_DIR / worktree_id
    branch_name = f"agent/{issue_number}"

    if worktree_path.exists():
        # Tier 1: Ask git to remove it cleanly
        result = subprocess.run(
            ["git", "-C", project_dir, "worktree", "remove",
             str(worktree_path), "--force"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning("git worktree remove failed for %s: %s",
                        worktree_path, result.stderr.strip())
            # Tier 2: Nuclear — rm the directory, then prune stale refs
            try:
                shutil.rmtree(worktree_path)
                log.info("Removed worktree directory manually: %s", worktree_path)
            except OSError as e:
                log.error("Failed to rm worktree directory %s: %s", worktree_path, e)
                return False

    # Always prune to clean up stale worktree references
    subprocess.run(
        ["git", "-C", project_dir, "worktree", "prune"],
        capture_output=True, text=True,
    )

    # Delete the branch (ignore errors if already deleted)
    subprocess.run(
        ["git", "-C", project_dir, "branch", "-D", branch_name],
        capture_output=True, text=True,
    )
    return True


# ---------------------------------------------------------------------------
# Agent spawning
# ---------------------------------------------------------------------------

def spawn_agent(config, routes, item, worktree_path):
    """Spawn a claude CLI process for this issue."""
    agent_type = routes["columns"][item["status"]]["agent"]
    agents_root = Path(config["agents_root"])
    agents_md = agents_root / agent_type / "AGENTS.md"

    if not agents_md.exists():
        log.error("Agent file not found: %s", agents_md)
        return None

    system_prompt = agents_md.read_text()

    # Detect spanning project
    is_spanning = item.get("target_project") in config.get("spanning_projects", [])
    sibling_projects = []
    if is_spanning:
        sibling_projects = get_sibling_projects(
            config["projects_root"],
            exclude=[item["target_project"]],
        )
        log.info("Issue #%d: spanning mode — adding %d sibling project dirs",
                 item["issue_number"], len(sibling_projects))

    if is_spanning and sibling_projects:
        work_scope = (f"Your primary working directory is {worktree_path}/, "
                      f"but you also have access to the sibling projects listed below.")
    else:
        work_scope = f"Work ONLY in your assigned directory: {worktree_path}/"

    assignment = f"""## Your Assignment

**Issue:** #{item['issue_number']} — {item['title']}
**Project:** {item.get('target_project', 'unknown')}
**Working directory:** {worktree_path}/
**Pipeline stage:** {item['status']}

## Operational Constraints
You are a headless agent. Follow these rules:
- Do NOT run /login, /memory, or any slash commands
- Do NOT use these skills (they spawn processes or require human interaction):
  schedule, loop, claude-session-driver, commit-push-pr, dispatching-parallel-agents,
  subagent-driven-development, using-git-worktrees, finishing-a-development-branch,
  requesting-code-review, receiving-code-review, writing-skills, writing-plans,
  executing-plans, brainstorming, using-superpowers
- You MAY use: systematic-debugging, episodic-memory:remembering-conversations,
  planning-with-files:plan, verification-before-completion, simplify
- Execute the assignment directly. Do not ask clarifying questions — document
  ambiguity and proceed with the most reasonable interpretation.
- These rules override any proactive instructions in CLAUDE.md

## Issue Details
{item['body']}

## Instructions
1. {work_scope}
2. Read the project's CLAUDE.md or history.md if it exists before starting.
3. Do the work described in the issue.
4. Commit your changes with clear messages referencing issue #{item['issue_number']}.
5. If you create any GitHub issues, immediately add each one to the project board:
   gh project item-add {config['github_project_number']} --owner {config['github_repo'].split('/')[0]} --url <issue-url>
6. If you are blocked and cannot complete the work, output a line starting
   with "##BLOCKED##" followed by what's blocking you.
7. When done, output this EXACT block (fill in each field):
   ##SUMMARY##
   DONE: <one sentence of what was accomplished>
   FILES: <comma-separated list of changed files>
   COMMITS: <commit SHAs or "none">
   FOLLOWUP: <issues to create, or "none">
   ##END##
"""

    if is_spanning and sibling_projects:
        project_list = "\n".join(f"- {p}" for p in sibling_projects)
        assignment += f"""
## Available Projects
You have direct access to the following project directories.
You may read, modify, and commit in any of them:
{project_list}
"""

    # Inject project-specific gotchas from history.md "Important Notes" section.
    history_path = Path(worktree_path) / "history.md"
    if history_path.exists():
        history_text = history_path.read_text()
        if "## Important Notes" in history_text:
            notes = history_text.split("## Important Notes")[1].split("\n##")[0].strip()
            if notes:
                assignment += f"\n## Project Notes (from history.md)\n{notes}\n"

    # Epic context enrichment: inject parent Epic info for sub-issues.
    if item.get("parent"):
        parent = item["parent"]
        assignment += f"""
## Epic Context
This issue is a sub-issue (story/task) of a larger Epic.

**Parent Epic:** #{parent['number']} — {parent['title']}
**Epic Description:**
{parent.get('body', '(no description)')}
"""
        siblings = item.get("siblings", [])
        if siblings:
            sibling_list = "\n".join(
                f"- #{s['number']} {s['title']} — **{s['state']}**"
                for s in siblings
            )
            assignment += f"""
**Related sub-issues (siblings):**
{sibling_list}

Focus on YOUR assigned sub-issue. The sibling list is for context only —
do not attempt to do work assigned to other sub-issues.
"""

    output_file = OUTPUT_DIR / f"{item['issue_number']}.log"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if DRY_RUN:
        log.info("[DRY RUN] Would spawn %s for issue #%d in %s",
                 agent_type, item["issue_number"], worktree_path)
        return None

    cmd = [
        CLAUDE_BIN, "--print",
        "--system-prompt", system_prompt,
        "--append-system-prompt", assignment,
        "--add-dir", worktree_path,
    ]

    # Spanning projects get --add-dir for all sibling repos
    for proj_path in sibling_projects:
        cmd.extend(["--add-dir", proj_path])

    cmd.extend([
        "--permission-mode", config.get("claude_permission_mode", "acceptEdits"),
        "--name", f"half-bakery-{item['issue_number']}-{agent_type}",
        "Execute the assignment above.",
    ])

    try:
        with open(output_file, "w") as out:
            proc = subprocess.Popen(
                cmd, stdout=out, stderr=subprocess.STDOUT,
                cwd=worktree_path,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
            )
    except OSError as e:
        log.error("Failed to spawn agent for issue #%d: %s",
                  item["issue_number"], e)
        return None

    log.info("Spawned agent %s for issue #%d (PID %d)",
             agent_type, item["issue_number"], proc.pid)
    return proc.pid


# ---------------------------------------------------------------------------
# Dispatcher phases
# ---------------------------------------------------------------------------

def phase_timeout_check(state, config, fields_cache):
    """Kill agents that have exceeded the timeout."""
    timeout_minutes = config.get("agent_timeout_minutes", 30)
    now = datetime.now(timezone.utc)

    for issue_num, info in list(state["running"].items()):
        started = datetime.fromisoformat(info["started"])
        elapsed = (now - started).total_seconds() / 60

        if elapsed > timeout_minutes:
            log.warning("Issue #%s timed out after %.0f minutes, killing PID %d",
                        issue_num, elapsed, info["pid"])
            kill_process(info["pid"])

            repo = info.get("issue_repo", config["github_repo"])
            gh_issue_comment(repo, int(issue_num),
                f"**Agent timed out** after {elapsed:.0f} minutes "
                f"(limit: {timeout_minutes}m). Moving to Review.\n\n"
                f"Agent type: `{info['agent']}`\n"
                f"Worktree branch `{info.get('branch', 'unknown')}` preserved for inspection.")
            move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")

            # Preserve worktree for inspection on timeout
            del state["running"][issue_num]
            save_state(state)


def phase_poll_and_dispatch(state, config, fields_cache, routes):
    """Poll the board and dispatch agents for ready issues."""
    max_concurrent = config.get("max_concurrent", 3)
    running_count = len(state["running"])

    if running_count >= max_concurrent:
        log.info("At max concurrency (%d/%d), skipping poll",
                 running_count, max_concurrent)
        return

    items = poll_board(config, fields_cache, routes)

    # --- Sibling resolution (Step 6 of epic/sub-issue design) ---
    # Build a map of epic_number -> sub_issues list from Epic items on the board.
    # This is used to enrich sub-issue assignments with their siblings list.
    epic_sub_issues: dict = {}
    for item in items:
        if item.get("sub_issues_total", 0) > 0:
            epic_sub_issues[item["issue_number"]] = item["sub_issues"]

    # For sub-issues whose parent Epic is NOT on the board, fetch via API.
    # Cache the results to avoid duplicate calls for siblings sharing a parent.
    off_board_parent_cache: dict = {}
    for item in items:
        parent = item.get("parent")
        if not parent:
            continue
        pnum = parent["number"]
        if pnum not in epic_sub_issues and pnum not in off_board_parent_cache:
            parent_repo = parent.get("repo", item["issue_repo"])
            off_board_parent_cache[pnum] = fetch_parent_sub_issues(
                parent_repo, pnum
            )

    # Populate siblings on each sub-issue item (everyone with a parent).
    for item in items:
        parent = item.get("parent")
        if not parent:
            continue
        pnum = parent["number"]
        all_siblings = (
            epic_sub_issues.get(pnum) or off_board_parent_cache.get(pnum) or []
        )
        # Exclude self from the sibling list.
        item["siblings"] = [
            s for s in all_siblings if s["number"] != item["issue_number"]
        ]

    running_issues = set(state["running"].keys())

    for item in items:
        if str(item["issue_number"]) in running_issues:
            continue
        if running_count >= max_concurrent:
            break

        # Skip Epics — they are containers; only sub-issues get dispatched.
        if item.get("sub_issues_total", 0) > 0:
            log.info(
                "Issue #%d is an Epic (%d sub-issues, %d completed) — skipping dispatch",
                item["issue_number"],
                item["sub_issues_total"],
                item["sub_issues_completed"],
            )
            continue

        # Auto-route from Ready
        if item["status"] == "Ready":
            target_column = auto_route(item, routes)
            log.info("Auto-routing issue #%d to %s", item["issue_number"], target_column)
            move_issue_to_column(fields_cache, item["item_id"], target_column)
            item["status"] = target_column

        # Validate target project directory
        target_project = item["target_project"]
        project_dir = resolve_project_dir(config["projects_root"], target_project)
        if not project_dir:
            log.warning("Project directory for '%s' not found under %s, skipping issue #%d",
                        target_project, config["projects_root"], item["issue_number"])
            continue

        # Create worktree
        result = create_worktree(project_dir, item["issue_number"], target_project)
        if not result:
            log.error("Failed to create worktree for issue #%d", item["issue_number"])
            continue
        worktree_path, branch_name = result

        # Spawn agent
        pid = spawn_agent(config, routes, item, worktree_path)
        if pid is None:
            cleanup_worktree(project_dir, item["issue_number"], target_project)
            continue

        entry = {
            "agent": routes["columns"][item["status"]]["agent"],
            "pid": pid,
            "started": datetime.now(timezone.utc).isoformat(),
            "project": target_project,
            "column": item["status"],
            "worktree": worktree_path,
            "branch": branch_name,
            "item_id": item["item_id"],
            "issue_repo": item.get("issue_repo", config["github_repo"]),
        }
        # Store parent Epic reference so harvest can post progress updates.
        if item.get("parent"):
            entry["parent_issue"] = item["parent"]
        state["running"][str(item["issue_number"])] = entry
        running_count += 1
        log.info("Dispatched issue #%d to %s (PID %d)",
                 item["issue_number"], item["status"], pid)


def _extract_summary(output_text):
    """Parse the ##SUMMARY##...##END## block from agent output.

    Returns a dict with keys DONE, FILES, COMMITS, FOLLOWUP, or None if not found.
    """
    in_block = False
    fields = {}
    for line in output_text.splitlines():
        stripped = line.strip()
        if stripped == "##SUMMARY##":
            in_block = True
            continue
        if stripped == "##END##":
            break
        if in_block and ":" in stripped:
            key, _, value = stripped.partition(":")
            fields[key.strip()] = value.strip()
    return fields if fields else None


def phase_harvest(state, config, fields_cache, routes):
    """Check for finished agents (completed or crashed) and handle results."""

    for issue_num, info in list(state["running"].items()):
        try:
            if is_pid_alive(info["pid"]):
                continue

            repo = info.get("issue_repo", config["github_repo"])
            output_file = OUTPUT_DIR / f"{issue_num}.log"
            output_text = ""
            if output_file.exists():
                output_text = output_file.read_text()

            project_dir = str(Path(config["projects_root"]) / info.get("project", ""))

            # Distinguish completion from crash: a completed agent produces
            # meaningful output. A crash produces empty or error-only output.
            has_meaningful_output = len(output_text.strip()) > 50

            if not has_meaningful_output:
                # Crash — no useful output
                log.warning("Issue #%s: PID %d exited with no meaningful output (crash)",
                            issue_num, info["pid"])
                last_output = output_text[-2000:] if output_text else "(no output)"
                gh_issue_comment(repo, int(issue_num),
                    f"**Agent crashed** (PID {info['pid']} exited unexpectedly). "
                    f"Moving to Review.\n\n"
                    f"Agent type: `{info['agent']}`\n\n"
                    f"<details><summary>Last output</summary>\n\n```\n{last_output}\n```\n</details>")
                move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")
                cleanup_worktree(project_dir, int(issue_num), info.get("project"))
                output_file.unlink(missing_ok=True)
                del state["running"][issue_num]
                save_state(state)
                continue

            log.info("Issue #%s: agent completed (PID %d)", issue_num, info["pid"])

            # Check for BLOCKED marker
            blocker = None
            for line in output_text.splitlines():
                stripped = line.strip()
                if stripped.startswith("##BLOCKED##"):
                    blocker = stripped.replace("##BLOCKED##", "BLOCKED:", 1)
                    break
                elif stripped.startswith("BLOCKED:"):
                    blocker = stripped
                    break
            is_blocked = blocker is not None

            if is_blocked:
                log.info("Issue #%s is blocked: %s", issue_num, blocker)
                gh_issue_comment(repo, int(issue_num),
                    f"**Agent blocked** — moving to Review.\n\n"
                    f"`{blocker}`\n\n"
                    f"Agent type: `{info['agent']}`")
                move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")
                cleanup_worktree(project_dir, int(issue_num), info.get("project"))
                # Notify parent Epic that a sub-issue is blocked.
                if info.get("parent_issue"):
                    parent_num = info["parent_issue"]["number"]
                    parent_repo = info["parent_issue"].get("repo", repo)
                    summary = fetch_epic_summary(parent_repo, parent_num)
                    progress = (
                        f"{summary['completed']}/{summary['total']}"
                        if summary else "?"
                    )
                    gh_issue_comment(parent_repo, parent_num,
                        f"📋 **Sub-issue #{issue_num} blocked** — Epic progress: {progress}.\n\n"
                        f"`{blocker}`")
                    log.info("Epic #%d: sub-issue #%s blocked, notified",
                             parent_num, issue_num)
            else:
                # Attempt merge
                merge_ok, merge_error = merge_worktree(project_dir, info.get("branch", ""))

                if not merge_ok:
                    log.warning("Merge conflict for issue #%s, moving to Review", issue_num)
                    gh_issue_comment(repo, int(issue_num),
                        f"**Merge conflict** — moving to Review.\n\n"
                        f"Branch `{info.get('branch', '')}` preserved for manual merging.\n\n"
                        f"Agent type: `{info['agent']}`\n"
                        f"Error: `{merge_error}`")
                    move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")
                    # Don't clean up worktree on merge conflict — preserve for manual merge
                else:
                    # Success — post summary, advance to next column
                    next_column = routes["columns"].get(info["column"], {}).get("next", "Done")

                    # Extract ##SUMMARY## block for the visible comment
                    structured = _extract_summary(output_text)
                    if structured:
                        comment_body = (
                            f"**{info['agent']}** completed.\n\n"
                            f"**What was done:** {structured.get('DONE', '(see output)')}\n\n"
                            f"**Files changed:** {structured.get('FILES', 'none listed')}\n\n"
                            f"**Commits:** {structured.get('COMMITS', 'none listed')}\n\n"
                        )
                        followup = structured.get("FOLLOWUP", "none")
                        if followup and followup.lower() != "none":
                            comment_body += f"**Follow-up needed:** {followup}\n\n"
                        comment_body += f"Moving to **{next_column}**."
                    else:
                        # No structured summary — truncate raw output
                        raw = output_text[-2000:] if len(output_text) > 2000 else output_text
                        comment_body = (
                            f"**{info['agent']}** completed.\n\n"
                            f"<details><summary>Agent output</summary>\n\n"
                            f"```\n{raw}\n```\n</details>\n\n"
                            f"Moving to **{next_column}**."
                        )
                    gh_issue_comment(repo, int(issue_num), comment_body)

                    if next_column == "Done":
                        move_issue_to_column(fields_cache, info.get("item_id", ""), "Done")
                        gh_issue_close(repo, int(issue_num))
                    else:
                        move_issue_to_column(fields_cache, info.get("item_id", ""), next_column)

                    cleanup_worktree(project_dir, int(issue_num), info.get("project"))

                    # Epic progress tracking: post update on parent when a
                    # sub-issue completes; auto-close Epic when all are done.
                    if info.get("parent_issue"):
                        parent_num = info["parent_issue"]["number"]
                        parent_repo = info["parent_issue"].get("repo", repo)
                        summary = fetch_epic_summary(parent_repo, parent_num)
                        if summary and summary["total"] > 0:
                            progress = f"{summary['completed']}/{summary['total']}"
                            percent = summary["percent_completed"]
                            if summary["completed"] == summary["total"]:
                                gh_issue_comment(parent_repo, parent_num,
                                    f"🎉 **Epic complete** — all {summary['total']} "
                                    f"sub-issues resolved.\n\n"
                                    f"Closing this Epic automatically.")
                                gh_issue_close(parent_repo, parent_num)
                                log.info("Epic #%d: all sub-issues complete, auto-closed",
                                         parent_num)
                            else:
                                gh_issue_comment(parent_repo, parent_num,
                                    f"📊 **Progress update** — sub-issue #{issue_num} "
                                    f"completed.\n\n"
                                    f"**{progress}** sub-issues done ({percent}% complete).")
                                log.info("Epic #%d: progress %s (%d%%)",
                                         parent_num, progress, percent)

            output_file.unlink(missing_ok=True)
            del state["running"][issue_num]
            save_state(state)
        except Exception:
            log.exception("Error harvesting issue #%s, skipping", issue_num)
            continue


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def cleanup_orphans(state, config):
    """Remove worktree directories and agent branches not tracked in state."""
    # Build sets for matching active work
    active_worktrees = set()
    running_issues = set(state.get("running", {}).keys())
    for info in state.get("running", {}).values():
        wt = info.get("worktree", "")
        if wt:
            active_worktrees.add(Path(wt).name)

    # Clean orphaned worktree directories
    if WORKTREE_DIR.exists():
        for entry in WORKTREE_DIR.iterdir():
            if not entry.is_dir():
                continue
            if entry.name in active_worktrees:
                continue
            log.info("Cleaning orphaned worktree: %s", entry)
            # Determine which project this worktree belongs to
            git_file = entry / ".git"
            project_dir = None
            if git_file.exists():
                try:
                    gitdir_line = git_file.read_text().strip()
                    if gitdir_line.startswith("gitdir:"):
                        gitdir_path = gitdir_line.split(":", 1)[1].strip()
                        repo_git = Path(gitdir_path).parent.parent
                        project_dir = str(repo_git.parent)
                except (OSError, IndexError):
                    pass

            if project_dir and Path(project_dir).exists():
                # Parse worktree dir name: "repo-name-N" or legacy "N"
                parts = entry.name.rsplit("-", 1)
                num = int(parts[-1]) if parts[-1].isdigit() else 0
                repo = parts[0] if len(parts) > 1 and parts[-1].isdigit() else None
                cleanup_worktree(project_dir, num, repo)
            else:
                try:
                    shutil.rmtree(entry)
                    log.info("Removed orphaned directory: %s", entry)
                except OSError as e:
                    log.warning("Failed to remove orphaned directory %s: %s", entry, e)

    # Clean orphaned agent/* branches from all known projects
    projects_root = Path(config["projects_root"])
    if projects_root.exists():
        for child in projects_root.iterdir():
            if not child.is_dir() or not (child / ".git").exists():
                continue
            result = subprocess.run(
                ["git", "-C", str(child), "branch", "--list", "agent/*"],
                capture_output=True, text=True,
            )
            for line in result.stdout.splitlines():
                branch = line.strip().lstrip("* ")
                parts = branch.split("/")
                if len(parts) == 2 and parts[1].isdigit():
                    if parts[1] not in running_issues:
                        log.info("Deleting orphaned branch %s in %s",
                                 branch, child.name)
                        subprocess.run(
                            ["git", "-C", str(child), "branch", "-D", branch],
                            capture_output=True, text=True,
                        )


def validate_environment():
    """Fail fast if critical dependencies are missing or misconfigured."""
    errors = []

    if not Path(CLAUDE_BIN).exists():
        errors.append(f"Claude binary not found at {CLAUDE_BIN}")

    for cfg_file in (CONFIG_DIR / "dispatcher.json", CONFIG_DIR / "column-routes.json"):
        if not cfg_file.exists():
            errors.append(f"Config file missing: {cfg_file}")

    try:
        config = load_config()
        for key in ("projects_root", "agents_root"):
            path = Path(config.get(key, ""))
            if not path.exists():
                errors.append(f"{key} does not exist: {path}")
    except (json.JSONDecodeError, KeyError) as e:
        errors.append(f"Invalid dispatcher.json: {e}")

    if not shutil.which("gh"):
        errors.append("GitHub CLI (gh) not found in PATH")

    if errors:
        for err in errors:
            log.error("Startup validation failed: %s", err)
        sys.exit(1)


def main():
    global DRY_RUN
    parser = argparse.ArgumentParser(description="Half Bakery Dispatcher")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without spawning agents or moving issues")
    args = parser.parse_args()
    DRY_RUN = args.dry_run

    setup_logging()
    log.info("Dispatcher starting%s", " (DRY RUN)" if DRY_RUN else "")

    # Ensure directories exist
    for d in (STATE_DIR, CACHE_DIR, OUTPUT_DIR, WORKTREE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)

    validate_environment()

    lock_fd = acquire_lock()
    state = None
    try:
        config = load_config()
        routes = load_routes()
        state = load_state()

        cleanup_orphans(state, config)

        fields_cache = get_project_fields(config)
        if not fields_cache:
            log.error("Cannot fetch project fields, aborting")
            return

        # Phase 2: Timeout check
        phase_timeout_check(state, config, fields_cache)
        save_state(state)

        # Phase 3: Harvest completed/crashed agents (before polling, to free slots)
        phase_harvest(state, config, fields_cache, routes)
        save_state(state)

        # Phase 4: Poll and dispatch
        phase_poll_and_dispatch(state, config, fields_cache, routes)
        state["last_poll"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

        log.info("Dispatcher cycle complete. Running: %d agents",
                 len(state["running"]))
    except Exception:
        log.exception("Dispatcher error")
        if state is not None:
            try:
                save_state(state)
            except Exception:
                log.exception("Failed to save state during error recovery")
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
