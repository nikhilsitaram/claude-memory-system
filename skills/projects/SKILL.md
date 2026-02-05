---
name: projects
description: Manage Claude Code projects - list status, move/rename, merge orphans, cleanup stale data
user-invocable: true
---

# Project Management

Manage Claude Code project data including session history, file history, and memory system data.

## When to Use

- User asks about project status or health
- User mentions renaming or moving a project folder
- User wants to clean up old/stale project data
- User asks about orphaned folders in ~/.claude/
- User asks "what happened to my project data" after renaming

## Available Functions

Import the library in Python code blocks:

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home() / ".claude/scripts"))
from project_manager import (
    # Discovery
    list_projects,
    find_orphaned_folders,
    find_stale_entries,

    # Validation
    validate_move,
    validate_merge_orphan,

    # Planning (show what will happen)
    plan_move,
    plan_merge_orphan,
    plan_cleanup,

    # Execution (require confirmed=True)
    execute_move,
    execute_merge_orphan,
    execute_cleanup,

    # Recovery
    restore_from_backup,
    list_backups,

    # Memory file merge helper
    get_memory_files_for_merge,
)
```

## Function Reference

| Function | Purpose | Returns |
|----------|---------|---------|
| `list_projects()` | Get status of all indexed projects | `list[ProjectStatus]` |
| `find_orphaned_folders()` | Find Claude folders without valid projects | `list[OrphanInfo]` |
| `find_stale_entries()` | Find index entries where path is gone | `list[dict]` |
| `validate_move(old, new)` | Check if move can succeed | `ValidationResult` |
| `validate_merge_orphan(orphan, target)` | Check if merge can succeed | `ValidationResult` |
| `plan_move(old, new, mode)` | Show what move will do | `OperationPlan` |
| `plan_merge_orphan(orphan, target)` | Show what merge will do | `OperationPlan` |
| `plan_cleanup()` | Show what cleanup will do | `OperationPlan` |
| `execute_move(...)` | Perform move (needs `confirmed=True`) | `dict` |
| `execute_merge_orphan(...)` | Merge orphaned data (needs `confirmed=True`) | `dict` |
| `execute_cleanup(...)` | Remove stale entries (needs `confirmed=True`) | `dict` |
| `restore_from_backup(path)` | Undo last operation | `dict` |
| `list_backups()` | List available backups | `list[dict]` |
| `get_memory_files_for_merge(src, dst)` | Get memory files for intelligent merge | `dict` |

## Decision Tree

```
START: Get current state with list_projects() and find_orphaned_folders()

IF source directory EXISTS and user wants to move/rename:
   -> Use move flow (validate_move -> plan_move -> execute_move)
   -> This moves both the directory AND all Claude Code data

IF source directory GONE but orphaned Claude folders exist:
   -> Use merge-orphan flow
   -> This is the common case when user already renamed folder manually
   -> Steps:
      1. validate_merge_orphan(orphan_folder_name, target_path)
      2. plan_merge_orphan(orphan_folder_name, target_path)
      3. Show plan to user, get confirmation
      4. execute_merge_orphan(orphan_folder_name, target_path, confirmed=True)
      5. If both projects have memory files, use get_memory_files_for_merge()
         and perform intelligent merge (Claude reads both, writes combined)

IF user wants to clean up stale data:
   -> Use cleanup flow (plan_cleanup -> execute_cleanup)
   -> Only removes index entries, NOT folders

IF something went wrong:
   -> Use restore_from_backup() with the backup path from the operation result
```

## Example Workflows

### Status Check (`/projects` or `/projects list`)

```python
from project_manager import list_projects, find_orphaned_folders

projects = list_projects()
orphans = find_orphaned_folders()

# Present results
print("Projects:")
for p in projects:
    status = "ok" if p.exists else "MISSING"
    print(f"  [{status}] {p.name}: {p.original_path}")
    if p.issues:
        for issue in p.issues:
            print(f"      ! {issue}")

if orphans:
    print("\nOrphaned folders:")
    for o in orphans:
        orig = o.original_path_from_index or "(unknown original path)"
        print(f"  {o.folder_name}")
        print(f"    Was: {orig}")
