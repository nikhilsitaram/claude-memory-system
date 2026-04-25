#!/usr/bin/env python3
"""Unit tests for memory_utils.py"""

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest
from memory_utils import (
    DEFAULT_SETTINGS,
    SHORT_TERM_TOKENS_PER_DAY,
    FileLock,
    _calculate_token_limits,
    _clear_projects_index_cache,
    _deep_merge,
    estimate_tokens,
    extract_entry_keywords,
    filter_daily_content,
    find_current_project,
    from_iso_z,
    get_memory_dir,
    get_pending_recall_dir,
    get_sessions_original_path,
    get_synthesis_log_file,
    get_synthesis_state_file,
    get_synthesis_stats_file,
    get_working_days,
    is_routed_match,
    load_json_file,
    load_sessions_index,
    load_settings,
    load_synthesis_state,
    local_today,
    parse_markdown_sections,
    project_name_to_filename,
    resolve_git_subdir_to_root,
    resolve_project_path_to_name,
    resolve_session_path,
    resolve_worktree_to_main_repo,
    save_json_file,
    save_synthesis_state,
    to_iso_z,
    update_synthesis_state,
    utc_to_local_datestr,
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


class TestFilterDailyContentMultiScope:
    """Tests for pipe-delimited multi-scope tag filtering."""

    def test_single_scope_unchanged(self):
        """Existing single-scope tags still work."""
        content = "# 2026-02-23\n## Actions\n- [cartwheel/implement] Built OAuth\n"
        result = filter_daily_content(content, "cartwheel")
        assert "[cartwheel/implement] Built OAuth" in result

    def test_multi_scope_matches_first(self):
        """Multi-scope entry matches on first scope."""
        content = "# 2026-02-23\n## Learnings\n- [global|cartwheel/gotcha] MTU issue\n"
        result = filter_daily_content(content, "global")
        assert "MTU issue" in result

    def test_multi_scope_matches_second(self):
        """Multi-scope entry matches on second scope."""
        content = "# 2026-02-23\n## Learnings\n- [global|cartwheel/gotcha] MTU issue\n"
        result = filter_daily_content(content, "cartwheel")
        assert "MTU issue" in result

    def test_multi_scope_no_match(self):
        """Multi-scope entry doesn't match unrelated scope."""
        content = "# 2026-02-23\n## Learnings\n- [global|cartwheel/gotcha] MTU issue\n"
        result = filter_daily_content(content, "investing")
        assert result == ""

    def test_mixed_single_and_multi_scope(self):
        """File with both single and multi-scope entries filters correctly."""
        content = (
            "# 2026-02-23\n## Actions\n"
            "- [cartwheel/implement] OAuth flow\n"
            "- [global|cartwheel/implement] CI pipeline\n"
            "- [global/implement] Git hooks\n"
        )
        result = filter_daily_content(content, "cartwheel")
        assert "OAuth flow" in result
        assert "CI pipeline" in result
        assert "Git hooks" not in result


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

    def test_case_insensitive_key_stored_uppercase(self):
        """Keys stored with mixed case still match lowercase PWD."""
        index = {
            "projects": {
                "/Users/nsitaram/personal/project": {
                    "name": "project",
                    "originalPath": "/Users/nsitaram/personal/project",
                }
            }
        }
        result = find_current_project(index, "/users/nsitaram/personal/project")
        assert result is not None
        assert result["name"] == "project"

    def test_case_variants_merged(self):
        """Two entries differing only in case get merged."""
        index = {
            "projects": {
                "/home/user/project": {
                    "name": "project",
                    "workDays": ["2026-01-01"],
                    "encodedPaths": ["enc-a"],
                },
                "/Home/User/Project": {
                    "name": "project",
                    "workDays": ["2026-01-02"],
                    "encodedPaths": ["enc-b"],
                },
            }
        }
        result = find_current_project(index, "/home/user/project")
        assert result is not None
        assert result["name"] == "project"
        assert sorted(result["workDays"]) == ["2026-01-01", "2026-01-02"]
        assert sorted(result["encodedPaths"]) == ["enc-a", "enc-b"]

    def test_basename_fallback_corrects_stale_path(self, tmp_path):
        """Tier 2: basename match against stale entry auto-corrects the path."""
        live_dir = tmp_path / "myproject"
        live_dir.mkdir()
        index_file = tmp_path / "projects-index.json"

        index = {
            "projects": {
                "/home/olduser/myproject": {
                    "name": "myproject",
                    "originalPath": "/home/olduser/myproject",
                    "workDays": ["2026-01-01"],
                    "encodedPaths": ["enc-a"],
                }
            }
        }
        with patch("memory_utils.get_projects_index_file", return_value=index_file):
            result = find_current_project(index, str(live_dir))
        assert result is not None
        assert result["name"] == "myproject"
        assert result["originalPath"] == str(live_dir)

    def test_basename_fallback_no_match_when_path_exists(self, tmp_path):
        """Tier 2 does not fire when the indexed path exists on disk."""
        live_dir = tmp_path / "myproject"
        live_dir.mkdir()
        index = {
            "projects": {
                str(live_dir).lower(): {
                    "name": "myproject",
                    "originalPath": str(live_dir),
                    "workDays": ["2026-01-01"],
                }
            }
        }
        result = find_current_project(index, str(live_dir))
        assert result is not None
        assert result["name"] == "myproject"

    def test_basename_fallback_ambiguous_skipped(self, tmp_path):
        """Tier 2 returns None when multiple stale entries share the basename."""
        live_dir = tmp_path / "myproject"
        live_dir.mkdir()
        index = {
            "projects": {
                "/home/user1/myproject": {
                    "name": "myproject",
                    "originalPath": "/home/user1/myproject",
                },
                "/home/user2/myproject": {
                    "name": "myproject",
                    "originalPath": "/home/user2/myproject",
                },
            }
        }
        result = find_current_project(index, str(live_dir))
        assert result is None

    def test_basename_fallback_ignores_live_entries(self, tmp_path):
        """Tier 2 only considers stale entries (path doesn't exist on disk)."""
        live_dir = tmp_path / "myproject"
        live_dir.mkdir()
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        index = {
            "projects": {
                str(other_dir).lower(): {
                    "name": "myproject",
                    "originalPath": str(other_dir),
                }
            }
        }
        result = find_current_project(index, str(live_dir))
        assert result is None

    def test_basename_fallback_persists_correction(self, tmp_path):
        """Tier 2 writes the corrected index to disk."""
        live_dir = tmp_path / "myproject"
        live_dir.mkdir()
        index_file = tmp_path / "projects-index.json"

        index = {
            "projects": {
                "/home/olduser/myproject": {
                    "name": "myproject",
                    "originalPath": "/home/olduser/myproject",
                    "workDays": ["2026-01-01"],
                    "encodedPaths": ["enc-a"],
                }
            }
        }
        with patch("memory_utils.get_projects_index_file", return_value=index_file):
            find_current_project(index, str(live_dir))

        assert index_file.exists()
        import json
        saved = json.loads(index_file.read_text())
        keys = list(saved["projects"].keys())
        assert len(keys) == 1
        assert keys[0] == str(live_dir).lower()


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


class TestLocalTimezoneHelpers:
    def test_local_today_returns_date(self):
        from datetime import date
        result = local_today()
        assert isinstance(result, date)
        # Should match datetime.now().date()
        assert result == datetime.now().date()

    def test_utc_to_local_datestr_format(self):
        dt = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        result = utc_to_local_datestr(dt)
        # Should be YYYY-MM-DD format
        assert len(result) == 10
        assert result[4] == "-"
        assert result[7] == "-"

    def test_utc_to_local_datestr_late_night_utc(self):
        """A late-night UTC time should map to local date, not UTC date."""
        # 11:30 PM UTC on Feb 24 = 5:30 PM CT on Feb 24 (CT = UTC-6)
        # But 5:00 AM UTC on Feb 24 = 11:00 PM CT on Feb 23
        dt = datetime(2026, 2, 24, 5, 0, 0, tzinfo=timezone.utc)
        result = utc_to_local_datestr(dt)
        # Result depends on system timezone, but should be a valid date
        assert len(result) == 10
        # The key invariant: result should match what astimezone() gives
        expected = dt.astimezone().strftime("%Y-%m-%d")
        assert result == expected

    def test_utc_to_local_datestr_noon_utc(self):
        """Midday UTC should produce a valid local date string."""
        dt = datetime(2026, 7, 4, 12, 0, 0, tzinfo=timezone.utc)
        result = utc_to_local_datestr(dt)
        expected = dt.astimezone().strftime("%Y-%m-%d")
        assert result == expected


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

# =============================================================================
# Resolve Worktree to Main Repo Tests
# =============================================================================


class TestWorktreePatternFallback:
    """Tests for _worktree_pattern_fallback()."""

    def test_worktree_path_resolves_to_parent(self):
        """Path with /.worktrees/ returns everything before the marker."""
        from memory_utils import _worktree_pattern_fallback
        assert _worktree_pattern_fallback("/repo/.worktrees/feature") == "/repo"

    def test_nested_worktree_path(self):
        """Path with subdirectory after worktree name."""
        from memory_utils import _worktree_pattern_fallback
        assert _worktree_pattern_fallback("/repo/.worktrees/feature/src/main") == "/repo"

    def test_claude_worktrees_path_resolves(self):
        """Path with /.claude/worktrees/ returns everything before .claude/worktrees/."""
        from memory_utils import _worktree_pattern_fallback
        assert _worktree_pattern_fallback("/Users/nsitaram/personal/claude-caliper/.claude/worktrees/pipeline-gates") == "/Users/nsitaram/personal/claude-caliper"

    def test_claude_worktrees_nested_path(self):
        """Nested /.claude/worktrees/ path resolves correctly."""
        from memory_utils import _worktree_pattern_fallback
        assert _worktree_pattern_fallback("/home/user/repo/.claude/worktrees/feature/src") == "/home/user/repo"

    def test_non_worktree_path_unchanged(self):
        """Path without any worktree marker returns unchanged."""
        from memory_utils import _worktree_pattern_fallback
        assert _worktree_pattern_fallback("/tmp/not-a-repo") == "/tmp/not-a-repo"

    def test_worktrees_in_deep_path(self):
        """Worktree marker deep in path resolves correctly."""
        from memory_utils import _worktree_pattern_fallback
        assert _worktree_pattern_fallback("/home/user/projects/myrepo/.worktrees/bugfix") == "/home/user/projects/myrepo"


class TestResolveWorktreeToMainRepo:
    """Tests for resolve_worktree_to_main_repo()."""

    def test_worktree_resolves_to_main_repo(self):
        """When git says toplevel != common-dir parent, return main repo root."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/repo/.worktrees/feature\n"),
                MagicMock(returncode=0, stdout="/repo/.git\n"),
            ]
            result = resolve_worktree_to_main_repo("/repo/.worktrees/feature/src")
            assert result == "/repo"

    def test_main_repo_returns_unchanged(self):
        """When toplevel == common-dir parent, it's the main repo -- return as-is."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/home/user/project\n"),
                MagicMock(returncode=0, stdout="/home/user/project/.git\n"),
            ]
            result = resolve_worktree_to_main_repo("/home/user/project")
            assert result == "/home/user/project"

    def test_non_git_non_worktree_returns_unchanged(self):
        """Non-git directory without /.worktrees/ pattern returns unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            result = resolve_worktree_to_main_repo("/tmp/not-a-repo")
            assert result == "/tmp/not-a-repo"

    def test_git_not_installed_non_worktree_returns_unchanged(self):
        """If git not found and no worktree pattern, return original path."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = resolve_worktree_to_main_repo("/some/path")
            assert result == "/some/path"

    def test_empty_git_output_non_worktree_returns_unchanged(self):
        """If git returns empty output and no worktree pattern, return unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="\n"),
            ]
            result = resolve_worktree_to_main_repo("/some/path")
            assert result == "/some/path"

    def test_git_failure_with_worktree_path_uses_fallback(self):
        """If git fails but path has /.worktrees/, use pattern fallback."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            result = resolve_worktree_to_main_repo("/repo/.worktrees/feature")
            assert result == "/repo"

    def test_git_common_dir_failure_with_worktree_path_uses_fallback(self):
        """If second git call fails but path has /.worktrees/, use fallback."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/repo/.worktrees/feature\n"),
                subprocess.CalledProcessError(128, "git"),
            ]
            result = resolve_worktree_to_main_repo("/repo/.worktrees/feature")
            assert result == "/repo"

    def test_first_call_nonzero_non_worktree_returns_unchanged(self):
        """If --show-toplevel returns nonzero and no worktree pattern, return unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = resolve_worktree_to_main_repo("/some/path")
            assert result == "/some/path"

    def test_first_call_nonzero_with_worktree_path_uses_fallback(self):
        """If --show-toplevel returns nonzero but path has /.worktrees/, use fallback."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = resolve_worktree_to_main_repo("/repo/.worktrees/deleted-branch")
            assert result == "/repo"

    def test_deleted_worktree_resolves_via_fallback(self):
        """Deleted worktree directory (git fails) resolves via pattern."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("no such directory")
            result = resolve_worktree_to_main_repo("/home/user/myproject/.worktrees/feature-x")
            assert result == "/home/user/myproject"

    def test_worktrees_subdir_in_repo_resolves_via_fallback(self):
        """/.worktrees/ subdirectory inside a repo (not a real git worktree) uses pattern fallback."""
        with patch("memory_utils.subprocess.run") as mock_run:
            # Git succeeds — toplevel and common_dir parent match (not a real worktree)
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/home/user/project\n"),
                MagicMock(returncode=0, stdout="/home/user/project/.git\n"),
            ]
            result = resolve_worktree_to_main_repo("/home/user/project/.worktrees/feature")
            assert result == "/home/user/project"


