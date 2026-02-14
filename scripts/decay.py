#!/usr/bin/env python3
"""
Age-based decay for Claude Code Memory System.

Processes long-term memory files and:
1. Archives learnings older than decay.ageDays (default: 30)
2. Purges archive entries older than decay.archiveRetentionDays (default: 365)

Usage:
    python decay.py              # Run decay on all memory files
    python decay.py --dry-run    # Show what would be archived/purged

Requirements: Python 3.9+
"""

import argparse
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Import from memory_utils
try:
    from memory_utils import (
        check_python_version,
        get_global_memory_file,
        get_memory_dir,
        get_project_memory_dir,
        get_projects_index_file,
        load_json_file,
        load_settings,
        project_name_to_filename,
    )
except ImportError:
    # Support running from repo directory
    sys.path.insert(0, str(Path(__file__).parent))
    from memory_utils import (
        check_python_version,
        get_global_memory_file,
        get_memory_dir,
        get_project_memory_dir,
        get_projects_index_file,
        load_json_file,
        load_settings,
        project_name_to_filename,
    )

# Default decay thresholds (used as fallbacks when settings.json missing)
DEFAULT_AGE_DAYS = 30
DEFAULT_PROJECT_WORKING_DAYS = 20
DEFAULT_ARCHIVE_RETENTION_DAYS = 365

# Pattern to extract date from learning: - (YYYY-MM-DD) [type] description
DATE_PATTERN = re.compile(r"\((\d{4}-\d{2}-\d{2})\)")

# Auto-pinned sections (never decay)
AUTO_PINNED_SECTIONS = {
    "## About Me",
    "## Current Projects",
    "## Technical Environment",
    "## Patterns & Preferences",
    "## Pinned",
}

# Pattern to match archive section headers: ## Archived YYYY-MM-DD
ARCHIVE_HEADER_PATTERN = re.compile(r"^## Archived (\d{4}-\d{2}-\d{2})$")

# Decay-eligible sections (same for global and project)
DECAY_ELIGIBLE_SECTIONS = {
    "## Key Actions",
    "## Key Decisions",
    "## Key Learnings",
    "## Key Lessons",
}


def parse_learning_date(line: str) -> date | None:
    """Extract creation date from learning line."""
    match = DATE_PATTERN.search(line)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def is_protected_section(section_name: str) -> bool:
    """Check if section is auto-pinned (never decays)."""
    return section_name in AUTO_PINNED_SECTIONS


def is_decay_eligible(section_name: str) -> bool:
    """Check if section is eligible for decay."""
    return section_name in DECAY_ELIGIBLE_SECTIONS


def parse_sections(content: str) -> list[tuple[str, str]]:
    """Parse markdown into sections (header, content) tuples."""
    sections = []
    current_header = ""
    current_content = []

    for line in content.split("\n"):
        if line.startswith("## "):
            if current_header or current_content:
                sections.append((current_header, "\n".join(current_content)))
            current_header = line.strip()
            current_content = []
        else:
            current_content.append(line)

    # Don't forget the last section
    if current_header or current_content:
        sections.append((current_header, "\n".join(current_content)))

    return sections


def parse_learnings(section_content: str) -> list[tuple[str, date | None]]:
    """Parse learnings from section content with their dates.

    Format: "- (date) [type] description"
    """
    learnings = []

    for line in section_content.split("\n"):
        stripped = line.strip()
        # Format: "- (date) [type] description"
        if stripped.startswith("- "):
            learnings.append((line, parse_learning_date(line)))

    return learnings


