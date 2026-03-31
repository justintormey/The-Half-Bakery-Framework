#!/usr/bin/env python3
"""Half Bakery v2 Dispatcher — polls GitHub Projects and spawns Claude agents.

Runs every 5 minutes via launchd. Deterministic dispatch, stateless agents.
All coordination stays in this script; agents get only their persona + assignment.
"""

import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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

def gh_graphql(query, jq_filter=None):
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    if jq_filter:
        cmd.extend(["--jq", jq_filter])
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error("GraphQL error: %s", result.stderr)
        return None
    if jq_filter:
        return result.stdout.strip()
    return json.loads(result.stdout)


def gh_issue_comment(repo, issue_number, body):
    subprocess.run(
        ["gh", "issue", "comment", str(issue_number),
         "--repo", repo, "--body", body],
        capture_output=True, text=True,
    )


def gh_issue_close(repo, issue_number):
    subprocess.run(
        ["gh", "issue", "close", str(issue_number), "--repo", repo],
        capture_output=True, text=True,
    )


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

    project_num = config["github_project_number"]
    owner = config["github_repo"].split("/")[0]
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
    project_num = config["github_project_number"]
    owner = config["github_repo"].split("/")[0]
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
        target_project = None
        for fv in node.get("fieldValues", {}).get("nodes", []):
            field = fv.get("field", {})
            field_name = field.get("name")
            if field_name == "Status" and "name" in fv:
                status = fv["name"]
            elif field_name == "Target Project" and "text" in fv:
                target_project = fv["text"]

        if status and status in dispatchable_columns:
            items.append({
                "item_id": node["id"],
                "issue_number": content["number"],
                "title": content["title"],
                "body": content.get("body", ""),
                "status": status,
                "target_project": target_project,
            })

    return items


def move_issue_to_column(fields_cache, item_id, column_name):
    """Move a project item to a different Status column."""
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
    """Determine which column a Ready item should go to based on keywords."""
    text = (item["title"] + " " + item["body"]).lower()
    for column, keywords in routes.get("auto_route_keywords", {}).items():
        for kw in keywords:
            if kw.lower() in text:
                return column
    return routes.get("default_route", "Engineering")


# ---------------------------------------------------------------------------
# Git worktree management
# ---------------------------------------------------------------------------

