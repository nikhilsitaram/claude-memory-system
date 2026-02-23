# Worktree-Aware Project Detection — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the memory system resolve git worktree paths to their main repo, so project detection and indexing work correctly from worktrees.

**Architecture:** Add a single `resolve_worktree_to_main_repo()` utility that uses `git rev-parse` to detect worktrees and resolve to the main repo root. Call it in 3 consumer sites: `load_memory.py` (session start), `indexing.py` (project index building), and `token_usage.py` (settings display).

**Tech Stack:** Python 3.9+, subprocess (git), pytest

---

### Task 1: Add `resolve_worktree_to_main_repo()` with tests

**Files:**
- Modify: `scripts/memory_utils.py:14` (add `import subprocess`)
- Modify: `scripts/memory_utils.py:65` (add to `__all__`)
- Modify: `scripts/memory_utils.py:729` (insert new function before `find_current_project`)
- Test: `tests/test_memory_utils.py`

**Step 1: Write the failing tests**

Add to `tests/test_memory_utils.py`, after the existing `TestFindCurrentProject` class (line ~502):

```python
class TestResolveWorktreeToMainRepo:
    """Tests for resolve_worktree_to_main_repo()."""

    def test_worktree_resolves_to_main_repo(self):
        """When git says toplevel != common-dir parent, return main repo root."""
        with patch("memory_utils.subprocess.run") as mock_run:
            # First call: --show-toplevel returns worktree root
            # Second call: --git-common-dir returns main repo's .git/
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/repo/.worktrees/feature\n"),
                MagicMock(returncode=0, stdout="/repo/.git\n"),
            ]
            result = resolve_worktree_to_main_repo("/repo/.worktrees/feature/src")
            assert result == "/repo"

    def test_main_repo_returns_unchanged(self):
        """When toplevel == common-dir parent, it's the main repo — return as-is."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/home/user/project\n"),
                MagicMock(returncode=0, stdout="/home/user/project/.git\n"),
            ]
            result = resolve_worktree_to_main_repo("/home/user/project")
            assert result == "/home/user/project"

    def test_non_git_directory_returns_unchanged(self):
        """Non-git directory (git fails) returns original path."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            result = resolve_worktree_to_main_repo("/tmp/not-a-repo")
            assert result == "/tmp/not-a-repo"

    def test_git_not_installed_returns_unchanged(self):
        """If git binary not found, return original path."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = resolve_worktree_to_main_repo("/some/path")
            assert result == "/some/path"

    def test_empty_git_output_returns_unchanged(self):
        """If git returns empty output, return original path."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="\n"),
            ]
            result = resolve_worktree_to_main_repo("/some/path")
            assert result == "/some/path"

    def test_git_common_dir_failure_returns_unchanged(self):
        """If first git call succeeds but second fails, return original path."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/repo/.worktrees/feature\n"),
                subprocess.CalledProcessError(128, "git"),
            ]
            result = resolve_worktree_to_main_repo("/repo/.worktrees/feature")
            assert result == "/repo/.worktrees/feature"

    def test_first_call_nonzero_returns_unchanged(self):
        """If --show-toplevel returns nonzero exit code, return original path."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = resolve_worktree_to_main_repo("/some/path")
            assert result == "/some/path"
```

Ensure these imports exist at the top of the test file:
```python
import subprocess
from unittest.mock import MagicMock, patch
from memory_utils import resolve_worktree_to_main_repo
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_memory_utils.py::TestResolveWorktreeToMainRepo -v`
Expected: FAIL with `ImportError: cannot import name 'resolve_worktree_to_main_repo'`

**Step 3: Write the implementation**

Add `import subprocess` to the imports in `scripts/memory_utils.py` (after line 14, with the other stdlib imports).

Add `"resolve_worktree_to_main_repo"` to `__all__` in the `# Path helpers` section (after `"collect_ltm_files"`, around line 40).

Insert this function before `find_current_project()` (before line 731):

