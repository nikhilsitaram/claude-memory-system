#!/usr/bin/env python3
"""
Unit tests for decay.py

Run with: python -m pytest tests/test_decay.py -v
"""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from decay import (  # noqa: I001
    ARCHIVE_HEADER_PATTERN,
    DATE_PATTERN,
    DEFAULT_AGE_DAYS,
    DEFAULT_ARCHIVE_RETENTION_DAYS,
    DEFAULT_PROJECT_WORKING_DAYS,
    append_to_archive,
    build_project_work_days_map,
    decay_file,
    is_decay_eligible,
    is_protected_section,
    parse_learning_date,
    parse_learnings,
    parse_sections,
    purge_old_archives,
    should_decay_entry,
)

# =============================================================================
# Date Parsing Tests
# =============================================================================


@pytest.mark.parametrize("line,expected", [
    ("- (2026-01-15) [pattern] Some learning", date(2026, 1, 15)),
    ("- [pattern] No date here", None),
    ("- (2026-13-45) [pattern] Invalid date", None),
    ("- Some text (2026-06-15) more text", date(2026, 6, 15)),
])
def test_parse_learning_date(line, expected):
    assert parse_learning_date(line) == expected


class TestDatePattern:
    def test_matches_standard_format(self):
        match = DATE_PATTERN.search("(2026-01-15)")
        assert match is not None
        assert match.group(1) == "2026-01-15"

    def test_no_match_without_parens(self):
        match = DATE_PATTERN.search("2026-01-15")
        assert match is None


class TestArchiveHeaderPattern:
    def test_matches_archive_header(self):
        match = ARCHIVE_HEADER_PATTERN.match("## Archived 2026-01-15")
        assert match is not None
        assert match.group(1) == "2026-01-15"

    def test_no_match_regular_header(self):
        match = ARCHIVE_HEADER_PATTERN.match("## Key Learnings")
        assert match is None


# =============================================================================
# Section Classification Tests
# =============================================================================


@pytest.mark.parametrize("section", [
    "## About Me", "## Pinned", "## Current Projects",
])
def test_protected_sections(section):
    assert is_protected_section(section)


@pytest.mark.parametrize("section", ["## Key Learnings", "## Random"])
def test_non_protected(section):
    assert not is_protected_section(section)


@pytest.mark.parametrize("section", [
    "## Key Actions", "## Key Decisions", "## Key Learnings", "## Key Lessons",
])
def test_decay_eligible(section):
    assert is_decay_eligible(section)


@pytest.mark.parametrize("section", [
    "## About Me", "## Pinned", "## Random Section",
])
def test_not_decay_eligible(section):
    assert not is_decay_eligible(section)


# =============================================================================
# Section Parsing Tests
# =============================================================================


class TestParseSections:
    def test_basic_sections(self):
        content = "## Section 1\nContent 1\n## Section 2\nContent 2"
        sections = parse_sections(content)
        assert len(sections) == 2
        assert sections[0][0] == "## Section 1"
        assert "Content 1" in sections[0][1]

    def test_preamble_before_sections(self):
        content = "# Title\nPreamble\n## Section 1\nContent"
        sections = parse_sections(content)
        assert len(sections) == 2
        # First section is the preamble (no header)
        assert sections[0][0] == ""
        assert "# Title" in sections[0][1]

    def test_empty_content(self):
        sections = parse_sections("")
        assert len(sections) == 1
        assert sections[0][0] == ""

    def test_multiline_section(self):
        content = "## Section\nLine 1\nLine 2\nLine 3"
        sections = parse_sections(content)
        assert len(sections) == 1
        assert "Line 1" in sections[0][1]
        assert "Line 3" in sections[0][1]


class TestParseLearnings:
    def test_basic_learnings(self):
        content = "- (2026-01-01) [pattern] First\n- (2026-01-02) [gotcha] Second"
        learnings = parse_learnings(content)
        assert len(learnings) == 2
        assert learnings[0][1] == date(2026, 1, 1)

    def test_no_date_learning(self):
        content = "- [pattern] Undated learning"
        learnings = parse_learnings(content)
        assert len(learnings) == 1
        assert learnings[0][1] is None

    def test_non_list_lines_ignored(self):
        content = "Some text\n<!-- comment -->\n- (2026-01-01) [tip] Real learning"
        learnings = parse_learnings(content)
        assert len(learnings) == 1

    def test_empty_content(self):
        assert parse_learnings("") == []


# =============================================================================
# Decay File Tests
# =============================================================================


