#!/usr/bin/env python3
"""
Broad integration tests for the git-aware subdir resolution chain.

These tests are RED initially (functions don't exist yet) and should
turn GREEN after all implementation tasks complete.

Tests cover:
1. Full chain: resolve_session_path with a worktree path
2. Full chain: Non-worktree git subdir collapses to repo root
3. Full chain: Gitignored subdir stays separate
4. End-to-end wiring: load_memory.main calls resolve_session_path
5. End-to-end wiring: indexing.build_projects_index calls resolve_session_path
6. find_current_project 2-arg: works with exactly 2 args

Run with: python -m pytest tests/test_integration_git_subdir.py -v
"""

import copy
import json
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Attempt to import not-yet-existing functions.
# Tests that need them are marked xfail so the file itself doesn't crash.
# ---------------------------------------------------------------------------
try:
    from memory_utils import resolve_git_subdir_to_root, resolve_session_path
    _RESOLVE_SESSION_PATH_EXISTS = True
except ImportError:
    resolve_git_subdir_to_root = None  # type: ignore[assignment]
    resolve_session_path = None        # type: ignore[assignment]
    _RESOLVE_SESSION_PATH_EXISTS = False

# These exist today and must always import cleanly.
from memory_utils import (  # noqa: E402
    DEFAULT_SETTINGS,
    find_current_project,
    load_settings,
)

# =============================================================================
# Markers / helpers
# =============================================================================

needs_resolve_session_path = pytest.mark.xfail(
    not _RESOLVE_SESSION_PATH_EXISTS,
    reason="resolve_session_path not yet implemented in memory_utils.py",
    strict=True,
)

# find_current_project must accept exactly 2 args
import inspect as _inspect

_FCP_TWO_ARGS = len(_inspect.signature(find_current_project).parameters) == 2

# Wiring checks: callers must import resolve_session_path (not just that it exists)
_LOAD_MEMORY_WIRED = _RESOLVE_SESSION_PATH_EXISTS and hasattr(
    __import__("load_memory"), "resolve_session_path"
)
_INDEXING_WIRED = _RESOLVE_SESSION_PATH_EXISTS and hasattr(
    __import__("indexing"), "resolve_session_path"
)

needs_load_memory_wired = pytest.mark.xfail(
    not _LOAD_MEMORY_WIRED,
    reason="load_memory.py not yet wired to use resolve_session_path",
    strict=True,
)
needs_indexing_wired = pytest.mark.xfail(
    not _INDEXING_WIRED,
    reason="indexing.py not yet wired to use resolve_session_path",
    strict=True,
)
needs_fcp_two_args = pytest.mark.xfail(
    not _FCP_TWO_ARGS,
    reason="find_current_project still requires 3 args; will be simplified in Task 2",
    strict=True,
)


def _make_fake_settings() -> dict:
    """Derive a valid settings dict from DEFAULT_SETTINGS (no hardcoded values)."""
    settings = copy.deepcopy(DEFAULT_SETTINGS)
    settings["totalTokenBudget"] = (
        settings.get("globalLongTerm", {}).get("tokenLimit", 3000)
        + settings.get("projectLongTerm", {}).get("tokenLimit", 3000)
    )
    return settings


