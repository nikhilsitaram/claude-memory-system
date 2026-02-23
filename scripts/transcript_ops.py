#!/usr/bin/env python3
"""
Transcript parsing and extraction for Claude Code Memory System.

Split from indexing.py to reduce file size for faster reads.

Provides:
1. JSONL transcript parsing (extract assistant messages, filter noise)
2. Transcript extraction (group by day, format for synthesis)
3. Pending days calculation

Requirements: Python 3.9+
"""

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from indexing import get_session_date, list_pending_sessions
from memory_utils import get_captured_sessions

__all__ = [
    # Parsing
    "extract_text_content",
    "should_skip_message",
    "parse_jsonl_file",
    "parse_jsonl_file_from_line",
    # Extraction
    "extract_transcripts",
    "extract_transcripts_incremental",
    "format_transcripts_for_output",
    "format_transcripts_incremental",
    "get_pending_days",
]

# =============================================================================
# Key Interfaces
# =============================================================================
# Parsing:
#   extract_text_content(content) -> str
#   should_skip_message(content) -> bool
#   parse_jsonl_file(filepath) -> list[dict]
# Extraction:
#   extract_transcripts(day?, exclude_session_id?) -> dict[str, list[dict]]
#   format_transcripts_for_output(daily_data) -> str
#   get_pending_days(exclude_session_id?) -> list[str]
# =============================================================================


