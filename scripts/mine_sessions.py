#!/usr/bin/env python3
"""Mine Claude Code session transcripts for workflow patterns and insights.

Usage:
  python3 mine_sessions.py [--project-dir DIR] [--json] [--dashboard] [--verbose]

Options:
  --project-dir DIR   Directory containing JSONL transcripts
                      (default: ~/.claude/projects/-Users-taxfix-projects-data-dbt-models/)
  --json              Output sessions.json and analysis.json only
  --dashboard         Generate dashboard.html
  --verbose           Print per-session details to stdout
  --output-dir DIR    Where to write output files (default: ~/.claude/session_analysis/)
"""

import collections
import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Import helpers from filter_transcript.py (same directory)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filter_transcript import extract_ticket_id, extract_branch_from_transcript

# --- Constants ---

MIN_FILE_SIZE = 5_000  # Skip files under 5KB
BURST_GAP_SECONDS = 600  # 10 minutes — gap between activity bursts
MSG_PREVIEW_CHARS = 300

# Bash command classification patterns
BASH_PATTERNS = [
    ("dbt_run", re.compile(r"\bdbt\s+run\b")),
    ("dbt_compile", re.compile(r"\bdbt\s+compile\b")),
    ("dbt_test", re.compile(r"\bdbt\s+test\b")),
    ("dbt_build", re.compile(r"\bdbt\s+build\b")),
    ("dbt_show", re.compile(r"\bdbt\s+show\b")),
    ("dbt_deps", re.compile(r"\bdbt\s+deps\b")),
    ("dbt_other", re.compile(r"\bdbt\s+")),
    ("git_commit", re.compile(r"\bgit\s+commit\b")),
    ("git_push", re.compile(r"\bgit\s+push\b")),
    ("git_other", re.compile(r"\bgit\s+")),
    ("gh_pr", re.compile(r"\bgh\s+pr\b")),
    ("gh_other", re.compile(r"\bgh\s+")),
    ("python", re.compile(r"\bpython3?\s+")),
    ("snowsql", re.compile(r"\bsnowsql\b")),
]

# MCP server grouping
MCP_PREFIXES = {
    "snowflake": "mcp__snowflake__",
    "dbt-cloud": "mcp__dbt-cloud__",
    "looker": "mcp__looker__",
    "slack": "mcp__claude_ai_Slack__",
    "atlassian": "mcp__claude_ai_Atlassian__",
    "notion": "mcp__claude_ai_Notion__",
    "chrome": "mcp__claude-in-chrome__",
}

# Task category keywords in first_user_message
CATEGORY_KEYWORDS = {
    "analytics": re.compile(r"cohort|retention|report|analysis|metric|dashboard|data\s+for|numbers|csv|tsv", re.I),
    "bugfix": re.compile(r"fix|bug|broken|failing|error|wrong|issue|incorrect|doesn.t work|not working", re.I),
    "investigation": re.compile(r"why|check|investigate|look into|what.s happening|debug|diagnose|root cause", re.I),
    "infra": re.compile(r"setup|config|ci|cd|workflow|hook|script|deploy|permission|access", re.I),
    "review": re.compile(r"review|pr comment|feedback|approve", re.I),
}

# File layer classification
def classify_file_layer(path: str) -> str:
    if not path:
        return "other"
    p = path.lower()
    for layer in ("sources", "models/marts", "marts", "models", "macros", "seeds", "tests", "snapshots"):
        if f"/{layer}/" in p or p.startswith(f"{layer}/"):
            return layer.replace("models/marts", "marts")
    if "schema.yml" in p or "schema.yaml" in p:
        return "schema"
    return "other"


def classify_bash_command(cmd: str) -> str:
    """Classify a bash command into a subcategory."""
    for category, pattern in BASH_PATTERNS:
        if pattern.search(cmd):
            return category
    return "other"


def classify_mcp(tool_name: str) -> str | None:
    """Map a tool name to its MCP server group."""
    for group, prefix in MCP_PREFIXES.items():
        if tool_name.startswith(prefix):
            return group
    return None


def classify_task(session: dict) -> str:
    """Rule-based task classification from multiple signals."""
    scores: dict[str, float] = collections.defaultdict(float)

    # Signal 1: Branch prefix
    branch = session.get("branch", "")
    if branch.startswith("feat/"):
        scores["feature"] += 2
    elif branch.startswith("fix/"):
        scores["bugfix"] += 2
    elif branch.startswith("improvement/") or branch.startswith("refactor/"):
        scores["refactor"] += 2
    elif branch.startswith("chore/") or branch.startswith("test/"):
        scores["infra"] += 2
    elif branch in ("master", "main", ""):
        scores["ad-hoc"] += 1

    # Signal 2: First user message keywords
    msg = session.get("first_user_message", "")
    for cat, pattern in CATEGORY_KEYWORDS.items():
        if pattern.search(msg):
            scores[cat] += 1.5

    # Signal 3: Tool profile
    tools = session.get("tool_counts", {})
    total_tools = sum(tools.values()) or 1
    snowflake = tools.get("mcp__snowflake__run_snowflake_query", 0)
    edits = tools.get("Edit", 0) + tools.get("Write", 0)
    reads = tools.get("Read", 0) + tools.get("Grep", 0)

    if snowflake > 10 and edits < 5:
        scores["analytics"] += 2
    if edits > 10:
        scores["feature"] += 1
    if reads > 20 and edits < 5:
        scores["investigation"] += 1.5

    # Signal 4: Files edited
    file_layers = session.get("file_layers_edited", {})
    if file_layers.get("marts", 0) > 0 or file_layers.get("models", 0) > 0:
        scores["feature"] += 0.5

    if not scores:
        return "ad-hoc"

    return max(scores, key=scores.get)


