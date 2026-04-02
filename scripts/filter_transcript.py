#!/usr/bin/env python3
"""Filter a Claude Code JSONL transcript to extract conversation-relevant content.

Usage:
  python3 filter_transcript.py <transcript_path> [--session-id <id>]
    Filter a transcript and output conversation text with metadata.
    If --session-id is provided, only include events from that session
    (incremental compile — much faster for multi-session transcripts).

  python3 filter_transcript.py --find-transcript <project_dir> <branch>
    Find the most recent JSONL transcript matching a branch. Prints path to stdout.

Exits silently (code 0) if no usable transcript found.
"""

import json
import os
import re
import subprocess
import sys

MAX_USER_MSG_CHARS = 1000
MAX_ASSISTANT_TEXT_CHARS = 4000
MIN_TRANSCRIPT_BYTES = 50_000
NON_TASK_BRANCHES = {"master", "main", "develop", "staging", "production"}

# Tool results worth preserving — contain evidence, data, investigation summaries
VALUABLE_TOOLS = {
    "mcp__snowflake__run_snowflake_query",
    "Bash",
    "Task",
    "TaskOutput",
    "Grep",
    "mcp__claude_ai_Slack__slack_read_thread",
    "mcp__claude_ai_Slack__slack_read_channel",
    "mcp__claude_ai_Slack__slack_search_public",
    "mcp__claude_ai_Slack__slack_search_public_and_private",
    "mcp__dbt-cloud__get_job_run_error",
    "mcp__dbt-cloud__get_job_details",
}
MAX_VALUABLE_TOOL_RESULT_CHARS = 2000
MAX_OTHER_TOOL_RESULT_CHARS = 200


def extract_ticket_id(branch: str) -> str | None:
    """Extract ticket ID (e.g., BPI-1051) from branch name.

    Looks for patterns like BPI-1051, PDA-2709, JIRA-123.
    Falls back to a slug of the branch name (after prefix like feat/, fix/, etc.).
    """
    # Standard ticket ID pattern: 2+ uppercase letters, dash, digits
    match = re.search(r"[A-Za-z]{2,}-\d+", branch)
    if match:
        candidate = match.group(0).upper()
        # Avoid false positives from branch slugs (e.g., "prep-frontend" → "PREP-1")
        # Ticket IDs typically have the letters as a short prefix (2-5 chars)
        prefix = candidate.split("-")[0]
        if len(prefix) <= 6:
            return candidate

    # Fallback: slug the branch name (strip common prefixes)
    slug = re.sub(r"^(feat|fix|improvement|refactor|chore|test|hotfix)/", "", branch)
    slug = slug.strip("/").replace("/", "-")
    if slug in NON_TASK_BRANCHES:
        return None
    return slug if slug else None


def extract_branch_from_transcript(lines: list[dict]) -> str | None:
    """Get the git branch from the last non-sidechain user message.

    Sessions can span multiple branches (e.g., worktree switches).
    The last user message's branch best represents the session's final context.
    """
    last_branch = None
    for event in lines:
        if event.get("isSidechain"):
            continue
        if event.get("type") == "user":
            branch = event.get("gitBranch")
            if branch:
                last_branch = branch
    # Fall back to last branch from any non-sidechain event
    if not last_branch:
        for event in lines:
            if not event.get("isSidechain"):
                branch = event.get("gitBranch")
                if branch:
                    last_branch = branch
    return last_branch


def get_session_id(lines: list[dict]) -> str | None:
    """Get session ID from transcript metadata."""
    for event in lines:
        sid = event.get("sessionId")
        if sid:
            return sid
    return None


