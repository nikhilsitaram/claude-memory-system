---
name: projects
description: Use when user asks about project status, wants to rename/move a project, clean up stale data, or mentions orphaned folders in ~/.claude/
user-invokable: true
---

# Project Management

Manage Claude Code project data (sessions, file history, memory) when projects are moved, renamed, or need cleanup.

## When to Use

- User asks about project status or health
- User mentions renaming or moving a project folder
- User wants to clean up old/stale project data
- User asks about orphaned folders in ~/.claude/
- User asks "what happened to my project data" after renaming

## Import

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".claude/scripts"))
from project_manager import (
    list_projects, find_orphaned_folders, find_stale_entries,
    validate_move, validate_merge_orphan,
    plan_move, plan_merge_orphan, plan_cleanup,
    execute_move, execute_merge_orphan, execute_cleanup,
    restore_from_backup, list_backups, get_memory_files_for_merge,
)
```

## Decision Tree

```
START: list_projects() + find_orphaned_folders()

Source directory EXISTS → move flow (validate_move → plan_move → execute_move)
Source directory GONE, orphaned folders exist → merge-orphan flow (most common)
Stale index entries → cleanup flow (plan_cleanup → execute_cleanup)
Something went wrong → restore_from_backup()
```

## Example: Merge Orphan (Most Common Case)

User already renamed `~/personal/personal-shopper` to `~/personal/cartwheel`. Claude Code folders remain at the old encoded path.

```python
orphan_folder = "-home-nsitaram-personal-personal-shopper"
target = Path.home() / "personal/cartwheel"

# 1. Validate
validation = validate_merge_orphan(orphan_folder, target)
if not validation.valid:
    print(f"Cannot merge: {validation.issues}")

# 2. Plan and show user
plan = plan_merge_orphan(orphan_folder, target)
print(plan.summary)  # Show to user, get confirmation

# 3. Execute (only after user confirms!)
result = execute_merge_orphan(orphan_folder, target, confirmed=True)
# result includes backup_path, renamed_folders

# 4. If both had memory files, merge them intelligently
files = get_memory_files_for_merge(
    result["orphan_project_name"], result["target_project_name"]
)
# Read both, deduplicate, combine sections, write merged result
```

See `reference.md` for full function reference, additional workflow examples, and data location details.

## Rules

1. **Always show plan before executing** — users must understand what will happen
2. **Never pass `confirmed=True` without explicit user approval**
3. **Backups are automatic** — tell user where they're stored (result includes `backup_path`)
4. **Orphaned folders are renamed, not deleted** — renamed to `.merged.bak` for safety
5. **Memory files need intelligent merge** — deduplicate, combine sections, preserve unique content

## Common Mistakes

- **Executing without showing plan** — Always call `plan_*()` and display before `execute_*()`
- **Assuming encoded path can be decoded** — Encoding is lossy (`/` and `.` both become `-`). Use `sessions-index.json` for authoritative original path.
- **Concatenating memory files** — Memory merges require intelligent dedup, not concatenation

## Path Encoding

Claude Code encodes paths by replacing `/` and `.` with `-`:
- `/home/user/my-project` → `-home-user-my-project`

**Important**: This encoding is LOSSY. Always use `sessions-index.json` for the authoritative original path.
