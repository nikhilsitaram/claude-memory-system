#!/usr/bin/env python3
"""
One-time migration script for Claude Code Memory System.

Transforms existing memory files from old nested format to new flat format:

Old daily format:
    ## Learnings
    - **Title** [scope/type]: Description
      - Lesson: Actionable takeaway

New daily format:
    ## Learnings
    - [scope/type] Title - Description

    ## Lessons
    - [scope/type] Actionable takeaway

Old long-term format (in ## Pinned and ## Key Learnings):
    - **Title** [type] (date): Description
      - Lesson: Actionable takeaway

New long-term format:
    - [type] (date) Title - Description
    (lessons extracted to ## Key Lessons)

Usage:
    python migrate_format.py              # Run migration
    python migrate_format.py --dry-run    # Preview changes without writing

Requirements: Python 3.9+
"""

import argparse
import re
import sys
from pathlib import Path

# Import from memory_utils for path helpers
try:
    from memory_utils import (
        check_python_version,
        get_memory_dir,
        get_project_memory_dir,
        get_global_memory_file,
        get_daily_dir,
    )
except ImportError:
    # Support running from repo directory
    sys.path.insert(0, str(Path(__file__).parent))
    from memory_utils import (
        check_python_version,
        get_memory_dir,
        get_project_memory_dir,
        get_global_memory_file,
        get_daily_dir,
    )


def migrate_lesson_line(line: str) -> str:
    """
    Transform a lesson line from old to new format.

    Old: - [type] (date): Description
    New: - [type] (date) Description

    Removes the colon after the date.
    """
    stripped = line.strip()

    # Pattern: - [tag] (date): text
    pattern = r'^(\s*)- \[([^\]]+)\]\s*\((\d{4}-\d{2}-\d{2})\):\s*(.*)$'
    match = re.match(pattern, line)

    if match:
        indent, tag, date, text = match.groups()
        return f"{indent}- [{tag}] ({date}) {text}"

    return line


def migrate_learning_line(line: str) -> tuple[str, str | None]:
    """
    Transform a learning line from old to new format.

    Old: - **Title** [scope/type]: Description
    New: - [scope/type] Title - Description

    Also handles long-term format with dates:
    Old: - **Title** [type] (date): Description
    New: - [type] (date) Title - Description

    Returns (migrated_line, lesson_if_found).
    """
    stripped = line.strip()

    # Already in new format (check for tag at start, no bold)
    if stripped.startswith("- [") and "**" not in stripped:
        # But might need colon removal - handle that in migrate_lesson_line
        return migrate_lesson_line(line), None

    # Match old format: - **Title** [tag] (optional date): Description
    # Pattern captures: title, tag, optional date, optional description
    pattern = r'^(\s*)- \*\*(.+?)\*\* \[([^\]]+)\](?:\s*\((\d{4}-\d{2}-\d{2})\))?:?\s*(.*)$'
    match = re.match(pattern, line)

    if not match:
        return line, None

    indent, title, tag, date, desc = match.groups()

    # Build new format
    date_part = f" ({date})" if date else ""
    if desc:
        new_line = f"{indent}- [{tag}]{date_part} {title} - {desc}"
    else:
        new_line = f"{indent}- [{tag}]{date_part} {title}"

    return new_line, None


def extract_lesson_from_indented(line: str) -> str | None:
    """Extract lesson text from '  - Lesson: ...' line."""
    stripped = line.strip()
    if stripped.startswith("- Lesson:"):
        return stripped[len("- Lesson:"):].strip()
    return None


def get_tag_from_learning(learning_line: str) -> str | None:
    """Extract tag from learning line like '- [scope/type] ...'"""
    match = re.search(r'\[([^\]]+)\]', learning_line)
    if match:
        tag = match.group(1)
        # Strip scope if present (e.g., 'claude-memory-system/error' -> 'error')
        if '/' in tag:
            return tag.split('/')[-1]
        return tag
    return None


