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

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import get_captured_sessions
from indexing import SessionInfo, list_pending_sessions, get_session_date

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


def extract_text_content(content) -> str:
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


def format_transcripts_for_output(daily_data: dict[str, list[dict]]) -> str:
    """Format extracted transcripts for human-readable output."""
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

            for msg in session["messages"]:
                role_label = "USER" if msg["role"] == "user" else "CLAUDE"
                output.append(f"\n[{role_label}]")
                output.append(msg["content"])

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
