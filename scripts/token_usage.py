#!/usr/bin/env python3
"""Calculate memory system token usage."""

import os
import sys
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    filter_daily_content,
    find_current_project,
    get_daily_dir,
    get_global_memory_file,
    get_project_memory_dir,
    get_projects_index_file,
    load_json_file,
    load_settings,
    project_name_to_filename,
)


def calculate_usage():
    """Calculate token usage for all memory components."""
    cwd = os.getcwd()

    # Load settings using shared utility (handles missing file + JSON errors)
    settings = load_settings()

    global_short_days = settings["globalShortTerm"]["workingDays"]
    global_long_limit = settings["globalLongTerm"]["tokenLimit"]
    global_short_limit = settings["globalShortTerm"]["tokenLimit"]
    project_short_days = settings["projectShortTerm"]["workingDays"]
    project_long_limit = settings["projectLongTerm"]["tokenLimit"]
    project_short_limit = settings["projectShortTerm"]["tokenLimit"]
    total_budget = settings["totalTokenBudget"]
    include_subdirs = settings["projectSettings"]["includeSubdirectories"]

    # Global long-term
    global_memory = get_global_memory_file()
    global_long_term_tokens = global_memory.stat().st_size // 4 if global_memory.exists() else 0

    # Global short-term (daily files filtered to [global/*] tags)
    daily_dir = get_daily_dir()
    daily_files = sorted(daily_dir.glob("*.md"), reverse=True)[:global_short_days] if daily_dir.exists() else []
    global_short_term_bytes = 0
    for f in daily_files:
        content = f.read_text(encoding="utf-8")
        filtered = filter_daily_content(content, "global")
        global_short_term_bytes += len(filtered.encode("utf-8"))
    global_short_term_tokens = global_short_term_bytes // 4

    # Project: find by CWD match using shared utility (lowercases CWD correctly)
    projects_index = load_json_file(get_projects_index_file(), {})
    current_project = find_current_project(projects_index, cwd, include_subdirs)
    project_name = current_project.get("name") if current_project else None

    # Project long-term (uses project_name_to_filename for correct kebab-case)
    project_long_term_tokens = 0
    if project_name:
        project_file = get_project_memory_dir() / project_name_to_filename(project_name)
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
