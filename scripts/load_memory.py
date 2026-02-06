#!/usr/bin/env python3
"""
SessionStart hook - loads memory context for Claude Code.

This script runs on: startup, resume, clear, compact

It performs:
1. Loads global long-term memory
2. Loads project-specific long-term memory (if applicable)
3. Loads global short-term memory (recent daily summaries, filtered to [global/*] tags)
4. Loads project short-term memory (project history, filtered to [project/*] tags)
5. Checks for pending transcripts and prompts for synthesis

Output is printed to stdout and injected into Claude Code's context.

Requirements: Python 3.9+
"""

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    check_python_version,
    filter_daily_content,
    find_current_project,
    get_memory_dir,
    get_daily_dir,
    get_project_memory_dir,
    get_global_memory_file,
    get_projects_index_file,
    get_captured_sessions,
    load_settings,
    load_json_file,
    project_name_to_filename,
    get_working_days,
)

from indexing import list_pending_sessions


def get_last_synthesis_file() -> Path:
    """Get the path to the .last-synthesis timestamp file."""
    return get_memory_dir() / ".last-synthesis"


def should_synthesize(settings: dict) -> bool:
    """
    Determine if synthesis should run based on scheduling rules.

    Synthesis runs if:
    1. .last-synthesis file doesn't exist (never synthesized)
    2. Last synthesis was on a different day (UTC) - first session of day
    3. More than intervalHours since last synthesis

    Args:
        settings: Memory settings dict with synthesis.intervalHours

    Returns:
        True if synthesis should run, False otherwise
    """
    last_synthesis_file = get_last_synthesis_file()
    interval_hours = settings.get("synthesis", {}).get("intervalHours", 2)

    try:
        if not last_synthesis_file.exists():
            return True  # Never synthesized

        last_time_str = last_synthesis_file.read_text(encoding="utf-8").strip()
        last_time = datetime.fromisoformat(last_time_str)

        # Ensure timezone awareness
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        hours_since = (now - last_time).total_seconds() / 3600

        # First session of day (UTC) OR >interval since last
        return last_time.date() < now.date() or hours_since > interval_hours

    except (ValueError, OSError, IOError):
        return True  # Fallback: always synthesize if file missing/invalid


def update_last_synthesis_time() -> None:
    """Update .last-synthesis file with current UTC timestamp."""
    last_synthesis_file = get_last_synthesis_file()
    last_synthesis_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    last_synthesis_file.write_text(timestamp, encoding="utf-8")


def load_global_memory() -> tuple[str, int]:
    """Load global long-term memory file. Returns (content, bytes)."""
    global_file = get_global_memory_file()
    if not global_file.exists():
        return "", 0

    try:
        content = global_file.read_text(encoding="utf-8")
        return content, len(content.encode("utf-8"))
    except IOError:
        return "", 0


def load_project_memory(project_name: str) -> tuple[str, int]:
    """Load project-specific long-term memory. Returns (content, bytes)."""
    project_memory_dir = get_project_memory_dir()
    filename = project_name_to_filename(project_name)
    project_file = project_memory_dir / filename

    if not project_file.exists():
        return "", 0

    try:
        content = project_file.read_text(encoding="utf-8")
        return content, len(content.encode("utf-8"))
    except IOError:
        return "", 0


def load_daily_summaries(days_limit: int, scope: str = "global") -> tuple[list[tuple[str, str]], int]:
    """
    Load recent daily summaries, filtered by scope.

    Args:
        days_limit: Maximum number of working days to load
        scope: Filter scope - "global" for global entries, or project name for project entries

    Returns (list of (date, content) tuples, total bytes).
    """
    daily_dir = get_daily_dir()
    working_days = get_working_days(days_limit)
    summaries = []
    total_bytes = 0

    for date in working_days:
        daily_file = daily_dir / f"{date}.md"
        if daily_file.exists():
            try:
                raw_content = daily_file.read_text(encoding="utf-8")
                filtered_content = filter_daily_content(raw_content, scope)
                if filtered_content:
                    summaries.append((date, filtered_content))
                    total_bytes += len(filtered_content.encode("utf-8"))
            except IOError:
                continue

    return summaries, total_bytes


