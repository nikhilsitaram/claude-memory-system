---
status: Not Yet Started
---

# Git-Aware Subdirectory Project Mapping — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the `includeProjectSubdirs` boolean setting with git-aware resolution: subdirectories of a git repo map to the repo root unless gitignored.

**Architecture:** New `resolve_git_subdir_to_root()` function chains after existing `resolve_worktree_to_main_repo()`. A convenience wrapper `resolve_session_path()` composes both. All callers (`load_memory.py`, `indexing.py`, `token_usage.py`) switch to the wrapper. `find_current_project()` simplifies to exact-match-only. The `projectSettings.includeSubdirectories` setting is removed.

**Tech Stack:** Python 3.9+, subprocess (git CLI), pytest

**Design doc:** `docs/plans/2026-03-01-git-subdir-mapping/design-git-subdir-mapping.md`

---

## Phases

### Phase 1 — Core Resolution Functions
**Status:** Not Yet Started

- [ ] Task 1: Add `resolve_git_subdir_to_root()` and `resolve_session_path()` to memory_utils.py
- [ ] Task 2: Simplify `find_current_project()` to exact-match-only

### Phase 2 — Wire Up Callers
**Status:** Not Yet Started

- [ ] Task 3: Update load_memory.py to use `resolve_session_path()`
- [ ] Task 4: Update indexing.py to use `resolve_session_path()`
- [ ] Task 5: Update token_usage.py to use `resolve_session_path()`

### Phase 3 — Remove Deprecated Setting
**Status:** Not Yet Started

- [ ] Task 6: Remove `projectSettings.includeSubdirectories` from settings, templates, docs, and skills

---

## Task Details

### Task 1: Add `resolve_git_subdir_to_root()` and `resolve_session_path()`

**Files:**
- Modify: `scripts/memory_utils.py:828-829` (insert new functions after `resolve_worktree_to_main_repo`)
- Modify: `scripts/memory_utils.py:23-78` (update `__all__` exports)
- Test: `tests/test_memory_utils.py` (new class after `TestResolveWorktreeToMainRepo` at line 1070)

**Verification:** `python3 -m pytest tests/test_memory_utils.py -v -k "TestResolveGitSubdir or TestResolveSessionPath"`

**Done when:** 10+ tests pass covering all edge cases in the design doc's edge case table. `resolve_git_subdir_to_root` correctly shells out to `git rev-parse --show-toplevel` and `git check-ignore -q`, and `resolve_session_path` chains both resolvers.

**Avoid:** Don't import `resolve_session_path` from callers yet — that's Task 3-5. Don't modify `find_current_project` yet — that's Task 2.

**Step 1: Write the failing tests**

First, add `resolve_git_subdir_to_root` and `resolve_session_path` to the import block at the top of `tests/test_memory_utils.py` (line 13, inside the `from memory_utils import (` block).

Then add the following test classes after `TestResolveWorktreeToMainRepo` (line 1070):

