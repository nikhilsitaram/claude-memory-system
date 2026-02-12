#!/usr/bin/env python3
"""
Unit tests for memory_utils.py

Run with: python -m pytest tests/test_memory_utils.py -v
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

# Add scripts directory to path
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from memory_utils import (
    DEFAULT_SETTINGS,
    FileLock,
    LOCK_STALE_SECONDS,
    SHORT_TERM_TOKENS_PER_DAY,
    _calculate_token_limits,
    _deep_merge,
    estimate_tokens,
    filter_daily_content,
    find_current_project,
    get_captured_sessions,
    add_captured_session,
    remove_captured_session,
    get_working_days,
    load_json_file,
    load_settings,
    project_name_to_filename,
    save_json_file,
)


# =============================================================================
# Token Estimation Tests
# =============================================================================


class TestEstimateTokens:
    def test_empty_string(self):
        assert estimate_tokens("") == 0

    def test_basic_text(self):
        # 20 chars → 5 tokens
        assert estimate_tokens("12345678901234567890") == 5

    def test_short_text(self):
        # 3 chars → 0 (integer division)
        assert estimate_tokens("abc") == 0

    def test_four_chars(self):
        assert estimate_tokens("abcd") == 1


# =============================================================================
# Settings Tests
# =============================================================================


class TestLoadSettings:
    def test_defaults_when_no_file(self):
        with mock.patch("memory_utils.get_settings_file") as mock_sf:
            mock_sf.return_value = Path("/nonexistent/settings.json")
            settings = load_settings()
            assert settings["globalShortTerm"]["workingDays"] == DEFAULT_SETTINGS["globalShortTerm"]["workingDays"]
            assert settings["projectShortTerm"]["workingDays"] == DEFAULT_SETTINGS["projectShortTerm"]["workingDays"]
            assert settings["globalLongTerm"]["tokenLimit"] == DEFAULT_SETTINGS["globalLongTerm"]["tokenLimit"]

    def test_calculated_token_limits(self):
        with mock.patch("memory_utils.get_settings_file") as mock_sf:
            mock_sf.return_value = Path("/nonexistent/settings.json")
            settings = load_settings()
            assert settings["globalShortTerm"]["tokenLimit"] == 2 * SHORT_TERM_TOKENS_PER_DAY
            assert settings["projectShortTerm"]["tokenLimit"] == 7 * SHORT_TERM_TOKENS_PER_DAY

    def test_total_budget_calculation(self):
        with mock.patch("memory_utils.get_settings_file") as mock_sf:
            mock_sf.return_value = Path("/nonexistent/settings.json")
            settings = load_settings()
            expected = (
                DEFAULT_SETTINGS["globalLongTerm"]["tokenLimit"]
                + DEFAULT_SETTINGS["globalShortTerm"]["workingDays"] * SHORT_TERM_TOKENS_PER_DAY
                + DEFAULT_SETTINGS["projectLongTerm"]["tokenLimit"]
                + DEFAULT_SETTINGS["projectShortTerm"]["workingDays"] * SHORT_TERM_TOKENS_PER_DAY
            )
            assert settings["totalTokenBudget"] == expected

    def test_user_overrides_merge(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"globalShortTerm": {"workingDays": 5}}, f)
            f.flush()
            try:
                with mock.patch("memory_utils.get_settings_file") as mock_sf:
                    mock_sf.return_value = Path(f.name)
                    settings = load_settings()
                    assert settings["globalShortTerm"]["workingDays"] == 5
                    # Other defaults preserved
                    assert settings["projectShortTerm"]["workingDays"] == DEFAULT_SETTINGS["projectShortTerm"]["workingDays"]
            finally:
                os.unlink(f.name)

    def test_invalid_json_returns_defaults(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("not valid json {{{")
            f.flush()
            try:
                with mock.patch("memory_utils.get_settings_file") as mock_sf:
                    mock_sf.return_value = Path(f.name)
                    settings = load_settings()
                    assert settings["globalShortTerm"]["workingDays"] == DEFAULT_SETTINGS["globalShortTerm"]["workingDays"]
            finally:
                os.unlink(f.name)


class TestDeepMerge:
    def test_flat_merge(self):
        assert _deep_merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}

    def test_override(self):
        assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}

    def test_nested_merge(self):
        base = {"nested": {"a": 1, "b": 2}}
        override = {"nested": {"b": 3, "c": 4}}
        result = _deep_merge(base, override)
        assert result == {"nested": {"a": 1, "b": 3, "c": 4}}

    def test_does_not_mutate_base(self):
        base = {"a": 1}
        _deep_merge(base, {"b": 2})
        assert base == {"a": 1}


# =============================================================================
# JSON File Utilities Tests
# =============================================================================


class TestJsonFileUtils:
    def test_load_nonexistent(self):
        result = load_json_file(Path("/nonexistent/file.json"), {"default": True})
        assert result == {"default": True}

    def test_load_valid_json(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump({"key": "value"}, f)
            f.flush()
            try:
                result = load_json_file(Path(f.name))
                assert result == {"key": "value"}
            finally:
                os.unlink(f.name)

    def test_load_invalid_json_returns_default(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            f.write("broken")
            f.flush()
            try:
                result = load_json_file(Path(f.name), [])
                assert result == []
            finally:
                os.unlink(f.name)

    def test_save_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "sub" / "dir" / "data.json"
            assert save_json_file(filepath, {"saved": True})
            assert filepath.exists()
            assert json.loads(filepath.read_text()) == {"saved": True}

    def test_save_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = Path(tmpdir) / "test.json"
            data = {"nested": {"list": [1, 2, 3]}}
            save_json_file(filepath, data)
            loaded = load_json_file(filepath)
            assert loaded == data


# =============================================================================
# Project Name to Filename Tests
# =============================================================================


class TestProjectNameToFilename:
    def test_simple_name(self):
        assert project_name_to_filename("myproject") == "myproject-long-term-memory.md"

    def test_spaces(self):
        assert project_name_to_filename("My Project") == "my-project-long-term-memory.md"

    def test_special_characters(self):
        assert project_name_to_filename("My@Project!") == "myproject-long-term-memory.md"

    def test_consecutive_hyphens(self):
        assert project_name_to_filename("my--project") == "my-project-long-term-memory.md"

    def test_leading_trailing_hyphens(self):
        assert project_name_to_filename("-project-") == "project-long-term-memory.md"


# =============================================================================
# Captured Sessions Tests
# =============================================================================


class TestCapturedSessions:
    def test_empty_when_no_file(self):
        with mock.patch("memory_utils.get_captured_file") as mock_cf:
            mock_cf.return_value = Path("/nonexistent/.captured")
            assert get_captured_sessions() == set()

    def test_read_captured(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".captured", delete=False
        ) as f:
            f.write("session-1\nsession-2\n\nsession-3\n")
            f.flush()
            try:
                with mock.patch("memory_utils.get_captured_file") as mock_cf:
                    mock_cf.return_value = Path(f.name)
                    result = get_captured_sessions()
                    assert result == {"session-1", "session-2", "session-3"}
            finally:
                os.unlink(f.name)

    def test_add_captured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            captured_file = Path(tmpdir) / ".captured"
            with mock.patch("memory_utils.get_captured_file") as mock_cf:
                mock_cf.return_value = captured_file
                add_captured_session("new-session")
                assert "new-session" in captured_file.read_text()

    def test_add_duplicate_skipped(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            captured_file = Path(tmpdir) / ".captured"
            captured_file.write_text("existing\n")
            with mock.patch("memory_utils.get_captured_file") as mock_cf:
                mock_cf.return_value = captured_file
                add_captured_session("existing")
                # Should not have duplicate
                lines = [l for l in captured_file.read_text().splitlines() if l.strip()]
                assert lines.count("existing") == 1

    def test_remove_captured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            captured_file = Path(tmpdir) / ".captured"
            captured_file.write_text("keep-me\nremove-me\nalso-keep\n")
            with mock.patch("memory_utils.get_captured_file") as mock_cf:
                mock_cf.return_value = captured_file
                assert remove_captured_session("remove-me") is True
                content = captured_file.read_text()
                assert "remove-me" not in content
                assert "keep-me" in content

    def test_remove_nonexistent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            captured_file = Path(tmpdir) / ".captured"
            captured_file.write_text("session-1\n")
            with mock.patch("memory_utils.get_captured_file") as mock_cf:
                mock_cf.return_value = captured_file
                assert remove_captured_session("nonexistent") is False


# =============================================================================
# Working Days Tests
# =============================================================================


class TestGetWorkingDays:
    def test_empty_when_no_dir(self):
        with mock.patch("memory_utils.get_daily_dir") as mock_dd:
            mock_dd.return_value = Path("/nonexistent/daily")
            assert get_working_days(7) == []

    def test_returns_sorted_descending(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            daily_dir = Path(tmpdir)
            (daily_dir / "2026-01-01.md").write_text("day 1")
            (daily_dir / "2026-01-03.md").write_text("day 3")
            (daily_dir / "2026-01-02.md").write_text("day 2")

            with mock.patch("memory_utils.get_daily_dir") as mock_dd:
                mock_dd.return_value = daily_dir
                days = get_working_days(10)
                assert days == ["2026-01-03", "2026-01-02", "2026-01-01"]

    def test_respects_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            daily_dir = Path(tmpdir)
            for i in range(1, 6):
                (daily_dir / f"2026-01-0{i}.md").write_text(f"day {i}")

            with mock.patch("memory_utils.get_daily_dir") as mock_dd:
                mock_dd.return_value = daily_dir
                days = get_working_days(2)
                assert len(days) == 2
                assert days[0] == "2026-01-05"


# =============================================================================
# Filter Daily Content Tests
# =============================================================================


class TestFilterDailyContent:
    SAMPLE_DAILY = """# 2026-02-01