def should_decay_entry(
    learning_date: date,
    age_days: int,
    today: date,
    project_work_days: list[str] | None = None,
    project_decay_threshold: int | None = None,
) -> bool:
    """Determine if a learning entry should be decayed.

    For global LTM: calendar-day based (entry older than age_days).
    For project LTM: working-day based (more than project_decay_threshold
    project work days have occurred since the entry date).
    """
    if project_work_days is not None and project_decay_threshold is not None:
        # Working-day decay: count project work days strictly after the learning date
        learning_date_str = learning_date.isoformat()
        days_after = sum(1 for d in project_work_days if d > learning_date_str)
        return days_after >= project_decay_threshold
    else:
        # Calendar-day decay
        cutoff_date = today - timedelta(days=age_days)
        return learning_date < cutoff_date


def build_project_work_days_map() -> dict[str, list[str]]:
    """Build a mapping from project LTM filename to work days list.

    Reads projects-index.json and maps each project's expected LTM filename
    to its list of work days.
    """
    index = load_json_file(get_projects_index_file(), {})
    mapping: dict[str, list[str]] = {}
    for project_info in index.get("projects", {}).values():
        name = project_info.get("name", "")
        work_days = sorted(project_info.get("workDays", []))
        if name:
            filename = project_name_to_filename(name)
            mapping[filename] = work_days
    return mapping


def decay_file(
    filepath: Path,
    age_days: int,
    dry_run: bool = False,
    project_work_days: list[str] | None = None,
    project_decay_threshold: int | None = None,
) -> tuple[int, list[str]]:
    """
    Process a memory file, archiving old learnings.

    For project files, pass project_work_days and project_decay_threshold
    to use working-day-based decay instead of calendar-day decay.

    Returns (archived_count, archived_learnings).
    """
    if not filepath.exists():
        return 0, []

    content = filepath.read_text(encoding="utf-8")
    sections = parse_sections(content)
    today = datetime.now(timezone.utc).date()

    archived_learnings = []
    modified_sections = []

    for header, section_content in sections:
        if is_protected_section(header):
            # Keep protected sections unchanged
            modified_sections.append((header, section_content))
            continue

        if not is_decay_eligible(header):
            # Keep non-eligible sections unchanged
            modified_sections.append((header, section_content))
            continue

        # Parse and filter learnings
        learnings = parse_learnings(section_content)
        kept_learnings = []

        for learning_text, learning_date in learnings:
            if learning_date is None:
                # No date = protected from decay
                kept_learnings.append(learning_text)
            elif not should_decay_entry(
                learning_date, age_days, today,
                project_work_days, project_decay_threshold,
            ):
                # Recent enough to keep
                kept_learnings.append(learning_text)
            else:
                # Old learning - archive it
                archived_learnings.append(
                    f"{learning_text.strip()}\n  - *Source: {filepath.name}*"
                )

        # Reconstruct section with kept learnings
        if kept_learnings:
            new_content = "\n".join(kept_learnings)
        else:
            # Keep section header comment if present
            lines = section_content.split("\n")
            comment_lines = [line for line in lines if line.strip().startswith("<!--")]
            new_content = "\n".join(comment_lines) if comment_lines else ""

        modified_sections.append((header, new_content))

    if archived_learnings and not dry_run:
        # Write updated file
        new_content = ""
        for header, section_content in modified_sections:
            if header:
                new_content += f"{header}\n"
            new_content += f"{section_content}\n"

        filepath.write_text(new_content.strip() + "\n", encoding="utf-8")

    return len(archived_learnings), archived_learnings


def append_to_archive(learnings: list[str], dry_run: bool = False) -> None:
    """Append archived learnings to decay archive."""
    if not learnings:
        return

    archive_file = get_memory_dir() / ".decay-archive.md"
    today_header = f"## Archived {datetime.now(timezone.utc).date().isoformat()}"

    if dry_run:
        return

    # Read existing archive or create new
    if archive_file.exists():
        content = archive_file.read_text(encoding="utf-8")
    else:
        content = "# Decay Archive\n\n"

    # Check if today's header already exists
    if today_header in content:
        # Append to existing section
        lines = content.split("\n")
        new_lines = []
        for line in lines:
            new_lines.append(line)
            if line.strip() == today_header:
                # Add learnings after header
                for learning in learnings:
                    new_lines.append(learning)
                    new_lines.append("")

        content = "\n".join(new_lines)
    else:
        # Add new section at top (after header)
        parts = content.split("\n", 2)
        header = parts[0] if parts else "# Decay Archive"
        rest = parts[2] if len(parts) > 2 else ""

        new_section = f"\n{today_header}\n"
        for learning in learnings:
            new_section += f"{learning}\n\n"

        content = f"{header}\n{new_section}{rest}"

    archive_file.write_text(content.strip() + "\n", encoding="utf-8")


