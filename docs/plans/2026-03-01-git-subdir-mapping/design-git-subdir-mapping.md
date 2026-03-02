# Design: Git-Aware Subdirectory Project Mapping

## Problem

Subdirectories of a git repo (e.g., `cartwheel/frontend`) are treated as separate projects
from their parent repo (`cartwheel`). This splits memory that should be unified. The current
`includeProjectSubdirs` setting enables prefix matching but is a blunt boolean — it can't
distinguish between a legitimate sub-project (like `swyfft/projects/granada`, which is
gitignored) and a normal subdirectory.

## Solution

Replace `includeProjectSubdirs` with git-aware resolution: any subdirectory of a git repo
maps to the repo root **unless** the subdirectory is gitignored. Gitignored subdirectories
are treated as separate projects.

## Resolution Chain

```
CWD → resolve_worktree_to_main_repo() → resolve_git_subdir_to_root() → project path
```

Priority: worktree resolution first (unchanged), then git-subdir resolution (new).

## New Function: `resolve_git_subdir_to_root(path: str) -> str`

Location: `scripts/memory_utils.py`

Algorithm:
1. `git -C <path> rev-parse --show-toplevel` → git root
2. If path == git root → return unchanged
3. Compute relative path from git root
4. `git -C <git_root> check-ignore -q <relative_path>`
5. Exit 0 (ignored) → return original path (separate project)
6. Exit 1 (not ignored) → return git root (collapse to parent)
7. Any error → return path unchanged (safe fallback)

## Convenience Wrapper: `resolve_session_path(path: str) -> str`

Chains worktree + git-subdir resolution. Both `build_projects_index` and `load_memory.py`
call this instead of calling `resolve_worktree_to_main_repo` directly.

## Changes

### memory_utils.py
- Add `resolve_git_subdir_to_root()`
- Add `resolve_session_path()` convenience wrapper
- Simplify `find_current_project()`: remove `include_subdirs` parameter, always exact match
- Remove `projectSettings.includeSubdirectories` from `DEFAULT_SETTINGS`

### indexing.py
- Replace `resolve_worktree_to_main_repo()` calls with `resolve_session_path()`

### load_memory.py
- Replace `resolve_worktree_to_main_repo(os.getcwd())` with `resolve_session_path(os.getcwd())`
- Remove `include_subdirs` from `find_current_project()` call
- Remove `settings["projectSettings"]["includeSubdirectories"]` read

### token_usage.py
- Replace `resolve_worktree_to_main_repo(os.getcwd())` with `resolve_session_path(os.getcwd())`
- Remove `include_subdirs` from `find_current_project()` call
- Remove `settings["projectSettings"]["includeSubdirectories"]` read

### templates/settings.json
- Remove `projectSettings.includeSubdirectories`

### synthesis.py (if applicable)
- Any `resolve_worktree_to_main_repo` calls updated to `resolve_session_path`

## Edge Cases

| Case | Behavior |
|---|---|
| CWD is git root | No-op |
| CWD is non-gitignored subdir | Collapsed to git root |
| CWD is gitignored subdir | Kept as separate project |
| CWD is in a worktree | Worktree resolves first, git-subdir no-ops |
| CWD not in any git repo | Returned as-is (exact match) |
| `git` not installed | Returned as-is (safe fallback) |
| Nested git repos (submodules) | `--show-toplevel` returns inner repo root; stays separate |

## Setting Removal

`projectSettings.includeSubdirectories` is removed entirely. The git-aware logic replaces
it. Non-git directories fall back to exact match.

## Performance

Two subprocess calls for non-root subdirs (`rev-parse` + `check-ignore`), both <10ms.
Runs once per project folder at index-build time and once per session start at load time.
