#!/usr/bin/env python3
"""Unit tests for memory_utils.py"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
from memory_utils import (
    DEFAULT_SETTINGS,
    SHORT_TERM_TOKENS_PER_DAY,
    FileLock,
    _calculate_token_limits,
    _deep_merge,
    add_captured_session,
    estimate_tokens,
    extract_entry_keywords,
    filter_daily_content,
    find_current_project,
    from_iso_z,
    get_captured_sessions,
    get_sessions_original_path,
    get_synthesis_state_file,
    get_working_days,
    is_routed_match,
    load_json_file,
    load_sessions_index,
    load_settings,
    load_synthesis_state,
    project_name_to_filename,
    prune_captured_from_state,
    remove_captured_session,
    save_json_file,
    save_synthesis_state,
    to_iso_z,
    update_synthesis_state,
)

# =============================================================================
# Token Estimation Tests
# =============================================================================


@pytest.mark.parametrize("text,expected", [
    ("", 0),
    ("12345678901234567890", 5),
    ("abc", 0),
    ("abcd", 1),
])
def test_estimate_tokens(text, expected):
    assert estimate_tokens(text) == expected


# =============================================================================
# Settings Tests
# =============================================================================


class TestLoadSettings:
    @pytest.fixture
    def no_settings_file(self):
        with mock.patch("memory_utils.get_settings_file") as mock_sf:
            mock_sf.return_value = Path("/nonexistent/settings.json")
            yield

    def test_defaults_when_no_file(self, no_settings_file):
        settings = load_settings()
        assert settings["globalShortTerm"]["workingDays"] == DEFAULT_SETTINGS["globalShortTerm"]["workingDays"]
        assert settings["projectShortTerm"]["workingDays"] == DEFAULT_SETTINGS["projectShortTerm"]["workingDays"]
        assert settings["globalLongTerm"]["tokenLimit"] == DEFAULT_SETTINGS["globalLongTerm"]["tokenLimit"]

    def test_calculated_token_limits(self, no_settings_file):
        settings = load_settings()
        assert settings["globalShortTerm"]["tokenLimit"] == 2 * SHORT_TERM_TOKENS_PER_DAY
        assert settings["projectShortTerm"]["tokenLimit"] == DEFAULT_SETTINGS["projectShortTerm"]["workingDays"] * SHORT_TERM_TOKENS_PER_DAY

    def test_total_budget_calculation(self, no_settings_file):
        settings = load_settings()
        expected = (
            DEFAULT_SETTINGS["globalLongTerm"]["tokenLimit"]
            + DEFAULT_SETTINGS["globalShortTerm"]["workingDays"] * SHORT_TERM_TOKENS_PER_DAY
            + DEFAULT_SETTINGS["projectLongTerm"]["tokenLimit"]
            + DEFAULT_SETTINGS["projectShortTerm"]["workingDays"] * SHORT_TERM_TOKENS_PER_DAY
        )
        assert settings["totalTokenBudget"] == expected

    def test_user_overrides_merge(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text(json.dumps({"globalShortTerm": {"workingDays": 5}}))
        with mock.patch("memory_utils.get_settings_file") as mock_sf:
            mock_sf.return_value = f
            settings = load_settings()
            assert settings["globalShortTerm"]["workingDays"] == 5
            # Other defaults preserved
            assert settings["projectShortTerm"]["workingDays"] == DEFAULT_SETTINGS["projectShortTerm"]["workingDays"]

    def test_invalid_json_returns_defaults(self, tmp_path):
        f = tmp_path / "settings.json"
        f.write_text("not valid json {{{")
        with mock.patch("memory_utils.get_settings_file") as mock_sf:
            mock_sf.return_value = f
            settings = load_settings()
            assert settings["globalShortTerm"]["workingDays"] == DEFAULT_SETTINGS["globalShortTerm"]["workingDays"]


class TestCalculateTokenLimits:
    def test_fallback_defaults_match_default_settings(self):
        """_calculate_token_limits fallbacks must match DEFAULT_SETTINGS."""
        # Pass empty settings to trigger all fallbacks
        result = _calculate_token_limits({})
        expected_project_days = DEFAULT_SETTINGS["projectShortTerm"]["workingDays"]  # 5
        assert result["projectShortTerm"]["tokenLimit"] == expected_project_days * SHORT_TERM_TOKENS_PER_DAY

    def test_fallback_global_short_term_matches_default_settings(self):
        """Global short-term fallback must match DEFAULT_SETTINGS."""
        result = _calculate_token_limits({})
        expected_global_days = DEFAULT_SETTINGS["globalShortTerm"]["workingDays"]  # 2
        assert result["globalShortTerm"]["tokenLimit"] == expected_global_days * SHORT_TERM_TOKENS_PER_DAY

    def test_fallback_long_term_limits_match_default_settings(self):
        """Long-term token limit fallbacks must match DEFAULT_SETTINGS."""
        result = _calculate_token_limits({})
        expected_total = (
            DEFAULT_SETTINGS["globalLongTerm"]["tokenLimit"]
            + DEFAULT_SETTINGS["globalShortTerm"]["workingDays"] * SHORT_TERM_TOKENS_PER_DAY
            + DEFAULT_SETTINGS["projectLongTerm"]["tokenLimit"]
            + DEFAULT_SETTINGS["projectShortTerm"]["workingDays"] * SHORT_TERM_TOKENS_PER_DAY
        )
        assert result["totalTokenBudget"] == expected_total


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

    def test_load_valid_json(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text(json.dumps({"key": "value"}))
        result = load_json_file(f)
        assert result == {"key": "value"}

    def test_load_invalid_json_returns_default(self, tmp_path):
        f = tmp_path / "broken.json"
        f.write_text("broken")
        result = load_json_file(f, [])
        assert result == []

    def test_save_creates_parent_dirs(self, tmp_path):
        filepath = tmp_path / "sub" / "dir" / "data.json"
        assert save_json_file(filepath, {"saved": True})
        assert filepath.exists()
        assert json.loads(filepath.read_text()) == {"saved": True}

    def test_save_round_trip(self, tmp_path):
        filepath = tmp_path / "test.json"
        data = {"nested": {"list": [1, 2, 3]}}
        save_json_file(filepath, data)
        loaded = load_json_file(filepath)
        assert loaded == data


# =============================================================================
# Project Name to Filename Tests
# =============================================================================


@pytest.mark.parametrize("name,expected", [
    ("myproject", "myproject-long-term-memory.md"),
    ("My Project", "my-project-long-term-memory.md"),
    ("My@Project!", "myproject-long-term-memory.md"),
    ("my--project", "my-project-long-term-memory.md"),
    ("-project-", "project-long-term-memory.md"),
])
def test_project_name_to_filename(name, expected):
    assert project_name_to_filename(name) == expected


# =============================================================================
# Captured Sessions Tests
# =============================================================================


class TestCapturedSessions:
    def test_empty_when_no_file(self):
        with mock.patch("memory_utils.get_captured_file") as mock_cf:
            mock_cf.return_value = Path("/nonexistent/.captured")
            assert get_captured_sessions() == set()

    def test_read_captured(self, tmp_path):
        f = tmp_path / ".captured"
        f.write_text("session-1\nsession-2\n\nsession-3\n")
        with mock.patch("memory_utils.get_captured_file") as mock_cf:
            mock_cf.return_value = f
            result = get_captured_sessions()
            assert result == {"session-1", "session-2", "session-3"}

    def test_add_captured(self, tmp_path):
        captured_file = tmp_path / ".captured"
        with mock.patch("memory_utils.get_captured_file") as mock_cf:
            mock_cf.return_value = captured_file
            add_captured_session("new-session")
            assert "new-session" in captured_file.read_text()

    def test_add_duplicate_skipped(self, tmp_path):
        captured_file = tmp_path / ".captured"
        captured_file.write_text("existing\n")
        with mock.patch("memory_utils.get_captured_file") as mock_cf:
            mock_cf.return_value = captured_file
            add_captured_session("existing")
            # Should not have duplicate
            lines = [line for line in captured_file.read_text().splitlines() if line.strip()]
            assert lines.count("existing") == 1

    def test_remove_captured(self, tmp_path):
        captured_file = tmp_path / ".captured"
        captured_file.write_text("keep-me\nremove-me\nalso-keep\n")
        with mock.patch("memory_utils.get_captured_file") as mock_cf:
            mock_cf.return_value = captured_file
            assert remove_captured_session("remove-me") is True
            content = captured_file.read_text()
            assert "remove-me" not in content
            assert "keep-me" in content

    def test_remove_nonexistent(self, tmp_path):
        captured_file = tmp_path / ".captured"
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

    def test_returns_sorted_descending(self, tmp_path):
        daily_dir = tmp_path
        (daily_dir / "2026-01-01.md").write_text("day 1")
        (daily_dir / "2026-01-03.md").write_text("day 3")
        (daily_dir / "2026-01-02.md").write_text("day 2")

        with mock.patch("memory_utils.get_daily_dir") as mock_dd:
            mock_dd.return_value = daily_dir
            days = get_working_days(10)
            assert days == ["2026-01-03", "2026-01-02", "2026-01-01"]

    def test_respects_limit(self, tmp_path):
        daily_dir = tmp_path
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

    def test_html_comments_stripped(self):
        content = """# 2026-02-01
