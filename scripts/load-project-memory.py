#!/usr/bin/env python3
"""
Load project memory for a specified directory.

This script allows manual loading of project history for any directory,
regardless of your current working directory. Useful for cross-referencing
work done in other projects.

Usage:
    python3 load-project-memory.py <project_path>
    python3 load-project-memory.py ~/personal/personal-shopper
    python3 load-project-memory.py ~/claude-code/projects/granada

The script will:
1. Normalize the path (expand ~, resolve to absolute)
2. Look up the project in projects-index.json (case-insensitive)
3. Output daily summaries for the last 14 work days
"""

import sys
import json
from pathlib import Path


def load_project_memory(project_path: str, max_days: int = 14) -> str:
    """Load project memory and return as formatted string."""
    memory_dir = Path.home() / ".claude" / "memory"
    index_file = memory_dir / "projects-index.json"
    daily_dir = memory_dir / "daily"

    # Normalize path
    project_path = Path(project_path).expanduser().resolve()
    project_path_lower = str(project_path).lower()

    # Check if index exists
    if not index_file.exists():
        return f"Error: Project index not found at {index_file}\nRun: python3 ~/.claude/skills/synthesize/build_projects_index.py"

    # Load index
    try:
        with open(index_file) as f:
            index = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return f"Error: Could not read project index: {e}"

    projects = index.get("projects", {})

    # Look up project (case-insensitive)
    if project_path_lower not in projects:
        # Try to find partial matches for helpful error message
        matches = [p for p in projects.keys() if project_path_lower in p or p in project_path_lower]
        if matches:
            return f"Error: Project not found: {project_path}\n\nDid you mean one of these?\n" + "\n".join(f"  - {projects[m]['originalPath']}" for m in matches[:5])
        else:
            return f"Error: Project not found: {project_path}\n\nKnown projects:\n" + "\n".join(f"  - {p['originalPath']}" for p in projects.values())

    project = projects[project_path_lower]
    project_name = project.get("name", "unknown")
    work_days = project.get("workDays", [])

    if not work_days:
        return f"Project '{project_name}' has no recorded work days."

    # Get last N work days
    project_days = sorted(work_days, reverse=True)[:max_days]

    # Build output
    lines = [
        f"## Project Memory: {project_name}",
        f"**Path**: {project['originalPath']}",
        f"**Work Days**: {len(work_days)} total, showing last {len(project_days)}",
        "",
    ]

    # Output each day's summary (oldest first for chronological reading)
    for date in sorted(project_days):
        daily_file = daily_dir / f"{date}.md"
        if daily_file.exists():
            lines.append(f"### {date}")
            lines.append(daily_file.read_text())
            lines.append("")
        else:
            lines.append(f"### {date}")
            lines.append("(No daily summary found)")
            lines.append("")

    return "\n".join(lines)


def list_projects() -> str:
    """List all known projects."""
    memory_dir = Path.home() / ".claude" / "memory"
    index_file = memory_dir / "projects-index.json"

    if not index_file.exists():
        return f"Error: Project index not found at {index_file}\nRun: python3 ~/.claude/skills/synthesize/build_projects_index.py"

    try:
        with open(index_file) as f:
            index = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        return f"Error: Could not read project index: {e}"

    projects = index.get("projects", {})

    lines = ["## Known Projects", ""]
    for path, data in sorted(projects.items(), key=lambda x: x[1]["name"].lower()):
        name = data["name"]
        original = data["originalPath"]
        days = len(data.get("workDays", []))
        latest = data["workDays"][-1] if data.get("workDays") else "N/A"
        lines.append(f"- **{name}** ({days} work days, latest: {latest})")
        lines.append(f"  `{original}`")

    return "\n".join(lines)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ["-h", "--help"]:
        print(__doc__)
        print("\nOptions:")
        print("  --list    List all known projects")
        print("  --days N  Load last N work days (default: 14)")
        sys.exit(0)

    # Handle --list flag
    if sys.argv[1] == "--list":
        print(list_projects())
        sys.exit(0)

    # Parse optional --days argument
    max_days = 14
    project_path = sys.argv[1]

    if len(sys.argv) >= 4 and sys.argv[2] == "--days":
        try:
            max_days = int(sys.argv[3])
        except ValueError:
            print(f"Error: Invalid --days value: {sys.argv[3]}")
            sys.exit(1)

    # Load and print project memory
    print(load_project_memory(project_path, max_days))


if __name__ == "__main__":
    main()