```python
class TestResolveGitSubdirToRoot:
    """Tests for resolve_git_subdir_to_root()."""

    def test_git_root_returns_unchanged(self):
        """Path that IS the git root returns unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="/home/user/project\n")
            result = resolve_git_subdir_to_root("/home/user/project")
            assert result == "/home/user/project"

    def test_non_ignored_subdir_collapses_to_root(self):
        """Non-gitignored subdir collapses to git root."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/home/user/cartwheel\n"),  # show-toplevel
                MagicMock(returncode=1, stdout=""),  # check-ignore: not ignored
            ]
            result = resolve_git_subdir_to_root("/home/user/cartwheel/frontend")
            assert result == "/home/user/cartwheel"

    def test_gitignored_subdir_stays_separate(self):
        """Gitignored subdir stays as its own project."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/home/user/swyfft\n"),  # show-toplevel
                MagicMock(returncode=0, stdout=""),  # check-ignore: IS ignored
            ]
            result = resolve_git_subdir_to_root("/home/user/swyfft/projects/granada")
            assert result == "/home/user/swyfft/projects/granada"

    def test_not_in_git_repo_returns_unchanged(self):
        """Path not in any git repo returns unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = resolve_git_subdir_to_root("/tmp/not-a-repo/subdir")
            assert result == "/tmp/not-a-repo/subdir"

    def test_git_not_installed_returns_unchanged(self):
        """If git is not installed, return path unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = resolve_git_subdir_to_root("/home/user/project/src")
            assert result == "/home/user/project/src"

    def test_git_timeout_returns_unchanged(self):
        """If git times out, return path unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("git", 5)
            result = resolve_git_subdir_to_root("/home/user/project/src")
            assert result == "/home/user/project/src"

    def test_empty_toplevel_returns_unchanged(self):
        """If git returns empty toplevel, return path unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="\n")
            result = resolve_git_subdir_to_root("/some/path")
            assert result == "/some/path"

    def test_deeply_nested_subdir(self):
        """Deeply nested non-ignored subdir still collapses to root."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/home/user/project\n"),
                MagicMock(returncode=1, stdout=""),  # not ignored
            ]
            result = resolve_git_subdir_to_root("/home/user/project/src/lib/utils")
            assert result == "/home/user/project"

    def test_check_ignore_called_with_relative_path(self):
        """Verify check-ignore receives the relative path, not absolute."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/home/user/swyfft\n"),
                MagicMock(returncode=1, stdout=""),
            ]
            resolve_git_subdir_to_root("/home/user/swyfft/projects/granada")
            # Second call should be check-ignore with relative path
            check_ignore_call = mock_run.call_args_list[1]
            cmd = check_ignore_call[0][0]
            assert cmd == ["git", "-C", "/home/user/swyfft", "check-ignore", "-q", "projects/granada"]

    def test_check_ignore_error_returns_unchanged(self):
        """If check-ignore itself errors (not exit 0 or 1), return unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/home/user/project\n"),
                subprocess.CalledProcessError(128, "git"),
            ]
            result = resolve_git_subdir_to_root("/home/user/project/subdir")
            assert result == "/home/user/project/subdir"


class TestResolveSessionPath:
    """Tests for resolve_session_path() — worktree + git-subdir chain."""

    def test_worktree_resolves_first(self):
        """Worktree resolution fires first, git-subdir sees root, no-ops."""
        with patch("memory_utils.resolve_worktree_to_main_repo") as mock_wt, \
             patch("memory_utils.resolve_git_subdir_to_root") as mock_gs:
            mock_wt.return_value = "/home/user/project"
            mock_gs.return_value = "/home/user/project"  # already root, no-op
            result = resolve_session_path("/home/user/project/.worktrees/feat")
            assert result == "/home/user/project"
            mock_wt.assert_called_once_with("/home/user/project/.worktrees/feat")
            mock_gs.assert_called_once_with("/home/user/project")

    def test_non_worktree_subdir_collapses(self):
        """Non-worktree subdir goes through git-subdir resolution."""
        with patch("memory_utils.resolve_worktree_to_main_repo") as mock_wt, \
             patch("memory_utils.resolve_git_subdir_to_root") as mock_gs:
            mock_wt.return_value = "/home/user/cartwheel/frontend"  # not a worktree
            mock_gs.return_value = "/home/user/cartwheel"  # collapsed
            result = resolve_session_path("/home/user/cartwheel/frontend")
            assert result == "/home/user/cartwheel"

    def test_gitignored_subdir_stays_separate(self):
        """Gitignored subdir passes through both resolvers unchanged."""
        with patch("memory_utils.resolve_worktree_to_main_repo") as mock_wt, \
             patch("memory_utils.resolve_git_subdir_to_root") as mock_gs:
            mock_wt.return_value = "/home/user/swyfft/projects/granada"
            mock_gs.return_value = "/home/user/swyfft/projects/granada"
            result = resolve_session_path("/home/user/swyfft/projects/granada")
            assert result == "/home/user/swyfft/projects/granada"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_memory_utils.py -v -k "TestResolveGitSubdir or TestResolveSessionPath"`