## Actions
<!-- What was done. Tag [scope/action]. -->
- [global/implement] Set up new hooks
"""
        result = filter_daily_content(content, "global")
        assert "<!--" not in result
        assert "[global/implement]" in result

    def test_html_comments_dont_count_as_content(self):
        """Section with only comments and no entries should not appear."""
        content = """# 2026-02-01
## Actions
<!-- What was done. Tag [scope/action]. -->
## Learnings
- [global/pattern] Some pattern
"""
        result = filter_daily_content(content, "global")
        assert "## Actions" not in result
        assert "## Learnings" in result

    def test_routed_entries_skipped(self):
        content = """# 2026-02-01
## Learnings
- [routed][global/pattern] Already in LTM
- [global/gotcha] Still only in STM
"""
        result = filter_daily_content(content, "global")
        assert "[routed]" not in result
        assert "[global/gotcha] Still only in STM" in result

    def test_routed_entries_skipped_project_scope(self):
        content = """# 2026-02-01
## Learnings
- [routed][myproject/pattern] Already routed
- [myproject/gotcha] Not routed
"""
        result = filter_daily_content(content, "myproject")
        assert "[routed]" not in result
        assert "[myproject/gotcha] Not routed" in result

    def test_routed_entries_not_counted_as_content(self):
        """A section with only routed entries should not appear in output."""
        content = """# 2026-02-01