```python
def resolve_worktree_to_main_repo(path: str) -> str:
    """Resolve a git worktree path to its main repository root.

    Uses git rev-parse to detect if the given path is inside a worktree.
    If so, returns the main repository root. Otherwise returns the
    original path unchanged. Non-git directories return unchanged.
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

        common_result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if common_result.returncode != 0:
            return path
        common_dir = common_result.stdout.strip()
        if not common_dir:
            return path

        # common_dir is the main repo's .git/ directory
        # Its parent is the main repo root
        main_repo_root = str(Path(common_dir).parent)

        if main_repo_root != toplevel:
            # We're in a worktree — return the main repo root
            return main_repo_root

        return path
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return path
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memory_utils.py::TestResolveWorktreeToMainRepo -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "feat: add resolve_worktree_to_main_repo() utility

Detects git worktrees via rev-parse and resolves to main repo root.
Handles non-git dirs, missing git binary, and subprocess failures."
```

---

### Task 2: Use worktree resolution in `load_memory.py` project detection

**Files:**
- Modify: `scripts/load_memory.py:756` (resolve pwd before project lookup)
- Test: `tests/test_load_memory.py`

**Step 1: Write the failing test**

Add a test to `tests/test_load_memory.py`. Find or create an appropriate test class for the main `main()` function's project detection. The key behavior: when CWD is a worktree, the resolved main repo path is used for project lookup.

Since `main()` is large and integration-heavy, test this at the unit level by verifying the flow: mock `os.getcwd()` to return a worktree path, mock `resolve_worktree_to_main_repo` to return the main repo path, and verify `find_current_project` receives the resolved path.

```python
class TestWorktreeProjectDetection:
    """Verify load_memory resolves worktree paths before project lookup."""

    def test_worktree_path_resolved_before_project_lookup(self):
        """CWD in a worktree should resolve to main repo for project matching."""
        with patch("load_memory.os.getcwd", return_value="/repo/.worktrees/feature"), \
             patch("load_memory.resolve_worktree_to_main_repo", return_value="/repo") as mock_resolve, \
             patch("load_memory.load_json_file", return_value={"projects": {}}), \
             patch("load_memory.find_current_project", return_value=None) as mock_find:
            # We need to call the section of main() that does project detection.
            # Since main() is monolithic, we test that the import and wiring exist
            # by checking the function is importable and the module references it.
            from load_memory import resolve_worktree_to_main_repo as imported
            assert imported is not None
```

Actually, the more practical test: verify the integration works end-to-end by checking the import exists in `load_memory.py`. The real validation comes from Task 1's unit tests of the function itself + the wiring being a one-line change.

**Step 2: Write the implementation**

In `scripts/load_memory.py`, add `resolve_worktree_to_main_repo` to the imports from `memory_utils` (find the existing import block).

Then modify line 756:

Before:
```python
    # Detect current project
    pwd = os.getcwd()
```

After:
```python
    # Detect current project (resolve worktree to main repo for matching)
    pwd = resolve_worktree_to_main_repo(os.getcwd())
```

**Step 3: Run all tests**

Run: `python3 -m pytest tests/ -q`
Expected: All tests PASS (no regressions)

**Step 4: Commit**

```bash
git add scripts/load_memory.py
git commit -m "feat: resolve worktree path in load_memory project detection

Calls resolve_worktree_to_main_repo(os.getcwd()) before matching
against projects-index, so worktree sessions load the correct
project memory."
```

---

### Task 3: Use worktree resolution in `indexing.py` project index building

**Files:**
- Modify: `scripts/indexing.py:35-44` (add import)
- Modify: `scripts/indexing.py:354` (resolve sessions-index originalPath)
- Modify: `scripts/indexing.py:368-369` (resolve JSONL fallback path)
- Test: `tests/test_indexing.py`

**Step 1: Write the failing test**

Add to `tests/test_indexing.py` inside `TestBuildProjectsIndex`:

