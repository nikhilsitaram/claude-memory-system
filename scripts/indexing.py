#!/usr/bin/env python3
"""
Indexing utilities for Claude Code Memory System.

Provides:
1. Session discovery (scanning ~/.claude/projects/ for transcripts)
2. Project index building (maps projects to their work days)

Transcript extraction is in transcript_ops.py (split for smaller reads).

Usage:
    # Build/rebuild project index
    python indexing.py build-index

    # List days with recent transcripts
    python indexing.py list-recent

Requirements: Python 3.9+
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    check_python_version,
    from_iso_z,
    get_memory_dir,
    get_projects_dir,
    get_projects_index_file,
    get_sessions_original_path,
    load_sessions_index,
    to_iso_z,
)

# Sessions smaller than this are likely empty/metadata-only (2-3 messages ~ 1000 bytes)
MIN_SESSION_SIZE_BYTES = 1000

# Default window for recent session filtering (days)
DEFAULT_RECENCY_WINDOW_DAYS = 7

__all__ = [
    # Constants
    "MIN_SESSION_SIZE_BYTES",
    "DEFAULT_RECENCY_WINDOW_DAYS",
    # Data classes
    "SessionInfo",
    # Session discovery
    "list_all_sessions",
    "has_assistant_message",
    "list_recent_sessions",
    "get_session_date",
    # Project index
    "build_projects_index",
]

# =============================================================================
# Key Interfaces
# =============================================================================
# Session discovery:
#   SessionInfo                            dataclass: session_id, transcript_path, ...
#   list_all_sessions() -> list[SessionInfo]
#   list_recent_sessions(max_age_days, ...) -> list[SessionInfo]
#   has_assistant_message(filepath) -> bool
#   get_session_date(session) -> str
# Project index:
#   build_projects_index() -> dict
# CLI: python indexing.py {build-index,list-recent}
# =============================================================================


# =============================================================================
# Session Discovery
# =============================================================================


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


def _parse_index_datetime(date_str: str) -> Optional[datetime]:
    """Parse ISO format datetime from sessions-index.json."""
    if not date_str:
        return None
    try:
        # Handle "2026-01-25T21:48:21.826Z" format
        return from_iso_z(date_str)
    except ValueError:
        return None


def _load_sessions_index(project_folder: Path) -> dict:
    """
    Load sessions-index.json for a project folder.

    Returns dict mapping session_id to entry metadata, or empty dict if missing.
    """
    data = load_sessions_index(project_folder)
    if not data:
        return {}

    original_path = get_sessions_original_path(data)
    index = {}
    for entry in data.get("entries", []):
        session_id = entry.get("sessionId")
        if session_id:
            index[session_id] = {
                "created": entry.get("created"),
                "summary": entry.get("summary"),
                "projectPath": original_path,
            }

    return index


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
                )
            )

    # Sort by file modification time (newest first)
    sessions.sort(key=lambda s: s.file_mtime, reverse=True)
    return sessions


def has_assistant_message(filepath: Path) -> bool:
    """Quick check: does this JSONL have at least one assistant message?"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") == "assistant":
                        return True
                except json.JSONDecodeError:
                    continue
    except IOError:
        pass
    return False


def list_recent_sessions(
    max_age_days: int = DEFAULT_RECENCY_WINDOW_DAYS,
    min_file_size: int = MIN_SESSION_SIZE_BYTES,
    exclude_session_id: str | None = None,
    verify_content: bool = False,
) -> list[SessionInfo]:
    """
    List recent sessions eligible for synthesis.

    Filters by file modification time instead of a captured set.

    Args:
        max_age_days: Only include sessions modified within this many days
        min_file_size: Minimum file size in bytes (default MIN_SESSION_SIZE_BYTES)
        exclude_session_id: Optional session ID to exclude (e.g., the active session)
        verify_content: If True, parse JSONL to verify at least one assistant message exists

    Returns list of SessionInfo for sessions that:
    - Have mtime within max_age_days
    - Meet minimum file size threshold
    - Are not the excluded session
    - (If verify_content) contain at least one assistant message
    """
    all_sessions = list_all_sessions()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    return [
        s
        for s in all_sessions
        if s.file_mtime >= cutoff
        and s.file_size >= min_file_size
        and s.session_id != exclude_session_id
        and (not verify_content or has_assistant_message(s.transcript_path))
    ]


def get_session_date(session: SessionInfo) -> str:
    """
    Get date string (YYYY-MM-DD) for a session.

    Prefers index.created if available, falls back to file_mtime.
    """
    if session.created:
        return session.created.strftime("%Y-%m-%d")
    return session.file_mtime.strftime("%Y-%m-%d")


# =============================================================================
# Project Index Building
# =============================================================================


def _extract_from_jsonl(folder: Path) -> tuple[str, set[str]]:
    """
    Extract original path and work days from JSONL transcript files.

    Reads the first line of each .jsonl file to get cwd and timestamp.
    Returns (original_path, work_days_set). original_path may be empty
    if no cwd field is found.
    """
    original_path = ""
    work_days: set[str] = set()

    for jsonl_file in sorted(folder.glob("*.jsonl")):
        try:
            with open(jsonl_file, "r", encoding="utf-8") as f:
                first_line = f.readline().strip()
                if not first_line:
                    continue
                data = json.loads(first_line)

                # Extract cwd as original path (first valid one wins)
                cwd = data.get("cwd", "")
                if cwd and not original_path:
                    original_path = cwd

                # Extract timestamp as work day
                timestamp = data.get("timestamp", "")
                if timestamp:
                    dt = from_iso_z(timestamp)
                    work_days.add(dt.strftime("%Y-%m-%d"))
        except (json.JSONDecodeError, IOError, ValueError):
            continue

    return original_path, work_days