## Learnings
- [routed][global/pattern] Already in LTM
## Actions
- [global/implement] Did something
"""
        result = filter_daily_content(content, "global")
        assert "## Learnings" not in result
        assert "## Actions" in result
        assert "[global/implement]" in result


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
    def test_acquire_and_release(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        lock = FileLock(lock_path, timeout=2.0)
        assert lock.acquire() is True
        assert lock_path.exists()
        lock.release()
        assert not lock_path.exists()

    def test_context_manager(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        with FileLock(lock_path, timeout=2.0):
            assert lock_path.exists()
        assert not lock_path.exists()

    def test_writes_pid(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        with FileLock(lock_path, timeout=2.0):
            pid_file = lock_path / "pid"
            assert pid_file.exists()
            assert int(pid_file.read_text().strip()) == os.getpid()

    def test_stale_lock_removed_by_dead_pid(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        # Create a stale lock with a dead PID
        lock_path.mkdir()
        (lock_path / "pid").write_text("999999999")  # Very unlikely to be a real PID

        lock = FileLock(lock_path, timeout=2.0)
        assert lock.acquire() is True
        lock.release()

    def test_timeout_when_locked(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        # Acquire lock
        lock1 = FileLock(lock_path, timeout=2.0)
        lock1.acquire()

        # Second lock should timeout (owner PID is alive — it's us)
        lock2 = FileLock(lock_path, timeout=0.3, poll_interval=0.1)
        assert lock2.acquire() is False

        lock1.release()

    def test_double_release_is_safe(self, tmp_path):
        lock_path = tmp_path / "test.lock"
        lock = FileLock(lock_path, timeout=2.0)
        lock.acquire()
        lock.release()
        lock.release()  # Should not raise


# =============================================================================
# Routed Matching Tests
# =============================================================================


class TestRoutedMatching:
    def test_extract_keywords_strips_tags_and_stopwords(self):
        keywords = extract_entry_keywords(
            "- [claude-memory-system/gotcha] Missing defaultdict import crashed build_projects_index()"
        )
        assert "defaultdict" in keywords
        assert "crashed" in keywords
        assert "claude-memory-system" not in keywords  # tag stripped
        assert "the" not in keywords  # stopword stripped

    def test_match_same_concept_different_wording(self):
        stm = "- [claude-memory-system/gotcha] Missing defaultdict import crashed build_projects_index()"
        ltm = "- (2026-02-12) [gotcha] Missing imports cause cascading failures in indexing — defaultdict missing from build_projects_index()"
        assert is_routed_match(stm, ltm) is True

    def test_no_match_different_concepts(self):
        stm = "- [claude-memory-system/pattern] FileLock prevents concurrent file corruption"
        ltm = "- (2026-02-12) [gotcha] Missing imports cause cascading failures in indexing"
        assert is_routed_match(stm, ltm) is False

    def test_match_with_high_keyword_overlap(self):
        stm = "- [global/pattern] ETL schedule awareness - REBUILDDATAWAREHOUSE runs 6 PM CT"
        ltm = "- (2026-01-28) [pattern] ETL schedule awareness - REBUILDDATAWAREHOUSE runs 6 PM CT"
        assert is_routed_match(stm, ltm) is True

    def test_already_routed_entry_ignored(self):
        """extract_entry_keywords should handle [routed] prefix gracefully."""
        keywords = extract_entry_keywords(
            "- [routed][global/pattern] Already marked"
        )
        assert "already" in keywords
        assert "routed" not in keywords


# =============================================================================
# ISO Datetime Helper Tests
# =============================================================================


class TestIsoDatetimeHelpers:
    def test_to_iso_z_converts_utc(self):
        dt = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone.utc)
        result = to_iso_z(dt)
        assert result.endswith("Z")
        assert "+00:00" not in result

    def test_to_iso_z_format(self):
        dt = datetime(2026, 2, 18, 15, 30, 45, tzinfo=timezone.utc)
        assert to_iso_z(dt) == "2026-02-18T15:30:45Z"

    def test_from_iso_z_parses_z_suffix(self):
        result = from_iso_z("2026-02-18T12:00:00Z")
        assert result.tzinfo is not None
        assert result.year == 2026
        assert result.hour == 12

    def test_from_iso_z_parses_offset(self):
        result = from_iso_z("2026-02-18T12:00:00+00:00")
        assert result.tzinfo is not None
        assert result.year == 2026

    def test_roundtrip(self):
        dt = datetime(2026, 2, 18, 15, 30, 45, tzinfo=timezone.utc)
        assert from_iso_z(to_iso_z(dt)) == dt

    def test_roundtrip_with_microseconds(self):
        dt = datetime(2026, 2, 18, 15, 30, 45, 123456, tzinfo=timezone.utc)
        assert from_iso_z(to_iso_z(dt)) == dt


# =============================================================================
# LTM Entry Pattern Tests
# =============================================================================


class TestLtmEntryPattern:
    def test_matches_dated_entry(self):
        from memory_utils import LTM_ENTRY_PATTERN
        assert LTM_ENTRY_PATTERN.match("- (2026-02-18) [pattern] Some text")

    def test_matches_indented_dated_entry(self):
        from memory_utils import LTM_ENTRY_PATTERN
        assert LTM_ENTRY_PATTERN.match("  - (2026-01-01) [gotcha] Indented")

    def test_rejects_non_dated_tagged(self):
        from memory_utils import LTM_ENTRY_PATTERN
        assert not LTM_ENTRY_PATTERN.match("- [scope/type] No date")

    def test_rejects_section_header(self):
        from memory_utils import LTM_ENTRY_PATTERN
        assert not LTM_ENTRY_PATTERN.match("## Section header")

    def test_rejects_plain_text(self):
        from memory_utils import LTM_ENTRY_PATTERN
        assert not LTM_ENTRY_PATTERN.match("Just plain text without bullet")

    def test_rejects_empty_string(self):
        from memory_utils import LTM_ENTRY_PATTERN
        assert not LTM_ENTRY_PATTERN.match("")


# =============================================================================
# Collect LTM Files Tests
# =============================================================================


class TestCollectLtmFiles:
    def test_collects_global_and_project_files(self, tmp_path):
        from memory_utils import collect_ltm_files

        global_f = tmp_path / "global-long-term-memory.md"
        global_f.write_text("# Global\n")
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        proj_f = proj_dir / "foo-long-term-memory.md"
        proj_f.write_text("# Foo\n")

        with mock.patch("memory_utils.get_global_memory_file", return_value=global_f), \
             mock.patch("memory_utils.get_project_memory_dir", return_value=proj_dir):
            files = collect_ltm_files()

        assert len(files) == 2
        assert global_f in files
        assert proj_f in files

    def test_only_global_when_no_project_dir(self, tmp_path):
        from memory_utils import collect_ltm_files

        global_f = tmp_path / "global-long-term-memory.md"
        global_f.write_text("# Global\n")
        proj_dir = tmp_path / "project-memory"  # does not exist

        with mock.patch("memory_utils.get_global_memory_file", return_value=global_f), \
             mock.patch("memory_utils.get_project_memory_dir", return_value=proj_dir):
            files = collect_ltm_files()

        assert files == [global_f]

    def test_empty_when_nothing_exists(self, tmp_path):
        from memory_utils import collect_ltm_files

        global_f = tmp_path / "global-long-term-memory.md"  # does not exist
        proj_dir = tmp_path / "project-memory"  # does not exist

        with mock.patch("memory_utils.get_global_memory_file", return_value=global_f), \
             mock.patch("memory_utils.get_project_memory_dir", return_value=proj_dir):
            files = collect_ltm_files()

        assert files == []

    def test_multiple_project_files(self, tmp_path):
        from memory_utils import collect_ltm_files

        global_f = tmp_path / "global-long-term-memory.md"
        global_f.write_text("# Global\n")
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        (proj_dir / "foo-long-term-memory.md").write_text("# Foo\n")
        (proj_dir / "bar-long-term-memory.md").write_text("# Bar\n")
        # Non-LTM file should be excluded
        (proj_dir / "notes.md").write_text("# Notes\n")

        with mock.patch("memory_utils.get_global_memory_file", return_value=global_f), \
             mock.patch("memory_utils.get_project_memory_dir", return_value=proj_dir):
            files = collect_ltm_files()

        assert len(files) == 3  # global + 2 project files
        assert global_f in files
        filenames = {f.name for f in files}
        assert "foo-long-term-memory.md" in filenames
        assert "bar-long-term-memory.md" in filenames
        assert "notes.md" not in filenames


# =============================================================================
# Sessions Index Tests
# =============================================================================


class TestLoadSessionsIndex:
    """Tests for load_sessions_index function."""

    def test_valid_file(self, tmp_path):
        """Should return parsed dict from sessions-index.json."""
        folder = tmp_path / "project"
        folder.mkdir()
        (folder / "sessions-index.json").write_text(json.dumps({
            "originalPath": "/home/user/proj",
            "entries": [{"sessionId": "abc"}],
        }))
        result = load_sessions_index(folder)
        assert result["originalPath"] == "/home/user/proj"
        assert len(result["entries"]) == 1

    def test_missing_file(self, tmp_path):
        """Should return empty dict when file doesn't exist."""
        folder = tmp_path / "project"
        folder.mkdir()
        result = load_sessions_index(folder)
        assert result == {}

    def test_invalid_json(self, tmp_path):
        """Should return empty dict on malformed JSON."""
        folder = tmp_path / "project"
        folder.mkdir()
        (folder / "sessions-index.json").write_text("not json{{{")
        result = load_sessions_index(folder)
        assert result == {}

    def test_empty_file(self, tmp_path):
        """Should return empty dict on empty file."""
        folder = tmp_path / "project"
        folder.mkdir()
        (folder / "sessions-index.json").write_text("")
        result = load_sessions_index(folder)
        assert result == {}