# =============================================================================
# Should Decay Entry Tests
# =============================================================================


class TestShouldDecayEntry:
    """Test the should_decay_entry function for both calendar and working-day modes."""

    def test_calendar_decay_old_entry(self):
        """Entry older than age_days should decay."""
        today = date(2026, 2, 12)
        learning_date = date(2026, 1, 1)  # 42 days ago
        assert should_decay_entry(learning_date, age_days=DEFAULT_AGE_DAYS, today=today) is True

    def test_calendar_decay_recent_entry(self):
        """Entry newer than age_days should not decay."""
        today = date(2026, 2, 12)
        learning_date = date(2026, 2, 1)  # 11 days ago
        assert should_decay_entry(learning_date, age_days=DEFAULT_AGE_DAYS, today=today) is False

    def test_calendar_decay_exact_boundary(self):
        """Entry exactly at age_days boundary should not decay (>=, not >)."""
        today = date(2026, 2, 12)
        learning_date = date(2026, 1, 13)  # exactly 30 days ago
        assert should_decay_entry(learning_date, age_days=DEFAULT_AGE_DAYS, today=today) is False

    def test_working_day_decay_enough_days(self):
        """Entry with >= threshold work days after it should decay."""
        learning_date = date(2026, 1, 1)
        # More work days than threshold after Jan 1
        work_days = [f"2026-01-{d:02d}" for d in range(2, 2 + DEFAULT_PROJECT_WORKING_DAYS + 5)]
        assert should_decay_entry(
            learning_date, age_days=DEFAULT_AGE_DAYS, today=date(2026, 2, 12),
            project_work_days=work_days, project_decay_threshold=DEFAULT_PROJECT_WORKING_DAYS,
        ) is True

    def test_working_day_decay_not_enough_days(self):
        """Entry with fewer than threshold work days should not decay."""
        learning_date = date(2026, 1, 1)
        # Only 5 work days after Jan 1
        work_days = ["2026-01-05", "2026-01-10", "2026-01-15", "2026-01-20", "2026-01-25"]
        assert should_decay_entry(
            learning_date, age_days=DEFAULT_AGE_DAYS, today=date(2026, 2, 12),
            project_work_days=work_days, project_decay_threshold=DEFAULT_PROJECT_WORKING_DAYS,
        ) is False

    def test_working_day_decay_ignores_calendar_age(self):
        """Even a very old entry survives if not enough work days occurred."""
        learning_date = date(2025, 6, 1)  # 8+ months ago
        # Only 3 work days total after that
        work_days = ["2025-06-15", "2025-09-01", "2026-01-15"]
        assert should_decay_entry(
            learning_date, age_days=DEFAULT_AGE_DAYS, today=date(2026, 2, 12),
            project_work_days=work_days, project_decay_threshold=DEFAULT_PROJECT_WORKING_DAYS,
        ) is False

    def test_working_day_decay_only_counts_after_entry(self):
        """Work days before the learning date don't count."""
        learning_date = date(2026, 1, 15)
        work_days = [
            # 10 days before entry (don't count)
            "2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05",
            "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-10",
            # 5 days after entry (do count)
            "2026-01-20", "2026-01-25", "2026-01-30", "2026-02-05", "2026-02-10",
        ]
        assert should_decay_entry(
            learning_date, age_days=DEFAULT_AGE_DAYS, today=date(2026, 2, 12),
            project_work_days=work_days, project_decay_threshold=DEFAULT_PROJECT_WORKING_DAYS,
        ) is False

    def test_working_day_decay_exact_threshold(self):
        """Exactly threshold work days should trigger decay (>=)."""
        learning_date = date(2026, 1, 1)
        work_days = [f"2026-01-{d:02d}" for d in range(2, 2 + DEFAULT_PROJECT_WORKING_DAYS)]
        assert should_decay_entry(
            learning_date, age_days=DEFAULT_AGE_DAYS, today=date(2026, 2, 12),
            project_work_days=work_days, project_decay_threshold=DEFAULT_PROJECT_WORKING_DAYS,
        ) is True

    def test_working_day_same_day_not_counted(self):
        """Work day on same date as learning should not count as 'after'."""
        learning_date = date(2026, 1, 15)
        work_days = ["2026-01-15", "2026-01-20"]  # same day + 1 after
        assert should_decay_entry(
            learning_date, age_days=DEFAULT_AGE_DAYS, today=date(2026, 2, 12),
            project_work_days=work_days, project_decay_threshold=2,
        ) is False


