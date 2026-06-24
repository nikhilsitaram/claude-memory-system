---
name: projects
description: Use when user asks about project status, wants to rename/move a project, clean up stale data, or mentions orphaned folders in ~/.claude/
user-invokable: true
---

# Project Management

Manage Claude Code project data (sessions, file history, memory) when projects are moved, renamed, or need cleanup.

## How the index is built (read this first)

`projects-index.json` is **rebuilt from scratch hourly** by synthesis (`indexing.build_projects_index`). It derives each project's path from the **`cwd` field recorded inside the newest `.jsonl` transcript** in each `~/.claude/projects/<encoded>/` folder. Current Claude Code does **not** write `sessions-index.json` — the transcript `cwd` is the authoritative path signal.

Consequence: a rename only sticks if the transcripts' `cwd` is updated. Change the index but leave the old `cwd` in the transcripts, and the next synthesis rebuild silently reverts your change. `execute_move` / `execute_merge_orphan` now rewrite `cwd` in the destination transcripts automatically — but **always confirm durability** with `rebuild_and_verify_index()` after executing.

> Upstream: if Claude Code ships native session relocation (anthropics/claude-code#27967 — "Allow relocating active sessions to a new working directory"), parts of this move flow could defer to it. Until then, the `cwd` rewrite is the durable mechanism.

## Running

Use a **3.10+ interpreter**. The system `python3` on macOS is 3.9 and crashes on this library's `X | None` type hints. Run snippets with `uv run --no-project python - <<'PY' … PY` (or an explicit `python3.12` / `python3.13`), never bare `python3`.

Do **not** run a move from a live session whose cwd *is* the project being moved — that session keeps appending the old path to `history.jsonl` and its transcript after the move, re-polluting the data. Run from `~`, and ideally close other open sessions in that project first.

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
    rebuild_and_verify_index,
    restore_from_backup, list_backups, get_memory_files_for_merge,
)
```

## Decision Tree

```
START: list_projects() + find_orphaned_folders() + find_stale_entries()

Source directory EXISTS → move flow (validate_move → plan_move → execute_move)
Source directory GONE, orphaned folders exist → merge-orphan flow (most common)
Stale index entries → cleanup flow (plan_cleanup → execute_cleanup)
Something went wrong → restore_from_backup()

ALWAYS after any execute_move / execute_merge_orphan:
  rebuild_and_verify_index(new_path)  → check result["durable"] is True
```

**Hybrid state** (seen when a rename was done partly by hand): source gone **and** the new
location already has its own transcript folder **and** the index already merged both encoded
paths under one name. The move/merge tools handle the files, but the rename still won't stick
until the old `cwd` is gone from every transcript — run `rebuild_and_verify_index()` and, if
`durable` is False, rewrite the lingering `cwd`s (`rewrite_cwd_in_transcripts`) and re-verify.

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

# 4. Verify the rename is durable (survives the next synthesis rebuild)
check = rebuild_and_verify_index(str(target))
print(check["message"])          # warn the user if not durable
assert check["durable"], "rename will revert — see lingering cwd in transcripts"

# 5. If both had memory files, merge them intelligently
files = get_memory_files_for_merge(
    result["orphan_project_name"], result["target_project_name"]
)
# Read both, deduplicate, combine sections, write merged result
```

See `reference.md` for full function reference, additional workflow examples, and data location details.

## Rules

1. **Always show plan before executing** — users must understand what will happen
2. **Never pass `confirmed=True` without explicit user approval**
3. **Always verify durability** — after `execute_move`/`execute_merge_orphan`, call `rebuild_and_verify_index(new_path)` and confirm `result["durable"]`; the index is rebuilt from transcript `cwd` hourly, so an unverified rename can silently revert
4. **Backups are automatic** — tell user where they're stored (result includes `backup_path`)
5. **Orphaned folders are renamed, not deleted** — renamed to `.merged.bak` for safety
6. **Memory files need intelligent merge** — deduplicate, combine sections, preserve unique content

## Common Mistakes

- **Executing without showing plan** — Always call `plan_*()` and display before `execute_*()`
- **Skipping the durability check** — A move can report `success: True` yet revert on the next hourly synthesis if any transcript still records the old `cwd`. Always `rebuild_and_verify_index()`.
- **Running the move from inside the project's own live session** — that session re-pollutes `history.jsonl`/transcripts with the old path after the move. Run from `~`.
- **Assuming encoded path can be decoded** — Encoding is lossy (`/` and `.` both become `-`). Read the authoritative path from the transcript `cwd`, not by decoding the folder name.
- **Concatenating memory files** — Memory merges require intelligent dedup, not concatenation

## Path Encoding

Claude Code encodes paths by replacing `/` and `.` with `-`:
- `/home/user/my-project` → `-home-user-my-project`

**Important**: This encoding is LOSSY (you cannot reliably decode it back). The authoritative original path is the `cwd` field inside the folder's `.jsonl` transcripts (`get_original_path_from_folder()` reads `sessions-index.json` when present, but current Claude Code no longer writes it, so `cwd` is the real source of truth).