Expected: FAIL — `ImportError: cannot import name 'resolve_git_subdir_to_root'`

**Step 3: Write minimal implementation**

In `scripts/memory_utils.py`, insert after `resolve_worktree_to_main_repo` (after line 828):

```python
def resolve_git_subdir_to_root(path: str) -> str:
    """Resolve a git subdirectory to its repository root.

    If path is inside a git repo but is not the root:
      - If the relative path is gitignored -> return path unchanged (separate project)
      - If not gitignored -> return git root (collapse to parent project)

    If path IS the git root, or not in a git repo, returns unchanged.
    Falls back to returning path unchanged on any error.
    """
    try:
        toplevel_result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if toplevel_result.returncode != 0:
            return path
        toplevel = toplevel_result.stdout.strip()
        if not toplevel:
            return path

        # Normalize both paths for comparison
        norm_path = os.path.normpath(path)
        norm_toplevel = os.path.normpath(toplevel)

        if norm_path == norm_toplevel:
            return path  # Already at git root

        # Compute relative path from git root
        rel_path = os.path.relpath(norm_path, norm_toplevel)

        # Check if relative path is gitignored
        ignore_result = subprocess.run(
            ["git", "-C", norm_toplevel, "check-ignore", "-q", rel_path],
            capture_output=True, text=True, timeout=5,
        )

        if ignore_result.returncode == 0:
            # Path IS gitignored — keep as separate project
            return path
        elif ignore_result.returncode == 1:
            # Path is NOT gitignored — collapse to git root
            return toplevel
        else:
            # Unexpected error from check-ignore
            return path
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired, OSError):
        return path


def resolve_session_path(path: str) -> str:
    """Full resolution chain: worktree -> git-subdir -> result.

    Applies both resolution steps in order:
    1. resolve_worktree_to_main_repo — handles git worktrees
    2. resolve_git_subdir_to_root — handles non-root subdirs of git repos
    """
    path = resolve_worktree_to_main_repo(path)
    path = resolve_git_subdir_to_root(path)
    return path
```

Then update `__all__` in `scripts/memory_utils.py` to add the two new exports. Add after line 47 (`"resolve_worktree_to_main_repo",`):

```python
    "resolve_git_subdir_to_root",
    "resolve_session_path",
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memory_utils.py -v -k "TestResolveGitSubdir or TestResolveSessionPath"`
Expected: 13/13 PASS

**Step 5: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "feat: add resolve_git_subdir_to_root and resolve_session_path

New resolution function collapses git subdirs to repo root unless
gitignored. resolve_session_path chains worktree + git-subdir
resolution. 13 new tests."
```

---

### Task 2: Simplify `find_current_project()` to exact-match-only

**Files:**
- Modify: `scripts/memory_utils.py:831-854` (the `find_current_project` function)
- Test: `tests/test_memory_utils.py:483-549` (the `TestFindCurrentProject` class)

**Verification:** `python3 -m pytest tests/test_memory_utils.py -v -k "TestFindCurrentProject"`

**Done when:** `find_current_project` no longer accepts `include_subdirs` parameter. All calls use 2 args only. Old subdir-match tests are removed or updated. 5 tests pass.

**Avoid:** Don't update callers yet — they still pass `include_subdirs`. That's Tasks 3-5. The function signature change will temporarily break callers, but we're not running the full test suite until after Task 5 wires everything up. Also update the Key Interfaces comment at line ~102 of `memory_utils.py` to reflect the new 2-arg signature: `#   find_current_project(index, pwd) -> dict | None`.

**Step 1: Update tests first**

Replace the `TestFindCurrentProject` class in `tests/test_memory_utils.py` (lines 483-549):