# =============================================================================
# Build Project Work Days Map Tests
# =============================================================================


class TestBuildProjectWorkDaysMap:
    def test_builds_mapping(self):
        """Should map LTM filenames to work days lists."""
        index = {
            "projects": {
                "/path/to/project": {
                    "name": "my-project",
                    "workDays": ["2026-01-01", "2026-01-05"],
                },
            }
        }
        with mock.patch("decay.load_json_file", return_value=index):
            with mock.patch("decay.get_projects_index_file"):
                mapping = build_project_work_days_map()
                assert "my-project-long-term-memory.md" in mapping
                assert mapping["my-project-long-term-memory.md"] == ["2026-01-01", "2026-01-05"]

    def test_empty_index(self):
        """Empty index returns empty mapping."""
        with mock.patch("decay.load_json_file", return_value={}):
            with mock.patch("decay.get_projects_index_file"):
                mapping = build_project_work_days_map()
                assert mapping == {}

    def test_case_normalization(self):
        """Project names are lowercased in filenames."""
        index = {
            "projects": {
                "/path": {
                    "name": "1099-Report",
                    "workDays": ["2026-01-01"],
                }
            }
        }
        with mock.patch("decay.load_json_file", return_value=index):
            with mock.patch("decay.get_projects_index_file"):
                mapping = build_project_work_days_map()
                assert "1099-report-long-term-memory.md" in mapping

    def test_sorts_work_days(self):
        """Work days should be sorted in output."""
        index = {
            "projects": {
                "/path": {
                    "name": "test",
                    "workDays": ["2026-02-01", "2026-01-01", "2026-01-15"],
                }
            }
        }
        with mock.patch("decay.load_json_file", return_value=index):
            with mock.patch("decay.get_projects_index_file"):
                mapping = build_project_work_days_map()
                assert mapping["test-long-term-memory.md"] == [
                    "2026-01-01", "2026-01-15", "2026-02-01"
                ]


# =============================================================================
# Decay File Tests
# =============================================================================


class TestDecayFile:
    def _make_memory_file(self, tmp_path, content):
        filepath = tmp_path / "test-memory.md"
        filepath.write_text(content)
        return filepath

    def test_decay_old_learning(self, tmp_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_AGE_DAYS * 2)).strftime("%Y-%m-%d")
        content = f"""## Pinned
- Permanent item

## Key Learnings
<!-- Subject to decay -->
- ({old_date}) [pattern] Old learning that should be archived
"""
        filepath = self._make_memory_file(tmp_path, content)
        count, archived = decay_file(filepath, age_days=DEFAULT_AGE_DAYS)
        assert count == 1
        assert "Old learning" in archived[0]

        # Verify file was updated
        new_content = filepath.read_text()
        assert "Old learning" not in new_content
        # Pinned section preserved
        assert "Permanent item" in new_content

    def test_keep_recent_learning(self, tmp_path):
        recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        content = f"""## Key Learnings
- ({recent_date}) [pattern] Recent learning
"""
        filepath = self._make_memory_file(tmp_path, content)
        count, archived = decay_file(filepath, age_days=DEFAULT_AGE_DAYS)
        assert count == 0
        assert filepath.read_text() == content

    def test_pinned_section_protected(self, tmp_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_AGE_DAYS * 2)).strftime("%Y-%m-%d")
        content = f"""## Pinned
- ({old_date}) [pattern] Old but pinned

## Key Learnings
- ({old_date}) [pattern] Old and eligible
"""
        filepath = self._make_memory_file(tmp_path, content)
        count, _ = decay_file(filepath, age_days=DEFAULT_AGE_DAYS)
        assert count == 1  # Only the Key Learnings entry
        new_content = filepath.read_text()
        assert "Old but pinned" in new_content

    def test_undated_learning_protected(self, tmp_path):
        content = """## Key Learnings
- [pattern] No date means no decay
"""
        filepath = self._make_memory_file(tmp_path, content)
        count, _ = decay_file(filepath, age_days=DEFAULT_AGE_DAYS)
        assert count == 0

    def test_dry_run_no_changes(self, tmp_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_AGE_DAYS * 2)).strftime("%Y-%m-%d")
        content = f"""## Key Learnings
- ({old_date}) [pattern] Should be archived
"""
        filepath = self._make_memory_file(tmp_path, content)
        count, archived = decay_file(filepath, age_days=DEFAULT_AGE_DAYS, dry_run=True)
        assert count == 1
        # File should NOT be modified
        assert filepath.read_text() == content

    def test_nonexistent_file(self):
        count, archived = decay_file(Path("/nonexistent/file.md"), age_days=DEFAULT_AGE_DAYS)
        assert count == 0
        assert archived == []

    def test_multiple_sections_decayed(self, tmp_path):
        old_date = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_AGE_DAYS * 2)).strftime("%Y-%m-%d")
        content = f"""## Key Actions
- ({old_date}) [implement] Old action

## Key Learnings
- ({old_date}) [pattern] Old learning

## Key Lessons
- ({old_date}) [insight] Old lesson
"""
        filepath = self._make_memory_file(tmp_path, content)
        count, _ = decay_file(filepath, age_days=DEFAULT_AGE_DAYS)
        assert count == 3

    def test_working_day_decay_keeps_entry_with_few_days(self, tmp_path):
        """Project file with few work days should keep old entries."""
        content = """## Key Learnings
<!-- Subject to decay -->
- (2025-06-01) [pattern] Old but few project work days
"""
        work_days = ["2025-07-01", "2025-10-01", "2026-01-15"]  # only 3 days after
        filepath = self._make_memory_file(tmp_path, content)
        count, _ = decay_file(
            filepath, age_days=DEFAULT_AGE_DAYS,
            project_work_days=work_days,
            project_decay_threshold=DEFAULT_PROJECT_WORKING_DAYS,
        )
        assert count == 0

    def test_working_day_decay_archives_entry_with_many_days(self, tmp_path):
        """Project file with enough work days should archive old entries."""
        content = """## Key Learnings
<!-- Subject to decay -->
- (2026-01-01) [pattern] Should be archived
"""
        work_days = [f"2026-01-{d:02d}" for d in range(2, 2 + DEFAULT_PROJECT_WORKING_DAYS + 3)]
        filepath = self._make_memory_file(tmp_path, content)
        count, archived = decay_file(
            filepath, age_days=DEFAULT_AGE_DAYS,
            project_work_days=work_days,
            project_decay_threshold=DEFAULT_PROJECT_WORKING_DAYS,
        )
        assert count == 1
        assert "Should be archived" in archived[0]