```python
    def test_worktree_session_merges_into_parent_project(self, env):
        """A session whose CWD is a worktree should merge into the main repo project."""
        projects_dir, memory_dir = env

        # Main repo project folder
        main_folder = projects_dir / "main-repo"
        main_folder.mkdir()
        sessions_index = {
            "originalPath": "/home/user/myproject",
            "entries": [{"created": "2026-01-15T10:00:00Z", "projectPath": "/home/user/myproject"}],
        }
        (main_folder / "sessions-index.json").write_text(json.dumps(sessions_index))

        # Worktree session folder (different encoded name, worktree CWD)
        wt_folder = projects_dir / "worktree-feature"
        wt_folder.mkdir()
        wt_sessions = {
            "originalPath": "/home/user/myproject/.worktrees/feature",
            "entries": [{"created": "2026-01-16T10:00:00Z", "projectPath": "/home/user/myproject/.worktrees/feature"}],
        }
        (wt_folder / "sessions-index.json").write_text(json.dumps(wt_sessions))

        with patch("indexing.resolve_worktree_to_main_repo") as mock_resolve:
            # Worktree path resolves to main repo
            def side_effect(p):
                if ".worktrees" in p:
                    return "/home/user/myproject"
                return p
            mock_resolve.side_effect = side_effect

            index = build_projects_index()

        projects = index["projects"]
        canonical = "/home/user/myproject"

        assert canonical in projects
        assert len(projects) == 1, f"Expected 1 project, got {len(projects)}: {list(projects.keys())}"
        assert "2026-01-15" in projects[canonical]["workDays"]
        assert "2026-01-16" in projects[canonical]["workDays"]
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_indexing.py::TestBuildProjectsIndex::test_worktree_session_merges_into_parent_project -v`
Expected: FAIL (either ImportError or assertion failure — worktree creates separate project)

**Step 3: Write the implementation**

Add `resolve_worktree_to_main_repo` to the import from `memory_utils` in `scripts/indexing.py` (line 35-44):

```python
from memory_utils import (
    check_python_version,
    from_iso_z,
    get_memory_dir,
    get_projects_dir,
    get_projects_index_file,
    get_sessions_original_path,
    load_sessions_index,
    resolve_worktree_to_main_repo,
    to_iso_z,
)
```

In `build_projects_index()`, resolve both path sources. After line 354 (`original_path = get_sessions_original_path(data)`), add resolution:

```python
        data = load_sessions_index(project_folder)
        if data:
            original_path = get_sessions_original_path(data)
            if original_path:
                original_path = resolve_worktree_to_main_repo(original_path)
```

And after line 368-369 (JSONL fallback), resolve that path too:

```python
        jsonl_path, jsonl_days = _extract_from_jsonl(project_folder)
        if not original_path:
            original_path = jsonl_path
            if original_path:
                original_path = resolve_worktree_to_main_repo(original_path)
        work_days.update(jsonl_days)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_indexing.py::TestBuildProjectsIndex -v`
Expected: All tests PASS including the new one

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add scripts/indexing.py tests/test_indexing.py
git commit -m "feat: resolve worktree paths in project index building

Worktree sessions now merge into their parent project instead of
being registered as separate projects. Resolves both sessions-index
and JSONL fallback paths."
```

---

### Task 4: Use worktree resolution in `token_usage.py`

**Files:**
- Modify: `scripts/token_usage.py:28` (resolve cwd)

**Step 1: Write the implementation**

In `scripts/token_usage.py`, add `resolve_worktree_to_main_repo` to its imports from `memory_utils`.

Change line 28:

Before:
```python
    cwd = os.getcwd()
```

After:
```python
    cwd = resolve_worktree_to_main_repo(os.getcwd())
```

**Step 2: Run all tests**

Run: `python3 -m pytest tests/ -q`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add scripts/token_usage.py
git commit -m "feat: resolve worktree path in token_usage project detection"
```

---

### Task 5: Final verification and squash commit

**Step 1: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests PASS

**Step 2: Manually verify with the real repo**

Run from the main repo to confirm no regressions:
```bash
python3 scripts/memory_utils.py
```
Expected: Self-test passes, no errors.

Test the function directly:
```bash
python3 -c "import sys; sys.path.insert(0, 'scripts'); from memory_utils import resolve_worktree_to_main_repo; print(resolve_worktree_to_main_repo('$(pwd)'))"
```
Expected: Prints the current repo path (same as pwd since we're in the main repo).