def create_worktree(project_dir, issue_number):
    """Create a git worktree for this issue in the target project."""
    worktree_path = WORKTREE_DIR / str(issue_number)
    branch_name = f"agent/{issue_number}"

    if worktree_path.exists():
        log.warning("Worktree already exists at %s, cleaning up", worktree_path)
        cleanup_worktree(project_dir, issue_number)

    result = subprocess.run(
        ["git", "-C", project_dir, "worktree", "add",
         str(worktree_path), "-b", branch_name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Branch might already exist, try without -b
        result = subprocess.run(
            ["git", "-C", project_dir, "worktree", "add",
             str(worktree_path), branch_name],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.error("Failed to create worktree: %s", result.stderr)
            return None

    return str(worktree_path), branch_name


def merge_worktree(project_dir, branch_name):
    """Merge the agent branch back into the project's default branch."""
    # Get the default branch name
    result = subprocess.run(
        ["git", "-C", project_dir, "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True,
    )
    default_branch = result.stdout.strip() if result.returncode == 0 else "main"

    result = subprocess.run(
        ["git", "-C", project_dir, "merge", branch_name, "--no-edit"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("Merge conflict for %s: %s", branch_name, result.stderr)
        return False
    return True


def cleanup_worktree(project_dir, issue_number):
    """Remove the worktree and delete the branch."""
    worktree_path = WORKTREE_DIR / str(issue_number)
    branch_name = f"agent/{issue_number}"

    if worktree_path.exists():
        subprocess.run(
            ["git", "-C", project_dir, "worktree", "remove",
             str(worktree_path), "--force"],
            capture_output=True, text=True,
        )

    subprocess.run(
        ["git", "-C", project_dir, "branch", "-D", branch_name],
        capture_output=True, text=True,
    )


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

    assignment = f"""## Your Assignment

**Issue:** #{item['issue_number']} — {item['title']}
**Project:** {item.get('target_project', 'unknown')}
**Working directory:** {worktree_path}/
**Pipeline stage:** {item['status']}

## Issue Details
{item['body']}

## Instructions
1. Work ONLY in your assigned directory: {worktree_path}/
2. Read the project's CLAUDE.md or history.md if it exists before starting.
3. Do the work described in the issue.
4. Commit your changes with clear messages referencing issue #{item['issue_number']}.
5. If you are blocked and cannot complete the work, output a line starting
   with "##BLOCKED##" followed by what's blocking you.
6. Output a structured summary when done:
   - What you did
   - Files changed (with paths)
   - Any follow-up issues that should be created
"""

    output_file = OUTPUT_DIR / f"{item['issue_number']}.log"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    cmd = [
        "claude", "--print",
        "--system-prompt", system_prompt,
        "--append-system-prompt", assignment,
        "--add-dir", worktree_path,
        "--permission-mode", config.get("claude_permission_mode", "acceptEdits"),
        "--name", f"half-bakery-{item['issue_number']}-{agent_type}",
        "Execute the assignment above.",
    ]

    with open(output_file, "w") as out:
        proc = subprocess.Popen(
            cmd, stdout=out, stderr=subprocess.STDOUT,
            cwd=worktree_path,
        )

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

            repo = config["github_repo"]
            gh_issue_comment(repo, int(issue_num),
                f"**Agent timed out** after {elapsed:.0f} minutes "
                f"(limit: {timeout_minutes}m). Moving to Review.\n\n"
                f"Agent type: `{info['agent']}`\n"
                f"Worktree branch `{info.get('branch', 'unknown')}` preserved for inspection.")
            move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")

            # Preserve worktree for inspection on timeout
            del state["running"][issue_num]


def phase_poll_and_dispatch(state, config, fields_cache, routes):
    """Poll the board and dispatch agents for ready issues."""
    max_concurrent = config.get("max_concurrent", 3)
    running_count = len(state["running"])

    if running_count >= max_concurrent:
        log.info("At max concurrency (%d/%d), skipping poll",
                 running_count, max_concurrent)
        return

    items = poll_board(config, fields_cache, routes)
    running_issues = set(state["running"].keys())

    for item in items:
        if str(item["issue_number"]) in running_issues:
            continue
        if running_count >= max_concurrent:
            break

        # Auto-route from Ready
        if item["status"] == "Ready":
            target_column = auto_route(item, routes)
            log.info("Auto-routing issue #%d to %s", item["issue_number"], target_column)
            move_issue_to_column(fields_cache, item["item_id"], target_column)
            item["status"] = target_column

        # Validate target project
        target_project = item.get("target_project")
        if not target_project:
            log.warning("Issue #%d has no Target Project, skipping",
                        item["issue_number"])
            continue

        project_dir = str(Path(config["projects_root"]) / target_project)
        if not Path(project_dir).exists():
            log.warning("Project directory %s does not exist, skipping issue #%d",
                        project_dir, item["issue_number"])
            continue

        # Check if it's a git repo
        if not (Path(project_dir) / ".git").exists():
            log.warning("Project %s is not a git repo, skipping issue #%d",
                        target_project, item["issue_number"])
            continue

        # Create worktree
        result = create_worktree(project_dir, item["issue_number"])
        if not result:
            log.error("Failed to create worktree for issue #%d", item["issue_number"])
            continue
        worktree_path, branch_name = result

        # Spawn agent
        pid = spawn_agent(config, routes, item, worktree_path)
        if pid is None:
            cleanup_worktree(project_dir, item["issue_number"])
            continue

        state["running"][str(item["issue_number"])] = {
            "agent": routes["columns"][item["status"]]["agent"],
            "pid": pid,
            "started": datetime.now(timezone.utc).isoformat(),
            "project": target_project,
            "column": item["status"],
            "worktree": worktree_path,
            "branch": branch_name,
            "item_id": item["item_id"],
        }
        running_count += 1
        log.info("Dispatched issue #%d to %s (PID %d)",
                 item["issue_number"], item["status"], pid)


def phase_harvest(state, config, fields_cache, routes):
    """Check for finished agents (completed or crashed) and handle results."""
    repo = config["github_repo"]

    for issue_num, info in list(state["running"].items()):
        if is_pid_alive(info["pid"]):
            continue

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
            cleanup_worktree(project_dir, int(issue_num))
            output_file.unlink(missing_ok=True)
            del state["running"][issue_num]
            continue

        log.info("Issue #%s: agent completed (PID %d)", issue_num, info["pid"])

        # Check for BLOCKED marker — look for "##BLOCKED##" prefix
        # which is distinct from the instructions text
        blocker = None
        for line in output_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("##BLOCKED##"):
                blocker = stripped.replace("##BLOCKED##", "BLOCKED:", 1)
                break
        is_blocked = blocker is not None

        if is_blocked:

            log.info("Issue #%s is blocked: %s", issue_num, blocker)
            gh_issue_comment(repo, int(issue_num),
                f"**Agent blocked** — moving to Review.\n\n"
                f"`{blocker}`\n\n"
                f"Agent type: `{info['agent']}`")
            move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")
            cleanup_worktree(project_dir, int(issue_num))
        else:
            # Attempt merge
            merge_ok = merge_worktree(project_dir, info.get("branch", ""))

            if not merge_ok:
                log.warning("Merge conflict for issue #%s, moving to Review", issue_num)
                gh_issue_comment(repo, int(issue_num),
                    f"**Merge conflict** — moving to Review.\n\n"
                    f"Branch `{info.get('branch', '')}` preserved for manual merging.\n\n"
                    f"Agent type: `{info['agent']}`\n\n"
                    f"<details><summary>Agent summary</summary>\n\n"
                    f"{output_text[-3000:]}\n</details>")
                move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")
                # Don't clean up worktree on merge conflict — preserve for manual merge
            else:
                # Success — post summary, advance to next column
                next_column = routes["columns"].get(info["column"], {}).get("next", "Done")

                # Truncate output for comment (GitHub has a 65536 char limit)
                summary = output_text[-3000:] if len(output_text) > 3000 else output_text
                gh_issue_comment(repo, int(issue_num),
                    f"**Agent completed** — `{info['agent']}` finished.\n\n"
                    f"<details><summary>Agent output</summary>\n\n"
                    f"{summary}\n</details>\n\n"
                    f"Moving to **{next_column}**.")

                if next_column == "Done":
                    move_issue_to_column(fields_cache, info.get("item_id", ""), "Done")
                    gh_issue_close(repo, int(issue_num))
                else:
                    move_issue_to_column(fields_cache, info.get("item_id", ""), next_column)

                cleanup_worktree(project_dir, int(issue_num))

        output_file.unlink(missing_ok=True)
        del state["running"][issue_num]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    setup_logging()
    log.info("Dispatcher starting")

    # Ensure directories exist
    for d in (STATE_DIR, CACHE_DIR, OUTPUT_DIR, WORKTREE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)

    lock_fd = acquire_lock()
    try:
        config = load_config()
        routes = load_routes()
        state = load_state()

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
    finally:
        release_lock(lock_fd)


if __name__ == "__main__":
    main()
