# Project Management — Full Reference

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
| `execute_move(...)` | Perform move (needs `confirmed=True`); rewrites transcript `cwd` | `dict` |
| `execute_merge_orphan(...)` | Merge orphaned data (needs `confirmed=True`); rewrites transcript `cwd` | `dict` |
| `execute_cleanup(...)` | Remove stale entries (needs `confirmed=True`) | `dict` |
| `rebuild_and_verify_index(path)` | Rebuild index from transcripts; confirm rename is durable | `dict` |
| `rewrite_cwd_in_transcripts(folder, old, new)` | Rewrite `cwd` field in a folder's `.jsonl` transcripts | `dict` |
| `refresh_synthesis_offsets(session_ids, folder)` | Reset synthesis byte-offsets after a `cwd` rewrite | `int` |
| `restore_from_backup(path)` | Undo last operation | `dict` |
| `list_backups()` | List available backups | `list[dict]` |
| `get_memory_files_for_merge(src, dst)` | Get memory files for intelligent merge | `dict` |

`execute_move` / `execute_merge_orphan` return an extra `cwd_files_rewritten` count.
`rebuild_and_verify_index` returns `{"durable": bool, "entry": dict|None, "stale_paths": list[str], "message": str}` — check `durable` before declaring a move complete.

## Additional Workflow Examples

### Status Check (`/projects` or `/projects list`)

```python
from project_manager import list_projects, find_orphaned_folders

projects = list_projects()
orphans = find_orphaned_folders()

for p in projects:
    status = "ok" if p.exists else "MISSING"
    print(f"  [{status}] {p.name}: {p.original_path}")
    if p.issues:
        for issue in p.issues:
            print(f"      ! {issue}")

if orphans:
    for o in orphans:
        orig = o.original_path or "(unknown original path)"
        print(f"  {o.folder_name} — Was: {orig}")
```

### Move Project (Full Migration)

```python
from pathlib import Path
from project_manager import validate_move, plan_move, execute_move

old = Path.home() / "old-location/project"
new = Path.home() / "new-location/project"

validation = validate_move(old, new)
if not validation.valid:
    print(f"Cannot move: {validation.issues}")

plan = plan_move(old, new, merge_mode="merge")
print(plan.summary)

result = execute_move(old, new, merge_mode="merge", confirmed=True)

# Durability check — the index is rebuilt from transcript cwd hourly, so a
# move that doesn't update cwd silently reverts. execute_move now rewrites cwd,
# but always confirm:
check = rebuild_and_verify_index(str(new))
print(check["message"])
assert check["durable"]
```

### Cleanup Stale Entries

```python
from project_manager import plan_cleanup, execute_cleanup

plan = plan_cleanup()
print(plan.summary)
result = execute_cleanup(confirmed=True)
```

### Recovery

```python
from project_manager import list_backups, restore_from_backup

backups = list_backups()
result = restore_from_backup(backups[0]["path"])
```

## Data Locations

### Claude Code Data (`~/.claude/`)

| Subdirectory | Contents |
|--------------|----------|
| `projects/{encoded}/` | `.jsonl` session transcripts (each records `cwd` — the authoritative project path). `sessions-index.json` may be absent; current Claude Code no longer writes it |
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
