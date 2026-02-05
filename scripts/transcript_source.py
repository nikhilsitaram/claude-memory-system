#!/usr/bin/env python3
"""
Transcript source abstraction for Claude Code Memory System.

Provides discovery and reading of transcripts directly from Claude Code's storage
(~/.claude/projects/) instead of copying them via hooks.

Design principle: Filesystem is source of truth, sessions-index.json is optional
metadata enrichment. This handles the 26.3% of sessions missing from the index.

Requirements: Python 3.9+
"""

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    check_python_version,
    get_projects_dir,
    get_captured_sessions,
)


@dataclass
class SessionInfo:
    """
    Information about a Claude Code session transcript.

    Combines filesystem data (always available) with index metadata (optional).
    """

    session_id: str
    transcript_path: Path
    project_hash: str  # Folder name (e.g., -home-nsitaram-claude-memory-system)

    # From filesystem (always available):
    file_mtime: datetime  # File modification time
    file_size: int  # Bytes

    # From index (optional, may be None):
    project_path: Optional[str] = None  # Original path like /home/nsitaram/project
    created: Optional[datetime] = None  # Session creation time from index
    summary: Optional[str] = None  # AI-generated summary
    message_count: Optional[int] = None  # From index, None if not indexed


def _parse_index_datetime(date_str: str) -> Optional[datetime]:
    """Parse ISO format datetime from sessions-index.json."""
    if not date_str:
        return None
    try:
        # Handle "2026-01-25T21:48:21.826Z" format
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_sessions_index(project_folder: Path) -> dict:
    """
    Load sessions-index.json for a project folder.

    Returns dict mapping session_id to entry metadata, or empty dict if missing.
    """
    index_file = project_folder / "sessions-index.json"
    if not index_file.exists():
        return {}

    try:
        with open(index_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        # Build lookup by sessionId
        index = {}
        entries = data.get("entries", [])

        # Get original path (try root-level first, then entries[0].projectPath)
        original_path = data.get("originalPath", "")
        if not original_path and entries:
            original_path = entries[0].get("projectPath", "")

        for entry in entries:
            session_id = entry.get("sessionId")
            if session_id:
                index[session_id] = {
                    "created": entry.get("created"),
                    "summary": entry.get("summary"),
                    "projectPath": original_path,
                }

        return index

    except (json.JSONDecodeError, IOError):
        return {}


def list_all_sessions() -> list[SessionInfo]:
    """
    List all sessions from Claude Code's projects directory.

    Primary: Scans all .jsonl files in ~/.claude/projects/
    Secondary: Enriches with sessions-index.json metadata when available

    Returns list of SessionInfo sorted by file modification time (newest first).
    """
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return []

    sessions = []

    for project_folder in projects_dir.iterdir():
        if not project_folder.is_dir():
            continue

        project_hash = project_folder.name

        # Load index for this project (may be empty)
        index = _load_sessions_index(project_folder)

        # Scan all .jsonl files
        for jsonl_file in project_folder.glob("*.jsonl"):
            # Skip subagent files
            if "subagent" in jsonl_file.name.lower():
                continue

            session_id = jsonl_file.stem

            # Get file stats (always available)
            try:
                stat = jsonl_file.stat()
                file_mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                file_size = stat.st_size
            except OSError:
                continue

            # Enrich with index metadata if available
            entry = index.get(session_id, {})
            created = _parse_index_datetime(entry.get("created", ""))
            project_path = entry.get("projectPath")
            summary = entry.get("summary")

            sessions.append(
                SessionInfo(
                    session_id=session_id,
                    transcript_path=jsonl_file,
                    project_hash=project_hash,
                    file_mtime=file_mtime,
                    file_size=file_size,
                    project_path=project_path,
                    created=created,
                    summary=summary,
                    message_count=None,  # Would require parsing file
                )
            )

    # Sort by file modification time (newest first)
    sessions.sort(key=lambda s: s.file_mtime, reverse=True)
    return sessions


def list_pending_sessions(
    captured: set[str], min_file_size: int = 1000
) -> list[SessionInfo]:
    """
    Filter to unprocessed sessions.

    Args:
        captured: Set of already-captured session IDs
        min_file_size: Minimum file size in bytes (default 1000 ≈ 2-3 messages)

    Returns list of SessionInfo for sessions that:
    - Have not been captured
    - Meet minimum file size threshold
    """
    all_sessions = list_all_sessions()

    return [
        s
        for s in all_sessions
        if s.session_id not in captured and s.file_size >= min_file_size
    ]


def get_session_date(session: SessionInfo) -> str:
    """
    Get date string (YYYY-MM-DD) for a session.

    Prefers index.created if available, falls back to file_mtime.
    """
    if session.created:
        return session.created.strftime("%Y-%m-%d")
    return session.file_mtime.strftime("%Y-%m-%d")


def list_pending_days(captured: set[str], min_file_size: int = 1000) -> list[str]:
    """
    List all days that have pending (uncaptured) sessions.

    Args:
        captured: Set of already-captured session IDs
        min_file_size: Minimum file size in bytes

    Returns sorted list of date strings (YYYY-MM-DD).
    """
    pending = list_pending_sessions(captured, min_file_size)
    days = set(get_session_date(s) for s in pending)
    return sorted(days)


if __name__ == "__main__":
    # Self-test
    check_python_version()

    print("Transcript Source Self-Test")
    print("=" * 50)

    captured = get_captured_sessions()
    print(f"Already captured: {len(captured)} sessions")
    print()

    all_sessions = list_all_sessions()
    print(f"Total sessions in projects/: {len(all_sessions)}")

    # Count sessions with index metadata
    indexed = sum(1 for s in all_sessions if s.created is not None)
    print(f"  With index metadata: {indexed}")
    print(f"  Missing from index: {len(all_sessions) - indexed}")
    print()

    pending = list_pending_sessions(captured)
    print(f"Pending sessions: {len(pending)}")

    if pending:
        print("\nPending by day:")
        days = list_pending_days(captured)
        for day in days[:5]:
            day_sessions = [s for s in pending if get_session_date(s) == day]
            total_size = sum(s.file_size for s in day_sessions)
            print(f"  {day}: {len(day_sessions)} sessions ({total_size / 1024:.1f} KB)")
        if len(days) > 5:
            print(f"  ... and {len(days) - 5} more days")