```python
class TestFindCurrentProject:
    def test_exact_match(self):
        index = {
            "projects": {
                "/home/user/project": {
                    "name": "project",
                    "originalPath": "/home/user/project",
                }
            }
        }
        result = find_current_project(index, "/home/user/project")
        assert result is not None
        assert result["name"] == "project"

    def test_no_match(self):
        index = {"projects": {"/home/user/project": {"name": "project"}}}
        result = find_current_project(index, "/home/user/other")
        assert result is None

    def test_subdirectory_does_not_match(self):
        """Subdirectory matching removed — resolution handles this upstream."""
        index = {
            "projects": {
                "/home/user/project": {
                    "name": "project",
                    "originalPath": "/home/user/project",
                }
            }
        }
        result = find_current_project(index, "/home/user/project/subdir")
        assert result is None

    def test_empty_projects(self):
        result = find_current_project({"projects": {}}, "/home/user")
        assert result is None

    def test_case_insensitive_match(self):
        """Keys in index are lowercase; PWD is lowercased for lookup."""
        index = {
            "projects": {
                "/home/user/project": {
                    "name": "project",
                    "originalPath": "/home/User/Project",
                }
            }
        }
        result = find_current_project(index, "/home/User/Project")
        assert result is not None
        assert result["name"] == "project"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_memory_utils.py -v -k "TestFindCurrentProject"`
Expected: FAIL — `TypeError: find_current_project() missing 1 required positional argument: 'include_subdirs'` (new tests call with 2 args but function still expects 3)

**Step 3: Simplify the function**

Replace `find_current_project` in `scripts/memory_utils.py` (lines 831-854):

```python
def find_current_project(projects_index: dict, pwd: str) -> dict | None:
    """
    Find the project matching the current working directory.

    Uses exact match only. Subdirectory resolution is handled upstream
    by resolve_session_path() before this function is called.

    Returns project dict with 'name', 'originalPath', 'workDays' or None.
    """
    projects = projects_index.get("projects", {})
    pwd_lower = pwd.lower()
    return projects.get(pwd_lower)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memory_utils.py -v -k "TestFindCurrentProject"`
Expected: 5/5 PASS

**Step 5: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "refactor: simplify find_current_project to exact-match-only

Subdirectory matching removed — resolve_session_path handles this
upstream. include_subdirs parameter removed."
```

---

### Task 3: Update load_memory.py to use `resolve_session_path()`

**Files:**
- Modify: `scripts/load_memory.py:33` (import line)
- Modify: `scripts/load_memory.py:46` (import line — remove `resolve_worktree_to_main_repo`)
- Modify: `scripts/load_memory.py:688` (remove `include_subdirs` read)
- Modify: `scripts/load_memory.py:795` (change resolution call)
- Modify: `scripts/load_memory.py:797` (remove `include_subdirs` from `find_current_project` call)
- Test: `tests/test_load_memory.py` (update any tests that mock `resolve_worktree_to_main_repo` or pass `include_subdirs`)

**Verification:** `python3 -m pytest tests/test_load_memory.py -v`

**Done when:** `load_memory.py` imports `resolve_session_path` instead of `resolve_worktree_to_main_repo`, calls it at line 795, and passes only 2 args to `find_current_project`. No reference to `includeSubdirectories` remains. All existing load_memory tests pass.

**Avoid:** Don't modify `indexing.py` or `token_usage.py` — those are Tasks 4 and 5.

**Step 1: Update existing test mocks**

In `tests/test_load_memory.py`, update these specific references:
- Line ~1279: Change `resolve_worktree_to_main_repo` to `resolve_session_path` in the import assertion test
- Line ~1474: Change `monkeypatch.setattr("load_memory.resolve_worktree_to_main_repo", ...)` to `monkeypatch.setattr("load_memory.resolve_session_path", ...)`
- Search for any other `resolve_worktree_to_main_repo` or `includeSubdirectories` references and update accordingly.

**Step 2: Update imports in load_memory.py**

In the import block (around line 33-46), replace `resolve_worktree_to_main_repo` with `resolve_session_path`:

```python
# Change this import:
    resolve_worktree_to_main_repo,