def migrate_daily_file(content: str) -> str:
    """
    Migrate a daily file from old to new format.

    Extracts lessons into a separate ## Lessons section.
    """
    lines = content.split('\n')
    result_lines = []
    lessons = []
    in_learnings_section = False
    current_learning_tag = None

    i = 0
    while i < len(lines):
        line = lines[i]

        # Track section
        if line.startswith('## '):
            in_learnings_section = line.strip() == '## Learnings'
            result_lines.append(line)
            i += 1
            continue

        if in_learnings_section:
            # Check for learning line
            if line.strip().startswith('- **'):
                migrated, _ = migrate_learning_line(line)
                result_lines.append(migrated)
                current_learning_tag = get_tag_from_learning(migrated)
                i += 1

                # Check for nested lesson
                if i < len(lines):
                    next_line = lines[i]
                    lesson_text = extract_lesson_from_indented(next_line)
                    if lesson_text and current_learning_tag:
                        # Preserve scope from the learning for the lesson
                        learning_match = re.search(r'\[([^\]]+)\]', line)
                        if learning_match:
                            full_tag = learning_match.group(1)
                            lessons.append(f"- [{full_tag}] {lesson_text}")
                        i += 1
                continue
            # Already new format or other content
            result_lines.append(line)
            i += 1
        else:
            result_lines.append(line)
            i += 1

    # Build output with ## Lessons section
    output = '\n'.join(result_lines)

    # Add ## Lessons section if we extracted lessons
    if lessons:
        # Find where to insert (after ## Learnings section content, or at end)
        if '## Lessons' not in output:
            # Ensure there's a newline before ## Lessons
            if not output.endswith('\n\n'):
                if output.endswith('\n'):
                    output += '\n'
                else:
                    output += '\n\n'
            output += '## Lessons\n'
            for lesson in lessons:
                output += f"{lesson}\n"

    return output


def migrate_long_term_file(content: str) -> str:
    """
    Migrate a long-term memory file from old to new format.

    Handles ## Pinned, ## Key Decisions, and ## Key Learnings sections.
    Extracts nested lessons from Pinned to Key Lessons (but not from other sections
    since they should already be in Key Lessons).
    """
    lines = content.split('\n')
    result_lines = []
    in_migrate_section = False
    current_section = None

    # Sections where we migrate format
    migrate_sections = {'## Pinned', '## Key Decisions', '## Key Learnings', '## Key Lessons'}

    i = 0
    while i < len(lines):
        line = lines[i]

        # Track section
        if line.startswith('## '):
            current_section = line.strip()
            in_migrate_section = current_section in migrate_sections
            result_lines.append(line)
            i += 1
            continue

        if in_migrate_section:
            stripped = line.strip()
            # Check for old-format learning line (with **)
            if stripped.startswith('- **'):
                migrated, _ = migrate_learning_line(line)
                result_lines.append(migrated)

                # Check for nested lesson line to skip
                if i + 1 < len(lines):
                    next_line = lines[i + 1]
                    lesson_text = extract_lesson_from_indented(next_line)
                    if lesson_text:
                        # Skip the nested lesson line - lessons should already be in Key Lessons
                        i += 2
                        continue

                i += 1
                continue

            # Check for new-format line that might need colon removal
            if stripped.startswith('- ['):
                migrated = migrate_lesson_line(line)
                result_lines.append(migrated)
                i += 1
                continue

            result_lines.append(line)
            i += 1
        else:
            result_lines.append(line)
            i += 1

    return '\n'.join(result_lines)


def migrate_file(filepath: Path, dry_run: bool = False) -> tuple[bool, str]:
    """
    Migrate a single file.

    Returns (changed, message).
    """
    if not filepath.exists():
        return False, f"File not found: {filepath}"

    content = filepath.read_text(encoding='utf-8')

    # Detect file type and migrate
    if filepath.name.endswith('-long-term-memory.md') or filepath.name == 'global-long-term-memory.md':
        new_content = migrate_long_term_file(content)
    else:
        new_content = migrate_daily_file(content)

    if content == new_content:
        return False, f"No changes needed: {filepath.name}"

    if not dry_run:
        filepath.write_text(new_content, encoding='utf-8')

    return True, f"{'Would migrate' if dry_run else 'Migrated'}: {filepath.name}"


def main() -> int:
    """Main entry point."""
    check_python_version()

    parser = argparse.ArgumentParser(
        description="Migrate memory files from old nested format to new flat format"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be migrated without making changes"
    )
    args = parser.parse_args()

    if args.dry_run:
        print("DRY RUN - no changes will be made\n")

    files_to_migrate = []

    # Daily files
    daily_dir = get_daily_dir()
    if daily_dir.exists():
        files_to_migrate.extend(sorted(daily_dir.glob('*.md')))

    # Global long-term memory
    global_file = get_global_memory_file()
    if global_file.exists():
        files_to_migrate.append(global_file)

    # Project long-term memory files
    project_dir = get_project_memory_dir()
    if project_dir.exists():
        files_to_migrate.extend(sorted(project_dir.glob('*-long-term-memory.md')))

    if not files_to_migrate:
        print("No memory files found to migrate")
        return 0

    print(f"Found {len(files_to_migrate)} files to check\n")

    changed_count = 0
    for filepath in files_to_migrate:
        changed, message = migrate_file(filepath, args.dry_run)
        print(message)
        if changed:
            changed_count += 1

    print(f"\n{'Would migrate' if args.dry_run else 'Migrated'} {changed_count} file(s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