def purge_old_archives(retention_days: int, dry_run: bool = False) -> int:
    """Remove archive sections older than retention_days."""
    archive_file = get_memory_dir() / ".decay-archive.md"

    if not archive_file.exists():
        return 0

    content = archive_file.read_text(encoding="utf-8")
    today = datetime.now(timezone.utc).date()
    cutoff_date = today - timedelta(days=retention_days)

    # Parse archive sections
    lines = content.split("\n")
    new_lines = []
    skip_until_next_header = False
    purged_count = 0

    for line in lines:
        match = ARCHIVE_HEADER_PATTERN.match(line.strip())
        if match:
            try:
                archive_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                if archive_date < cutoff_date:
                    skip_until_next_header = True
                    purged_count += 1
                    continue
                else:
                    skip_until_next_header = False
            except ValueError:
                skip_until_next_header = False

        if not skip_until_next_header:
            new_lines.append(line)

    if purged_count > 0 and not dry_run:
        archive_file.write_text("\n".join(new_lines).strip() + "\n", encoding="utf-8")

    return purged_count


def main() -> int:
    """Main entry point."""
    check_python_version()

    parser = argparse.ArgumentParser(
        description="Apply age-based decay to long-term memory files"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be archived/purged without making changes"
    )
    args = parser.parse_args()

    settings = load_settings()
    age_days = settings.get("decay", {}).get("ageDays", DEFAULT_AGE_DAYS)
    project_working_days = settings.get("decay", {}).get("projectWorkingDays", DEFAULT_PROJECT_WORKING_DAYS)
    retention_days = settings.get("decay", {}).get("archiveRetentionDays", DEFAULT_ARCHIVE_RETENTION_DAYS)

    if args.dry_run:
        print("DRY RUN - no changes will be made")
        print()

    print(f"Decay settings: global={age_days} calendar days, project={project_working_days} working days, purge after {retention_days} days")
    print()

    total_archived = 0
    all_archived_learnings = []

    # Process global memory (calendar-day decay)
    global_file = get_global_memory_file()
    if global_file.exists():
        count, learnings = decay_file(global_file, age_days, args.dry_run)
        if count > 0:
            print(f"Global memory: archived {count} learning(s)")
            total_archived += count
            all_archived_learnings.extend(learnings)

    # Process project memory files (working-day decay)
    project_dir = get_project_memory_dir()
    if project_dir.exists():
        work_days_map = build_project_work_days_map()
        for project_file in project_dir.glob("*-long-term-memory.md"):
            work_days = work_days_map.get(project_file.name, [])
            count, learnings = decay_file(
                project_file, age_days, args.dry_run,
                project_work_days=work_days if work_days else None,
                project_decay_threshold=project_working_days if work_days else None,
            )
            if count > 0:
                print(f"{project_file.name}: archived {count} learning(s)")
                total_archived += count
                all_archived_learnings.extend(learnings)

    # Append to archive
    if all_archived_learnings:
        append_to_archive(all_archived_learnings, args.dry_run)
        print(f"\nTotal archived: {total_archived} learning(s)")
    else:
        print("No learnings to archive")

    # Purge old archives
    purged = purge_old_archives(retention_days, args.dry_run)
    if purged > 0:
        print(f"Purged {purged} old archive section(s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