# To:
    resolve_session_path,
```

**Step 3: Remove the include_subdirs read**

Delete line 688:
```python
    include_subdirs = settings["projectSettings"]["includeSubdirectories"]
```

**Step 4: Update the resolution call**

Change line 795 from:
```python
    pwd = resolve_worktree_to_main_repo(os.getcwd())
```
To:
```python
    pwd = resolve_session_path(os.getcwd())
```

**Step 5: Update find_current_project call**

Change line 797 from:
```python
    current_project = find_current_project(projects_index, pwd, include_subdirs)
```
To:
```python
    current_project = find_current_project(projects_index, pwd)
```

**Step 6: Run tests**

Run: `python3 -m pytest tests/test_load_memory.py -v`
Expected: All pass

**Step 7: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "refactor: load_memory uses resolve_session_path

Replaces resolve_worktree_to_main_repo with resolve_session_path
for git-aware subdirectory resolution. Removes includeSubdirectories
setting read."
```

---

### Task 4: Update indexing.py to use `resolve_session_path()`

**Files:**
- Modify: `scripts/indexing.py:43` (change import from `resolve_worktree_to_main_repo` to `resolve_session_path`)
- Modify: `scripts/indexing.py:358` (replace call)
- Modify: `scripts/indexing.py:375` (replace call)
- Test: `tests/test_indexing.py` (update mocks if they reference `resolve_worktree_to_main_repo`)

**Verification:** `python3 -m pytest tests/test_indexing.py -v`

**Done when:** Both `resolve_worktree_to_main_repo` calls in `build_projects_index` are replaced with `resolve_session_path`. Import updated. All indexing tests pass.

**Avoid:** Don't change `resolve_worktree_to_main_repo` itself — other files may still import it directly for their own purposes. Just change the callers.

**Step 1: Update import**

Change line 43 from:
```python
    resolve_worktree_to_main_repo,
```
To:
```python
    resolve_session_path,
```

**Step 2: Update both call sites**

Line 358:
```python
# From:
                original_path = resolve_worktree_to_main_repo(original_path)
# To:
                original_path = resolve_session_path(original_path)
```

Line 375:
```python
# From:
                original_path = resolve_worktree_to_main_repo(original_path)
# To:
                original_path = resolve_session_path(original_path)
```

**Step 3: Update test mocks**

Search `tests/test_indexing.py` for `resolve_worktree_to_main_repo` and update patches to `resolve_session_path`.

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_indexing.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add scripts/indexing.py tests/test_indexing.py
git commit -m "refactor: indexing uses resolve_session_path

build_projects_index now resolves both worktrees and git subdirs
when building the project index."
```

---

### Task 5: Update token_usage.py to use `resolve_session_path()`

**Files:**
- Modify: `scripts/token_usage.py:23` (change import)
- Modify: `scripts/token_usage.py:29` (change resolution call)
- Modify: `scripts/token_usage.py:41` (remove `include_subdirs` read)
- Modify: `scripts/token_usage.py:62` (remove `include_subdirs` from `find_current_project` call)

**Verification:** `python3 -m pytest tests/ -v -k "token_usage"` (if tests exist; otherwise `python3 scripts/token_usage.py` manual check)

**Done when:** `token_usage.py` uses `resolve_session_path` and passes 2 args to `find_current_project`. No reference to `includeSubdirectories`.

**Avoid:** This is a small file; don't over-complicate. Just swap imports and remove the setting read.

**Step 1: Update import**

```python
# From:
    resolve_worktree_to_main_repo,
# To:
    resolve_session_path,
```

**Step 2: Update resolution call (line 29)**

```python
# From:
    cwd = resolve_worktree_to_main_repo(os.getcwd())