def parse_session(filepath: str) -> dict | None:
    """Parse a single JSONL transcript file into session metadata."""
    file_size = os.path.getsize(filepath)
    if file_size < MIN_FILE_SIZE:
        return None

    events = []
    try:
        with open(filepath) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        return None

    if not events:
        return None

    # --- Basic metadata ---
    session_id = None
    branches_seen = set()
    timestamps = []
    tool_id_to_name: dict[str, str] = {}
    tool_counts: dict[str, int] = collections.defaultdict(int)
    tool_errors: dict[str, int] = collections.defaultdict(int)
    total_tool_calls = 0
    total_tool_errors = 0
    bash_categories: dict[str, int] = collections.defaultdict(int)
    files_edited: list[str] = []
    files_read: list[str] = []
    file_layers_edited: dict[str, int] = collections.defaultdict(int)
    mcp_usage: dict[str, int] = collections.defaultdict(int)
    skills_used: list[str] = []
    agents_used: list[str] = []
    user_msg_count = 0
    assistant_msg_count = 0
    first_user_message = ""
    last_user_message = ""

    for event in events:
        if event.get("isSidechain"):
            continue

        # Session ID
        if not session_id:
            session_id = event.get("sessionId")

        # Branch
        branch = event.get("gitBranch")
        if branch:
            branches_seen.add(branch)

        # Timestamp
        ts_str = event.get("timestamp")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                timestamps.append(ts)
            except (ValueError, TypeError):
                pass

        event_type = event.get("type")
        message = event.get("message", {})

        # --- Assistant events ---
        if event_type == "assistant" and isinstance(message, dict):
            assistant_msg_count += 1
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "unknown")
                        tool_id = block.get("id", "")
                        tool_id_to_name[tool_id] = tool_name
                        tool_counts[tool_name] += 1
                        total_tool_calls += 1

                        # MCP grouping
                        mcp_group = classify_mcp(tool_name)
                        if mcp_group:
                            mcp_usage[mcp_group] += 1

                        # Bash command classification
                        tool_input = block.get("input", {})
                        if tool_name == "Bash" and isinstance(tool_input, dict):
                            cmd = tool_input.get("command", "")
                            if cmd:
                                cat = classify_bash_command(cmd)
                                bash_categories[cat] += 1

                        # File tracking
                        if tool_name in ("Edit", "Write") and isinstance(tool_input, dict):
                            fp = tool_input.get("file_path", "")
                            if fp:
                                files_edited.append(fp)
                                layer = classify_file_layer(fp)
                                file_layers_edited[layer] += 1
                        elif tool_name == "Read" and isinstance(tool_input, dict):
                            fp = tool_input.get("file_path", "")
                            if fp:
                                files_read.append(fp)

                        # Skill/Agent tracking
                        if tool_name == "Skill" and isinstance(tool_input, dict):
                            skill = tool_input.get("skill", "")
                            if skill:
                                skills_used.append(skill)
                        elif tool_name == "Agent" and isinstance(tool_input, dict):
                            agent_type = tool_input.get("subagent_type", "general")
                            skills_used.append(f"agent:{agent_type}")

        # --- User events ---
        elif event_type == "user":
            content = message if isinstance(message, (str, dict)) else ""

            # Plain text user message
            if isinstance(content, str) and content.strip():
                if content.startswith("---") or content.startswith("<system"):
                    continue
                user_msg_count += 1
                if not first_user_message:
                    first_user_message = content[:MSG_PREVIEW_CHARS]
                last_user_message = content[:MSG_PREVIEW_CHARS]

            # List content with possible tool_results
            elif isinstance(content, dict):
                inner = content.get("content", "")
                if isinstance(inner, str) and inner.strip():
                    if not inner.startswith("---") and not inner.startswith("<system"):
                        user_msg_count += 1
                        if not first_user_message:
                            first_user_message = inner[:MSG_PREVIEW_CHARS]
                        last_user_message = inner[:MSG_PREVIEW_CHARS]
                elif isinstance(inner, list):
                    for block in inner:
                        if isinstance(block, dict):
                            if block.get("type") == "tool_result":
                                tool_id = block.get("tool_use_id", "")
                                tool_name = tool_id_to_name.get(tool_id, "unknown")
                                if block.get("is_error"):
                                    tool_errors[tool_name] += 1
                                    total_tool_errors += 1
                            elif block.get("type") == "text":
                                text = block.get("text", "")
                                if text.strip() and not text.startswith("<system"):
                                    user_msg_count += 1
                                    if not first_user_message:
                                        first_user_message = text[:MSG_PREVIEW_CHARS]
                                    last_user_message = text[:MSG_PREVIEW_CHARS]

    if not timestamps:
        return None

    # --- Timing ---
    timestamps.sort()
    started_at = timestamps[0]
    ended_at = timestamps[-1]
    wall_duration = (ended_at - started_at).total_seconds() / 60

    # Active time: sum of burst durations (gap < 10min)
    active_seconds = 0
    burst_start = timestamps[0]
    prev_ts = timestamps[0]
    for ts in timestamps[1:]:
        gap = (ts - prev_ts).total_seconds()
        if gap > BURST_GAP_SECONDS:
            active_seconds += (prev_ts - burst_start).total_seconds()
            burst_start = ts
        prev_ts = ts
    active_seconds += (prev_ts - burst_start).total_seconds()
    # Add minimum 30s per burst to account for thinking time
    active_minutes = max(active_seconds / 60, 0.5)

    # --- Branch resolution ---
    branch = extract_branch_from_transcript(events) or ""
    ticket_id = extract_ticket_id(branch) if branch else None

    # Autonomy ratio
    autonomy_ratio = (total_tool_calls / user_msg_count) if user_msg_count > 0 else 0

    # Error rate
    error_rate = (total_tool_errors / total_tool_calls) if total_tool_calls > 0 else 0

    session = {
        "session_id": session_id or os.path.basename(filepath).replace(".jsonl", ""),
        "file": os.path.basename(filepath),
        "file_size_kb": round(file_size / 1024, 1),
        "branch": branch,
        "branches_seen": sorted(branches_seen),
        "ticket_id": ticket_id,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "wall_duration_minutes": round(wall_duration, 1),
        "active_minutes": round(active_minutes, 1),
        "event_count": len(events),
        "user_msg_count": user_msg_count,
        "assistant_msg_count": assistant_msg_count,
        "total_tool_calls": total_tool_calls,
        "total_tool_errors": total_tool_errors,
        "error_rate": round(error_rate, 3),
        "autonomy_ratio": round(autonomy_ratio, 2),
        "tool_counts": dict(sorted(tool_counts.items(), key=lambda x: -x[1])),
        "tool_errors": dict(sorted(tool_errors.items(), key=lambda x: -x[1])) if tool_errors else {},
        "bash_categories": dict(sorted(bash_categories.items(), key=lambda x: -x[1])),
        "mcp_usage": dict(sorted(mcp_usage.items(), key=lambda x: -x[1])),
        "files_edited": list(set(files_edited)),
        "files_read_count": len(set(files_read)),
        "file_layers_edited": dict(file_layers_edited),
        "skills_used": list(set(skills_used)),
        "first_user_message": first_user_message,
        "last_user_message": last_user_message,
        "task_category": "",  # filled next
        "hour_of_day": started_at.hour,
        "day_of_week": started_at.strftime("%A"),
    }

    session["task_category"] = classify_task(session)
    return session


