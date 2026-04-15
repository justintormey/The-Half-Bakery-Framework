"""Claude Max usage tracker for Half Bakery dispatcher.

Tracks REAL token consumption using two complementary signals:

1. Per-agent-session tokens: dispatcher captures exact usage from
   `claude --print --output-format json` and logs it here.
2. 5-hour rolling window: Claude Max resets every ~5 hours from first
   message. We sum tokens in the last 5h to estimate window consumption.
3. 429 rate_limit_error detection: emergency circuit breaker from debug logs.
4. stats-cache.json: daily totals as a cross-check.

Rate limit facts (Claude Max 20x, $200/mo):
  - 5-hour ROLLING window (not fixed clock)
  - ~220K output tokens per window (community estimate, not official)
  - Separate weekly rolling cap (~24-40 Opus hours/week)
  - All Claude surfaces (web, Code, mobile) share the same pool
  - Peak hours (weekdays 5am-11am PT) burn faster

Run standalone: python3 usage_tracker.py          (print current status)
Run standalone: python3 usage_tracker.py --save    (save snapshot)
Import:         from usage_tracker import get_usage_status, record_session
"""

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

log = logging.getLogger("dispatcher.usage")

STATS_CACHE = Path.home() / ".claude" / "stats-cache.json"
DEBUG_DIR = Path.home() / ".claude" / "debug"
USAGE_DIR = Path.home() / ".half-bakery" / "usage"
SESSION_LOG = USAGE_DIR / "sessions.jsonl"

# ── Empirical ceilings ──────────────────────────────────────────────
# These are conservative estimates. Adjust based on observed 429 patterns.

WINDOW_HOURS = 5
WINDOW_OUTPUT_CEILING = 300_000   # output tokens per 5h window (Max 5x plan, mostly Sonnet)
WINDOW_INPUT_CEILING = 2_000_000  # input tokens per 5h window (generous, cached doesn't count)
DAILY_SESSION_CEILING = 80        # sessions per day

# Weekly all-models cap for Max 5x. At 40% used on Monday with 5 days
# remaining, we need to target ~12% per day = ~720K tokens/day.
# Total weekly budget estimated at ~6M tokens (conservative for 5x plan).
WEEKLY_OUTPUT_CEILING = 6_000_000

# Cooldown after rate limit hit
RATE_LIMIT_COOLDOWN_MIN = 30


# ── Session recording ────────────────────────────────────────────────

