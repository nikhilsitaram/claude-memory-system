#!/usr/bin/env python3
"""
Indexing utilities for Claude Code Memory System.

Provides:
1. Session discovery (scanning ~/.claude/projects/ for transcripts)
2. Project index building (maps projects to their work days)

Transcript extraction is in transcript_ops.py (split for smaller reads).

Usage:
    # Extract transcripts for a specific day (pure read, no marking)
    python indexing.py extract 2026-02-02 --output /tmp/extract.txt
    python indexing.py extract 2026-02-02 --exclude-session SESSION_ID --output /tmp/extract.txt

    # Mark sessions captured after successful synthesis
    python indexing.py mark-captured --sidecar /tmp/extract.sessions
    python indexing.py mark-captured SESSION_ID [SESSION_ID ...]

    # Uncapture sessions (make pending again)
    python indexing.py uncapture SESSION_ID [SESSION_ID ...]
    python indexing.py uncapture-date 2026-01-25 2026-02-02

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
    get_captured_file,
    add_captured_session,
    remove_captured_session,
)

# Sessions smaller than this are likely empty/metadata-only (2-3 messages ≈ 1000 bytes)
MIN_SESSION_SIZE_BYTES = 1000

# =============================================================================
# Key Interfaces
# =============================================================================
# Session discovery:
#   SessionInfo                            dataclass: session_id, transcript_path, ...
#   list_all_sessions() -> list[SessionInfo]
#   list_pending_sessions(captured, ...) -> list[SessionInfo]
#   has_assistant_message(filepath) -> bool
#   get_session_date(session) -> str
# Project index:
#   build_projects_index() -> dict
# CLI: python indexing.py {extract,mark-captured,uncapture,uncapture-date,build-index,list-pending}
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


def list_pending_sessions(
    captured: set[str],
    min_file_size: int = MIN_SESSION_SIZE_BYTES,
    exclude_session_id: str | None = None,
    verify_content: bool = False,
) -> list[SessionInfo]:
    """
    Filter to unprocessed sessions.

    Args:
        captured: Set of already-captured session IDs
        min_file_size: Minimum file size in bytes (default MIN_SESSION_SIZE_BYTES)
        exclude_session_id: Optional session ID to exclude (e.g., the active session)
        verify_content: If True, parse JSONL to verify at least one assistant message exists

    Returns list of SessionInfo for sessions that:
    - Have not been captured
    - Meet minimum file size threshold
    - Are not the excluded session
    - (If verify_content) contain at least one assistant message
    """
    all_sessions = list_all_sessions()

    return [
        s
        for s in all_sessions
        if s.session_id not in captured
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
                    dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
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

        sessions_file = project_folder / "sessions-index.json"
        if sessions_file.exists():
            try:
                with open(sessions_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Could not read {sessions_file}: {e}", file=sys.stderr)
                data = {}

            # Get original path: try root-level first, then entries[0].projectPath
            original_path = data.get("originalPath", "")
            entries = data.get("entries", [])
            if not original_path and entries:
                original_path = entries[0].get("projectPath", "")

            # Extract work days from session entries
            for entry in entries:
                created = entry.get("created")
                if created:
                    try:
                        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
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
    """Handle extract command. Pure read operation — never marks sessions."""
    from transcript_ops import extract_transcripts, format_transcripts_for_output
    exclude_id = getattr(args, "exclude_session", None)
    daily_data = extract_transcripts(args.day, exclude_session_id=exclude_id)

    if not daily_data:
        print("No pending transcripts found.", file=sys.stderr)
        return 1

    if args.json:
        # JSON output
        output = json.dumps(daily_data, indent=2)
    else:
        # Human-readable output
        output = format_transcripts_for_output(daily_data)

    if args.output:
        Path(args.output).write_text(output, encoding="utf-8")
        print(f"Output written to: {args.output}", file=sys.stderr)

        # Write sidecar .sessions file with session IDs and file sizes
        sidecar_path = Path(args.output).with_suffix(".sessions")
        sidecar_lines = []
        for day_sessions in daily_data.values():
            for session in day_sessions:
                sidecar_lines.append(session["session_id"])
        sidecar_path.write_text("\n".join(sidecar_lines) + "\n", encoding="utf-8")
        print(f"Session IDs written to: {sidecar_path}", file=sys.stderr)
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
    from transcript_ops import get_pending_days
    days = get_pending_days()
    if days:
        print("Pending transcript days:")
        for day in days:
            print(f"  {day}")
    else:
        print("No pending transcripts.")
    return 0


def cmd_mark_captured(args: argparse.Namespace) -> int:
    """
    Mark sessions as captured.

    Two modes:
    1. --sidecar: Read session IDs from sidecar file, skip today's sessions
    2. Explicit IDs: Mark listed sessions unconditionally
    """
    today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if args.sidecar:
        # Mode 1: Read from sidecar file, skip today's sessions
        sidecar_path = Path(args.sidecar)
        if not sidecar_path.exists():
            print(f"Sidecar file not found: {sidecar_path}", file=sys.stderr)
            return 1

        session_ids = [
            line.strip()
            for line in sidecar_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        if not session_ids:
            print("No session IDs in sidecar file.", file=sys.stderr)
            return 1

        # Look up dates for each session to determine if it's "today"
        captured = get_captured_sessions()
        all_sessions = list_all_sessions()
        session_lookup = {s.session_id: s for s in all_sessions}

        marked = 0
        skipped_today = 0

        for sid in session_ids:
            session = session_lookup.get(sid)
            if session:
                session_date = get_session_date(session)
                if session_date == today_utc:
                    skipped_today += 1
                    continue

            add_captured_session(sid, captured)
            captured.add(sid)
            marked += 1

        print(f"Marked {marked} sessions, skipped {skipped_today} (today's sessions)", file=sys.stderr)

    else:
        # Mode 2: Explicit IDs — mark unconditionally
        if not args.session_ids:
            print("Provide session IDs or --sidecar file.", file=sys.stderr)
            return 1

        captured = get_captured_sessions()
        for sid in args.session_ids:
            add_captured_session(sid, captured)
            captured.add(sid)

        print(f"Marked {len(args.session_ids)} sessions as captured.", file=sys.stderr)

    return 0


def cmd_uncapture(args: argparse.Namespace) -> int:
    """Remove session IDs from .captured to make them pending again."""
    if not args.session_ids:
        print("Provide at least one session ID.", file=sys.stderr)
        return 1

    removed = 0
    for sid in args.session_ids:
        if remove_captured_session(sid):
            removed += 1

    print(f"Uncaptured {removed} of {len(args.session_ids)} sessions.", file=sys.stderr)
    return 0


def cmd_uncapture_date(args: argparse.Namespace) -> int:
    """Uncapture all sessions for given date(s)."""
    if not args.dates:
        print("Provide at least one date (YYYY-MM-DD).", file=sys.stderr)
        return 1

    target_dates = set(args.dates)
    captured = get_captured_sessions()
    all_sessions = list_all_sessions()

    # Find captured sessions that fall on the target dates
    to_uncapture = []
    for session in all_sessions:
        if session.session_id in captured:
            session_date = get_session_date(session)
            if session_date in target_dates:
                to_uncapture.append(session.session_id)

    if not to_uncapture:
        print(f"No captured sessions found for dates: {', '.join(sorted(target_dates))}", file=sys.stderr)
        return 0

    removed = 0
    for sid in to_uncapture:
        if remove_captured_session(sid):
            removed += 1

    print(f"Uncaptured {removed} sessions for dates: {', '.join(sorted(target_dates))}", file=sys.stderr)
    return 0


def main() -> int:
    """Main entry point."""
    check_python_version()

    parser = argparse.ArgumentParser(
        description="Indexing utilities for Claude Code Memory System"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Extract command (pure read — never marks sessions)
    extract_parser = subparsers.add_parser(
        "extract", help="Extract transcripts for synthesis (does not mark captured)"
    )
    extract_parser.add_argument("day", nargs="?", help="Specific day to extract (YYYY-MM-DD)")
    extract_parser.add_argument("--output", "-o", help="Output file path (also creates .sessions sidecar)")
    extract_parser.add_argument("--json", action="store_true", help="Output as JSON")
    extract_parser.add_argument(
        "--exclude-session",
        help="Exclude this session ID from extraction (e.g., the active session)",
    )
    extract_parser.set_defaults(func=cmd_extract)

    # Mark-captured command
    mark_parser = subparsers.add_parser(
        "mark-captured", help="Mark sessions as captured after successful synthesis"
    )
    mark_parser.add_argument("session_ids", nargs="*", help="Session IDs to mark (unconditional)")
    mark_parser.add_argument(
        "--sidecar",
        help="Read session IDs from sidecar file (skips today's sessions)",
    )
    mark_parser.set_defaults(func=cmd_mark_captured)

    # Uncapture command
    uncapture_parser = subparsers.add_parser(
        "uncapture", help="Remove sessions from captured list (make pending again)"
    )
    uncapture_parser.add_argument("session_ids", nargs="+", help="Session IDs to uncapture")
    uncapture_parser.set_defaults(func=cmd_uncapture)

    # Uncapture-date command
    uncapture_date_parser = subparsers.add_parser(
        "uncapture-date", help="Uncapture all sessions for given date(s)"
    )
    uncapture_date_parser.add_argument("dates", nargs="+", help="Dates to uncapture (YYYY-MM-DD)")
    uncapture_date_parser.set_defaults(func=cmd_uncapture_date)

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
