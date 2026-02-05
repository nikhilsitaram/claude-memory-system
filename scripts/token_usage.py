#!/usr/bin/env python3
"""Calculate memory system token usage."""

import json
import os
import sys
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from load_memory import filter_daily_content


def calculate_usage():
    """Calculate token usage for all memory components."""
    memory_dir = Path.home() / ".claude" / "memory"
    cwd = os.getcwd()

    # Read settings
    settings_file = memory_dir / "settings.json"
    settings = json.loads(settings_file.read_text()) if settings_file.exists() else {}

    # Settings with defaults
    global_short_days = settings.get("globalShortTerm", {}).get("workingDays", 2)
    global_long_limit = settings.get("globalLongTerm", {}).get("tokenLimit", 5000)
    project_short_days = settings.get("projectShortTerm", {}).get("workingDays", 7)
    project_long_limit = settings.get("projectLongTerm", {}).get("tokenLimit", 5000)

    # Short-term limits are calculated: workingDays × 750
    global_short_limit = global_short_days * 750
    project_short_limit = project_short_days * 750

    # Total budget is sum of all 4 components
    total_budget = global_long_limit + global_short_limit + project_long_limit + project_short_limit

    # Global long-term
    global_memory = memory_dir / "global-long-term-memory.md"
    global_long_term_tokens = global_memory.stat().st_size // 4 if global_memory.exists() else 0

    # Global short-term (daily files filtered to [global/*] tags)
    daily_dir = memory_dir / "daily"
    daily_files = sorted(daily_dir.glob("*.md"), reverse=True)[:global_short_days] if daily_dir.exists() else []
    global_short_term_bytes = 0
    for f in daily_files:
        content = f.read_text(encoding="utf-8")
        filtered = filter_daily_content(content, "global")
        global_short_term_bytes += len(filtered.encode("utf-8"))
    global_short_term_tokens = global_short_term_bytes // 4

    # Project: find by CWD match
    project_name = None
    project_days = []
    projects_index = memory_dir / "projects-index.json"
    if projects_index.exists():
        idx = json.loads(projects_index.read_text())
        for path, data in idx.get("projects", {}).items():
            if cwd == path or cwd.startswith(path + "/"):
                project_name = data.get("name")
                project_days = data.get("workDays", [])[:project_short_days]
                break

    # Project long-term
    project_long_term_tokens = 0
    if project_name:
        project_file = memory_dir / "project-memory" / f"{project_name}-long-term-memory.md"
        project_long_term_tokens = project_file.stat().st_size // 4 if project_file.exists() else 0

    # Project short-term (daily files filtered to [project/*] tags)
    project_short_term_bytes = 0
    project_short_days_actual = 0
    if project_name:
        # Scan all daily files for project-tagged content (up to limit)
        all_daily_files = sorted(daily_dir.glob("*.md"), reverse=True) if daily_dir.exists() else []
        for day_file in all_daily_files:
            if project_short_days_actual >= project_short_days:
                break
            content = day_file.read_text(encoding="utf-8")
            filtered = filter_daily_content(content, project_name)
            if filtered:
                project_short_term_bytes += len(filtered.encode("utf-8"))
                project_short_days_actual += 1
    project_short_term_tokens = project_short_term_bytes // 4

    # Count actual global days with content
    global_short_days_actual = sum(1 for f in daily_files if filter_daily_content(f.read_text(encoding="utf-8"), "global"))

    total_tokens = global_long_term_tokens + global_short_term_tokens + project_long_term_tokens + project_short_term_tokens

    # Output as key=value for easy parsing
    print(f"project_name={project_name or 'none'}")
    print(f"global_long_term_tokens={global_long_term_tokens}")
    print(f"global_long_limit={global_long_limit}")
    print(f"global_short_term_tokens={global_short_term_tokens}")
    print(f"global_short_limit={global_short_limit}")
    print(f"global_short_days_actual={global_short_days_actual}")
    print(f"project_long_term_tokens={project_long_term_tokens}")
    print(f"project_long_limit={project_long_limit}")
    print(f"project_short_term_tokens={project_short_term_tokens}")
    print(f"project_short_limit={project_short_limit}")
    print(f"project_short_days_actual={project_short_days_actual}")
    print(f"total_tokens={total_tokens}")
    print(f"total_budget={total_budget}")


if __name__ == "__main__":
    calculate_usage()