```

### Merge Orphan (Common Case)

User already renamed `~/personal/personal-shopper` to `~/personal/cartwheel`.
Claude Code folders remain at `-home-nsitaram-personal-personal-shopper`.

```python
from pathlib import Path
from project_manager import (
    validate_merge_orphan,
    plan_merge_orphan,
    execute_merge_orphan,
    get_memory_files_for_merge,
)

orphan_folder = "-home-nsitaram-personal-personal-shopper"
target = Path.home() / "personal/cartwheel"

# 1. Validate
validation = validate_merge_orphan(orphan_folder, target)
if not validation.valid:
    print(f"Cannot merge: {validation.issues}")
    return

# 2. Plan and show user
plan = plan_merge_orphan(orphan_folder, target)
print(plan.summary)
# ... show detailed plan, ask for confirmation ...

# 3. Execute (only after user confirms!)
result = execute_merge_orphan(orphan_folder, target, confirmed=True)
if result["success"]:
    print(f"Merged successfully. Backup at: {result['backup_path']}")
    print(f"Renamed folders (not deleted): {result['renamed_folders']}")

    # 4. If both had memory files, merge them intelligently
    if result.get("orphan_project_name") and result.get("target_project_name"):
        files = get_memory_files_for_merge(
            result["orphan_project_name"],
            result["target_project_name"]
        )
        if files["source_exists"] and files["dest_exists"]:
            # Read both, combine intelligently (Claude does this)
            # Write merged result to dest_path
            pass
```

### Move Project (Full Migration)

User wants to move `~/old-location/project` to `~/new-location/project`.

```python
from pathlib import Path
from project_manager import validate_move, plan_move, execute_move

old = Path.home() / "old-location/project"
new = Path.home() / "new-location/project"

# 1. Validate
validation = validate_move(old, new)
if not validation.valid:
    print(f"Cannot move: {validation.issues}")
    return
if validation.warnings:
    print(f"Warnings: {validation.warnings}")

# 2. Plan
plan = plan_move(old, new, merge_mode="merge")
print(plan.summary)

# 3. Execute after confirmation
result = execute_move(old, new, merge_mode="merge", confirmed=True)
print(result["message"])
```

### Cleanup Stale Entries

```python
from project_manager import plan_cleanup, execute_cleanup

# 1. See what's stale
plan = plan_cleanup()
print(plan.summary)

# 2. Execute after confirmation
result = execute_cleanup(confirmed=True)
print(result["message"])
```

### Recovery

```python
from project_manager import list_backups, restore_from_backup

# See available backups
backups = list_backups()
for b in backups:
    print(f"{b['timestamp']}: {b['files']}")

# Restore from a specific backup
result = restore_from_backup(backups[0]["path"])
print(result["message"])
```

## Rules

1. **Always show plan before executing** - Users must understand what will happen
2. **Never pass `confirmed=True` without explicit user approval**
3. **Backups are automatic** - Tell user where they're stored (result includes backup_path)
4. **Orphaned folders are renamed, not deleted** - Renamed to `.merged.bak` for safety
5. **Memory files need intelligent merge** - Not just concatenated; Claude should:
   - Combine similar sections (merge two "## Key Learnings" into one)
   - Deduplicate equivalent learnings
   - Preserve unique content from both files
   - Keep the merged file coherent and well-organized

## Data Locations

### Claude Code Data (`~/.claude/`)
| Subdirectory | Contents |
|--------------|----------|
| `projects/{encoded}/` | Session folders, `sessions-index.json`, `.jsonl` files |
| `file-history/{encoded}/` | File edit history |
| `todos/{encoded}/` | TODO items |
| `shell-snapshots/{encoded}/` | Shell state |
| `debug/{encoded}/` | Debug logs |
| `history.jsonl` | Global history (contains path references) |

### Memory System Data (`~/.claude/memory/`)
| File | Purpose |
|------|---------|
| `projects-index.json` | Maps projects to work days |
| `project-memory/{name}-long-term-memory.md` | Project-specific learnings |
| `.backups/{timestamp}/` | Automatic backups before operations |

## Path Encoding

Claude Code encodes paths by replacing `/` and `.` with `-`:
- `/home/user/my-project` -> `-home-user-my-project`
- `/home/user/.config` -> `-home-user--config`

**Important**: This encoding is LOSSY. You cannot reliably decode back.
Always use `sessions-index.json` inside the folder for the authoritative original path.