# To:
    cwd = resolve_session_path(os.getcwd())
```

**Step 3: Remove include_subdirs (line 41)**

Delete:
```python
    include_subdirs = settings["projectSettings"]["includeSubdirectories"]
```

**Step 4: Update find_current_project call (line 62)**

```python
# From:
    current_project = find_current_project(projects_index, cwd, include_subdirs)
# To:
    current_project = find_current_project(projects_index, cwd)
```

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All tests pass (this is the first time we run the full suite after all callers are updated)

**Step 6: Commit**

```bash
git add scripts/token_usage.py
git commit -m "refactor: token_usage uses resolve_session_path

Last caller updated. All callers now use git-aware resolution."
```

---

### Task 6: Remove `projectSettings.includeSubdirectories` from settings, templates, docs, and skills

**Files:**
- Modify: `scripts/memory_utils.py:226-228` (remove `projectSettings` from `DEFAULT_SETTINGS`)
- Modify: `templates/settings.json:20-23` (remove `projectSettings` block)
- Modify: `templates/settings.json:56` (remove from `_defaults`)
- Modify: `skills/settings/SKILL.md:55` (remove row from settings table)
- Modify: `README.md` (3 references: lines ~93, ~139, ~161)
- Modify: `CLAUDE.md` (update settings defaults table if it references `includeSubdirectories`)

**Verification:** `grep -r "includeSubdirectories\|include_subdirs\|includeProjectSubdirs" scripts/ templates/ skills/ tests/ README.md CLAUDE.md` should return zero results.

**Done when:** Zero references to the old setting remain in the codebase (except the design doc which documents the removal). Full test suite passes.

**Avoid:** Don't remove `projectSettings` key entirely from `DEFAULT_SETTINGS` if it might be used for future settings. Actually — check if it contains anything else. If `includeSubdirectories` is the only key under `projectSettings`, remove the entire `projectSettings` block. If there are other keys, only remove `includeSubdirectories`.

**Step 1: Remove from DEFAULT_SETTINGS**

In `scripts/memory_utils.py`, delete lines 226-228:
```python
    "projectSettings": {
        "includeSubdirectories": False,
    },
```

**Step 2: Remove from templates/settings.json**

Delete the `projectSettings` block (lines 20-23):
```json
  "projectSettings": {
    "includeSubdirectories": false,
    "_comment": "If true, /project/subdir matches /project for project memory loading"
  },
```

Delete from `_defaults` (line 56):
```json
    "projectSettings.includeSubdirectories": false,
```

**Step 3: Update skills/settings/SKILL.md**

Remove the row:
```
| `projectSettings.includeSubdirectories` | bool | — | false | Match subdirs to parent project |
```

**Step 4: Update README.md**

- Line ~93: Replace the sentence about `includeSubdirectories` with: "Git subdirectories automatically map to their repository root unless gitignored. Git worktree paths are resolved to the main repo via `git rev-parse`."
- Line ~139: Remove `"projectSettings": { "includeSubdirectories": false },` from the example config
- Line ~161: Remove the `projectSettings.includeSubdirectories` row from the settings table

**Step 5: Update CLAUDE.md**

Check the settings defaults table. If it references `includeSubdirectories`, remove that row. Replace with a note about git-aware resolution if appropriate.

**Step 6: Run verification**

```bash
grep -r "includeSubdirectories\|include_subdirs" scripts/ templates/ skills/ tests/ README.md CLAUDE.md
```

Expected: Zero results (design doc excluded from search path).

**Step 7: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 8: Commit**

```bash
git add scripts/memory_utils.py templates/settings.json skills/settings/SKILL.md README.md CLAUDE.md
git commit -m "chore: remove projectSettings.includeSubdirectories

Setting replaced by git-aware subdirectory resolution. Cleaned up
from DEFAULT_SETTINGS, templates, skills, README, and CLAUDE.md."
```
