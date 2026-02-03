#!/usr/bin/env python3
"""
Indexing utilities for Claude Code Memory System.

Provides:
1. Transcript extraction (JSONL parsing for synthesis)
2. Project index building (maps projects to their work days)
3. Transcript deletion (cross-platform, no bash required)

This module is called by the /synthesize skill to process transcripts
and maintain the project index.

Usage:
    # Extract transcripts for a specific day
    python indexing.py extract 2026-02-02

    # Extract all pending transcripts
    python indexing.py extract

    # Build/rebuild project index
    python indexing.py build-index

    # Delete processed transcripts (cross-platform)
    python indexing.py delete 2026-02-02
    python indexing.py delete 2026-02-01 2026-02-02

Requirements: Python 3.9+
"""

import argparse
import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    check_python_version,
    get_memory_dir,
    get_transcripts_dir,
    get_projects_dir,
    get_projects_index_file,
)


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
                            # Skip tool results and system content for user messages
                            if role == "user" and content.startswith("<"):
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
    Extract all transcripts, organized by day and session.

    Args:
        specific_day: Optional specific day to extract (YYYY-MM-DD format)

    Returns:
        Dict mapping date strings to lists of session dicts:
        {
            "2026-02-02": [
                {
                    "session_id": "abc123",
                    "filepath": "/path/to/file.jsonl",
                    "message_count": 42,
                    "messages": [{"role": "user", "content": "..."}, ...]
                },
                ...
            ],
            ...
        }
    """
    transcripts_dir = get_transcripts_dir()
    daily_data: dict[str, list[dict]] = defaultdict(list)

    if not transcripts_dir.exists():
        print(f"Transcript directory not found: {transcripts_dir}", file=sys.stderr)
        return dict(daily_data)

    for day_dir in sorted(transcripts_dir.iterdir()):
        if not day_dir.is_dir():
            continue

        day = day_dir.name

        # Skip if filtering for specific day
        if specific_day and day != specific_day:
            continue

        for jsonl_file in sorted(day_dir.glob("*.jsonl")):
            messages = parse_jsonl_file(jsonl_file)

            if messages:
                daily_data[day].append(
                    {
                        "session_id": jsonl_file.stem,
                        "filepath": str(jsonl_file),
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


def list_pending_days() -> list[str]:
    """List all days that have pending transcripts."""
    transcripts_dir = get_transcripts_dir()
    if not transcripts_dir.exists():
        return []

    days = []
    for day_dir in sorted(transcripts_dir.iterdir()):
        if day_dir.is_dir() and list(day_dir.glob("*.jsonl")):
            days.append(day_dir.name)

    return days


def delete_transcripts(day: str) -> tuple[bool, str]:
    """
    Delete transcript directory for a specific day.

    Cross-platform implementation using shutil.rmtree().

    Args:
        day: Date string in YYYY-MM-DD format

    Returns:
        Tuple of (success, message)
    """
    transcripts_dir = get_transcripts_dir()
    day_dir = transcripts_dir / day

    if not day_dir.exists():
        return False, f"Transcript directory not found: {day_dir}"

    if not day_dir.is_dir():
        return False, f"Not a directory: {day_dir}"

    # Count files before deletion for reporting
    file_count = len(list(day_dir.glob("*.jsonl")))
    total_size = sum(f.stat().st_size for f in day_dir.glob("*") if f.is_file())

    try:
        shutil.rmtree(day_dir)
        size_kb = total_size / 1024
        return True, f"Deleted {day}: {file_count} files ({size_kb:.1f} KB)"
    except OSError as e:
        return False, f"Failed to delete {day_dir}: {e}"


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

        original_path = data.get("originalPath", "")
        if not original_path:
            continue

        entries = data.get("entries", [])
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
        print("No transcripts found.", file=sys.stderr)
        return 1

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
    days = list_pending_days()
    if days:
        print("Pending transcript days:")
        for day in days:
            print(f"  {day}")
    else:
        print("No pending transcripts.")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    """Handle delete command."""
    if not args.days:
        print("Error: At least one day (YYYY-MM-DD) is required.", file=sys.stderr)
        return 1

    all_success = True
    for day in args.days:
        success, message = delete_transcripts(day)
        print(message)
        if not success:
            all_success = False

    return 0 if all_success else 1


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

    # Delete command
    delete_parser = subparsers.add_parser(
        "delete", help="Delete processed transcript directories (cross-platform)"
    )
    delete_parser.add_argument(
        "days", nargs="+", help="Days to delete (YYYY-MM-DD format)"
    )
    delete_parser.set_defaults(func=cmd_delete)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
