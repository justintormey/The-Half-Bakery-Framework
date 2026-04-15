"""Usage budgeting for Half Bakery dispatcher.

Time-aware concurrency limits: conservative during work hours,
aggressive evenings/weekends. Zero external dependencies.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("dispatcher.budget")

# 5-hour reset window for Claude Max
WINDOW_HOURS = 5


def get_budget_profile(config):
    """Return budget profile based on current time.

    Returns dict with: max_concurrent, mode, reserve_pct
    """
    budget_cfg = config.get("budget", {})
    now = datetime.now()
    weekday = now.weekday()  # 0=Mon, 6=Sun
    hour = now.hour

    work_days = budget_cfg.get("work_days", [0, 1, 2, 3, 4])
    work_start = budget_cfg.get("work_hours", {}).get("start", 9)
    work_end = budget_cfg.get("work_hours", {}).get("end", 18)

    conservative_max = budget_cfg.get("conservative_max", 1)
    moderate_max = budget_cfg.get("moderate_max", 2)
    aggressive_max = budget_cfg.get("aggressive_max", 3)

    if weekday in work_days and work_start <= hour < work_end:
        return {
            "max_concurrent": conservative_max,
            "mode": "conservative",
            "reserve_pct": budget_cfg.get("reserve_pct_work_hours", 40),
        }
    elif weekday in work_days and (
        (work_start - 2) <= hour < work_start or work_end <= hour < (work_end + 4)
    ):
        # Shoulder hours: 2h before work, 4h after work
        return {
            "max_concurrent": moderate_max,
            "mode": "moderate",
            "reserve_pct": budget_cfg.get("reserve_pct_shoulder", 20),
        }
    else:
        # Nights + weekends
        return {
            "max_concurrent": aggressive_max,
            "mode": "aggressive",
            "reserve_pct": 0,
        }


def should_defer_issue(item, budget_profile):
    """Defer only actual epics (issues with sub-issues) to aggressive mode.

    Regular issues always run regardless of budget mode.
    Only epics get deferred because they spawn multiple agent sessions.
    """
    if budget_profile["mode"] == "aggressive":
        return False

    # Only defer actual epics (issues with sub-issues)
    return item.get("sub_issues_total", 0) > 0


def update_session_stats(state, agent_type, duration_min, output_len):
    """Track rolling averages of agent session costs."""
    budget = state.setdefault("budget", {})
    history = budget.setdefault("history", {})
    stats = history.setdefault(agent_type, {
        "avg_duration_min": 0,
        "avg_output_tokens": 0,
        "total_sessions": 0,
    })

    n = stats["total_sessions"]
    # Rolling average (weighted toward recent)
    if n == 0:
        stats["avg_duration_min"] = duration_min
        stats["avg_output_tokens"] = output_len
    else:
        alpha = 0.3  # weight recent sessions higher
        stats["avg_duration_min"] = (1 - alpha) * stats["avg_duration_min"] + alpha * duration_min
        stats["avg_output_tokens"] = (1 - alpha) * stats["avg_output_tokens"] + alpha * output_len
    stats["total_sessions"] = n + 1

    # Track window usage
    now = datetime.now(timezone.utc)
    window_start = budget.get("window_start")
    if window_start:
        ws = datetime.fromisoformat(window_start)
        elapsed_hours = (now - ws).total_seconds() / 3600
        if elapsed_hours >= WINDOW_HOURS:
            # New window
            budget["window_start"] = now.isoformat()
            budget["sessions_this_window"] = 1
        else:
            budget["sessions_this_window"] = budget.get("sessions_this_window", 0) + 1
    else:
        budget["window_start"] = now.isoformat()
        budget["sessions_this_window"] = 1

    log.info("Budget: %s session #%d (%.0f min, %d chars). Window: %d sessions",
             agent_type, stats["total_sessions"], duration_min, output_len,
             budget["sessions_this_window"])


def get_budget_summary(state):
    """Return human-readable budget status for logging."""
    budget = state.get("budget", {})
    profile = get_budget_profile({})  # default config
    window_sessions = budget.get("sessions_this_window", 0)
    return (
        f"mode={profile['mode']} max_concurrent={profile['max_concurrent']} "
        f"reserve={profile['reserve_pct']}% window_sessions={window_sessions}"
    )