def extract_text_content(content: Any) -> str:
    """Extract text from message content (handles string or list format)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
        return "\n".join(text_parts)
    return ""


def should_skip_message(content: str) -> bool:
    """
    Filter out low-value messages from synthesis input.

    Filters:
    - Skill instruction injections (50%+ of typical extraction)
    - System reminders (injected throughout sessions)
    - User interruptions
    """
    if content.startswith("Base directory for this skill:"):
        return True
    if "<command-name>" in content[:200]:
        return True
    if "<system-reminder>" in content:
        return True
    if content.strip() == "[Request interrupted by user]":
        return True
    # Synthesis artifacts from previous runs
    if "===DAILY:" in content or "===ROUTE:" in content:
        return True
    if "synthesis.py apply" in content:
        return True
    if "## AUTO-SYNTHESIZE REQUIRED" in content:
        return True
    return False


def parse_jsonl_file(filepath: Path) -> list[dict]:
    """Parse a JSONL transcript file and extract messages."""
    messages = []

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    obj_type = obj.get("type")
                    if obj_type in ("user", "assistant"):
                        msg = obj.get("message", {})
                        role = msg.get("role", obj_type)
                        content = extract_text_content(msg.get("content", ""))
                        if content:
                            if role == "user":
                                continue
                            if should_skip_message(content):
                                continue
                            messages.append({"role": role, "content": content})
                except json.JSONDecodeError as e:
                    print(
                        f"Warning: JSON parse error in {filepath} line {line_num}: {e}",
                        file=sys.stderr,
                    )
                    continue
    except IOError as e:
        print(f"Warning: Could not read {filepath}: {e}", file=sys.stderr)

    return messages


def parse_jsonl_file_from_line(
    filepath: Path, start_line: int = 0
) -> tuple[list[dict], int]:
    """Parse a JSONL transcript file, optionally skipping initial lines.

    Args:
        filepath: Path to JSONL transcript file
        start_line: Number of non-blank JSONL lines to skip (0 = parse all)

    Returns:
        (messages, total_lines) where total_lines is the count of all non-blank
        lines in the file (for updating the high water mark).
    """
    messages = []
    line_count = 0  # non-blank lines seen

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                line_count += 1
                if line_count <= start_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                    obj_type = obj.get("type")
                    if obj_type in ("user", "assistant"):
                        msg = obj.get("message", {})
                        role = msg.get("role", obj_type)
                        content = extract_text_content(msg.get("content", ""))
                        if content:
                            if role == "user":
                                continue
                            if should_skip_message(content):
                                continue
                            messages.append({"role": role, "content": content})
                except json.JSONDecodeError:
                    continue
    except IOError:
        pass

    return messages, line_count


def extract_transcripts(
    specific_day: str | None = None,
    exclude_session_id: str | None = None,
) -> dict[str, list[dict]]:
    """
    Extract pending transcripts directly from Claude Code's projects directory.

    Args:
        specific_day: Optional specific day to extract (YYYY-MM-DD format)
        exclude_session_id: Optional session ID to exclude

    Returns:
        Dict mapping date strings to lists of session dicts.
    """
    captured = get_captured_sessions()
    pending = list_pending_sessions(captured, exclude_session_id=exclude_session_id)

    if specific_day:
        pending = [s for s in pending if get_session_date(s) == specific_day]

    if not pending:
        return {}

    daily_data: dict[str, list[dict]] = defaultdict(list)

    for session in pending:
        day = get_session_date(session)
        messages = parse_jsonl_file(session.transcript_path)

        if messages:
            daily_data[day].append(
                {
                    "session_id": session.session_id,
                    "filepath": str(session.transcript_path),
                    "project_path": session.project_path,
                    "message_count": len(messages),
                    "messages": messages,
                }
            )

    return dict(daily_data)


def extract_transcripts_incremental(
    state: dict,
    exclude_session_id: str | None = None,
) -> dict[str, list[dict]]:
    """Extract transcripts incrementally using synthesis state high water marks.

    For each pending session, compares current file size to stored offset:
    - Not in state: full parse (mode="full")
    - Same size: skip entirely (no output)
    - Grew: delta parse from stored line count (mode="delta")

    Each session dict includes: session_id, filepath, project_path, message_count,
    messages, mode ("full"|"delta"), current_offset, current_lines.

    Args:
        state: Synthesis state dict with "sessions" key
        exclude_session_id: Session ID to exclude from extraction

    Returns:
        Dict mapping date -> list of session dicts (only sessions with content).
    """
    captured = get_captured_sessions()
    pending = list_pending_sessions(captured, exclude_session_id=exclude_session_id)

    sessions_state = state.get("sessions", {})
    daily_data: dict[str, list[dict]] = defaultdict(list)

    for session in pending:
        sid = session.session_id
        current_size = session.file_size
        prev = sessions_state.get(sid)

        if prev and current_size == prev.get("offset", 0):
            # Unchanged — skip entirely
            continue

        if prev and current_size > prev.get("offset", 0):
            # Grew — delta extraction
            start_line = prev.get("lines", 0)
            messages, total_lines = parse_jsonl_file_from_line(
                session.transcript_path, start_line=start_line
            )
            mode = "delta"
        else:
            # New session (not in state) — full extraction
            messages, total_lines = parse_jsonl_file_from_line(
                session.transcript_path, start_line=0
            )
            mode = "full"

        if messages:
            day = get_session_date(session)
            daily_data[day].append({
                "session_id": sid,
                "filepath": str(session.transcript_path),
                "project_path": session.project_path,
                "message_count": len(messages),
                "messages": messages,
                "mode": mode,
                "current_offset": current_size,
                "current_lines": total_lines,
            })

    return dict(daily_data)


def format_transcripts_for_output(
    daily_data: dict[str, list[dict]],
    total_line_budget: int | None = None,
) -> str:
    """Format extracted transcripts for human-readable output.

    Args:
        daily_data: Dict mapping date strings to lists of session dicts.
        total_line_budget: If set, cap total output lines by dividing budget
            evenly across sessions. Sessions under the cap pass through
            untouched; over-cap sessions keep first 1/3 + last 2/3.
    """
    # Count total sessions for budget calculation
    all_sessions = [s for sessions in daily_data.values() for s in sessions]
    max_lines_per_session = None
    if total_line_budget and all_sessions:
        max_lines_per_session = total_line_budget // len(all_sessions)
        max_lines_per_session = max(max_lines_per_session, 15)  # floor

    output = []

    for day in sorted(daily_data.keys()):
        sessions = daily_data[day]
        total_messages = sum(s["message_count"] for s in sessions)
        output.append(f"\n{'='*70}")
        output.append(f"DAY: {day} ({len(sessions)} sessions, {total_messages} messages)")
        output.append(f"{'='*70}")

        for session in sessions:
            output.append(f"\n{'─'*70}")
            output.append(f"Session: {session['session_id']}")
            output.append(f"{'─'*70}")

            session_parts: list[str] = []
            for msg in session["messages"]:
                role_label = "USER" if msg["role"] == "user" else "CLAUDE"
                session_parts.append(f"\n[{role_label}]")
                session_parts.append(msg["content"])

            session_text = "\n".join(session_parts)
            actual_lines = session_text.split("\n")

            if max_lines_per_session and len(actual_lines) > max_lines_per_session:
                head = max_lines_per_session // 3
                tail = max_lines_per_session - head
                truncated = len(actual_lines) - head - tail
                output.append("\n".join(actual_lines[:head]))
                output.append(f"\n... [{truncated} lines truncated] ...")
                output.append("\n".join(actual_lines[-tail:]))
            else:
                output.append(session_text)

    return "\n".join(output)


def format_transcripts_incremental(
    daily_data: dict[str, list[dict]],
    total_line_budget: int | None = None,
) -> str:
    """Format incrementally-extracted transcripts for output.

    Like format_transcripts_for_output but marks delta sessions with
    '(continued -- new messages only)' in the header.

    Args:
        daily_data: Dict from extract_transcripts_incremental
        total_line_budget: Cap total output lines (divided across sessions)
    """
    all_sessions = [s for sessions in daily_data.values() for s in sessions]
    max_lines_per_session = None
    if total_line_budget and all_sessions:
        max_lines_per_session = total_line_budget // len(all_sessions)
        max_lines_per_session = max(max_lines_per_session, 15)

    output = []

    for day in sorted(daily_data.keys()):
        sessions = daily_data[day]
        total_messages = sum(s["message_count"] for s in sessions)
        output.append(f"\n{'='*70}")
        output.append(f"DAY: {day} ({len(sessions)} sessions, {total_messages} messages)")
        output.append(f"{'='*70}")

        for session in sessions:
            output.append(f"\n{'─'*70}")
            mode = session.get("mode", "full")
            if mode == "delta":
                output.append(
                    f"Session: {session['session_id']}"
                    " (continued — new messages only)"
                )
            else:
                output.append(f"Session: {session['session_id']}")
            output.append(f"{'─'*70}")

            session_parts: list[str] = []
            for msg in session["messages"]:
                role_label = "USER" if msg["role"] == "user" else "CLAUDE"
                session_parts.append(f"\n[{role_label}]")
                session_parts.append(msg["content"])

            session_text = "\n".join(session_parts)
            actual_lines = session_text.split("\n")

            if max_lines_per_session and len(actual_lines) > max_lines_per_session:
                head = max_lines_per_session // 3
                tail = max_lines_per_session - head
                truncated = len(actual_lines) - head - tail
                output.append("\n".join(actual_lines[:head]))
                output.append(f"\n... [{truncated} lines truncated] ...")
                output.append("\n".join(actual_lines[-tail:]))
            else:
                output.append(session_text)

    return "\n".join(output)


def get_pending_days(exclude_session_id: str | None = None) -> list[str]:
    """
    List all days that have pending transcripts.

    Args:
        exclude_session_id: Optional session ID to exclude
    """
    captured = get_captured_sessions()
    pending = list_pending_sessions(
        captured, exclude_session_id=exclude_session_id, verify_content=True
    )

    days = set()
    for session in pending:
        days.add(get_session_date(session))

    return sorted(days)