class CompletedProcessFake:
    """Minimal subprocess.CompletedProcess substitute for mocking."""

    def __init__(self, returncode: int, stdout: str, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# =============================================================================
# Test 1: Full chain — worktree path resolves correctly via resolve_session_path
# =============================================================================


class TestResolveSessionPathWorktree:
    """resolve_session_path collapses a worktree path to the main repo root."""

    @needs_resolve_session_path
    def test_worktree_path_resolves_to_main_repo(self, tmp_path):
        """When path is inside a worktree, resolve_session_path returns the main repo root."""
        main_repo = str(tmp_path / "myrepo")
        worktree_path = str(tmp_path / "myrepo" / ".worktrees" / "feat-branch")

        # Mock resolve_worktree_to_main_repo to return main_repo for worktree_path,
        # and mock resolve_git_subdir_to_root to return main_repo unchanged
        # (worktree itself is the root — no further git subdir resolution needed).
        with patch("memory_utils.resolve_worktree_to_main_repo", return_value=main_repo), \
             patch("memory_utils.resolve_git_subdir_to_root", return_value=main_repo):
            result = resolve_session_path(worktree_path)

        assert result == main_repo

    @needs_resolve_session_path
    def test_worktree_resolved_before_subdir_check(self, tmp_path):
        """Worktree resolution happens first, then git-subdir check on the resolved path."""
        main_repo = str(tmp_path / "myrepo")
        worktree_path = str(tmp_path / "myrepo" / ".worktrees" / "feat-branch")
        # simulate a subdir within the resolved main repo
        subdir_of_main = main_repo

        with patch("memory_utils.resolve_worktree_to_main_repo", return_value=main_repo) as mock_wt, \
             patch("memory_utils.resolve_git_subdir_to_root", return_value=subdir_of_main) as mock_gs:
            result = resolve_session_path(worktree_path)

        # resolve_worktree_to_main_repo must receive the original worktree path
        mock_wt.assert_called_once_with(worktree_path)
        # resolve_git_subdir_to_root must receive the worktree-resolved path
        mock_gs.assert_called_once_with(main_repo)
        assert result == subdir_of_main


# =============================================================================
# Test 2: Full chain — non-worktree git subdir collapses to repo root
# =============================================================================


class TestResolveSessionPathGitSubdir:
    """resolve_session_path collapses a tracked git subdir to the repo root."""

    @needs_resolve_session_path
    def test_tracked_subdir_collapses_to_root(self, tmp_path):
        """A subdir tracked by git (not gitignored) should collapse to repo root."""
        repo_root = str(tmp_path / "myrepo")
        subdir = str(tmp_path / "myrepo" / "packages" / "backend")

        # No worktree — resolve_worktree_to_main_repo returns path unchanged
        # resolve_git_subdir_to_root detects subdir is inside repo, returns root
        with patch("memory_utils.resolve_worktree_to_main_repo", side_effect=lambda p: p), \
             patch("memory_utils.resolve_git_subdir_to_root", return_value=repo_root):
            result = resolve_session_path(subdir)

        assert result == repo_root

    @needs_resolve_session_path
    def test_non_git_path_unchanged(self, tmp_path):
        """A path that is not inside a git repo is returned unchanged."""
        non_git_path = str(tmp_path / "not-a-repo" / "somedir")

        with patch("memory_utils.resolve_worktree_to_main_repo", side_effect=lambda p: p), \
             patch("memory_utils.resolve_git_subdir_to_root", side_effect=lambda p: p):
            result = resolve_session_path(non_git_path)

        assert result == non_git_path


# =============================================================================
# Test 3: Full chain — gitignored subdir stays separate
# =============================================================================


class TestResolveSessionPathGitignored:
    """resolve_session_path leaves gitignored subdirs as-is (no collapse)."""

    @needs_resolve_session_path
    def test_gitignored_subdir_stays_separate(self, tmp_path):
        """A gitignored subdir should NOT collapse to repo root."""
        gitignored_path = str(tmp_path / "myrepo" / "node_modules" / "some-pkg")

        # resolve_git_subdir_to_root returns path unchanged for gitignored dirs
        with patch("memory_utils.resolve_worktree_to_main_repo", side_effect=lambda p: p), \
             patch("memory_utils.resolve_git_subdir_to_root", side_effect=lambda p: p):
            result = resolve_session_path(gitignored_path)

        assert result == gitignored_path

    @needs_resolve_session_path
    def test_resolve_git_subdir_to_root_calls_subprocess(self, tmp_path):
        """resolve_git_subdir_to_root should call git to check ignore status."""
        subdir = str(tmp_path / "repo" / "tracked-subdir")

        # We test resolve_git_subdir_to_root directly here (it must exist)
        fake_toplevel = CompletedProcessFake(0, str(tmp_path / "repo"))
        fake_check_ignore = CompletedProcessFake(1, "")  # exit 1 = not ignored

        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [fake_toplevel, fake_check_ignore]
            result = resolve_git_subdir_to_root(subdir)

        # Should have called git twice: once for toplevel, once for check-ignore
        assert mock_run.call_count == 2
        assert "--show-toplevel" in mock_run.call_args_list[0].args[0]
        assert "check-ignore" in mock_run.call_args_list[1].args[0]
        # Non-ignored subdir should collapse to git root
        assert result == str(tmp_path / "repo")


# =============================================================================
# Test 4: End-to-end wiring — load_memory.main uses resolve_session_path
# =============================================================================


class TestLoadMemoryUsesResolveSessionPath:
    """load_memory.main must call resolve_session_path, not resolve_worktree_to_main_repo."""

    @needs_load_memory_wired
    def test_main_calls_resolve_session_path(self, tmp_path):
        """load_memory.main should delegate path resolution to resolve_session_path."""
        import load_memory

        fake_index = {"projects": {}}  # minimal — main() is heavily mocked
        fake_settings = _make_fake_settings()

        with patch("load_memory.load_settings", return_value=fake_settings), \
             patch("load_memory.load_json_file", return_value=fake_index), \
             patch("load_memory.get_recent_days", return_value=[]), \
             patch("load_memory.check_synthesis_errors", return_value=None), \
             patch("load_memory._load_from_db", return_value=""), \
             patch("load_memory.resolve_session_path") as mock_rsp, \
             patch("os.getcwd", return_value="/some/project/subdir"), \
             patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = True
            mock_rsp.return_value = "/some/project"
            load_memory.main()

        # resolve_session_path must have been called with the cwd
        mock_rsp.assert_called_once_with("/some/project/subdir")



# =============================================================================
# Test 5: End-to-end wiring — indexing.build_projects_index uses resolve_session_path
# =============================================================================


class TestIndexingUsesResolveSessionPath:
    """build_projects_index must call resolve_session_path for each session path."""

    @needs_indexing_wired
    def test_build_projects_index_calls_resolve_session_path(self, tmp_path):
        """build_projects_index should call resolve_session_path on each project path."""
        from indexing import build_projects_index

        # Set up a minimal fake projects directory with one project folder
        projects_dir = tmp_path / "projects"
        proj_folder = projects_dir / "-home-user-myrepo"
        proj_folder.mkdir(parents=True)

        sessions_index = {
            "entries": [
                {
                    "sessionId": "abc123",
                    "created": "2026-02-01T10:00:00Z",
                    "projectPath": "/home/user/myrepo/packages/backend",
                }
            ]
        }
        (proj_folder / "sessions-index.json").write_text(
            json.dumps(sessions_index), encoding="utf-8"
        )

        output_file = tmp_path / "projects-index.json"

        with patch("memory_utils.get_projects_dir", return_value=projects_dir), \
             patch("memory_utils.get_projects_index_file", return_value=output_file), \
             patch("indexing.get_projects_dir", return_value=projects_dir), \
             patch("indexing.get_projects_index_file", return_value=output_file), \
             patch("indexing.get_memory_dir", return_value=tmp_path), \
             patch("indexing.resolve_session_path") as mock_rsp:
            mock_rsp.return_value = "/home/user/myrepo"
            build_projects_index()

        # resolve_session_path must have been called at least once with the session path
        called_args = [c.args[0] for c in mock_rsp.call_args_list]
        assert "/home/user/myrepo/packages/backend" in called_args, (
            f"resolve_session_path was not called with the session path. "
            f"Called with: {called_args}"
        )



# =============================================================================
# Test 6: find_current_project with exactly 2 args
# =============================================================================


class TestFindCurrentProjectTwoArgs:
    """After Task 2, find_current_project should accept exactly 2 args."""

    @needs_fcp_two_args
    def test_two_args_exact_match(self):
        """find_current_project(index, pwd) should work with exactly 2 args."""
        index = {
            "projects": {
                "/home/user/myrepo": {"name": "myrepo", "originalPath": "/home/user/myrepo", "workDays": []},
            }
        }
        # This call must NOT raise TypeError — currently fails with "missing argument"
        result = find_current_project(index, "/home/user/myrepo")
        assert result is not None
        assert result["name"] == "myrepo"

    @needs_fcp_two_args
    def test_two_args_no_match(self):
        """find_current_project(index, pwd) returns None when pwd not in index."""
        index = {
            "projects": {
                "/home/user/myrepo": {"name": "myrepo", "originalPath": "/home/user/myrepo", "workDays": []},
            }
        }
        result = find_current_project(index, "/home/user/otherproject")
        assert result is None

    @needs_fcp_two_args
    def test_two_args_subdir_does_not_match(self):
        """After Task 2, subdir paths should NOT match (exact-match-only behaviour)."""
        index = {
            "projects": {
                "/home/user/myrepo": {"name": "myrepo", "originalPath": "/home/user/myrepo", "workDays": []},
            }
        }
        # A subdir of myrepo should NOT match under the new exact-match-only logic
        result = find_current_project(index, "/home/user/myrepo/packages/backend")
        assert result is None