def build_projects_index() -> dict:
    """
    Build a project-to-work-days index from Claude Code's project data.

    Scans sessions-index.json files and JSONL transcripts in ~/.claude/projects/
    to build a mapping of projects to the dates they have work sessions.
    JSONL files supplement sessions-index.json with missing work days and
    serve as fallback when sessions-index.json doesn't exist.

    Returns the index dict and also saves it to projects-index.json.
    """
    projects_dir = get_projects_dir()
    memory_dir = get_memory_dir()
    output_file = get_projects_index_file()

    # Ensure memory dir exists
    memory_dir.mkdir(parents=True, exist_ok=True)

    # Collect all projects and their work days
    # Key: lowercase project path for consistent lookup
    # Value: project metadata
    projects: dict[str, dict] = {}

    # Also track path variations (case differences) that map to same project
    path_variations: dict[str, set[str]] = defaultdict(set)

    if not projects_dir.exists():
        print(f"Projects directory not found: {projects_dir}", file=sys.stderr)
        return {"projects": {}}

    for project_folder in projects_dir.iterdir():
        if not project_folder.is_dir():
            continue

        # Try sessions-index.json first
        original_path = ""
        work_days: set[str] = set()

        data = load_sessions_index(project_folder)
        if data:
            original_path = get_sessions_original_path(data)

            # Extract work days from session entries
            for entry in data.get("entries", []):
                created = entry.get("created")
                if created:
                    try:
                        dt = from_iso_z(created)
                        work_days.add(dt.strftime("%Y-%m-%d"))
                    except ValueError:
                        continue

        # Supplement with JSONL transcripts (fallback for path, additional work days)
        jsonl_path, jsonl_days = _extract_from_jsonl(project_folder)
        if not original_path:
            original_path = jsonl_path
        work_days.update(jsonl_days)

        if not original_path or not work_days:
            continue

        # Use lowercase path as the canonical key for lookups
        canonical_path = original_path.lower()

        # Track all path variations
        path_variations[canonical_path].add(original_path)

        # If this project already exists (case variation), merge work days
        if canonical_path in projects:
            existing_days = set(projects[canonical_path]["workDays"])
            existing_days.update(work_days)
            projects[canonical_path]["workDays"] = sorted(existing_days)
            # Keep track of all encoded paths (folders)
            if project_folder.name not in projects[canonical_path]["encodedPaths"]:
                projects[canonical_path]["encodedPaths"].append(project_folder.name)
        else:
            # Extract project name from path
            project_name = Path(original_path).name

            projects[canonical_path] = {
                "name": project_name,
                "originalPath": original_path,  # Keep one original for display
                "encodedPaths": [project_folder.name],
                "workDays": sorted(work_days),
            }

    # Check for stale paths (projects where originalPath no longer exists)
    stale_projects = []
    for canonical_path, data in projects.items():
        original_path = data.get("originalPath", "")
        if original_path and not Path(original_path).exists():
            stale_projects.append({
                "name": data.get("name", "unknown"),
                "original_path": original_path,
                "work_days": len(data.get("workDays", [])),
            })

    # Emit warnings for stale paths
    if stale_projects:
        print(f"\nWarning: {len(stale_projects)} project(s) have missing paths:", file=sys.stderr)
        for stale in stale_projects:
            print(f"  - {stale['name']}: {stale['original_path']} ({stale['work_days']} work days)", file=sys.stderr)
        print("  Consider using /projects to migrate or cleanup stale data.\n", file=sys.stderr)

    # Build output structure
    output = {
        "version": 1,
        "lastUpdated": to_iso_z(datetime.now(timezone.utc)),
        "projects": projects,
        # Include a lookup table for path variations (for debugging)
        "pathVariations": {k: sorted(v) for k, v in path_variations.items() if len(v) > 1},
    }

    # Write output
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    return output


def print_index_summary(index: dict) -> None:
    """Print summary of project index."""
    projects = index.get("projects", {})
    output_file = get_projects_index_file()

    print(f"Built project index: {output_file}")
    print(f"  Projects found: {len(projects)}")

    for path, data in sorted(projects.items()):
        print(f"    {data['name']}: {len(data['workDays'])} work days")
        if len(data.get("encodedPaths", [])) > 1:
            print(f"      (merged from {len(data['encodedPaths'])} folders)")


# =============================================================================
# CLI Interface
# =============================================================================


def cmd_build_index(args: argparse.Namespace) -> int:
    """Handle build-index command."""
    index = build_projects_index()
    print_index_summary(index)
    return 0


def cmd_list_recent(args: argparse.Namespace) -> int:
    """Handle list-recent command."""
    from transcript_ops import get_recent_days
    days = get_recent_days()
    if days:
        print("Recent transcript days:")
        for day in days:
            print(f"  {day}")
    else:
        print("No recent transcripts.")
    return 0


def main() -> int:
    """Main entry point."""
    check_python_version()

    parser = argparse.ArgumentParser(
        description="Indexing utilities for Claude Code Memory System"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Build-index command
    build_parser = subparsers.add_parser(
        "build-index", help="Build/rebuild project index"
    )
    build_parser.set_defaults(func=cmd_build_index)

    # List-recent command
    list_parser = subparsers.add_parser(
        "list-recent", help="List days with recent transcripts"
    )
    list_parser.set_defaults(func=cmd_list_recent)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