def compute_aggregates(sessions: list[dict]) -> dict:
    """Compute aggregate analysis across all sessions."""

    # --- Time by category ---
    time_by_category: dict[str, float] = collections.defaultdict(float)
    sessions_by_category: dict[str, int] = collections.defaultdict(int)
    for s in sessions:
        cat = s["task_category"]
        time_by_category[cat] += s["active_minutes"]
        sessions_by_category[cat] += 1

    total_active = sum(time_by_category.values()) or 1
    time_by_category_pct = {k: round(v / total_active * 100, 1) for k, v in time_by_category.items()}

    # --- Effort by ticket ---
    ticket_effort: dict[str, dict] = {}
    for s in sessions:
        tid = s["ticket_id"]
        if not tid:
            continue
        if tid not in ticket_effort:
            ticket_effort[tid] = {"sessions": 0, "active_minutes": 0, "category": s["task_category"], "first_message": s["first_user_message"][:100]}
        ticket_effort[tid]["sessions"] += 1
        ticket_effort[tid]["active_minutes"] += s["active_minutes"]

    # Sort by effort
    ticket_effort = dict(sorted(ticket_effort.items(), key=lambda x: -x[1]["active_minutes"]))

    # --- Effort by ticket prefix ---
    prefix_effort: dict[str, list[float]] = collections.defaultdict(list)
    for s in sessions:
        tid = s["ticket_id"] or ""
        match = re.match(r"^([A-Z]{2,6})-\d+", tid)
        if match:
            prefix = match.group(1)
            prefix_effort[prefix].append(s["active_minutes"])

    prefix_stats = {}
    for prefix, times in prefix_effort.items():
        times.sort()
        n = len(times)
        prefix_stats[prefix] = {
            "count": n,
            "total_minutes": round(sum(times), 1),
            "median_minutes": round(times[n // 2], 1),
            "p75_minutes": round(times[int(n * 0.75)] if n > 1 else times[0], 1),
            "max_minutes": round(max(times), 1),
        }

    # --- Tool usage overall ---
    tool_totals: dict[str, int] = collections.defaultdict(int)
    for s in sessions:
        for tool, count in s["tool_counts"].items():
            tool_totals[tool] += count
    tool_totals = dict(sorted(tool_totals.items(), key=lambda x: -x[1])[:30])

    # --- Tool usage by category ---
    tool_by_category: dict[str, dict[str, int]] = {}
    for s in sessions:
        cat = s["task_category"]
        if cat not in tool_by_category:
            tool_by_category[cat] = collections.defaultdict(int)
        for tool, count in s["tool_counts"].items():
            tool_by_category[cat][tool] += count
    # Keep top 10 per category
    for cat in tool_by_category:
        d = tool_by_category[cat]
        tool_by_category[cat] = dict(sorted(d.items(), key=lambda x: -x[1])[:10])

    # --- Error hotspots ---
    error_totals: dict[str, dict] = collections.defaultdict(lambda: {"errors": 0, "calls": 0})
    for s in sessions:
        for tool, count in s["tool_counts"].items():
            error_totals[tool]["calls"] += count
        for tool, count in s["tool_errors"].items():
            error_totals[tool]["errors"] += count
    error_hotspots = []
    for tool, d in error_totals.items():
        if d["calls"] >= 5:  # minimum sample
            rate = d["errors"] / d["calls"]
            if rate > 0.05:  # 5%+ error rate
                error_hotspots.append({
                    "tool": tool,
                    "calls": d["calls"],
                    "errors": d["errors"],
                    "error_rate": round(rate, 3),
                })
    error_hotspots.sort(key=lambda x: -x["error_rate"])

    # --- File hotspots ---
    file_edit_counts: dict[str, int] = collections.defaultdict(int)
    for s in sessions:
        for fp in s["files_edited"]:
            file_edit_counts[fp] += 1
    file_hotspots = dict(sorted(file_edit_counts.items(), key=lambda x: -x[1])[:20])

    # --- Frustration signals ---
    frustration_sessions = []
    for s in sessions:
        reasons = []
        if s["error_rate"] > 0.15 and s["total_tool_calls"] > 10:
            reasons.append(f"high error rate ({s['error_rate']:.0%})")
        if s["autonomy_ratio"] < 1.5 and s["user_msg_count"] > 10:
            reasons.append(f"low autonomy ({s['autonomy_ratio']:.1f} tools/msg)")
        if s["active_minutes"] > 60:
            reasons.append(f"long session ({s['active_minutes']:.0f} min)")
        if reasons:
            frustration_sessions.append({
                "session_id": s["session_id"],
                "ticket_id": s["ticket_id"],
                "branch": s["branch"],
                "active_minutes": s["active_minutes"],
                "reasons": reasons,
                "first_message": s["first_user_message"][:150],
                "task_category": s["task_category"],
            })
    frustration_sessions.sort(key=lambda x: len(x["reasons"]), reverse=True)

    # --- Repeated patterns ---
    # Group by normalized first message keywords
    def normalize_msg(msg: str) -> str:
        msg = re.sub(r"[^a-z0-9\s]", "", msg.lower())
        words = [w for w in msg.split() if len(w) > 2]
        return " ".join(sorted(set(words[:10])))

    msg_groups: dict[str, list[dict]] = collections.defaultdict(list)
    for s in sessions:
        key = normalize_msg(s["first_user_message"])
        if key:
            msg_groups[key].append(s)

    repeated_patterns = []
    for key, group in msg_groups.items():
        if len(group) >= 2:
            avg_duration = sum(s["active_minutes"] for s in group) / len(group)
            has_automation = any(s["skills_used"] for s in group)
            repeated_patterns.append({
                "pattern": group[0]["first_user_message"][:120],
                "count": len(group),
                "avg_minutes": round(avg_duration, 1),
                "total_minutes": round(sum(s["active_minutes"] for s in group), 1),
                "has_automation": has_automation,
                "tickets": list(set(s["ticket_id"] for s in group if s["ticket_id"])),
            })
    repeated_patterns.sort(key=lambda x: -x["total_minutes"])

    # --- Automation coverage ---
    sessions_with_skills = sum(1 for s in sessions if s["skills_used"])
    sessions_with_agents = sum(1 for s in sessions if any("agent:" in sk for sk in s["skills_used"]))

    # --- MCP utilization ---
    mcp_totals: dict[str, int] = collections.defaultdict(int)
    mcp_session_counts: dict[str, int] = collections.defaultdict(int)
    for s in sessions:
        for mcp, count in s["mcp_usage"].items():
            mcp_totals[mcp] += count
            mcp_session_counts[mcp] += 1

    # --- Daily/weekly rhythm ---
    sessions_by_hour: dict[int, int] = collections.defaultdict(int)
    active_by_hour: dict[int, float] = collections.defaultdict(float)
    sessions_by_day: dict[str, int] = collections.defaultdict(int)
    active_by_day: dict[str, float] = collections.defaultdict(float)
    for s in sessions:
        h = s["hour_of_day"]
        d = s["day_of_week"]
        sessions_by_hour[h] += 1
        active_by_hour[h] += s["active_minutes"]
        sessions_by_day[d] += 1
        active_by_day[d] += s["active_minutes"]

    # --- Bash command breakdown ---
    bash_totals: dict[str, int] = collections.defaultdict(int)
    for s in sessions:
        for cat, count in s["bash_categories"].items():
            bash_totals[cat] += count
    bash_totals = dict(sorted(bash_totals.items(), key=lambda x: -x[1]))

    # --- Workflow sequences (top 3-tool chains) ---
    sequence_counts: dict[tuple, int] = collections.defaultdict(int)
    for s in sessions:
        # Rebuild tool sequence from tool_counts (approximation: we don't have ordering,
        # so we use the most common tools as the session's "signature")
        top_tools = list(s["tool_counts"].keys())[:5]
        for i in range(len(top_tools) - 2):
            seq = tuple(top_tools[i:i + 3])
            sequence_counts[seq] += 1
    top_sequences = sorted(sequence_counts.items(), key=lambda x: -x[1])[:15]

    return {
        "total_sessions": len(sessions),
        "total_active_hours": round(total_active / 60, 1),
        "avg_session_minutes": round(total_active / len(sessions), 1) if sessions else 0,
        "date_range": {
            "earliest": min(s["started_at"] for s in sessions),
            "latest": max(s["ended_at"] for s in sessions),
        },
        "time_by_category": dict(sorted(time_by_category.items(), key=lambda x: -x[1])),
        "time_by_category_pct": dict(sorted(time_by_category_pct.items(), key=lambda x: -x[1])),
        "sessions_by_category": dict(sorted(sessions_by_category.items(), key=lambda x: -x[1])),
        "ticket_effort": ticket_effort,
        "ticket_prefix_stats": prefix_stats,
        "tool_totals": tool_totals,
        "tool_by_category": tool_by_category,
        "error_hotspots": error_hotspots,
        "file_hotspots": file_hotspots,
        "frustration_sessions": frustration_sessions[:20],
        "repeated_patterns": repeated_patterns[:20],
        "automation_coverage": {
            "total_sessions": len(sessions),
            "with_skills": sessions_with_skills,
            "with_agents": sessions_with_agents,
            "coverage_pct": round(sessions_with_skills / len(sessions) * 100, 1) if sessions else 0,
        },
        "mcp_usage": {
            "totals": dict(sorted(mcp_totals.items(), key=lambda x: -x[1])),
            "session_counts": dict(sorted(mcp_session_counts.items(), key=lambda x: -x[1])),
        },
        "rhythm": {
            "by_hour": {str(h): {"sessions": sessions_by_hour[h], "active_minutes": round(active_by_hour[h], 1)} for h in sorted(sessions_by_hour)},
            "by_day": {d: {"sessions": sessions_by_day[d], "active_minutes": round(active_by_day[d], 1)} for d in ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] if d in sessions_by_day},
        },
        "bash_breakdown": bash_totals,
        "top_sequences": [{"tools": list(seq), "count": count} for seq, count in top_sequences],
    }


def generate_dashboard(sessions: list[dict], analysis: dict, output_path: str):
    """Generate a self-contained HTML dashboard."""

    sessions_json = json.dumps(sessions, default=str)
    analysis_json = json.dumps(analysis, default=str)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Claude Code Session Analysis</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e1e4e8; padding: 24px; }}
  h1 {{ font-size: 24px; font-weight: 600; margin-bottom: 8px; color: #fff; }}
  h2 {{ font-size: 16px; font-weight: 600; margin-bottom: 12px; color: #c9d1d9; }}
  .subtitle {{ color: #8b949e; font-size: 14px; margin-bottom: 24px; }}
  .grid {{ display: grid; gap: 20px; margin-bottom: 24px; }}
  .grid-4 {{ grid-template-columns: repeat(4, 1fr); }}
  .grid-2 {{ grid-template-columns: repeat(2, 1fr); }}
  .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }}
  .stat-card {{ text-align: center; }}
  .stat-value {{ font-size: 32px; font-weight: 700; color: #58a6ff; }}
  .stat-label {{ font-size: 12px; color: #8b949e; margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }}
  .chart-container {{ position: relative; height: 300px; }}
  .chart-container-tall {{ position: relative; height: 400px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th {{ text-align: left; padding: 8px 12px; border-bottom: 2px solid #30363d; color: #8b949e; font-weight: 600; cursor: pointer; user-select: none; }}
  th:hover {{ color: #58a6ff; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; }}
  tr:hover td {{ background: #1c2128; }}
  .tag {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 500; }}
  .tag-feature {{ background: #1f3a5f; color: #58a6ff; }}
  .tag-bugfix {{ background: #4a1e1e; color: #f85149; }}
  .tag-analytics {{ background: #2d1f4e; color: #bc8cff; }}
  .tag-investigation {{ background: #3b2e1a; color: #d29922; }}
  .tag-refactor {{ background: #1a3a2a; color: #3fb950; }}
  .tag-infra {{ background: #2d2d2d; color: #8b949e; }}
  .tag-ad-hoc {{ background: #2d2a1f; color: #d29922; }}
  .tag-review {{ background: #1f2d3a; color: #79c0ff; }}
  .frustration {{ color: #f85149; }}
  .section {{ margin-bottom: 32px; }}
  @media (max-width: 900px) {{ .grid-4 {{ grid-template-columns: repeat(2, 1fr); }} .grid-2 {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>

<h1>Claude Code Session Analysis</h1>
<p class="subtitle">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} &mdash; {analysis['total_sessions']} sessions analyzed</p>

<!-- Overview Cards -->
<div class="grid grid-4 section">
  <div class="card stat-card">
    <div class="stat-value">{analysis['total_sessions']}</div>
    <div class="stat-label">Total Sessions</div>
  </div>
  <div class="card stat-card">
    <div class="stat-value">{analysis['total_active_hours']}</div>
    <div class="stat-label">Active Hours</div>
  </div>
  <div class="card stat-card">
    <div class="stat-value">{analysis['avg_session_minutes']}</div>
    <div class="stat-label">Avg Minutes/Session</div>
  </div>
  <div class="card stat-card">
    <div class="stat-value">{analysis['automation_coverage']['coverage_pct']}%</div>
    <div class="stat-label">Automation Coverage</div>
  </div>
</div>

<!-- Time Allocation + Tool Usage -->
<div class="grid grid-2 section">
  <div class="card">
    <h2>Time Allocation by Task Type</h2>
    <div class="chart-container">
      <canvas id="timeChart"></canvas>
    </div>
  </div>
  <div class="card">
    <h2>Top Tools</h2>
    <div class="chart-container">
      <canvas id="toolChart"></canvas>
    </div>
  </div>
</div>

<!-- Effort by Ticket -->
<div class="card section">
  <h2>Top Tickets by Effort</h2>
  <div class="chart-container-tall">
    <canvas id="ticketChart"></canvas>
  </div>
</div>

<!-- Daily Rhythm -->
<div class="grid grid-2 section">
  <div class="card">
    <h2>Activity by Hour of Day</h2>
    <div class="chart-container">
      <canvas id="hourChart"></canvas>
    </div>
  </div>
  <div class="card">
    <h2>Activity by Day of Week</h2>
    <div class="chart-container">
      <canvas id="dayChart"></canvas>
    </div>
  </div>
</div>

<!-- Ticket Prefix Effort -->
<div class="card section">
  <h2>Effort by Ticket Prefix</h2>
  <div class="chart-container">
    <canvas id="prefixChart"></canvas>
  </div>
</div>

<!-- Frustration Signals -->
<div class="card section">
  <h2>Frustration Signals</h2>
  <table id="frustrationTable">
    <thead>
      <tr>
        <th onclick="sortTable('frustrationTable', 0)">Ticket</th>
        <th onclick="sortTable('frustrationTable', 1)">Branch</th>
        <th onclick="sortTable('frustrationTable', 2)">Category</th>
        <th onclick="sortTable('frustrationTable', 3, true)">Active Min</th>
        <th onclick="sortTable('frustrationTable', 4)">Issues</th>
        <th onclick="sortTable('frustrationTable', 5)">First Message</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
</div>

<!-- Repeated Patterns -->
<div class="card section">
  <h2>Repeated Patterns</h2>
  <table id="patternsTable">
    <thead>
      <tr>
        <th onclick="sortTable('patternsTable', 0)">Pattern</th>
        <th onclick="sortTable('patternsTable', 1, true)">Count</th>
        <th onclick="sortTable('patternsTable', 2, true)">Avg Min</th>
        <th onclick="sortTable('patternsTable', 3, true)">Total Min</th>
        <th onclick="sortTable('patternsTable', 4)">Automated?</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
</div>

<!-- Error Hotspots -->
<div class="card section">
  <h2>Error Hotspots</h2>
  <table id="errorTable">
    <thead>
      <tr>
        <th onclick="sortTable('errorTable', 0)">Tool</th>
        <th onclick="sortTable('errorTable', 1, true)">Calls</th>
        <th onclick="sortTable('errorTable', 2, true)">Errors</th>
        <th onclick="sortTable('errorTable', 3, true)">Error Rate</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
</div>

<!-- MCP Usage -->
<div class="grid grid-2 section">
  <div class="card">
    <h2>MCP Server Usage</h2>
    <div class="chart-container">
      <canvas id="mcpChart"></canvas>
    </div>
  </div>
  <div class="card">
    <h2>Bash Command Breakdown</h2>
    <div class="chart-container">
      <canvas id="bashChart"></canvas>
    </div>
  </div>
</div>

<!-- File Hotspots -->
<div class="card section">
  <h2>Most Edited Files</h2>
  <table id="fileTable">
    <thead>
      <tr>
        <th>File</th>
        <th onclick="sortTable('fileTable', 1, true)">Edit Sessions</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
</div>

<script>
const sessions = {sessions_json};
const analysis = {analysis_json};

// Color palette
const COLORS = {{
  feature: '#58a6ff', bugfix: '#f85149', analytics: '#bc8cff',
  investigation: '#d29922', refactor: '#3fb950', infra: '#8b949e',
  'ad-hoc': '#d2a822', review: '#79c0ff'
}};
const CHART_COLORS = ['#58a6ff','#f85149','#bc8cff','#d29922','#3fb950','#79c0ff','#f0883e','#8b949e','#db61a2','#7ee787'];

Chart.defaults.color = '#8b949e';
Chart.defaults.borderColor = '#30363d';
Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";

// --- Time Allocation Donut ---
const timeCats = Object.entries(analysis.time_by_category).sort((a,b) => b[1]-a[1]);
new Chart(document.getElementById('timeChart'), {{
  type: 'doughnut',
  data: {{
    labels: timeCats.map(([k,v]) => k + ' (' + analysis.time_by_category_pct[k] + '%)'),
    datasets: [{{ data: timeCats.map(([k,v]) => Math.round(v)), backgroundColor: timeCats.map(([k]) => COLORS[k] || '#8b949e') }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ position: 'right' }} }} }}
}});

// --- Tool Usage Bar ---
const toolEntries = Object.entries(analysis.tool_totals).slice(0, 15);
new Chart(document.getElementById('toolChart'), {{
  type: 'bar',
  data: {{
    labels: toolEntries.map(([k]) => k.replace('mcp__','').replace('__',' ')),
    datasets: [{{ data: toolEntries.map(([,v]) => v), backgroundColor: '#58a6ff' }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, indexAxis: 'y', plugins: {{ legend: {{ display: false }} }} }}
}});

// --- Ticket Effort Bar ---
const ticketEntries = Object.entries(analysis.ticket_effort).slice(0, 15);
new Chart(document.getElementById('ticketChart'), {{
  type: 'bar',
  data: {{
    labels: ticketEntries.map(([k]) => k),
    datasets: [{{
      label: 'Active Minutes',
      data: ticketEntries.map(([,v]) => Math.round(v.active_minutes)),
      backgroundColor: ticketEntries.map(([,v]) => COLORS[v.category] || '#8b949e')
    }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, indexAxis: 'y', plugins: {{ legend: {{ display: false }} }} }}
}});

// --- Hour of Day ---
const hours = analysis.rhythm.by_hour;
const hourLabels = Array.from({{length: 24}}, (_, i) => i);
new Chart(document.getElementById('hourChart'), {{
  type: 'bar',
  data: {{
    labels: hourLabels.map(h => h + ':00'),
    datasets: [{{ data: hourLabels.map(h => hours[h] ? Math.round(hours[h].active_minutes) : 0), backgroundColor: '#58a6ff' }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ title: {{ display: true, text: 'Active Minutes' }} }} }} }}
}});

// --- Day of Week ---
const dayOrder = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday'];
const days = analysis.rhythm.by_day;
new Chart(document.getElementById('dayChart'), {{
  type: 'bar',
  data: {{
    labels: dayOrder.filter(d => days[d]),
    datasets: [{{ data: dayOrder.filter(d => days[d]).map(d => Math.round(days[d].active_minutes)), backgroundColor: '#bc8cff' }}]
  }},
  options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ y: {{ title: {{ display: true, text: 'Active Minutes' }} }} }} }}
}});

// --- Ticket Prefix ---
const prefixEntries = Object.entries(analysis.ticket_prefix_stats);
if (prefixEntries.length) {{
  new Chart(document.getElementById('prefixChart'), {{
    type: 'bar',
    data: {{
      labels: prefixEntries.map(([k]) => k),
      datasets: [
        {{ label: 'Median (min)', data: prefixEntries.map(([,v]) => v.median_minutes), backgroundColor: '#58a6ff' }},
        {{ label: 'P75 (min)', data: prefixEntries.map(([,v]) => v.p75_minutes), backgroundColor: '#bc8cff' }},
        {{ label: 'Max (min)', data: prefixEntries.map(([,v]) => v.max_minutes), backgroundColor: '#f85149' }}
      ]
    }},
    options: {{ responsive: true, maintainAspectRatio: false }}
  }});
}}

// --- MCP Usage ---
const mcpEntries = Object.entries(analysis.mcp_usage.totals);
if (mcpEntries.length) {{
  new Chart(document.getElementById('mcpChart'), {{
    type: 'bar',
    data: {{
      labels: mcpEntries.map(([k]) => k),
      datasets: [{{ data: mcpEntries.map(([,v]) => v), backgroundColor: CHART_COLORS }}]
    }},
    options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
  }});
}}

// --- Bash Breakdown ---
const bashEntries = Object.entries(analysis.bash_breakdown);
if (bashEntries.length) {{
  new Chart(document.getElementById('bashChart'), {{
    type: 'bar',
    data: {{
      labels: bashEntries.map(([k]) => k),
      datasets: [{{ data: bashEntries.map(([,v]) => v), backgroundColor: CHART_COLORS }}]
    }},
    options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }} }}
  }});
}}

// --- Populate Tables ---
function tagHtml(cat) {{
  return '<span class="tag tag-' + cat + '">' + cat + '</span>';
}}

// Frustration table
const fBody = document.querySelector('#frustrationTable tbody');
analysis.frustration_sessions.forEach(s => {{
  const tr = document.createElement('tr');
  tr.innerHTML = '<td>' + (s.ticket_id || '-') + '</td>'
    + '<td>' + (s.branch || '-').substring(0, 40) + '</td>'
    + '<td>' + tagHtml(s.task_category) + '</td>'
    + '<td>' + Math.round(s.active_minutes) + '</td>'
    + '<td class="frustration">' + s.reasons.join(', ') + '</td>'
    + '<td>' + s.first_message.substring(0, 80) + '</td>';
  fBody.appendChild(tr);
}});

// Repeated patterns table
const pBody = document.querySelector('#patternsTable tbody');
analysis.repeated_patterns.forEach(p => {{
  const tr = document.createElement('tr');
  tr.innerHTML = '<td>' + p.pattern.substring(0, 80) + '</td>'
    + '<td>' + p.count + '</td>'
    + '<td>' + p.avg_minutes + '</td>'
    + '<td>' + p.total_minutes + '</td>'
    + '<td>' + (p.has_automation ? 'Yes' : '<span class="frustration">No</span>') + '</td>';
  pBody.appendChild(tr);
}});

// Error hotspots table
const eBody = document.querySelector('#errorTable tbody');
analysis.error_hotspots.forEach(e => {{
  const tr = document.createElement('tr');
  tr.innerHTML = '<td>' + e.tool.replace('mcp__','').replace('__',' ') + '</td>'
    + '<td>' + e.calls + '</td>'
    + '<td>' + e.errors + '</td>'
    + '<td class="frustration">' + (e.error_rate * 100).toFixed(1) + '%</td>';
  eBody.appendChild(tr);
}});

// File hotspots table
const fileBody = document.querySelector('#fileTable tbody');
Object.entries(analysis.file_hotspots).forEach(([f, c]) => {{
  const tr = document.createElement('tr');
  tr.innerHTML = '<td>' + f + '</td><td>' + c + '</td>';
  fileBody.appendChild(tr);
}});

// --- Sortable tables ---
function sortTable(tableId, colIdx, isNum) {{
  const table = document.getElementById(tableId);
  const tbody = table.querySelector('tbody');
  const rows = Array.from(tbody.querySelectorAll('tr'));
  const dir = table.dataset.sortDir === 'asc' ? 'desc' : 'asc';
  table.dataset.sortDir = dir;
  rows.sort((a, b) => {{
    let va = a.cells[colIdx].textContent.trim();
    let vb = b.cells[colIdx].textContent.trim();
    if (isNum) {{ va = parseFloat(va) || 0; vb = parseFloat(vb) || 0; }}
    if (dir === 'asc') return va > vb ? 1 : -1;
    return va < vb ? 1 : -1;
  }});
  rows.forEach(r => tbody.appendChild(r));
}}
</script>
</body>
</html>"""

    with open(output_path, "w") as f:
        f.write(html)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Mine Claude Code session transcripts")
    parser.add_argument("--project-dir", default=None, help="Directory with JSONL transcripts")
    parser.add_argument("--output-dir", default=None, help="Output directory")
    parser.add_argument("--json", action="store_true", help="Output JSON only")
    parser.add_argument("--dashboard", action="store_true", help="Generate HTML dashboard")
    parser.add_argument("--verbose", action="store_true", help="Print per-session details")
    args = parser.parse_args()

    # Default project dir
    if args.project_dir:
        project_dir = args.project_dir
    else:
        # Find the largest project dir
        base = os.path.expanduser("~/.claude/projects")
        if os.path.isdir(base):
            candidates = []
            for d in os.listdir(base):
                dp = os.path.join(base, d)
                if os.path.isdir(dp):
                    jsonl_count = sum(1 for f in os.listdir(dp) if f.endswith(".jsonl"))
                    if jsonl_count > 0:
                        candidates.append((jsonl_count, dp))
            candidates.sort(reverse=True)
            if candidates:
                project_dir = candidates[0][1]
            else:
                print("No JSONL transcripts found in ~/.claude/projects/")
                sys.exit(1)
        else:
            print("~/.claude/projects/ not found")
            sys.exit(1)

    # Output dir
    output_dir = args.output_dir or os.path.expanduser("~/.claude/session_analysis")
    os.makedirs(output_dir, exist_ok=True)

    # Find all JSONL files
    jsonl_files = []
    for f in os.listdir(project_dir):
        if f.endswith(".jsonl"):
            fp = os.path.join(project_dir, f)
            jsonl_files.append(fp)
    jsonl_files.sort(key=lambda x: os.path.getmtime(x))

    print(f"Found {len(jsonl_files)} transcript files in {project_dir}")

    # Parse all sessions
    sessions = []
    skipped = 0
    for i, fp in enumerate(jsonl_files):
        if args.verbose:
            print(f"  [{i+1}/{len(jsonl_files)}] {os.path.basename(fp)}...", end=" ")
        session = parse_session(fp)
        if session:
            sessions.append(session)
            if args.verbose:
                print(f"{session['task_category']} | {session['active_minutes']}min | {session['ticket_id'] or 'no-ticket'}")
        else:
            skipped += 1
            if args.verbose:
                print("skipped (too small or empty)")

    print(f"Parsed {len(sessions)} sessions ({skipped} skipped)")

    # Compute aggregates
    analysis = compute_aggregates(sessions)

    # Write JSON
    sessions_path = os.path.join(output_dir, "sessions.json")
    analysis_path = os.path.join(output_dir, "analysis.json")
    with open(sessions_path, "w") as f:
        json.dump(sessions, f, indent=2, default=str)
    with open(analysis_path, "w") as f:
        json.dump(analysis, f, indent=2, default=str)
    print(f"Written: {sessions_path}")
    print(f"Written: {analysis_path}")

    # Generate dashboard
    if args.dashboard or not args.json:
        dashboard_path = os.path.join(output_dir, "dashboard.html")
        generate_dashboard(sessions, analysis, dashboard_path)
        print(f"Written: {dashboard_path}")
        print(f"\nOpen in browser: file://{dashboard_path}")

    # Print summary to stdout
    if not args.json:
        print(f"\n{'='*60}")
        print(f"SUMMARY")
        print(f"{'='*60}")
        print(f"Sessions: {analysis['total_sessions']} | Active hours: {analysis['total_active_hours']} | Avg session: {analysis['avg_session_minutes']} min")
        print(f"Date range: {analysis['date_range']['earliest'][:10]} to {analysis['date_range']['latest'][:10]}")
        print(f"\nTime by category:")
        for cat, mins in analysis["time_by_category"].items():
            pct = analysis["time_by_category_pct"][cat]
            count = analysis["sessions_by_category"][cat]
            print(f"  {cat:20s} {mins:7.0f} min ({pct:5.1f}%) — {count} sessions")
        print(f"\nAutomation: {analysis['automation_coverage']['with_skills']}/{analysis['total_sessions']} sessions used skills/commands ({analysis['automation_coverage']['coverage_pct']}%)")
        print(f"Frustration signals: {len(analysis['frustration_sessions'])} sessions flagged")
        print(f"Repeated patterns: {len(analysis['repeated_patterns'])} clusters found")


if __name__ == "__main__":
    main()