class TestGetSessionsOriginalPath:
    """Tests for get_sessions_original_path function."""

    def test_root_level_original_path(self):
        """Should prefer root-level originalPath."""
        data = {
            "originalPath": "/home/user/proj",
            "entries": [{"projectPath": "/other/path"}],
        }
        assert get_sessions_original_path(data) == "/home/user/proj"

    def test_fallback_to_entries(self):
        """Should fall back to entries[0].projectPath."""
        data = {
            "entries": [{"projectPath": "/home/user/proj", "sessionId": "abc"}],
        }
        assert get_sessions_original_path(data) == "/home/user/proj"

    def test_empty_original_path_falls_back(self):
        """Should fall back when originalPath is empty string."""
        data = {
            "originalPath": "",
            "entries": [{"projectPath": "/fallback"}],
        }
        assert get_sessions_original_path(data) == "/fallback"

    def test_no_path_anywhere(self):
        """Should return empty string when no path found."""
        assert get_sessions_original_path({}) == ""
        assert get_sessions_original_path({"entries": []}) == ""

    def test_empty_entries(self):
        """Should return empty string with no originalPath and empty entries."""
        data = {"originalPath": "", "entries": []}
        assert get_sessions_original_path(data) == ""


# =============================================================================
# Synthesis State Tests
# =============================================================================