# =============================================================================
# resolve_git_subdir_to_root Tests
# =============================================================================


class TestResolveGitSubdirToRoot:
    """Tests for resolve_git_subdir_to_root()."""

    def test_git_root_returns_unchanged(self):
        """When path IS the git root, return unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="/repo\n")
            result = resolve_git_subdir_to_root("/repo")
            assert result == "/repo"

    def test_non_ignored_subdir_collapses_to_root(self):
        """Subdir that is NOT gitignored should collapse to git root."""
        with patch("memory_utils.subprocess.run") as mock_run:
            # First call: rev-parse --show-toplevel -> /repo
            # Second call: check-ignore -q -> returncode 1 (not ignored)
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/repo\n"),
                MagicMock(returncode=1, stdout=""),
            ]
            result = resolve_git_subdir_to_root("/repo/src/mypackage")
            assert result == "/repo"

    def test_gitignored_subdir_stays_separate(self):
        """Subdir that IS gitignored should remain as a separate project."""
        with patch("memory_utils.subprocess.run") as mock_run:
            # First call: rev-parse --show-toplevel -> /repo
            # Second call: check-ignore -q -> returncode 0 (ignored)
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/repo\n"),
                MagicMock(returncode=0, stdout="vendor/lib\n"),
            ]
            result = resolve_git_subdir_to_root("/repo/vendor/lib")
            assert result == "/repo/vendor/lib"

    def test_not_in_git_repo_returns_unchanged(self):
        """Path not in a git repo (git returns nonzero) returns unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = resolve_git_subdir_to_root("/tmp/not-a-repo")
            assert result == "/tmp/not-a-repo"

    def test_git_not_installed_returns_unchanged(self):
        """FileNotFoundError when git is missing — return path unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = resolve_git_subdir_to_root("/some/path")
            assert result == "/some/path"

    def test_git_timeout_returns_unchanged(self):
        """subprocess.TimeoutExpired — return path unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(["git"], 5)
            result = resolve_git_subdir_to_root("/some/path")
            assert result == "/some/path"

    def test_empty_toplevel_returns_unchanged(self):
        """If git returns empty stdout, return path unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="\n")
            result = resolve_git_subdir_to_root("/some/path")
            assert result == "/some/path"

    def test_deeply_nested_subdir_collapses(self):
        """Deeply nested non-ignored subdirs should still collapse to git root."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/repo\n"),
                MagicMock(returncode=1, stdout=""),
            ]
            result = resolve_git_subdir_to_root("/repo/a/b/c/d/e")
            assert result == "/repo"

    def test_check_ignore_called_with_relative_path(self):
        """check-ignore must be called with the relative path from git root."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/repo\n"),
                MagicMock(returncode=1, stdout=""),
            ]
            resolve_git_subdir_to_root("/repo/src/pkg")
            # Second call must be check-ignore with relative path
            second_call_args = mock_run.call_args_list[1][0][0]
            assert "check-ignore" in second_call_args
            assert "-q" in second_call_args
            assert "src/pkg" in second_call_args

    def test_check_ignore_error_returns_unchanged(self):
        """Unexpected check-ignore exit code (not 0 or 1) returns unchanged."""
        with patch("memory_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="/repo\n"),
                MagicMock(returncode=2, stdout=""),
            ]
            result = resolve_git_subdir_to_root("/repo/src/pkg")
            assert result == "/repo/src/pkg"


# =============================================================================
# resolve_session_path Tests
# =============================================================================


class TestResolveSessionPath:
    """Tests for resolve_session_path()."""

    def test_worktree_resolves_then_git_subdir_noops(self):
        """Worktree resolves to main repo root; git-subdir step sees root and no-ops."""
        with patch("memory_utils.resolve_worktree_to_main_repo") as mock_worktree, \
             patch("memory_utils.resolve_git_subdir_to_root") as mock_subdir:
            mock_worktree.return_value = "/repo"
            mock_subdir.return_value = "/repo"
            result = resolve_session_path("/repo/.worktrees/feature")
            mock_worktree.assert_called_once_with("/repo/.worktrees/feature")
            mock_subdir.assert_called_once_with("/repo")
            assert result == "/repo"

    def test_non_worktree_subdir_goes_through_git_subdir_resolution(self):
        """Non-worktree path passes straight to git-subdir resolver."""
        with patch("memory_utils.resolve_worktree_to_main_repo") as mock_worktree, \
             patch("memory_utils.resolve_git_subdir_to_root") as mock_subdir:
            mock_worktree.return_value = "/repo/src"
            mock_subdir.return_value = "/repo"
            result = resolve_session_path("/repo/src")
            mock_worktree.assert_called_once_with("/repo/src")
            mock_subdir.assert_called_once_with("/repo/src")
            assert result == "/repo"

    def test_gitignored_subdir_passes_through_both_resolvers_unchanged(self):
        """Gitignored subdir: worktree no-ops, git-subdir no-ops too."""
        with patch("memory_utils.resolve_worktree_to_main_repo") as mock_worktree, \
             patch("memory_utils.resolve_git_subdir_to_root") as mock_subdir:
            mock_worktree.return_value = "/repo/vendor/lib"
            mock_subdir.return_value = "/repo/vendor/lib"
            result = resolve_session_path("/repo/vendor/lib")
            mock_worktree.assert_called_once_with("/repo/vendor/lib")
            mock_subdir.assert_called_once_with("/repo/vendor/lib")
            assert result == "/repo/vendor/lib"


class TestExtractEntryKeywordsMultiScope:
    """Verify keyword extraction handles multi-scope tags like [global|cartwheel/gotcha]."""

    def test_strips_multi_scope_tag(self):
        entry = "- [global|cartwheel/gotcha] Tailscale MTU black hole"
        keywords = extract_entry_keywords(entry)
        assert "tailscale" in keywords
        assert "global" not in keywords
        assert "cartwheel" not in keywords
        assert "gotcha" not in keywords

    def test_strips_single_scope_unchanged(self):
        entry = "- [cartwheel/implement] Built OAuth flow"
        keywords = extract_entry_keywords(entry)
        assert "oauth" in keywords
        assert "cartwheel" not in keywords

    def test_strips_multi_scope_with_routed_prefix(self):
        entry = "- [routed][global|cartwheel/pattern] Shared CI config"
        keywords = extract_entry_keywords(entry)
        assert "shared" in keywords
        assert "config" in keywords
        assert "routed" not in keywords
        assert "global" not in keywords
        assert "cartwheel" not in keywords

    def test_strips_multi_scope_with_date(self):
        entry = "- (2026-02-23) [global|cartwheel/gotcha] Tailscale MTU"
        keywords = extract_entry_keywords(entry)
        assert "tailscale" in keywords
        assert "2026" not in keywords
        assert "global" not in keywords


# =============================================================================
# resolve_project_path_to_name Tests
# =============================================================================


class TestResolveProjectPathToName:
    """Tests for the shared project path-to-name resolution utility."""

    def setup_method(self):
        _clear_projects_index_cache()

    def test_direct_path_lookup(self, tmp_path):
        index = {"projects": {
            "/home/user/myproject": {"name": "myproject", "encodedPaths": ["-home-user-myproject"]}
        }}
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            assert resolve_project_path_to_name("/home/user/myproject") == "myproject"

    def test_encoded_path_fallback(self, tmp_path):
        index = {"projects": {
            "/home/user/myproject": {"name": "myproject", "encodedPaths": ["-home-user-myproject"]}
        }}
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            assert resolve_project_path_to_name(None, project_hash="-home-user-myproject") == "myproject"

    def test_worktree_prefix_fallback(self, tmp_path):
        index = {"projects": {
            "/home/user/repo": {
                "name": "repo",
                "encodedPaths": ["-home-user-repo", "-home-user-repo--worktrees-branch-a"],
            }
        }}
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = resolve_project_path_to_name(None, project_hash="-home-user-repo--worktrees-branch-b")
        assert result == "repo"

    def test_none_when_both_args_none(self):
        assert resolve_project_path_to_name(None) is None

    def test_none_when_not_found(self, tmp_path):
        index = {"projects": {}}
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            assert resolve_project_path_to_name("/unknown/path") is None

    def test_path_takes_precedence_over_hash(self, tmp_path):
        index = {"projects": {
            "/alpha": {"name": "alpha", "encodedPaths": ["-alpha"]},
            "/beta": {"name": "beta", "encodedPaths": ["-beta"]},
        }}
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            assert resolve_project_path_to_name("/alpha", project_hash="-beta") == "alpha"

    def test_caching_avoids_repeated_reads(self, tmp_path):
        index = {"projects": {
            "/proj": {"name": "proj", "encodedPaths": []}
        }}
        with mock.patch("memory_utils.load_json_file", return_value=index) as mock_load, \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            resolve_project_path_to_name("/proj")
            resolve_project_path_to_name("/proj")
            # Should only load once thanks to caching
            mock_load.assert_called_once()

    def test_case_insensitive_path_lookup(self, tmp_path):
        """Path stored with different case still resolves."""
        index = {"projects": {
            "/Users/Nsitaram/MyProject": {"name": "myproject", "encodedPaths": ["-users-nsitaram-myproject"]}
        }}
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            assert resolve_project_path_to_name("/users/nsitaram/myproject") == "myproject"

    def test_non_worktree_hash_no_prefix_fallback(self, tmp_path):
        """Hash without --worktrees- does not trigger prefix matching."""
        index = {"projects": {
            "/home/user/myproject": {"name": "myproject", "encodedPaths": ["-home-user-myproject"]}
        }}
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = resolve_project_path_to_name(None, project_hash="-home-user-myproject-subfolder")
        assert result is None


# =============================================================================
# parse_markdown_sections Tests
# =============================================================================


class TestParseMarkdownSections:
    """Tests for the shared markdown section parser."""

    def test_basic_sections(self):
        content = "## Section 1\nContent 1\n## Section 2\nContent 2"
        sections = parse_markdown_sections(content)
        assert len(sections) == 2
        assert sections[0][0] == "## Section 1"
        assert sections[0][1] == ["Content 1"]
        assert sections[1][0] == "## Section 2"
        assert sections[1][1] == ["Content 2"]

    def test_preamble_before_sections(self):
        content = "# Title\nPreamble\n## Section 1\nContent"
        sections = parse_markdown_sections(content)
        assert len(sections) == 2
        assert sections[0][0] == ""
        assert "# Title" in sections[0][1]
        assert "Preamble" in sections[0][1]
        assert sections[1][0] == "## Section 1"

    def test_empty_content(self):
        sections = parse_markdown_sections("")
        assert len(sections) == 1
        assert sections[0][0] == ""
        assert sections[0][1] == [""]

    def test_multiline_section(self):
        content = "## Section\nLine 1\nLine 2\nLine 3"
        sections = parse_markdown_sections(content)
        assert len(sections) == 1
        assert sections[0][1] == ["Line 1", "Line 2", "Line 3"]

    def test_returns_lines_not_joined_string(self):
        """Verify output is list of lines, not joined string."""
        content = "## Sec\nA\nB"
        sections = parse_markdown_sections(content)
        assert isinstance(sections[0][1], list)

    def test_decay_compat_join(self):
        """Verify that joining lines matches decay.py's original output."""
        content = "## Section 1\nContent 1\n## Section 2\nContent 2"
        sections = parse_markdown_sections(content)
        joined = [(h, "\n".join(lines)) for h, lines in sections]
        assert joined[0] == ("## Section 1", "Content 1")
        assert joined[1] == ("## Section 2", "Content 2")

    def test_no_sections_only_content(self):
        content = "Just plain text\nwith no headers"
        sections = parse_markdown_sections(content)
        assert len(sections) == 1
        assert sections[0][0] == ""
        assert sections[0][1] == ["Just plain text", "with no headers"]




