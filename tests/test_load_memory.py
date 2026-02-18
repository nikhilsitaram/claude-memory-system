#!/usr/bin/env python3
"""
Unit tests for load_memory.py

Run with: python -m pytest tests/test_load_memory.py -v
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from load_memory import (  # noqa: I001
    _build_synthesis_prompt,
    load_daily_summaries,
    load_global_memory,
    load_project_history,
    load_project_memory,
    should_synthesize,
)

# =============================================================================
# should_synthesize Tests
# =============================================================================


class TestShouldSynthesize:
    def _make_settings(self, interval_hours: int = 2) -> dict:
        return {"synthesis": {"intervalHours": interval_hours}}

    def test_true_when_no_file(self):
        """Returns True when .last-synthesis file doesn't exist."""
        with mock.patch("load_memory.get_last_synthesis_file") as mock_f:
            mock_f.return_value = Path("/nonexistent/.last-synthesis")
            assert should_synthesize(self._make_settings()) is True

    def test_true_on_new_day(self, tmp_path):
        """Returns True when last synthesis was on a different UTC day."""
        ts_file = tmp_path / ".last-synthesis"
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        ts_file.write_text(yesterday.isoformat())

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f:
            mock_f.return_value = ts_file
            assert should_synthesize(self._make_settings()) is True

    def test_false_within_interval(self, tmp_path):
        """Returns False when last synthesis is same day and within interval."""
        ts_file = tmp_path / ".last-synthesis"
        fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        thirty_min_ago = fixed_now - timedelta(minutes=30)
        ts_file.write_text(thirty_min_ago.isoformat())

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f, \
             mock.patch("load_memory.datetime") as mock_dt:
            mock_f.return_value = ts_file
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert should_synthesize(self._make_settings()) is False

    def test_true_after_interval(self, tmp_path):
        """Returns True when same day but past intervalHours."""
        ts_file = tmp_path / ".last-synthesis"
        fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        three_hours_ago = fixed_now - timedelta(hours=3)
        ts_file.write_text(three_hours_ago.isoformat())

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f, \
             mock.patch("load_memory.datetime") as mock_dt:
            mock_f.return_value = ts_file
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert should_synthesize(self._make_settings(interval_hours=2)) is True

    def test_true_on_invalid_file(self, tmp_path):
        """Returns True when file contains invalid content."""
        ts_file = tmp_path / ".last-synthesis"
        ts_file.write_text("not a valid timestamp")

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f:
            mock_f.return_value = ts_file
            assert should_synthesize(self._make_settings()) is True

    def test_respects_custom_interval(self, tmp_path):
        """Uses intervalHours from settings, not hardcoded default."""
        ts_file = tmp_path / ".last-synthesis"
        # Use fixed time to avoid UTC midnight edge case
        fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        three_hours_ago = fixed_now - timedelta(hours=3)
        ts_file.write_text(three_hours_ago.isoformat())

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f, \
             mock.patch("load_memory.datetime") as mock_dt:
            mock_f.return_value = ts_file
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert should_synthesize(self._make_settings(interval_hours=4)) is False


# =============================================================================
# load_global_memory Tests
# =============================================================================


class TestLoadGlobalMemory:
    def test_returns_content_when_exists(self, tmp_path):
        mem_file = tmp_path / "global-long-term-memory.md"
        mem_file.write_text("# Global Memory\nSome content here")

        with mock.patch("load_memory.get_global_memory_file") as mock_f:
            mock_f.return_value = mem_file
            content, size = load_global_memory()
            assert "Global Memory" in content
            assert size > 0

    def test_returns_empty_when_no_file(self):
        with mock.patch("load_memory.get_global_memory_file") as mock_f:
            mock_f.return_value = Path("/nonexistent/memory.md")
            content, size = load_global_memory()
            assert content == ""
            assert size == 0

    def test_returns_empty_on_io_error(self, tmp_path):
        mem_file = tmp_path / "memory.md"
        mem_file.write_text("content")
        # Make unreadable
        mem_file.chmod(0o000)

        with mock.patch("load_memory.get_global_memory_file") as mock_f:
            mock_f.return_value = mem_file
            content, size = load_global_memory()
            assert content == ""
            assert size == 0

        # Restore permissions for cleanup
        mem_file.chmod(0o644)


# =============================================================================
# load_project_memory Tests
# =============================================================================


class TestLoadProjectMemory:
    def test_returns_content(self, tmp_path):
        mem_file = tmp_path / "myproject-long-term-memory.md"
        mem_file.write_text("# myproject\nProject learnings")

        with mock.patch("load_memory.get_project_memory_dir") as mock_d:
            mock_d.return_value = tmp_path
            content, size = load_project_memory("myproject")
            assert "Project learnings" in content
            assert size > 0

    def test_returns_empty_when_missing(self, tmp_path):
        with mock.patch("load_memory.get_project_memory_dir") as mock_d:
            mock_d.return_value = tmp_path
            content, size = load_project_memory("nonexistent")
            assert content == ""
            assert size == 0

    def test_handles_special_chars_in_name(self, tmp_path):
        """Project names with special chars map to correct filenames."""
        # "My Project!" -> "my-project-long-term-memory.md"
        mem_file = tmp_path / "my-project-long-term-memory.md"
        mem_file.write_text("# My Project\nContent")

        with mock.patch("load_memory.get_project_memory_dir") as mock_d:
            mock_d.return_value = tmp_path
            content, size = load_project_memory("My Project!")
            assert "Content" in content


