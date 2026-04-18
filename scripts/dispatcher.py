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

# Smart dispatcher v3 modules
from budget import get_budget_profile, should_defer_issue, update_session_stats, get_budget_summary
from discoverer import phase_discover
from evaluator import evaluate, classify_issue, _extract_summary as eval_extract_summary, extract_verdict
from usage_tracker import get_usage_status, save_snapshot, record_session, prune_session_log

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


def canonical_id(issue_repo: str, issue_number: int) -> str:
    """Globally unique issue key: 'owner/repo/number'."""
    return f"{issue_repo}/{issue_number}"


def safe_id(cid: str) -> str:
    """Filename/branch-safe version of canonical_id — slashes become dashes."""
    return cid.replace("/", "-")


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

    # Check with/without common suffixes (-repo, -app, -ios, -mac, -web, -api)
    for suffix in ("-repo", "-app", "-ios", "-mac", "-web", "-api"):
        stripped = repo_name.removesuffix(suffix)
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


def create_followup_issues(followup_text, config, fields_cache, source_cid):
    """Parse FOLLOWUP/ISSUES_CREATED field and create GitHub issues on the board.

    Format (pipe-delimited, one issue per line):
        repo-name | Issue title | Brief description
    or just:
        Issue title | Brief description

    If repo-name is omitted, defaults to the source issue's repo.
    """
    if not followup_text or followup_text.strip().lower() in ("none", "n/a", ""):
        return []

    owner = config["github_repo"].split("/")[0]
    project_number = config["github_project_number"]
    default_repo = config["github_repo"]

    # Derive default repo from source CID (owner/repo/number)
    parts = source_cid.split("/")
    if len(parts) == 3:
        default_repo = f"{parts[0]}/{parts[1]}"

    created = []
    for raw_line in followup_text.splitlines():
        line = raw_line.strip().strip("-").strip()
        if not line or line.lower() == "none":
            continue

        segments = [s.strip() for s in line.split("|")]
        if len(segments) >= 3:
            repo_hint, title, body = segments[0], segments[1], segments[2]
            # repo_hint could be "owner/repo" or just "repo"
            repo = f"{owner}/{repo_hint}" if "/" not in repo_hint else repo_hint
        elif len(segments) == 2:
            repo = default_repo
            title, body = segments[0], segments[1]
        elif len(segments) == 1:
            repo = default_repo
            title = segments[0]
            body = f"Follow-up from {source_cid}"
        else:
            continue

        if not title:
            continue

        body_full = f"{body}\n\n_Auto-created from agent output on {source_cid}_"

        if DRY_RUN:
            log.info("[DRY RUN] Would create issue in %s: %s", repo, title)
            created.append(f"{repo}: {title}")
            continue

        result = subprocess.run(
            ["gh", "issue", "create", "--repo", repo,
             "--title", title, "--body", body_full],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            log.warning("Failed to create follow-up issue '%s' in %s: %s",
                        title, repo, result.stderr.strip())
            continue

        issue_url = result.stdout.strip()
        log.info("Created follow-up issue: %s", issue_url)

        # Add to project board and move to Ready
        add_result = subprocess.run(
            ["gh", "project", "item-add", str(project_number),
             "--owner", owner, "--url", issue_url],
            capture_output=True, text=True,
        )
        if add_result.returncode == 0:
            log.info("Added follow-up issue to board: %s", issue_url)
        else:
            log.warning("Could not add %s to board: %s", issue_url, add_result.stderr.strip())

        created.append(issue_url)

    return created


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
    """Query the board for dispatchable issues. Paginates to get ALL items."""
    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]

    all_nodes = []
    cursor = None
    while True:
        after = f', after: "{cursor}"' if cursor else ""
        query = f'''query {{
            user(login: "{owner}") {{
                projectV2(number: {project_num}) {{
                    items(first: 100{after}) {{
                        pageInfo {{ hasNextPage endCursor }}
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
            break

        items_data = data["data"]["user"]["projectV2"]["items"]
        all_nodes.extend(items_data["nodes"])

        if items_data["pageInfo"]["hasNextPage"]:
            cursor = items_data["pageInfo"]["endCursor"]
        else:
            break

    dispatchable_columns = set(routes["columns"].keys()) | {"Ready"}
    items = []
    for node in all_nodes:
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
                "canonical_id": canonical_id(issue_repo, content["number"]),
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
    branch_name = f"agent/{worktree_id}"

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
        # Prune stale worktree refs first — git refuses to delete a branch that
        # git thinks is checked out in a worktree, even with -D, even if the
        # worktree directory no longer exists.
        subprocess.run(
            ["git", "-C", project_dir, "worktree", "prune"],
            capture_output=True, text=True,
        )
        del_result = subprocess.run(
            ["git", "-C", project_dir, "branch", "-D", branch_name],
            capture_output=True, text=True,
        )
        if del_result.returncode != 0:
            log.error("Failed to delete orphaned branch %s: %s",
                      branch_name, del_result.stderr.strip())
            return None

    result = subprocess.run(
        ["git", "-C", project_dir, "worktree", "add",
         str(worktree_path), "-b", branch_name],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error("Failed to create worktree: %s",
                  (result.stderr or result.stdout).strip())
        return None

    return str(worktree_path), branch_name


def merge_worktree(project_dir, branch_name):
    """Merge the agent branch back into the project's default branch.

    Returns (success: bool, error_reason: str or None).
    Pre-checks: aborts stale merges, verifies branch, auto-commits dirty tree.
    Config-protection: files under config/ are reset to HEAD before any
    auto-commit so the dispatcher can never silently clobber config changes
    made by agents or humans (fix for issue #117).
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

    # Pre-check 3: Auto-commit dirty working tree so merges start clean.
    # IMPORTANT: Config files are protected — if any file under config/ is dirty,
    # we reset it to HEAD before committing.  Auto-commits must never clobber
    # intentional architectural decisions made by agents or humans.
    # See: https://github.com/justintormey/half-bakery/issues/117
    status_result = subprocess.run(
        ["git", "-C", project_dir, "status", "--porcelain"],
        capture_output=True, text=True,
    )
    if status_result.stdout.strip():
        # Identify dirty files under config/ and reset them to HEAD
        dirty_config_files = [
            line[3:]  # strip the two-char status prefix + space
            for line in status_result.stdout.splitlines()
            if line[3:].startswith("config/")
        ]
        if dirty_config_files:
            log.warning(
                "Auto-commit protection: resetting %d config file(s) to HEAD in %s "
                "to prevent clobbering intentional config changes: %s",
                len(dirty_config_files), project_dir, dirty_config_files,
            )
            subprocess.run(
                ["git", "-C", project_dir, "checkout", "HEAD", "--"] + dirty_config_files,
                capture_output=True, text=True,
            )

        # Re-check after resetting protected files — may be clean now
        status_result2 = subprocess.run(
            ["git", "-C", project_dir, "status", "--porcelain"],
            capture_output=True, text=True,
        )
        if status_result2.stdout.strip():
            log.info("Auto-committing dirty working tree in %s before merge", project_dir)
            subprocess.run(
                ["git", "-C", project_dir, "add", "-A"],
                capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "-C", project_dir, "commit", "-m",
                 "chore(dispatcher): auto-commit dirty tree before merge"],
                capture_output=True, text=True,
            )

    # Attempt the merge — retry once on macOS EDEADLK (transient VM lock failure)
    import time as _time
    for attempt in range(2):
        result = subprocess.run(
            ["git", "-C", project_dir, "merge", branch_name, "--no-edit"],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            return True, None

        error_msg = result.stderr.strip()
        log.error("Merge failed for %s (attempt %d): %s", branch_name, attempt + 1, error_msg)

        subprocess.run(
            ["git", "-C", project_dir, "merge", "--abort"],
            capture_output=True, text=True,
        )

        # EDEADLK: macOS post-wake transient VM lock — wait and retry
        if "deadlock" in error_msg.lower() or "ORIG_HEAD" in error_msg:
            if attempt == 0:
                log.warning("EDEADLK on merge for %s — retrying in 15s", branch_name)
                _time.sleep(15)
                continue
        break

    return False, error_msg


def cleanup_worktree(project_dir, issue_number, repo_name=None):
    """Remove the worktree and delete the branch. Always succeeds or returns False."""
    worktree_id = f"{repo_name}-{issue_number}" if repo_name else str(issue_number)
    worktree_path = WORKTREE_DIR / worktree_id
    branch_name = f"agent/{worktree_id}"

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

**Issue:** {item['canonical_id']} — {item['title']}
**GitHub:** https://github.com/{item['issue_repo']}/issues/{item['issue_number']}
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
3. Read half-bakery/docs/project-visions.md for the owner's vision and priorities.
4. Do the work described in the issue.
5. ALL output (code, docs, research, architecture, QA reports, analysis) goes in
   YOUR PROJECT'S directory under a `.agent/` folder — NEVER in the half-bakery repo.
   Example paths: `.agent/qa-report.md`, `.agent/research.md`, `.agent/architecture.md`
   The half-bakery repo is infrastructure only. The ONLY exception is
   half-bakery/docs/project-visions.md which you may READ but never WRITE.
6. Commit your changes with clear messages referencing issue #{item['issue_number']} ({item['issue_repo']}).
7. If you create any GitHub issues, immediately add each one to the project board:
   gh project item-add {config['github_project_number']} --owner {config['github_repo'].split('/')[0]} --url <issue-url>
   Always pass --repo {item['issue_repo']} when using `gh issue` commands for this issue.
8. If you are blocked and cannot complete the work, output a line starting
   with "##BLOCKED##" followed by what's blocking you.
9. When done, output this EXACT block (fill in each field):
   ##SUMMARY##
   DONE: <one sentence of what was accomplished>
   FILES: <comma-separated list of changed files>
   COMMITS: <commit SHAs or "none">
   FOLLOWUP: <pipe-delimited follow-up issues, one per line: "repo-name | Issue title | Brief description", or "none">
   ##END##
   For FOLLOWUP, use the short repo name (e.g. "vibecheck-app", "runbook"). The dispatcher will
   auto-create these issues on GitHub and add them to the board. Only list genuinely new work
   that emerged from your findings — not work already captured in existing issues.
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

    # Retry context: inject failure info so agent doesn't repeat mistakes
    if item.get("retry_context"):
        ctx = item["retry_context"]
        assignment += f"""
## Prior Attempt Failed (Retry {ctx['retry_count']})
**Reason:** {ctx['prior_failure']}
Do NOT repeat the same mistake. Address the issue directly.
Read the evaluation failure reason carefully and correct your approach.
"""

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

    output_file = OUTPUT_DIR / f"{safe_id(item['canonical_id'])}.log"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if DRY_RUN:
        log.info("[DRY RUN] Would spawn %s for issue #%d in %s",
                 agent_type, item["issue_number"], worktree_path)
        return None

    # Select model per agent type (default: opus)
    agent_models = config.get("agent_models", {})
    model = agent_models.get(agent_type, "opus")

    cmd = [
        CLAUDE_BIN, "--print",
        "--output-format", "json",
        "--model", model,
        "--system-prompt", system_prompt,
        "--append-system-prompt", assignment,
        "--add-dir", worktree_path,
    ]

    # Spanning projects get --add-dir for all sibling repos
    for proj_path in sibling_projects:
        cmd.extend(["--add-dir", proj_path])

    cmd.extend([
        "--permission-mode", config.get("claude_permission_mode", "acceptEdits"),
        "--name", f"half-bakery-{safe_id(item['canonical_id'])}-{agent_type}",
        "Execute the assignment above.",
    ])

    log.info("Agent %s using model: %s", agent_type, model)

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


def spawn_local_agent(config, provider, routes, item, worktree_path):
    """Spawn a local LLM agent via local_agent.py for this issue.

    Uses the same assignment prompt as spawn_agent() but routes to a local
    llama-server via the OpenAI-compatible API instead of the claude CLI.
    Returns None (triggering fallback) if the server is unreachable.
    """
    # Health check: is the local server reachable?
    base_url = provider.get("base_url", "")
    if base_url:
        import urllib.request
        try:
            health_url = base_url.rstrip("/").rsplit("/v1", 1)[0] + "/health"
            req = urllib.request.Request(health_url, method="GET")
            urllib.request.urlopen(req, timeout=3)
        except Exception:
            log.info("Local provider at %s is unreachable, skipping", base_url)
            return None

    agent_type = routes["columns"][item["status"]]["agent"]
    agents_root = Path(config["agents_root"])
    agents_md = agents_root / agent_type / "AGENTS.md"

    if not agents_md.exists():
        log.error("Agent file not found: %s", agents_md)
        return None

    # Build the same assignment prompt as spawn_agent
    is_spanning = item.get("target_project") in config.get("spanning_projects", [])
    sibling_projects = []
    if is_spanning:
        sibling_projects = get_sibling_projects(
            config["projects_root"],
            exclude=[item["target_project"]],
        )

    if is_spanning and sibling_projects:
        work_scope = (f"Your primary working directory is {worktree_path}/, "
                      f"but you also have access to the sibling projects listed below.")
    else:
        work_scope = f"Work ONLY in your assigned directory: {worktree_path}/"

    assignment = f"""## Your Assignment

**Issue:** {item['canonical_id']} — {item['title']}
**GitHub:** https://github.com/{item['issue_repo']}/issues/{item['issue_number']}
**Project:** {item.get('target_project', 'unknown')}
**Working directory:** {worktree_path}/
**Pipeline stage:** {item['status']}

## Operational Constraints
You are a headless agent running on a local LLM. Follow these rules:
- Execute the assignment directly. Do not ask clarifying questions.
- Use the provided tools (read_file, write_file, edit_file, bash, grep, glob) to do your work.
- Commit your changes using the bash tool with git commands.
- All file paths should be absolute paths.

## Issue Details
{item['body']}

## Instructions
1. {work_scope}
2. Read the project's CLAUDE.md or history.md if it exists before starting.
3. Read half-bakery/docs/project-visions.md for the owner's vision and priorities.
4. Do the work described in the issue.
5. ALL output (code, docs, research, architecture, QA reports, analysis) goes in
   YOUR PROJECT'S directory under a `.agent/` folder — NEVER in the half-bakery repo.
   Example paths: `.agent/qa-report.md`, `.agent/research.md`, `.agent/architecture.md`
   The half-bakery repo is infrastructure only. The ONLY exception is
   half-bakery/docs/project-visions.md which you may READ but never WRITE.
6. Commit your changes with clear messages referencing issue #{item['issue_number']} ({item['issue_repo']}).
7. If you create any GitHub issues, immediately add each one to the project board:
   gh project item-add {config['github_project_number']} --owner {config['github_repo'].split('/')[0]} --url <issue-url>
   Always pass --repo {item['issue_repo']} when using `gh issue` commands for this issue.
8. If you are blocked and cannot complete the work, output a line starting
   with "##BLOCKED##" followed by what's blocking you.
9. When done, output this EXACT block (fill in each field):
   ##SUMMARY##
   DONE: <one sentence of what was accomplished>
   FILES: <comma-separated list of changed files>
   COMMITS: <commit SHAs or "none">
   FOLLOWUP: <pipe-delimited follow-up issues, one per line: "repo-name | Issue title | Brief description", or "none">
   ##END##
   For FOLLOWUP, use the short repo name (e.g. "vibecheck-app", "runbook"). The dispatcher will
   auto-create these issues on GitHub and add them to the board. Only list genuinely new work
   that emerged from your findings — not work already captured in existing issues.
"""

    if is_spanning and sibling_projects:
        project_list = "\n".join(f"- {p}" for p in sibling_projects)
        assignment += f"""
## Available Projects
You have direct access to the following project directories.
You may read, modify, and commit in any of them:
{project_list}
"""

    # Retry context
    if item.get("retry_context"):
        ctx = item["retry_context"]
        assignment += f"""
## Prior Attempt Failed (Retry {ctx['retry_count']})
**Reason:** {ctx['prior_failure']}
Do NOT repeat the same mistake. Address the issue directly.
"""

    output_file = OUTPUT_DIR / f"{safe_id(item['canonical_id'])}.log"
    stderr_file = OUTPUT_DIR / f"{safe_id(item['canonical_id'])}.stderr.log"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if DRY_RUN:
        log.info("[DRY RUN] Would spawn local agent %s for issue #%d in %s",
                 agent_type, item["issue_number"], worktree_path)
        return None

    local_agent_script = SCRIPT_DIR / "local_agent.py"
    if not local_agent_script.exists():
        log.error("local_agent.py not found at %s", local_agent_script)
        return None

    cmd = [
        sys.executable, str(local_agent_script),
        "--base-url", provider["base_url"],
        "--model", provider.get("model", "default"),
        "--persona", str(agents_md),
        "--assignment", assignment,
        "--workdir", worktree_path,
        "--max-turns", str(provider.get("max_turns", 50)),
        "--ctx-size", str(provider.get("ctx_size", 65536)),
        "--verbose",
    ]

    log.info("Local agent %s for issue #%d → %s (model: %s)",
             agent_type, item["issue_number"],
             provider["base_url"], provider.get("model", "default"))

    try:
        # stdout → output_file (JSON only — parsed by phase_harvest)
        # stderr → stderr_file (verbose progress logs — for debugging)
        # Keeping them separate prevents verbose output from corrupting json.loads()
        with open(output_file, "w") as out, open(stderr_file, "w") as err:
            proc = subprocess.Popen(
                cmd, stdout=out, stderr=err,
                cwd=worktree_path,
                start_new_session=True,
                stdin=subprocess.DEVNULL,
            )
    except OSError as e:
        log.error("Failed to spawn local agent for issue #%d: %s",
                  item["issue_number"], e)
        return None

    log.info("Spawned local agent %s for issue #%d (PID %d)",
             agent_type, item["issue_number"], proc.pid)
    return proc.pid


def spawn_for_provider(config, routes, item, worktree_path):
    """Spawn agent using the configured provider, with fallback to claude CLI.

    Checks column-routes.json for a per-column 'provider' override, then
    falls back to config['default_provider'], then to 'claude'.
    """
    column = item["status"]
    route = routes["columns"].get(column, {})
    provider_name = route.get("provider", config.get("default_provider", "claude"))
    providers = config.get("providers", {})
    provider = providers.get(provider_name, {})

    if provider.get("type") == "local":
        log.info("Issue #%d: using local provider '%s' for column '%s'",
                 item["issue_number"], provider_name, column)
        pid = spawn_local_agent(config, provider, routes, item, worktree_path)
        if pid is not None:
            return pid, provider_name

        # Local spawn failed — fall back to the configured fallback provider.
        # Only "claude" is supported as a fallback; guard against misconfiguration.
        fallback_name = provider.get("fallback_provider", "claude")
        if fallback_name != "claude":
            log.error(
                "Unsupported fallback_provider '%s' for issue #%d — "
                "only 'claude' is supported. Defaulting to 'claude'.",
                fallback_name, item["issue_number"],
            )
            fallback_name = "claude"
        log.warning("Local agent spawn failed for #%d, falling back to '%s'",
                    item["issue_number"], fallback_name)
        pid = spawn_agent(config, routes, item, worktree_path)
        return pid, fallback_name
    else:
        pid = spawn_agent(config, routes, item, worktree_path)
        return pid, "claude"


# ---------------------------------------------------------------------------
# Dispatcher phases
# ---------------------------------------------------------------------------

def phase_timeout_check(state, config, fields_cache):
    """Kill agents that have exceeded the timeout."""
    timeout_minutes = config.get("agent_timeout_minutes", 30)
    now = datetime.now(timezone.utc)

    for cid, info in list(state["running"].items()):
        issue_number = int(cid.split("/")[-1])
        started = datetime.fromisoformat(info["started"])
        elapsed = (now - started).total_seconds() / 60

        if elapsed > timeout_minutes:
            log.warning("Issue %s timed out after %.0f minutes, killing PID %d",
                        cid, elapsed, info["pid"])
            kill_process(info["pid"])

            repo = info.get("issue_repo", config["github_repo"])
            gh_issue_comment(repo, issue_number,
                f"**Agent timed out** after {elapsed:.0f} minutes "
                f"(limit: {timeout_minutes}m). Moving to Review.\n\n"
                f"Agent type: `{info['agent']}`\n"
                f"Worktree branch `{info.get('branch', 'unknown')}` preserved for inspection.")
            move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")

            # Preserve worktree for inspection on timeout
            del state["running"][cid]
            save_state(state)


def phase_retry_queue(state, config, fields_cache, routes):
    """Re-dispatch issues from the retry queue with failure context."""
    retry_queue = state.get("retry_queue", {})
    if not retry_queue:
        return

    budget = get_budget_profile(config)
    max_concurrent = budget["max_concurrent"]
    running_count = len(state["running"])

    for cid, retry_info in list(retry_queue.items()):
        issue_number = int(cid.split("/")[-1])
        if running_count >= max_concurrent:
            break

        log.info("Retrying issue %s (attempt %d, prior: %s)",
                 cid, retry_info["retry_count"], retry_info["prior_failure"])

        target_project = retry_info.get("target_project", "")
        project_dir = resolve_project_dir(config["projects_root"], target_project)
        if not project_dir:
            log.warning("Retry: project dir not found for %s, moving to Stuck", target_project)
            move_issue_to_column(fields_cache, retry_info.get("item_id", ""), "Review")
            del retry_queue[cid]
            continue

        result = create_worktree(project_dir, issue_number, target_project)
        if not result:
            log.error("Retry: failed to create worktree for %s", cid)
            del retry_queue[cid]
            continue
        worktree_path, branch_name = result

        # Build a synthetic item for spawn_agent with retry context
        item = {
            "issue_number": issue_number,
            "canonical_id": cid,
            "title": retry_info.get("title", ""),
            "body": retry_info.get("body", ""),
            "status": retry_info.get("column", "Engineering"),
            "target_project": target_project,
            "issue_repo": retry_info.get("issue_repo", config["github_repo"]),
            "item_id": retry_info.get("item_id", ""),
            "parent": retry_info.get("parent_issue"),
            "retry_context": {
                "retry_count": retry_info["retry_count"],
                "prior_failure": retry_info["prior_failure"],
            },
        }

        # If force_provider is set (e.g., local agent failed), bypass routing
        if retry_info.get("force_provider") == "claude":
            log.info("Issue %s: forced provider 'claude' on retry", cid)
            pid = spawn_agent(config, routes, item, worktree_path)
            used_provider = "claude"
        else:
            pid, used_provider = spawn_for_provider(config, routes, item, worktree_path)
        if pid is None:
            cleanup_worktree(project_dir, issue_number, target_project)
            del retry_queue[cid]
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
            "issue_repo": item["issue_repo"],
            "retry_count": retry_info["retry_count"],
            "title": item["title"],
            "body": item["body"],
            "pipeline": retry_info.get("pipeline"),
            "pipeline_index": retry_info.get("pipeline_index"),
            "provider": used_provider,
        }
        if retry_info.get("parent_issue"):
            entry["parent_issue"] = retry_info["parent_issue"]
        state["running"][cid] = entry
        running_count += 1
        del retry_queue[cid]
        log.info("Retried issue %s (PID %d, provider: %s)", cid, pid, used_provider)

    save_state(state)


def phase_poll_and_dispatch(state, config, fields_cache, routes):
    """Poll the board and dispatch agents for ready issues.

    v3: Budget-aware concurrency, pipeline classification, big-build deferral.
    """
    budget = get_budget_profile(config)
    max_concurrent = budget["max_concurrent"]
    running_count = len(state["running"])

    # Usage-aware throttling: check actual consumption
    usage = get_usage_status()
    if usage["should_pause"]:
        log.warning("Usage: PAUSED — %s", usage["reason"])
        return
    if usage["should_throttle"]:
        max_concurrent = max(1, max_concurrent - 1)
        log.info("Usage: throttled to max_concurrent=%d — %s",
                 max_concurrent, usage["reason"])

    log.info("Budget: %s | window=%s%% (%d/%d output tokens, %d sessions)",
             get_budget_summary(state), usage["window_pct"],
             usage["window_output_tokens"], usage["window_output_ceiling"],
             usage["window_sessions"])

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

    # Prioritize Ready items over mid-pipeline items so the board's priority
    # order is respected. Within each group, board order (from poll_board) is
    # preserved. Without this, a single issue cycling through a long pipeline
    # monopolizes the concurrency slot and blocks top-of-backlog work.
    items = sorted(items, key=lambda x: 0 if x["status"] == "Ready" else 1)

    for item in items:
        if item["canonical_id"] in running_issues:
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

        # Skip items in non-dispatchable columns (Done, Review, Stuck, Backlog)
        non_dispatchable = set(routes.get("non_dispatchable", ["Review", "Review", "Done", "Backlog"]))
        if item["status"] in non_dispatchable:
            continue

        # Skip items whose status isn't a known column (safety guard)
        if item["status"] != "Ready" and item["status"] not in routes["columns"]:
            log.warning("Issue #%d has unknown status '%s', skipping",
                        item["issue_number"], item["status"])
            continue

        # Budget: defer big builds to off-hours
        if should_defer_issue(item, budget):
            log.info("Deferring big build issue #%d until aggressive mode",
                     item["issue_number"])
            continue

        # Auto-route from Ready with pipeline classification
        if item["status"] == "Ready":
            # Check if this issue has existing pipeline state (was routed back by Skeptic)
            pipeline_state = state.get("pipeline_state", {}).get(item["canonical_id"])
            if pipeline_state:
                pipeline = pipeline_state["pipeline"]
                pipeline_idx = pipeline_state["pipeline_index"]
                target_column = pipeline[pipeline_idx] if pipeline_idx < len(pipeline) else "Engineering"
                log.info("Resuming issue #%d at pipeline[%d]=%s (pipeline: %s)",
                         item["issue_number"], pipeline_idx, target_column,
                         " → ".join(pipeline))
            else:
                issue_type, pipeline = classify_issue(item["title"], item["body"])
                target_column = pipeline[0]
                pipeline_idx = 0
                log.info("Auto-routing issue #%d as '%s' → pipeline %s",
                         item["issue_number"], issue_type, " → ".join(pipeline))

            # If pipeline says "Done", close the issue instead of dispatching
            if target_column == "Done":
                log.info("Pipeline complete for issue #%d — closing", item["issue_number"])
                move_issue_to_column(fields_cache, item["item_id"], "Done")
                repo = item.get("issue_repo", config["github_repo"])
                gh_issue_close(repo, item["issue_number"])
                # Clean up pipeline state
                state.get("pipeline_state", {}).pop(item["canonical_id"], None)
                continue

            move_issue_to_column(fields_cache, item["item_id"], target_column)
            item["status"] = target_column
            item["_pipeline"] = pipeline
            item["_pipeline_index"] = pipeline_idx

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
            log.error("Failed to create worktree for %s", item["canonical_id"])
            continue
        worktree_path, branch_name = result

        # Spawn agent (routes through provider config)
        pid, used_provider = spawn_for_provider(config, routes, item, worktree_path)
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
            "title": item.get("title", ""),
            "body": item.get("body", "")[:500],  # truncate for state file size
            "provider": used_provider,
        }
        # Store pipeline for smart advancement
        if item.get("_pipeline"):
            entry["pipeline"] = item["_pipeline"]
            entry["pipeline_index"] = item.get("_pipeline_index", 0)
        # Store parent Epic reference so harvest can post progress updates.
        if item.get("parent"):
            entry["parent_issue"] = item["parent"]
        state["running"][item["canonical_id"]] = entry
        running_issues.add(item["canonical_id"])   # prevent duplicate dispatch same cycle
        running_count += 1
        log.info("Dispatched %s to %s (PID %d)",
                 item["canonical_id"], item["status"], pid)


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
    """Check for finished agents and evaluate their output smartly.

    Uses layered evaluation gates (evaluator.py) instead of the old
    ">50 chars = done" heuristic. Failed evaluations trigger retries
    with failure context before giving up.
    """

    for cid, info in list(state["running"].items()):
        issue_number = int(cid.split("/")[-1])
        try:
            if is_pid_alive(info["pid"]):
                continue

            repo = info.get("issue_repo", config["github_repo"])
            output_file = OUTPUT_DIR / f"{safe_id(cid)}.log"
            stderr_file = OUTPUT_DIR / f"{safe_id(cid)}.stderr.log"
            raw_output = ""
            if output_file.exists():
                raw_output = output_file.read_text()

            project_dir = str(Path(config["projects_root"]) / info.get("project", ""))

            # Parse JSON output to extract both result text and usage data.
            # With --output-format json, the output is a JSON object with
            # "result" (the text) and "usage" (token counts).
            output_text = raw_output
            session_usage = {}
            try:
                json_output = json.loads(raw_output)
                output_text = json_output.get("result", raw_output)
                session_usage = json_output.get("usage", {})
                session_usage["total_cost_usd"] = json_output.get("total_cost_usd", 0)
                session_usage["duration_ms"] = json_output.get("duration_ms", 0)
            except (json.JSONDecodeError, TypeError):
                # Not JSON — agent may have crashed before producing structured output
                pass

            # Record per-session token usage for rolling window tracking
            if session_usage:
                record_session(session_usage, info["agent"], issue_number)

            # Track session stats for budget (legacy rolling average)
            started = datetime.fromisoformat(info["started"])
            duration_min = (datetime.now(timezone.utc) - started).total_seconds() / 60
            update_session_stats(state, info["agent"], duration_min, len(output_text))

            log.info("Issue %s: agent finished (PID %d, %.0f min, provider: %s)",
                     cid, info["pid"], duration_min,
                     info.get("provider", "claude"))

            # Local provider crash fallback: if the local agent produced no
            # meaningful output, re-queue the issue to retry with claude.
            if (info.get("provider") == "local"
                    and len(output_text.strip()) < 50
                    and not output_text.strip().startswith("##BLOCKED##")):
                log.warning("Local agent produced no output for %s, re-queuing with claude",
                            cid)
                gh_issue_comment(repo, issue_number,
                    f"**Local agent failed** (empty output) — retrying with Claude.\n\n"
                    f"Agent type: `{info['agent']}`, Provider: `{info.get('provider')}`")
                cleanup_worktree(project_dir, issue_number, info.get("project"))
                # Queue for retry with claude — store as retry with forced provider
                retry_info = {
                    "retry_count": info.get("retry_count", 0),
                    "prior_failure": "local_agent_empty_output",
                    "item_id": info.get("item_id", ""),
                    "column": info.get("column", ""),
                    "title": info.get("title", ""),
                    "body": info.get("body", ""),
                    "issue_repo": repo,
                    "target_project": info.get("project", ""),
                    "parent_issue": info.get("parent_issue"),
                    "pipeline": info.get("pipeline"),
                    "pipeline_index": info.get("pipeline_index"),
                    "force_provider": "claude",
                }
                state.setdefault("retry_queue", {})[cid] = retry_info
                output_file.unlink(missing_ok=True)
                stderr_file.unlink(missing_ok=True)
                del state["running"][cid]
                save_state(state)
                continue

            # Check for BLOCKED marker first (unchanged behavior)
            blocker = None
            for line in output_text.splitlines():
                stripped = line.strip()
                if stripped.startswith("##BLOCKED##"):
                    blocker = stripped.replace("##BLOCKED##", "BLOCKED:", 1)
                    break
                elif stripped.startswith("BLOCKED:"):
                    blocker = stripped
                    break

            if blocker:
                log.info("Issue %s is blocked: %s", cid, blocker)
                gh_issue_comment(repo, issue_number,
                    f"**Agent blocked** — moving to Review.\n\n"
                    f"`{blocker}`\n\n"
                    f"Agent type: `{info['agent']}`")
                move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")
                cleanup_worktree(project_dir, issue_number, info.get("project"))
                _notify_epic_blocked(info, cid, blocker, repo, config)
                output_file.unlink(missing_ok=True)
                stderr_file.unlink(missing_ok=True)
                del state["running"][cid]
                save_state(state)
                continue

            # --- Smart Evaluation ---
            # Build a minimal item dict for the evaluator
            eval_item = {
                "title": info.get("title", ""),
                "body": info.get("body", ""),
            }
            eval_result, eval_reason = evaluate(
                output_text, eval_item, info, config, CLAUDE_BIN
            )
            log.info("Issue %s evaluation: %s — %s", cid, eval_result, eval_reason)

            if eval_result == "fail_retry":
                retry_count = info.get("retry_count", 0)
                max_retries = config.get("evaluation", {}).get("max_retries", 2)
                log.warning("Issue %s failed evaluation (retry %d/%d): %s",
                            cid, retry_count + 1, max_retries, eval_reason)
                gh_issue_comment(repo, issue_number,
                    f"**Evaluation failed** (attempt {retry_count + 1}/{max_retries}): "
                    f"{eval_reason}\n\nRetrying with failure context.")

                # Queue for retry — store failure context, clean worktree, re-dispatch next cycle
                retry_info = {
                    "retry_count": retry_count + 1,
                    "prior_failure": eval_reason,
                    "item_id": info.get("item_id", ""),
                    "column": info.get("column", ""),
                    "title": info.get("title", ""),
                    "body": info.get("body", ""),
                    "issue_repo": repo,
                    "target_project": info.get("project", ""),
                    "parent_issue": info.get("parent_issue"),
                    "pipeline": info.get("pipeline"),
                    "pipeline_index": info.get("pipeline_index"),
                }
                state.setdefault("retry_queue", {})[cid] = retry_info
                cleanup_worktree(project_dir, issue_number, info.get("project"))
                output_file.unlink(missing_ok=True)
                stderr_file.unlink(missing_ok=True)
                del state["running"][cid]
                save_state(state)
                continue

            if eval_result == "fail_stuck":
                log.warning("Issue %s permanently stuck: %s", cid, eval_reason)
                last_output = output_text[-1500:] if output_text else "(no output)"
                gh_issue_comment(repo, issue_number,
                    f"**Agent failed evaluation** — moving to Stuck.\n\n"
                    f"**Reason:** {eval_reason}\n"
                    f"**Retries exhausted:** {info.get('retry_count', 0)} attempts\n\n"
                    f"<details><summary>Last output</summary>\n\n```\n{last_output}\n```\n</details>")
                move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")
                cleanup_worktree(project_dir, issue_number, info.get("project"))
                output_file.unlink(missing_ok=True)
                stderr_file.unlink(missing_ok=True)
                del state["running"][cid]
                save_state(state)
                continue

            # --- Evaluation passed: merge and advance ---
            merge_ok, merge_error = merge_worktree(project_dir, info.get("branch", ""))

            if not merge_ok:
                is_transient = merge_error and ("deadlock" in merge_error.lower() or "ORIG_HEAD" in merge_error)
                label = "Transient merge failure (macOS EDEADLK)" if is_transient else "Merge conflict"
                log.warning("%s for issue %s, moving to Review", label, cid)
                gh_issue_comment(repo, issue_number,
                    f"**{label}** — moving to Review.\n\n"
                    f"Branch `{info.get('branch', '')}` preserved for manual merging.\n\n"
                    f"Agent type: `{info['agent']}`\n"
                    f"Error: `{merge_error}`")
                move_issue_to_column(fields_cache, info.get("item_id", ""), "Review")
            else:
                # --- Skeptic routing: the Skeptic decides where work goes ---
                if info.get("column") == "Skeptic":
                    MAX_SKEPTIC_REJECTIONS = config.get("evaluation", {}).get("max_skeptic_rejections", 3)
                    ps = state.get("pipeline_state", {}).get(cid, {})
                    skeptic_rejections = ps.get("skeptic_rejections", 0)

                    verdict = extract_verdict(output_text)
                    if verdict:
                        decision = verdict.get("DECISION", "").upper()
                        route = verdict.get("ROUTE", "").strip()
                        reason = verdict.get("REASON", "no reason given")
                        issues_created = verdict.get("ISSUES_CREATED", "none")

                        # Validate route is a real column
                        valid_columns = set(routes["columns"].keys()) | {"Ready", "Done", "Review"}
                        if route not in valid_columns:
                            route = "Review"
                            log.warning("Skeptic gave invalid route '%s', falling back to Review",
                                        verdict.get("ROUTE", ""))

                        # Guard: Skeptic must not route to itself (infinite loop)
                        if route == "Skeptic":
                            log.warning("Skeptic %s routed to itself — redirecting to Review", cid)
                            route = "Review"
                            decision = "REJECT"

                        if decision == "APPROVE":
                            next_column = route
                            # Reset rejection counter on approval
                            state.setdefault("pipeline_state", {}).setdefault(cid, {})["skeptic_rejections"] = 0
                            log.info("Skeptic APPROVED %s → %s: %s", cid, route, reason)
                        else:
                            skeptic_rejections += 1
                            if skeptic_rejections >= MAX_SKEPTIC_REJECTIONS:
                                next_column = "Review"
                                reason += f" [Skeptic rejection cap reached ({skeptic_rejections}/{MAX_SKEPTIC_REJECTIONS}) — escalating to Review]"
                                log.warning("Skeptic %s hit max rejections (%d) → Review", cid, skeptic_rejections)
                            else:
                                next_column = route
                                state.setdefault("pipeline_state", {}).setdefault(cid, {})["skeptic_rejections"] = skeptic_rejections
                                log.info("Skeptic REJECTED %s → %s (%d/%d): %s",
                                         cid, route, skeptic_rejections, MAX_SKEPTIC_REJECTIONS, reason)

                        comment_body = (
                            f"**Skeptic verdict: {decision}** → **{next_column}**\n\n"
                            f"**Reason:** {reason}\n\n"
                        )
                        skeptic_created = create_followup_issues(issues_created, config, fields_cache, cid)
                        if skeptic_created:
                            urls_md = "\n".join(f"- {u}" for u in skeptic_created)
                            comment_body += f"**Issues created ({len(skeptic_created)}):**\n{urls_md}\n\n"
                        elif issues_created and issues_created.lower() != "none":
                            comment_body += f"**Issues noted:** {issues_created}\n\n"

                        # Append raw verdict for transparency
                        raw = output_text[-1500:] if len(output_text) > 1500 else output_text
                        comment_body += (
                            f"<details><summary>Full verdict</summary>\n\n"
                            f"```\n{raw}\n```\n</details>"
                        )
                    else:
                        # Skeptic didn't produce a verdict — treat as rejection to Review
                        next_column = "Review"
                        comment_body = (
                            f"**Skeptic** completed but produced no verdict. "
                            f"Moving to **Review** for human inspection.\n\n"
                        )
                        log.warning("Skeptic %s: no verdict found in output", cid)
                else:
                    # --- Normal agents: advance through pipeline ---
                    pipeline = info.get("pipeline")
                    pipeline_idx = info.get("pipeline_index", 0)
                    if pipeline and pipeline_idx + 1 < len(pipeline):
                        next_column = pipeline[pipeline_idx + 1]
                    else:
                        next_column = routes["columns"].get(info["column"], {}).get("next", "Done")

                    structured = eval_extract_summary(output_text)
                    if structured:
                        comment_body = (
                            f"**{info['agent']}** completed. ✅ Evaluation passed.\n\n"
                            f"**What was done:** {structured.get('DONE', '(see output)')}\n\n"
                            f"**Files changed:** {structured.get('FILES', 'none listed')}\n\n"
                            f"**Commits:** {structured.get('COMMITS', 'none listed')}\n\n"
                        )
                        followup = structured.get("FOLLOWUP", "none")
                        created_urls = create_followup_issues(followup, config, fields_cache, cid)
                        if created_urls:
                            urls_md = "\n".join(f"- {u}" for u in created_urls)
                            comment_body += f"**Follow-up issues created ({len(created_urls)}):**\n{urls_md}\n\n"
                        elif followup and followup.lower() not in ("none", "n/a", ""):
                            comment_body += f"**Follow-up noted (not auto-created):** {followup}\n\n"
                        comment_body += f"Moving to **{next_column}**."
                    else:
                        raw = output_text[-2000:] if len(output_text) > 2000 else output_text
                        comment_body = (
                            f"**{info['agent']}** completed. ✅ Evaluation passed.\n\n"
                            f"<details><summary>Agent output</summary>\n\n"
                            f"```\n{raw}\n```\n</details>\n\n"
                            f"Moving to **{next_column}**."
                        )

                gh_issue_comment(repo, issue_number, comment_body)

                if next_column == "Done":
                    move_issue_to_column(fields_cache, info.get("item_id", ""), "Done")
                    gh_issue_close(repo, issue_number)
                else:
                    move_issue_to_column(fields_cache, info.get("item_id", ""), next_column)

                # Update pipeline index if using pipeline routing.
                # Find the target column nearest to (but not before) current position
                # to handle duplicate column names (e.g., Skeptic appears 3x in feature).
                # If Skeptic routes to Ready, preserve pipeline state so we resume correctly.
                pipeline = info.get("pipeline")
                if pipeline:
                    current_idx = info.get("pipeline_index", 0)
                    if next_column in pipeline:
                        # Find nearest occurrence at or after current position
                        new_idx = None
                        for i in range(len(pipeline)):
                            if pipeline[i] == next_column:
                                if i >= current_idx:
                                    new_idx = i
                                    break
                        # If not found forward, take the last occurrence before current
                        # (Skeptic routing backward is intentional)
                        if new_idx is None:
                            for i in range(len(pipeline) - 1, -1, -1):
                                if pipeline[i] == next_column:
                                    new_idx = i
                                    break
                        if new_idx is not None:
                            state.setdefault("pipeline_state", {})[cid] = {
                                "pipeline": pipeline,
                                "pipeline_index": new_idx,
                            }
                    elif next_column == "Ready":
                        # Skeptic sent to Ready — preserve pipeline at current position
                        # so we resume where we left off, not restart from scratch
                        state.setdefault("pipeline_state", {})[cid] = {
                            "pipeline": pipeline,
                            "pipeline_index": current_idx,
                        }

                cleanup_worktree(project_dir, issue_number, info.get("project"))

                # Epic progress tracking
                _notify_epic_progress(info, cid, repo)

            output_file.unlink(missing_ok=True)
            stderr_file.unlink(missing_ok=True)
            del state["running"][cid]
            save_state(state)
        except Exception:
            log.exception("Error harvesting issue %s, skipping", cid)
            continue


def _notify_epic_blocked(info, issue_num, blocker, repo, config):
    """Notify parent Epic that a sub-issue is blocked."""
    if not info.get("parent_issue"):
        return
    parent_num = info["parent_issue"]["number"]
    parent_repo = info["parent_issue"].get("repo", repo)
    summary = fetch_epic_summary(parent_repo, parent_num)
    progress = f"{summary['completed']}/{summary['total']}" if summary else "?"
    gh_issue_comment(parent_repo, parent_num,
        f"📋 **Sub-issue #{issue_num} blocked** — Epic progress: {progress}.\n\n"
        f"`{blocker}`")
    log.info("Epic #%d: sub-issue #%s blocked, notified", parent_num, issue_num)


def _notify_epic_progress(info, issue_num, repo):
    """Post Epic progress update and auto-close if all sub-issues done."""
    if not info.get("parent_issue"):
        return
    parent_num = info["parent_issue"]["number"]
    parent_repo = info["parent_issue"].get("repo", repo)
    summary = fetch_epic_summary(parent_repo, parent_num)
    if not summary or summary["total"] == 0:
        return
    progress = f"{summary['completed']}/{summary['total']}"
    percent = summary["percent_completed"]
    if summary["completed"] == summary["total"]:
        gh_issue_comment(parent_repo, parent_num,
            f"🎉 **Epic complete** — all {summary['total']} sub-issues resolved.\n\n"
            f"Closing this Epic automatically.")
        gh_issue_close(parent_repo, parent_num)
        log.info("Epic #%d: all sub-issues complete, auto-closed", parent_num)
    else:
        gh_issue_comment(parent_repo, parent_num,
            f"📊 **Progress update** — sub-issue #{issue_num} completed.\n\n"
            f"**{progress}** sub-issues done ({percent}% complete).")
        log.info("Epic #%d: progress %s (%d%%)", parent_num, progress, percent)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def cleanup_orphans(state, config):
    """Remove worktree directories and agent branches not tracked in state."""
    # Build sets for matching active work
    active_worktrees = set()
    for info in state.get("running", {}).values():
        wt = info.get("worktree", "")
        if wt:
            active_worktrees.add(Path(wt).name)

    # Build per-repo set of active issue numbers from canonical_id keys
    # canonical_id format: "owner/repo/number"
    running_issue_numbers_per_repo: dict = {}
    for cid in state.get("running", {}).keys():
        cid_parts = cid.rsplit("/", 1)
        if len(cid_parts) == 2:
            repo_key = cid_parts[0]   # "owner/repo"
            running_issue_numbers_per_repo.setdefault(repo_key, set()).add(cid_parts[1])

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

            # Resolve this repo's canonical owner/repo key from its remote URL
            remote_result = subprocess.run(
                ["git", "-C", str(child), "remote", "get-url", "origin"],
                capture_output=True, text=True,
            )
            child_repo_key = ""
            if remote_result.returncode == 0:
                url = remote_result.stdout.strip()
                # Convert https://github.com/owner/repo.git -> owner/repo
                child_repo_key = url.replace("https://github.com/", "").replace(".git", "")

            allowed_nums = running_issue_numbers_per_repo.get(child_repo_key, set())

            result = subprocess.run(
                ["git", "-C", str(child), "branch", "--list", "agent/*"],
                capture_output=True, text=True,
            )
            for line in result.stdout.splitlines():
                branch = line.strip().lstrip("* ")
                parts = branch.split("/")
                if len(parts) == 2 and parts[1].isdigit():
                    if parts[1] not in allowed_nums:
                        log.info("Deleting orphaned branch %s in %s",
                                 branch, child.name)
                        subprocess.run(
                            ["git", "-C", str(child), "branch", "-D", branch],
                            capture_output=True, text=True,
                        )


def _fix_board_orphans(config, fields_cache):
    """Fix board items with no status or stuck in Stuck — move to Ready.

    Also moves CLOSED items out of Ready to Done.
    Runs every cycle to prevent items from falling into limbo.
    """
    owner = config["github_repo"].split("/")[0]
    project_num = config["github_project_number"]
    project_id = fields_cache["project_id"]
    field_id = fields_cache["fields"]["Status"]["id"]
    ready_id = fields_cache["fields"]["Status"]["options"].get("Ready")
    done_id = fields_cache["fields"]["Status"]["options"].get("Done")

    if not ready_id or not done_id:
        return

    query = f'''query {{
        user(login: "{owner}") {{
            projectV2(number: {project_num}) {{
                items(first: 100) {{
                    nodes {{
                        id
                        content {{
                            ... on Issue {{ number state title }}
                        }}
                        fieldValues(first: 15) {{
                            nodes {{
                                ... on ProjectV2ItemFieldSingleSelectValue {{
                                    name
                                    field {{ ... on ProjectV2SingleSelectField {{ name }} }}
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
        return

    fixed = 0
    for node in data["data"]["user"]["projectV2"]["items"]["nodes"]:
        content = node.get("content")
        if not content:
            continue
        issue_state = content.get("state", "OPEN")
        status = None
        for fv in node.get("fieldValues", {}).get("nodes", []):
            field = fv.get("field", {})
            if field.get("name") == "Status" and "name" in fv:
                status = fv["name"]

        target = None
        title = content.get("title", "")
        backlog_id = fields_cache["fields"]["Status"]["options"].get("Backlog")

        if issue_state == "CLOSED" and status not in ("Done", None):
            # Closed but not in Done — fix immediately
            target = done_id
        elif issue_state == "OPEN" and status is None:
            # No status. Epics go to Backlog (they're containers, not dispatchable).
            is_epic = title.startswith("Let's ") or title.startswith("Epic:")
            target = backlog_id if (is_epic and backlog_id) else ready_id
        elif issue_state == "OPEN" and status == "Review":
            # Only move non-interview/review items back to Ready
            if not (title.startswith("[Interview]") or title.startswith("[Review]")):
                target = ready_id
        # [Interview] issues in Review stay there — they need human input

        if target:
            mutation = f'''mutation {{
                updateProjectV2ItemFieldValue(input: {{
                    projectId: "{project_id}"
                    itemId: "{node['id']}"
                    fieldId: "{field_id}"
                    value: {{ singleSelectOptionId: "{target}" }}
                }}) {{ projectV2Item {{ id }} }}
            }}'''
            result = gh_graphql(mutation)
            if result:
                fixed += 1

    if fixed:
        log.info("Board hygiene: fixed %d orphaned items", fixed)


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

        # Prune old session log entries (keep 14 days)
        try:
            prune_session_log()
        except Exception:
            pass

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

        # Phase 4: Process retry queue (re-dispatch failed evaluations)
        phase_retry_queue(state, config, fields_cache, routes)
        save_state(state)

        # Phase 5: Board hygiene — fix orphaned items (None/Stuck → Ready)
        if not DRY_RUN:
            _fix_board_orphans(config, fields_cache)

        # Phase 6: Discover work when queue is empty
        phase_discover(state, config, fields_cache, dry_run=DRY_RUN)
        save_state(state)

        # Phase 7: Poll and dispatch
        phase_poll_and_dispatch(state, config, fields_cache, routes)
        state["last_poll"] = datetime.now(timezone.utc).isoformat()
        save_state(state)

        # Save usage snapshot every cycle
        try:
            save_snapshot()
        except Exception:
            log.debug("Failed to save usage snapshot (non-critical)")

        budget_summary = get_budget_summary(state)
        log.info("Dispatcher cycle complete. Running: %d agents. %s",
                 len(state["running"]), budget_summary)
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