class TestGetPendingRecallDir:
    def test_returns_pending_recall_subdir(self):
        result = get_pending_recall_dir()
        assert result == get_memory_dir() / "pending-recall"
        assert result.name == "pending-recall"


class TestPreviousSessionRecallSetting:
    def test_default_settings_has_previous_session_recall(self):
        recall_settings = DEFAULT_SETTINGS["previousSessionRecall"]
        assert recall_settings["enabled"] is True
        assert recall_settings["tokenLimit"] == 1500

    def test_load_settings_merges_recall_override(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({
            "previousSessionRecall": {"tokenLimit": 1000}
        }))
        with patch("memory_utils.get_settings_file", return_value=settings_file):
            settings = load_settings()
        assert settings["previousSessionRecall"]["enabled"] is True
        assert settings["previousSessionRecall"]["tokenLimit"] == 1000

    def test_load_settings_recall_disabled(self, tmp_path):
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({
            "previousSessionRecall": {"enabled": False}
        }))
        with patch("memory_utils.get_settings_file", return_value=settings_file):
            settings = load_settings()
        assert settings["previousSessionRecall"]["enabled"] is False


# =============================================================================
# get_synthesis_stats_file Tests
# =============================================================================


class TestGetSynthesisStatsFile:
    """Tests for get_synthesis_stats_file()."""

    def test_returns_path_under_memory_dir(self):
        with patch("memory_utils.get_memory_dir", return_value=Path("/fake/memory")):
            result = get_synthesis_stats_file()
        assert result == Path("/fake/memory/.synthesis-stats.jsonl")

    def test_returns_path_type(self):
        result = get_synthesis_stats_file()
        assert isinstance(result, Path)

    def test_filename_is_dotfile(self):
        result = get_synthesis_stats_file()
        assert result.name.startswith(".")


# =============================================================================
# get_synthesis_log_file Tests
# =============================================================================


class TestGetSynthesisLogFile:
    """Tests for get_synthesis_log_file()."""

    def test_returns_path_on_darwin(self):
        with patch("memory_utils.sys.platform", "darwin"):
            result = get_synthesis_log_file()
        assert result is not None
        assert result.name == "synthesis.log"
        assert "Library/Logs/claude-memory" in str(result)

    def test_returns_none_on_linux(self):
        with patch("memory_utils.sys.platform", "linux"):
            result = get_synthesis_log_file()
        assert result is None

    def test_returns_none_on_windows(self):
        with patch("memory_utils.sys.platform", "win32"):
            result = get_synthesis_log_file()
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