def find_existing_context(ticket_id: str) -> str | None:
    """Find existing context file for this ticket in global ~/.claude/session_context/.

    Search order:
    1. Files named {ticket_id}_*.md (e.g., BPI-1051_expert-advice-chargebee-migration.md)
    2. Files where ticket_id appears in the Tickets: header line (handles epic files)
    """
    context_dir = os.path.join(os.path.expanduser("~"), ".claude", "session_context")
    if not os.path.isdir(context_dir):
        return None

    try:
        # Search 1: filename prefix match
        import glob as glob_mod
        matches = glob_mod.glob(os.path.join(context_dir, f"{ticket_id}_*.md"))
        if matches:
            with open(matches[0]) as f:
                return f.read()

        # Search 2: Tickets: header line contains ticket_id
        for fname in os.listdir(context_dir):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(context_dir, fname)
            try:
                with open(fpath) as f:
                    for line in f:
                        if line.startswith("Tickets:"):
                            if ticket_id in line:
                                f.seek(0)
                                return f.read()
                            break
                        if not line.strip() or line.startswith("#"):
                            break
            except Exception:
                continue
    except Exception:
        pass
    return None


def filter_transcript(lines: list[dict]) -> list[str]:
    """Extract user messages, assistant text, and tool results from JSONL events."""
    output = []
    # Track tool names from assistant tool_use blocks so we can label results
    tool_id_to_name: dict[str, str] = {}

    for event in lines:
        # Skip sidechain messages (subagent work)
        if event.get("isSidechain"):
            continue

        event_type = event.get("type")
        message = event.get("message", {})

        if event_type == "assistant" and isinstance(message, dict):
            content = message.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = block.get("text", "")[:MAX_ASSISTANT_TEXT_CHARS]
                        if text.strip():
                            output.append(f"ASSISTANT: {text}")
                    elif block.get("type") == "tool_use":
                        tool_id_to_name[block.get("id", "")] = block.get("name", "unknown")

        elif event_type == "user" and isinstance(message, dict):
            content = message.get("content", "")

            # Plain text user message
            if isinstance(content, str) and content.strip():
                if content.startswith("---") or content.startswith("<system"):
                    continue
                text = content[:MAX_USER_MSG_CHARS]
                output.append(f"USER: {text}")

            # List content — may contain tool_result blocks
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id", "")
                        tool_name = tool_id_to_name.get(tool_id, "unknown")

                        # Determine truncation limit based on tool value
                        if tool_name in VALUABLE_TOOLS:
                            limit = MAX_VALUABLE_TOOL_RESULT_CHARS
                        else:
                            limit = MAX_OTHER_TOOL_RESULT_CHARS

                        # Extract text from result content
                        rc = block.get("content", "")
                        if isinstance(rc, list):
                            text = "\n".join(
                                x.get("text", "") if isinstance(x, dict) else str(x)
                                for x in rc
                            )
                        else:
                            text = str(rc)

                        text = text[:limit].strip()
                        if text and len(text) > 20:  # skip trivial results
                            output.append(f"TOOL_RESULT ({tool_name}): {text}")

    return output


def get_last_branch_fast(filepath: str) -> str | None:
    """Get the last user-message branch from a JSONL file without loading it all into memory.

    Reads last 200KB of the file (enough to find the last user message).
    """
    try:
        file_size = os.path.getsize(filepath)
        with open(filepath) as f:
            # Read from near the end for efficiency on large files
            if file_size > 200_000:
                f.seek(file_size - 200_000)
                f.readline()  # skip partial line

            last_branch = None
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("isSidechain"):
                    continue
                if obj.get("type") == "user":
                    branch = obj.get("gitBranch")
                    if branch:
                        last_branch = branch
            return last_branch
    except Exception:
        return None