def record_session(usage_data, agent_type="unknown", issue_number=0):
    """Record a completed agent session's token usage.

    Called by the dispatcher after each agent finishes.
    usage_data comes from `claude --print --output-format json`.
    """
    USAGE_DIR.mkdir(parents=True, exist_ok=True)

    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "agent": agent_type,
        "issue": issue_number,
        "input_tokens": usage_data.get("input_tokens", 0),
        "output_tokens": usage_data.get("output_tokens", 0),
        "cache_read": usage_data.get("cache_read_input_tokens", 0),
        "cache_create": usage_data.get("cache_creation_input_tokens", 0),
        "cost_usd": usage_data.get("total_cost_usd", 0),
        "duration_ms": usage_data.get("duration_ms", 0),
    }

    with open(SESSION_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")

    log.info("Recorded session: %s #%s — %d in / %d out / $%.4f",
             agent_type, issue_number,
             entry["input_tokens"], entry["output_tokens"], entry["cost_usd"])


def get_window_sessions(hours=WINDOW_HOURS):
    """Get all sessions within the last N hours."""
    if not SESSION_LOG.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    sessions = []

    try:
        with open(SESSION_LOG) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    ts = datetime.fromisoformat(entry["ts"])
                    if ts >= cutoff:
                        sessions.append(entry)
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
    except OSError:
        pass

    return sessions


def get_weekly_sessions():
    """Get all sessions within the last 7 days."""
    return get_window_sessions(hours=7 * 24)


# ── Stats cache (daily cross-check) ──────────────────────────────────

def load_stats_cache():
    if not STATS_CACHE.exists():
        return None
    try:
        with open(STATS_CACHE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def get_today_tokens(stats=None):
    if stats is None:
        stats = load_stats_cache()
    if not stats:
        return {}
    today = datetime.now().strftime("%Y-%m-%d")
    for entry in stats.get("dailyModelTokens", []):
        if entry.get("date") == today:
            return entry.get("tokensByModel", {})
    return {}


def get_today_activity(stats=None):
    if stats is None:
        stats = load_stats_cache()
    if not stats:
        return {"messageCount": 0, "sessionCount": 0, "toolCallCount": 0}
    today = datetime.now().strftime("%Y-%m-%d")
    for entry in stats.get("dailyActivity", []):
        if entry.get("date") == today:
            return entry
    return {"messageCount": 0, "sessionCount": 0, "toolCallCount": 0}


# ── Rate limit detection ──────────────────────────────────────────────

def get_recent_rate_limits(hours=1):
    """Check debug logs for recent 429 rate_limit_error entries."""
    if not DEBUG_DIR.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    hits = []

    for path in DEBUG_DIR.iterdir():
        if not path.name.endswith(".txt"):
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                continue
        except OSError:
            continue
        try:
            text = path.read_text(errors="ignore")
            if "rate_limit_error" in text:
                hits.append((mtime.isoformat(), path.name))
        except OSError:
            continue

    return sorted(hits, reverse=True)


# ── Main status function ──────────────────────────────────────────────

def get_usage_status():
    """Return comprehensive usage status with rolling window tracking.

    The key signals:
    - window_output_tokens: output tokens from agent sessions in last 5h
    - window_pct: percentage of estimated 5h ceiling
    - rate_limited: 429 detected recently
    - should_throttle / should_pause: dispatch recommendations
    """
    # Rolling window from our session log
    window_sessions = get_window_sessions(WINDOW_HOURS)
    window_output = sum(s.get("output_tokens", 0) for s in window_sessions)
    window_input = sum(s.get("input_tokens", 0) for s in window_sessions)
    window_cost = sum(s.get("cost_usd", 0) for s in window_sessions)
    window_count = len(window_sessions)
    window_pct = (window_output / WINDOW_OUTPUT_CEILING * 100) if WINDOW_OUTPUT_CEILING else 0

    # Weekly Opus usage
    weekly = get_weekly_sessions()
    weekly_output = sum(s.get("output_tokens", 0) for s in weekly)
    weekly_pct = (weekly_output / WEEKLY_OUTPUT_CEILING * 100) if WEEKLY_OUTPUT_CEILING else 0

    # Daily stats from stats-cache (includes YOUR usage, not just agents)
    stats = load_stats_cache()
    daily_tokens = get_today_tokens(stats)
    daily_total = sum(daily_tokens.values())
    activity = get_today_activity(stats)

    # Rate limit detection
    rate_limits = get_recent_rate_limits(hours=1)
    recent_limit = get_recent_rate_limits(hours=RATE_LIMIT_COOLDOWN_MIN / 60)
    rate_limited = len(recent_limit) > 0

    # Decision logic
    should_pause = False
    should_throttle = False
    reason = "nominal"

    if rate_limited:
        should_pause = True
        reason = f"429 rate limited {len(recent_limit)}x in last {RATE_LIMIT_COOLDOWN_MIN}min"
    elif window_pct >= 85:
        should_pause = True
        reason = f"5h window at {window_pct:.0f}% ({window_output:,}/{WINDOW_OUTPUT_CEILING:,} output tokens)"
    elif weekly_pct >= 90:
        should_pause = True
        reason = f"weekly Opus cap at {weekly_pct:.0f}% ({weekly_output:,}/{WEEKLY_OUTPUT_CEILING:,})"
    elif window_pct >= 75:
        should_throttle = True
        reason = f"5h window at {window_pct:.0f}% — throttling"
    elif weekly_pct >= 75:
        should_throttle = True
        reason = f"weekly Opus at {weekly_pct:.0f}% — throttling"

    return {
        # Rolling 5h window (agent sessions only)
        "window_output_tokens": window_output,
        "window_input_tokens": window_input,
        "window_sessions": window_count,
        "window_cost_usd": round(window_cost, 4),
        "window_pct": round(window_pct, 1),
        # Weekly
        "weekly_output_tokens": weekly_output,
        "weekly_pct": round(weekly_pct, 1),
        # Daily (all Claude usage, from stats-cache)
        "today_tokens": daily_tokens,
        "today_total_tokens": daily_total,
        "today_sessions": activity.get("sessionCount", 0),
        "today_messages": activity.get("messageCount", 0),
        # Rate limits
        "recent_rate_limits": rate_limits,
        "rate_limited": rate_limited,
        # Recommendations
        "should_throttle": should_throttle,
        "should_pause": should_pause,
        "reason": reason,
        # Meta
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_hours": WINDOW_HOURS,
        "window_output_ceiling": WINDOW_OUTPUT_CEILING,
        "weekly_output_ceiling": WEEKLY_OUTPUT_CEILING,
    }


def save_snapshot(status=None):
    """Save a usage snapshot to ~/.half-bakery/usage/YYYY-MM-DD.jsonl."""
    if status is None:
        status = get_usage_status()
    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    snapshot_file = USAGE_DIR / f"{today}.jsonl"
    with open(snapshot_file, "a") as f:
        f.write(json.dumps(status) + "\n")
    return str(snapshot_file)


def get_weekly_summary():
    """Aggregate the last 7 days from stats-cache for reporting."""
    stats = load_stats_cache()
    if not stats:
        return None
    today = datetime.now()
    summary = []
    for i in range(7):
        date = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        tokens = {}
        activity = {"messageCount": 0, "sessionCount": 0, "toolCallCount": 0}
        for entry in stats.get("dailyModelTokens", []):
            if entry.get("date") == date:
                tokens = entry.get("tokensByModel", {})
                break
        for entry in stats.get("dailyActivity", []):
            if entry.get("date") == date:
                activity = entry
                break
        summary.append({
            "date": date,
            "total_tokens": sum(tokens.values()),
            "sessions": activity.get("sessionCount", 0),
            "messages": activity.get("messageCount", 0),
        })
    return summary


# ── Session log maintenance ───────────────────────────────────────────

def prune_session_log(keep_days=14):
    """Remove entries older than keep_days from the session log."""
    if not SESSION_LOG.exists():
        return
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    kept = []
    with open(SESSION_LOG) as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                ts = datetime.fromisoformat(entry["ts"])
                if ts >= cutoff:
                    kept.append(line)
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
    with open(SESSION_LOG, "w") as f:
        f.writelines(kept)


# ── CLI ───────────────────────────────────────────────────────────────

def print_status():
    status = get_usage_status()

    print("=" * 55)
    print("  Half Bakery — Claude Max Usage Tracker")
    print("=" * 55)
    print()
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print()

    # 5-hour rolling window
    print(f"  ── 5-Hour Rolling Window ──")
    print(f"  Output tokens: {status['window_output_tokens']:>10,} / {WINDOW_OUTPUT_CEILING:,}  ({status['window_pct']:.1f}%)")
    print(f"  Input tokens:  {status['window_input_tokens']:>10,}")
    print(f"  Agent sessions:{status['window_sessions']:>10}")
    print(f"  Est. cost:     ${status['window_cost_usd']:>9.4f}")
    print()

    # Weekly
    print(f"  ── Weekly Rolling ──")
    print(f"  Output tokens: {status['weekly_output_tokens']:>10,} / {WEEKLY_OUTPUT_CEILING:,}  ({status['weekly_pct']:.1f}%)")
    print()

    # Daily (all Claude usage)
    print(f"  ── Today (all Claude, from stats-cache) ──")
    print(f"  Total tokens:  {status['today_total_tokens']:>10,}")
    print(f"  Sessions:      {status['today_sessions']:>10}")
    print(f"  Messages:      {status['today_messages']:>10}")
    if status["today_tokens"]:
        for model, count in sorted(status["today_tokens"].items()):
            short = model.split("-20")[0] if "-20" in model else model
            print(f"    {short:30s} {count:>10,}")
    print()

    # Rate limits
    if status["recent_rate_limits"]:
        print(f"  ⚠️  Rate limits in last hour: {len(status['recent_rate_limits'])}")
        for ts, f in status["recent_rate_limits"][:3]:
            print(f"    {ts}")
        print()

    # Recommendation
    if status["should_pause"]:
        print(f"  🛑 PAUSE: {status['reason']}")
    elif status["should_throttle"]:
        print(f"  ⚠️  THROTTLE: {status['reason']}")
    else:
        print(f"  ✅ {status['reason']}")
    print()

    # Weekly history
    weekly = get_weekly_summary()
    if weekly:
        print(f"  ── Last 7 Days ──")
        print(f"  {'Date':12s} {'Tokens':>10s} {'Sessions':>10s} {'Messages':>10s}")
        print(f"  {'-'*12} {'-'*10} {'-'*10} {'-'*10}")
        for day in weekly:
            print(f"  {day['date']:12s} {day['total_tokens']:>10,} {day['sessions']:>10} {day['messages']:>10}")
        total_t = sum(d["total_tokens"] for d in weekly)
        total_s = sum(d["sessions"] for d in weekly)
        print(f"  {'TOTAL':12s} {total_t:>10,} {total_s:>10}")
    print()


if __name__ == "__main__":
    if "--save" in sys.argv:
        path = save_snapshot()
        print(f"Snapshot saved to {path}")
    if "--prune" in sys.argv:
        prune_session_log()
        print("Session log pruned")
    print_status()
