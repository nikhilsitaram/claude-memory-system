#!/usr/bin/env python3
"""
Indexing utilities for Claude Code Memory System.

Provides:
1. Session discovery (scanning ~/.claude/projects/ for transcripts)
2. Transcript extraction (JSONL parsing for synthesis)
3. Project index building (maps projects to their work days)

Usage:
    # Extract transcripts for a specific day
    python indexing.py extract 2026-02-02

    # Extract all pending transcripts
    python indexing.py extract

    # Build/rebuild project index
    python indexing.py build-index

    # List pending transcript days
    python indexing.py list-pending

Requirements: Python 3.9+
"""

import argparse
import json
import sys
from collections import defaultdict
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
    get_memory_dir,
    get_projects_dir,
    get_projects_index_file,
    get_captured_sessions,
    add_captured_session,
)


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


# =============================================================================
# Transcript Extraction
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
    # Skip skill instruction injections (major source of bloat)
    if content.startswith("Base directory for this skill:"):
        return True
    if "<command-name>" in content[:200]:
        return True

    # Skip system reminders (auto-injected, not user content)
    if "<system-reminder>" in content:
        return True

    # Skip interruptions (no useful content)
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

                    # Handle message objects - top level type is "user" or "assistant"
                    obj_type = obj.get("type")
                    if obj_type in ("user", "assistant"):
                        msg = obj.get("message", {})
                        role = msg.get("role", obj_type)
                        content = extract_text_content(msg.get("content", ""))

                        if content:
                            # Skip all user messages - Claude echoes important context
                            if role == "user":
                                continue
                            # Skip low-value messages (skill injections, system reminders)
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


def extract_transcripts(specific_day: str | None = None) -> dict[str, list[dict]]:
    """
    Extract pending transcripts directly from Claude Code's projects directory.

    Reads from ~/.claude/projects/ (source of truth) instead of copied files.
    Uses sessions-index.json for metadata enrichment when available.

    Args:
        specific_day: Optional specific day to extract (YYYY-MM-DD format)

    Returns:
        Dict mapping date strings to lists of session dicts:
        {
            "2026-02-02": [
                {
                    "session_id": "abc123",
                    "filepath": "/path/to/file.jsonl",
                    "project_path": "/home/user/project" or None,
                    "message_count": 42,
                    "messages": [{"role": "user", "content": "..."}, ...]
                },
                ...
            ],
            ...
        }
    """
    captured = get_captured_sessions()
    pending = list_pending_sessions(captured, min_file_size=1000)

    # Filter by specific day if requested
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
                    "project_path": session.project_path,  # May be None
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


def get_pending_days() -> list[str]:
    """
    List all days that have pending transcripts with extractable content.

    Uses the same filtering logic as extract_transcripts() to ensure
    list-pending and extract return consistent results.
    """
    # Use extract logic (without marking) to find days with actual content
    daily_data = extract_transcripts(specific_day=None)
    return sorted(daily_data.keys())


# =============================================================================
# Project Index Building
# =============================================================================


def build_projects_index() -> dict:
    """
    Build a project-to-work-days index from Claude Code's sessions-index.json files.

    Scans all sessions-index.json files in ~/.claude/projects/ and builds
    a mapping of projects to the dates they have work sessions.

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

        sessions_file = project_folder / "sessions-index.json"
        if not sessions_file.exists():
            continue

        try:
            with open(sessions_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not read {sessions_file}: {e}", file=sys.stderr)
            continue

        # Get original path: try root-level first, then entries[0].projectPath
        original_path = data.get("originalPath", "")
        entries = data.get("entries", [])
        if not original_path and entries:
            original_path = entries[0].get("projectPath", "")
        if not original_path:
            continue
        if not entries:
            continue

        # Extract work days from session entries
        work_days: set[str] = set()
        for entry in entries:
            created = entry.get("created")
            if created:
                try:
                    # Parse ISO format: "2026-01-25T21:48:21.826Z"
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    work_days.add(dt.strftime("%Y-%m-%d"))
                except ValueError:
                    continue

        if not work_days:
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
        "lastUpdated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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


def cmd_extract(args: argparse.Namespace) -> int:
    """Handle extract command."""
    daily_data = extract_transcripts(args.day)

    if not daily_data:
        print("No pending transcripts found.", file=sys.stderr)
        return 1

    # Mark extracted sessions as captured (unless --no-mark flag)
    if not getattr(args, "no_mark", False):
        captured = get_captured_sessions()
        for day_sessions in daily_data.values():
            for session in day_sessions:
                session_id = session["session_id"]
                add_captured_session(session_id, captured)
                captured.add(session_id)

    if args.json:
        # JSON output
        output = json.dumps(daily_data, indent=2)
        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
            print(f"Output written to: {args.output}", file=sys.stderr)
        else:
            print(output)
    else:
        # Human-readable output
        output = format_transcripts_for_output(daily_data)
        if args.output:
            Path(args.output).write_text(output, encoding="utf-8")
            print(f"Output written to: {args.output}", file=sys.stderr)
        else:
            print(output)

    return 0


def cmd_build_index(args: argparse.Namespace) -> int:
    """Handle build-index command."""
    index = build_projects_index()
    print_index_summary(index)
    return 0


def cmd_list_pending(args: argparse.Namespace) -> int:
    """Handle list-pending command."""
    days = get_pending_days()
    if days:
        print("Pending transcript days:")
        for day in days:
            print(f"  {day}")
    else:
        print("No pending transcripts.")
    return 0


def main() -> int:
    """Main entry point."""
    check_python_version()

    parser = argparse.ArgumentParser(
        description="Indexing utilities for Claude Code Memory System"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Extract command
    extract_parser = subparsers.add_parser(
        "extract", help="Extract transcripts for synthesis"
    )
    extract_parser.add_argument("day", nargs="?", help="Specific day to extract (YYYY-MM-DD)")
    extract_parser.add_argument("--output", "-o", help="Output file path")
    extract_parser.add_argument("--json", action="store_true", help="Output as JSON")
    extract_parser.add_argument(
        "--no-mark",
        action="store_true",
        help="Don't mark sessions as captured (for preview/debugging)",
    )
    extract_parser.set_defaults(func=cmd_extract)

    # Build-index command
    build_parser = subparsers.add_parser(
        "build-index", help="Build/rebuild project index"
    )
    build_parser.set_defaults(func=cmd_build_index)

    # List-pending command
    list_parser = subparsers.add_parser(
        "list-pending", help="List days with pending transcripts"
    )
    list_parser.set_defaults(func=cmd_list_pending)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