# =============================================================================
# load_daily_summaries Tests
# =============================================================================


SAMPLE_DAILY_GLOBAL = """# 2026-02-05
## Actions
- [global/implement] Set up new hooks
- [myproject/implement] Added feature X

## Learnings
- [global/pattern] Important global pattern
- [myproject/gotcha] Project-specific gotcha
"""

SAMPLE_DAILY_PROJECT = """# 2026-02-04
## Actions
- [global/document] Wrote docs
- [myproject/implement] Built the widget

## Learnings
- [myproject/pattern] Widget must be initialized first
"""


def _setup_daily_dir(tmp_path, include_global_only_day=False):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-02-05.md").write_text(SAMPLE_DAILY_GLOBAL)
    (daily_dir / "2026-02-04.md").write_text(SAMPLE_DAILY_PROJECT)
    if include_global_only_day:
        (daily_dir / "2026-02-03.md").write_text(
            "# 2026-02-03\n## Actions\n- [global/implement] Only global\n"
        )
    return daily_dir


class TestLoadDailySummaries:
    def test_global_scope_filtering(self, tmp_path):
        daily_dir = _setup_daily_dir(tmp_path)
        with mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory.get_working_days") as mock_wd:
            mock_dd.return_value = daily_dir
            mock_wd.return_value = ["2026-02-05", "2026-02-04"]

            summaries, total_bytes = load_daily_summaries(2, scope="global")
            all_content = " ".join(content for _, content in summaries)
            assert "[global/" in all_content
            assert "[myproject/" not in all_content

    def test_project_scope_filtering(self, tmp_path):
        daily_dir = _setup_daily_dir(tmp_path)
        with mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory.get_working_days") as mock_wd:
            mock_dd.return_value = daily_dir
            mock_wd.return_value = ["2026-02-05", "2026-02-04"]

            summaries, total_bytes = load_daily_summaries(2, scope="myproject")
            all_content = " ".join(content for _, content in summaries)
            assert "[myproject/" in all_content
            assert "[global/" not in all_content

    def test_respects_days_limit(self, tmp_path):
        """get_working_days already limits, so only those dates are loaded."""
        daily_dir = _setup_daily_dir(tmp_path)
        with mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory.get_working_days") as mock_wd:
            mock_dd.return_value = daily_dir
            mock_wd.return_value = ["2026-02-05"]  # Only 1 day

            summaries, _ = load_daily_summaries(1, scope="global")
            dates = [d for d, _ in summaries]
            assert "2026-02-04" not in dates

    def test_empty_when_no_matching_content(self, tmp_path):
        daily_dir = _setup_daily_dir(tmp_path)
        with mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory.get_working_days") as mock_wd:
            mock_dd.return_value = daily_dir
            mock_wd.return_value = ["2026-02-05"]

            summaries, total_bytes = load_daily_summaries(1, scope="other-project")
            assert summaries == []
            assert total_bytes == 0


# =============================================================================
# load_project_history Tests
# =============================================================================


class TestLoadProjectHistory:
    def test_loads_project_entries(self, tmp_path):
        daily_dir = _setup_daily_dir(tmp_path, include_global_only_day=True)
        with mock.patch("load_memory.get_daily_dir") as mock_dd:
            mock_dd.return_value = daily_dir
            project = {"name": "myproject"}
            summaries, total_bytes = load_project_history(project, days_limit=10)

            assert len(summaries) == 2  # Feb 4 and Feb 5 have myproject entries
            all_content = " ".join(content for _, content in summaries)
            assert "[myproject/" in all_content
            assert "[global/" not in all_content
            assert total_bytes > 0

    def test_oldest_first_ordering(self, tmp_path):
        """Output should be chronological (oldest first)."""
        daily_dir = _setup_daily_dir(tmp_path, include_global_only_day=True)
        with mock.patch("load_memory.get_daily_dir") as mock_dd:
            mock_dd.return_value = daily_dir
            project = {"name": "myproject"}
            summaries, _ = load_project_history(project, days_limit=10)
            dates = [d for d, _ in summaries]
            assert dates == sorted(dates)

    def test_respects_day_limit(self, tmp_path):
        daily_dir = _setup_daily_dir(tmp_path, include_global_only_day=True)
        with mock.patch("load_memory.get_daily_dir") as mock_dd:
            mock_dd.return_value = daily_dir
            project = {"name": "myproject"}
            summaries, _ = load_project_history(project, days_limit=1)
            assert len(summaries) == 1

    def test_empty_project_name(self):
        project = {"name": ""}
        summaries, total_bytes = load_project_history(project, days_limit=10)
        assert summaries == []
        assert total_bytes == 0


# =============================================================================
# Synthesis Prompt [routed] Marker Tests
# =============================================================================


class TestSynthesisPromptRoutedMarker:
    """Verify [routed] marking is handled by post-synthesis mark-routed script, not subagent."""

    def test_prompt_does_not_contain_routed_instruction(self):
        """Subagent should NOT be told to mark [routed] -- devtools.py mark-routed handles it."""
        prompt = _build_synthesis_prompt("", ["2026-02-01"])
        assert "Dedup marking" not in prompt

    def test_prompt_contains_mark_routed_in_cleanup(self):
        """Step 3 bash command should run mark-routed after synthesis."""
        prompt = _build_synthesis_prompt("", ["2026-02-01"])
        assert "devtools.py mark-routed" in prompt


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