## Actions
- [global/implement] Set up new hooks
- [myproject/implement] Added feature X

## Learnings
- [global/pattern] Important global pattern
- [myproject/gotcha] Project-specific gotcha
"""

    def test_global_scope(self):
        result = filter_daily_content(self.SAMPLE_DAILY, "global")
        assert "[global/implement]" in result
        assert "[global/pattern]" in result
        assert "[myproject/" not in result

    def test_project_scope(self):
        result = filter_daily_content(self.SAMPLE_DAILY, "myproject")
        assert "[myproject/implement]" in result
        assert "[myproject/gotcha]" in result
        assert "[global/" not in result

    def test_no_matching_scope(self):
        result = filter_daily_content(self.SAMPLE_DAILY, "other-project")
        assert result == ""

    def test_preserves_date_header(self):
        result = filter_daily_content(self.SAMPLE_DAILY, "global")
        assert "# 2026-02-01" in result

    def test_empty_content(self):
        assert filter_daily_content("", "global") == ""

    def test_date_only_returns_empty(self):
        result = filter_daily_content("# 2026-02-01\n", "global")
        assert result == ""

    def test_case_insensitive_scope(self):
        content = "# 2026-02-01\n## Actions\n- [Global/implement] Something\n"
        result = filter_daily_content(content, "global")
        assert "[Global/implement]" in result


# =============================================================================
# Find Current Project Tests
# =============================================================================


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
        result = find_current_project(index, "/home/user/project", include_subdirs=False)
        assert result is not None
        assert result["name"] == "project"

    def test_no_match(self):
        index = {"projects": {"/home/user/project": {"name": "project"}}}
        result = find_current_project(index, "/home/user/other", include_subdirs=False)
        assert result is None

    def test_subdirectory_match_when_enabled(self):
        index = {
            "projects": {
                "/home/user/project": {
                    "name": "project",
                    "originalPath": "/home/user/project",
                }
            }
        }
        result = find_current_project(
            index, "/home/user/project/subdir", include_subdirs=True
        )
        assert result is not None
        assert result["name"] == "project"

    def test_subdirectory_no_match_when_disabled(self):
        index = {
            "projects": {
                "/home/user/project": {
                    "name": "project",
                    "originalPath": "/home/user/project",
                }
            }
        }
        result = find_current_project(
            index, "/home/user/project/subdir", include_subdirs=False
        )
        assert result is None

    def test_longest_subdirectory_match(self):
        """When multiple projects match, pick the longest (most specific) path."""
        index = {
            "projects": {
                "/home/user": {"name": "user", "originalPath": "/home/user"},
                "/home/user/project": {
                    "name": "project",
                    "originalPath": "/home/user/project",
                },
            }
        }
        result = find_current_project(
            index, "/home/user/project/subdir", include_subdirs=True
        )
        assert result["name"] == "project"

    def test_empty_projects(self):
        result = find_current_project({"projects": {}}, "/home/user", include_subdirs=False)
        assert result is None


# =============================================================================
# FileLock Tests
# =============================================================================


class TestFileLock:
    def test_acquire_and_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            lock = FileLock(lock_path, timeout=2.0)
            assert lock.acquire() is True
            assert lock_path.exists()
            lock.release()
            assert not lock_path.exists()

    def test_context_manager(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            with FileLock(lock_path, timeout=2.0) as lock:
                assert lock_path.exists()
            assert not lock_path.exists()

    def test_writes_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            with FileLock(lock_path, timeout=2.0):
                pid_file = lock_path / "pid"
                assert pid_file.exists()
                assert int(pid_file.read_text().strip()) == os.getpid()

    def test_stale_lock_removed_by_dead_pid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            # Create a stale lock with a dead PID
            lock_path.mkdir()
            (lock_path / "pid").write_text("999999999")  # Very unlikely to be a real PID

            lock = FileLock(lock_path, timeout=2.0)
            assert lock.acquire() is True
            lock.release()

    def test_timeout_when_locked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            # Acquire lock
            lock1 = FileLock(lock_path, timeout=2.0)
            lock1.acquire()

            # Second lock should timeout (owner PID is alive — it's us)
            lock2 = FileLock(lock_path, timeout=0.3, poll_interval=0.1)
            assert lock2.acquire() is False

            lock1.release()

    def test_double_release_is_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = Path(tmpdir) / "test.lock"
            lock = FileLock(lock_path, timeout=2.0)
            lock.acquire()
            lock.release()
            lock.release()  # Should not raise


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