# =============================================================================
# Archive Tests
# =============================================================================


class TestAppendToArchive:
    def test_creates_new_archive(self, tmp_path):
        with mock.patch("decay.get_memory_dir") as mock_md:
            mock_md.return_value = tmp_path
            append_to_archive(["- (2026-01-01) [pattern] Test learning"])
            archive = tmp_path / ".decay-archive.md"
            assert archive.exists()
            content = archive.read_text()
            assert "Test learning" in content
            assert "# Decay Archive" in content

    def test_appends_to_existing(self, tmp_path):
        archive = tmp_path / ".decay-archive.md"
        archive.write_text("# Decay Archive\n\n## Archived 2026-01-01\nOld entry\n")

        with mock.patch("decay.get_memory_dir") as mock_md:
            mock_md.return_value = tmp_path
            append_to_archive(["- (2026-02-01) [pattern] New learning"])
            content = archive.read_text()
            assert "Old entry" in content
            assert "New learning" in content

    def test_dry_run_no_file(self, tmp_path):
        with mock.patch("decay.get_memory_dir") as mock_md:
            mock_md.return_value = tmp_path
            append_to_archive(["- test"], dry_run=True)
            assert not (tmp_path / ".decay-archive.md").exists()


class TestPurgeOldArchives:
    def test_purge_old_sections(self, tmp_path):
        archive = tmp_path / ".decay-archive.md"
        # Create archive with old and recent sections
        old_date = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_ARCHIVE_RETENTION_DAYS + 35)).strftime(
            "%Y-%m-%d"
        )
        recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        archive.write_text(
            f"# Decay Archive\n\n"
            f"## Archived {recent_date}\nRecent entry\n\n"
            f"## Archived {old_date}\nOld entry\n"
        )

        with mock.patch("decay.get_memory_dir") as mock_md:
            mock_md.return_value = tmp_path
            purged = purge_old_archives(retention_days=DEFAULT_ARCHIVE_RETENTION_DAYS)
            assert purged == 1
            content = archive.read_text()
            assert "Recent entry" in content
            assert "Old entry" not in content

    def test_no_archive_file(self, tmp_path):
        with mock.patch("decay.get_memory_dir") as mock_md:
            mock_md.return_value = tmp_path
            purged = purge_old_archives(retention_days=DEFAULT_ARCHIVE_RETENTION_DAYS)
            assert purged == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