class TestSynthesisState:
    def test_get_synthesis_state_file(self, tmp_path):
        """Returns .synthesis-state.json in memory dir."""
        with mock.patch("memory_utils.get_memory_dir", return_value=tmp_path):
            result = get_synthesis_state_file()
        assert result == tmp_path / ".synthesis-state.json"

    def test_load_empty(self, tmp_path):
        """Returns empty sessions dict when file doesn't exist."""
        with mock.patch("memory_utils.get_memory_dir", return_value=tmp_path):
            result = load_synthesis_state()
        assert result == {"sessions": {}}

    def test_save_and_load_roundtrip(self, tmp_path):
        """Saved state can be loaded back."""
        state = {"sessions": {"abc": {"offset": 100, "lines": 10, "last_synthesized": "2026-02-22T12:00:00Z"}}}
        state_file = tmp_path / ".synthesis-state.json"
        with mock.patch("memory_utils.get_synthesis_state_file", return_value=state_file):
            save_synthesis_state(state)
            loaded = load_synthesis_state()
        assert loaded == state

    def test_update_synthesis_state(self, tmp_path):
        """Updates offsets for given sessions, preserves others."""
        state_file = tmp_path / ".synthesis-state.json"
        initial = {"sessions": {"old": {"offset": 50, "lines": 5, "last_synthesized": "2026-02-22T10:00:00Z"}}}
        state_file.write_text(json.dumps(initial))

        with mock.patch("memory_utils.get_synthesis_state_file", return_value=state_file):
            update_synthesis_state({"new": {"offset": 200, "lines": 20}})
            result = load_synthesis_state()
        assert "old" in result["sessions"]
        assert "new" in result["sessions"]
        assert result["sessions"]["new"]["offset"] == 200
        assert "last_synthesized" in result["sessions"]["new"]

    def test_prune_captured_from_state(self, tmp_path):
        """Removes captured session IDs from state."""
        state_file = tmp_path / ".synthesis-state.json"
        state = {"sessions": {
            "keep": {"offset": 100, "lines": 10, "last_synthesized": "2026-02-22T10:00:00Z"},
            "remove": {"offset": 200, "lines": 20, "last_synthesized": "2026-02-22T10:00:00Z"},
        }}
        state_file.write_text(json.dumps(state))

        with mock.patch("memory_utils.get_synthesis_state_file", return_value=state_file):
            prune_captured_from_state({"remove", "not-present"})
            result = load_synthesis_state()
        assert "keep" in result["sessions"]
        assert "remove" not in result["sessions"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