def find_transcript(
    project_dir: str,
    target_branch: str,
    fallback_branches: list[str] | None = None,
) -> str | None:
    """Find the most recent JSONL transcript matching a branch.

    Tries target_branch first, then each fallback_branch in order.
    This handles worktree workflows where Claude Code launches from the main
    repo (branch=master) but work happens on a feature branch in a worktree.
    Scans at most 10 files (sorted by mtime, newest first).
    """
    if not os.path.isdir(project_dir):
        return None

    jsonl_files = []
    for f in os.listdir(project_dir):
        if f.endswith(".jsonl"):
            path = os.path.join(project_dir, f)
            jsonl_files.append((os.path.getmtime(path), path))

    # Sort by mtime descending (newest first)
    jsonl_files.sort(reverse=True)

    candidates = jsonl_files[:10]

    # Try each branch in priority order: target first, then fallbacks
    branches_to_try = [target_branch]
    if fallback_branches:
        branches_to_try.extend(b for b in fallback_branches if b != target_branch)

    for branch_candidate in branches_to_try:
        for _, path in candidates:
            if os.path.getsize(path) < MIN_TRANSCRIPT_BYTES:
                continue
            branch = get_last_branch_fast(path)
            if branch == branch_candidate:
                return path

    return None


def main():
    if len(sys.argv) < 2:
        sys.exit(0)

    # --find-transcript mode: find most recent transcript for a branch
    # Usage: --find-transcript <project_dir> <target_branch> [fallback_branch ...]
    if sys.argv[1] == "--find-transcript":
        if len(sys.argv) < 4:
            sys.exit(0)
        project_dir = sys.argv[2]
        target_branch = sys.argv[3]
        fallback_branches = sys.argv[4:] if len(sys.argv) > 4 else None
        result = find_transcript(project_dir, target_branch, fallback_branches)
        if result:
            print(result)
        sys.exit(0)

    # --find-latest mode: find the most recent transcript regardless of branch
    # Usage: --find-latest <project_dir>
    if sys.argv[1] == "--find-latest":
        if len(sys.argv) < 3:
            sys.exit(0)
        project_dir = sys.argv[2]
        if os.path.isdir(project_dir):
            jsonl_files = []
            for f in os.listdir(project_dir):
                if f.endswith(".jsonl"):
                    path = os.path.join(project_dir, f)
                    if os.path.getsize(path) >= MIN_TRANSCRIPT_BYTES:
                        jsonl_files.append((os.path.getmtime(path), path))
            jsonl_files.sort(reverse=True)
            if jsonl_files:
                print(jsonl_files[0][1])
        sys.exit(0)

    # Parse args: <transcript_path> [--session-id <id>]
    transcript_path = sys.argv[1]
    session_id_filter = None
    if "--session-id" in sys.argv:
        idx = sys.argv.index("--session-id")
        if idx + 1 < len(sys.argv):
            session_id_filter = sys.argv[idx + 1]

    if not transcript_path or not os.path.isfile(transcript_path):
        sys.exit(0)

    # Check file size
    file_size = os.path.getsize(transcript_path)
    if file_size < MIN_TRANSCRIPT_BYTES:
        sys.exit(0)

    # Parse JSONL
    lines = []
    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    if not lines:
        sys.exit(0)

    # Extract metadata from ALL lines (branch, ticket, session)
    branch = extract_branch_from_transcript(lines)
    if not branch:
        branch = "unknown"

    ticket_id = extract_ticket_id(branch) or "unknown"

    session_id = get_session_id(lines)

    # If session ID filter provided, only keep events from that session
    # (incremental compile — existing context already covers prior sessions)
    if session_id_filter:
        lines = [e for e in lines if e.get("sessionId") == session_id_filter]
        if not lines:
            sys.exit(0)

    # Filter conversation
    filtered = filter_transcript(lines)
    if not filtered:
        sys.exit(0)

    # Build output
    # Metadata header (machine-parseable first line)
    print(f"TICKET:{ticket_id} BRANCH:{branch} SESSION:{session_id or 'unknown'}")
    print()

    # Existing context (if any)
    existing = find_existing_context(ticket_id)
    if existing:
        print("--- EXISTING CONTEXT ---")
        print(existing)
        print("--- END EXISTING CONTEXT ---")
        print()

    # Filtered conversation
    print("--- SESSION TRANSCRIPT ---")
    for line in filtered:
        print(line)
        print()
    print("--- END SESSION TRANSCRIPT ---")


if __name__ == "__main__":
    main()