def load_project_history(
    project: dict, days_limit: int
) -> tuple[list[tuple[str, str]], int]:
    """
    Load project-specific work history (days worked in this project).

    Filters content to only include entries tagged with this project's name.
    Returns (list of (date, content) tuples, total bytes).
    """
    daily_dir = get_daily_dir()
    project_name = project.get("name", "")

    if not project_name:
        return [], 0

    # Get all daily files and filter by project content
    # We scan all daily files since project work may exist on any day
    all_daily_files = sorted(daily_dir.glob("*.md"), reverse=True)

    summaries = []
    total_bytes = 0

    for daily_file in all_daily_files:
        if len(summaries) >= days_limit:
            break

        try:
            raw_content = daily_file.read_text(encoding="utf-8")
            filtered_content = filter_daily_content(raw_content, project_name)
            if filtered_content:
                date = daily_file.stem  # YYYY-MM-DD from filename
                summaries.append((date, filtered_content))
                total_bytes += len(filtered_content.encode("utf-8"))
        except IOError:
            continue

    # Output oldest first for chronological reading
    summaries.reverse()

    return summaries, total_bytes


def count_pending_transcripts() -> int:
    """Count number of pending (unprocessed) session transcripts."""
    captured = get_captured_sessions()
    pending = list_pending_sessions(captured, min_file_size=1000)
    return len(pending)


def main() -> None:
    """Main entry point - outputs memory context to stdout."""
    check_python_version()

    # Load settings
    settings = load_settings()
    short_term_days = settings["globalShortTerm"]["workingDays"]
    project_days = settings["projectShortTerm"]["workingDays"]
    include_subdirs = settings["projectSettings"]["includeSubdirectories"]
    total_budget = settings["totalTokenBudget"]

    # Track total bytes for token estimation
    total_bytes = 0

    # Start output
    print("<memory>")

    # Include current local time for context
    now = datetime.now()
    utc_now = datetime.now(timezone.utc)
    utc_offset_hours = (now - utc_now.replace(tzinfo=None)).total_seconds() / 3600
    offset_sign = "+" if utc_offset_hours >= 0 else ""
    print(f"Current time: {now.strftime('%Y-%m-%d %H:%M')} (UTC{offset_sign}{utc_offset_hours:.0f})")
    print()

    # Check for pending transcripts (only if synthesis scheduling allows)
    pending_count = count_pending_transcripts()
    if pending_count > 0 and should_synthesize(settings):
        print("## AUTO-SYNTHESIZE REQUIRED")
        print(f"There are {pending_count} unprocessed session transcripts.")
        print()
        print("**IMPORTANT**: Before responding to the user, spawn a subagent to process transcripts:")
        print("```")
        print('Use the Task tool with subagent_type="general-purpose", model="haiku" and this prompt:')
        print('"Process pending memory transcripts using the /synthesize skill instructions.')
        print("Read ~/.claude/skills/synthesize/SKILL.md for the full process.")
        print("Extract transcripts, create daily summaries, update long-term memory files,")
        print('and delete processed transcript files. Return a brief summary of what was processed."')
        print("```")
        print()
        # Update synthesis timestamp so we don't prompt again within interval
        update_last_synthesis_time()

    # Load global long-term memory
    global_content, global_bytes = load_global_memory()
    total_bytes += global_bytes

    if global_content:
        print("## Long-Term Memory")
        print(global_content)
        print()

    # Detect current project
    pwd = os.getcwd()
    projects_index = load_json_file(get_projects_index_file(), {})
    current_project = find_current_project(projects_index, pwd, include_subdirs)

    # Load project-specific long-term memory
    if current_project:
        project_name = current_project.get("name", "")
        if project_name:
            project_content, project_bytes = load_project_memory(project_name)
            total_bytes += project_bytes

            if project_content:
                print(f"## Project Long-Term Memory: {project_name}")
                print(project_content)
                print()

    # Load global short-term memory (recent daily summaries, filtered to [global/*] tags)
    global_summaries, global_daily_bytes = load_daily_summaries(short_term_days, scope="global")
    total_bytes += global_daily_bytes

    if global_summaries:
        print("## Global Short-Term Memory")
        for date, content in global_summaries:
            print(f"### {date}")
            print(content)
            print()

    # Load project short-term memory (project history, filtered to [project/*] tags)
    if current_project:
        project_name = current_project.get("name", "unknown")
        project_history, history_bytes = load_project_history(current_project, project_days)
        total_bytes += history_bytes

        if project_history:
            print(f"## Project Short-Term Memory: {project_name}")
            print()
            for date, content in project_history:
                print(f"### {date}")
                print(content)
                print()

    print("</memory>")

    # Token estimation (informational)
    estimated_tokens = total_bytes // 4
    if estimated_tokens > total_budget:
        print(f"<!-- Memory usage: ~{estimated_tokens} tokens (budget: {total_budget}) -->")
        print("<!-- Consider running /synthesize to consolidate older sessions -->")


if __name__ == "__main__":
    main()
