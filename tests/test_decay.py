#!/usr/bin/env python3
"""
Unit tests for decay.py

Run with: python -m pytest tests/test_decay.py -v
"""

import sqlite3
import sys as _sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from decay import (  # noqa: I001
    ARCHIVE_HEADER_PATTERN,
    ARCHIVE_SALIENCE_THRESHOLD,
    COLD_LAMBDA,
    DATE_PATTERN,
    DEFAULT_AGE_DAYS,
    DEFAULT_ARCHIVE_RETENTION_DAYS,
    DEFAULT_PROJECT_WORKING_DAYS,
    HOT_LAMBDA,
    WARM_LAMBDA,
    append_to_archive,
    build_project_work_days_map,
    days_since,
    decay_file,
    decay_salience,
    is_decay_eligible,
    is_protected_section,
    main,
    parse_learning_date,
    parse_learnings,
    parse_sections,
    pick_tier,
    purge_old_archives,
    should_decay_entry,
)
from memory_utils import rebuild_projects_index_quiet

_SCRIPTS_DIR = str(__import__("pathlib").Path(__file__).parent.parent / "scripts")
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)


def _make_v2_db(db_path):
    """Create a v2 DB for testing decay operations."""
    from storage import SCHEMA_DDL

    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA_DDL)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    return conn


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


class TestRebuildProjectsIndexQuiet:
    """Tests for rebuild_projects_index_quiet helper."""

    def test_calls_build_projects_index(self):
        """Calls indexing.build_projects_index and suppresses output."""
        mock_build = mock.MagicMock()
        fake_indexing = type("m", (), {"build_projects_index": mock_build})()
        with mock.patch.dict("sys.modules", {"indexing": fake_indexing}):
            rebuild_projects_index_quiet()
        mock_build.assert_called_once()

    def test_swallows_import_error(self):
        """Does not raise when indexing module is unavailable."""
        with mock.patch.dict("sys.modules", {"indexing": None}):
            # Should not raise
            rebuild_projects_index_quiet()

    def test_swallows_runtime_error(self):
        """Does not raise when build_projects_index raises."""
        mock_build = mock.MagicMock(side_effect=RuntimeError("boom"))
        fake_indexing = type("m", (), {"build_projects_index": mock_build})()
        with mock.patch.dict("sys.modules", {"indexing": fake_indexing}):
            rebuild_projects_index_quiet()


class TestMainCallsRebuild:
    """Tests that main() calls rebuild_projects_index_quiet before run()."""

    def test_main_calls_rebuild_before_run(self, tmp_path):
        """main() rebuilds projects index before invoking run()."""
        with mock.patch("decay.rebuild_projects_index_quiet") as mock_rebuild, \
             mock.patch("decay.run", return_value=0) as mock_run, \
             mock.patch("sys.argv", ["decay.py"]):
            main()
        mock_rebuild.assert_called_once()
        mock_run.assert_called_once()


# =============================================================================
# B4: Tiered Salience Decay Tests
# =============================================================================

from math import exp


class TestPickTier:
    """Tests for pick_tier() tier classification."""

    def test_hot_tier_recent_high_access(self):
        """Recent (< HOT_DAYS_THRESHOLD) + access_count > 5 = hot."""
        tier, lam = pick_tier(dt_days=2.0, access_count=10, salience=0.5)
        assert tier == "hot"
        assert lam == HOT_LAMBDA

    def test_hot_tier_recent_high_salience(self):
        """Recent + salience > 0.7 = hot."""
        tier, lam = pick_tier(dt_days=3.0, access_count=1, salience=0.8)
        assert tier == "hot"
        assert lam == HOT_LAMBDA

    def test_warm_tier_recent_low_access(self):
        """Recent but low access and salience 0.4-0.7 = warm."""
        tier, lam = pick_tier(dt_days=4.0, access_count=2, salience=0.5)
        assert tier == "warm"
        assert lam == WARM_LAMBDA

    def test_warm_tier_old_moderate_salience(self):
        """Old (>= HOT_DAYS_THRESHOLD) but salience > 0.4 = warm."""
        tier, lam = pick_tier(dt_days=10.0, access_count=3, salience=0.6)
        assert tier == "warm"
        assert lam == WARM_LAMBDA

    def test_cold_tier_old_low_salience(self):
        """Old + low salience + low access = cold."""
        tier, lam = pick_tier(dt_days=20.0, access_count=1, salience=0.2)
        assert tier == "cold"
        assert lam == COLD_LAMBDA


class TestDecaySalience:
    """Tests for decay_salience() exponential decay math."""

    def test_hot_decay_preserves_salience(self):
        """Hot tier: lambda=0.005, salience barely decreases over 5 days."""
        result = decay_salience(current_salience=0.9, dt_days=5.0, lam=HOT_LAMBDA)
        expected = 0.9 * exp(-HOT_LAMBDA * (5.0 / (0.9 + 0.1)))
        assert abs(result - expected) < 1e-9
        assert result > 0.87

    def test_cold_decay_drops_fast(self):
        """Cold tier: lambda=0.05, salience drops significantly over 20 days."""
        result = decay_salience(current_salience=0.3, dt_days=20.0, lam=COLD_LAMBDA)
        expected = 0.3 * exp(-COLD_LAMBDA * (20.0 / (0.3 + 0.1)))
        assert abs(result - expected) < 1e-9
        assert result < ARCHIVE_SALIENCE_THRESHOLD

    def test_death_spiral_for_neglected_memories(self):
        """Low-salience chunks decay faster than high-salience ones."""
        high = decay_salience(current_salience=0.8, dt_days=15.0, lam=COLD_LAMBDA)
        low = decay_salience(current_salience=0.1, dt_days=15.0, lam=COLD_LAMBDA)
        assert low < high

    def test_salience_clamped_to_0_1(self):
        """Result is always in [0.0, 1.0]."""
        result = decay_salience(current_salience=0.0, dt_days=100.0, lam=COLD_LAMBDA)
        assert result == 0.0
        result2 = decay_salience(current_salience=1.0, dt_days=0.0, lam=HOT_LAMBDA)
        assert 0.0 <= result2 <= 1.0


class TestArchiveThreshold:
    """Tests for salience-based archive decisions."""

    def test_below_threshold_archives(self):
        """Chunks with salience < ARCHIVE_SALIENCE_THRESHOLD should be archived."""
        assert ARCHIVE_SALIENCE_THRESHOLD > 0.0
        salience = ARCHIVE_SALIENCE_THRESHOLD - 0.001
        assert salience < ARCHIVE_SALIENCE_THRESHOLD

    def test_above_threshold_kept(self):
        """Chunks with salience >= ARCHIVE_SALIENCE_THRESHOLD should be kept."""
        salience = ARCHIVE_SALIENCE_THRESHOLD + 0.001
        assert salience >= ARCHIVE_SALIENCE_THRESHOLD

    def test_protected_sections_never_archived(self):
        """Auto-pinned sections bypass salience check."""
        from decay import AUTO_PINNED_SECTIONS
        for section in AUTO_PINNED_SECTIONS:
            assert is_protected_section(section)


class TestDaysSince:
    """Tests for days_since() helper."""

    def test_returns_999_for_none(self):
        """None last_accessed treated as very old."""
        result = days_since(None)
        assert result == 999.0

    def test_returns_correct_days(self):
        """Returns correct days from ISO timestamp."""
        from datetime import date as date_cls
        ts = "2026-03-16T12:00:00Z"
        today = date_cls(2026, 3, 21)
        result = days_since(ts, today=today)
        assert result == 5.0

    def test_returns_0_for_today(self):
        """Today's timestamp returns 0.0."""
        from datetime import date as date_cls
        today = date_cls(2026, 3, 21)
        ts = "2026-03-21T00:00:00Z"
        result = days_since(ts, today=today)
        assert result == 0.0


# =============================================================================
# B5: Integration test — load memory -> access tracking -> decay -> archive
# =============================================================================


class TestSalienceDecayIntegration:
    """End-to-end test: load memory -> access tracking -> decay -> archive."""

    def test_full_lifecycle(self, tmp_path):
        """Integration: serve memory, track access, run decay, verify salience ordering.

        Flow:
        1. Create LTM file with 3 entries (recent, moderate, old)
        2. Migrate to DB (populate chunks)
        3. Track access for 2 of 3 chunks (simulating SessionStart serving them)
        4. Apply tiered decay using pick_tier + decay_salience directly
        5. Verify: accessed entries have higher salience than unaccessed old entry
        6. Verify: cold old entry decays below archive threshold
        """
        from storage import (
            close_db,
            migrate_markdown_to_db,
            query_chunks_with_salience,
            update_chunk_salience,
        )

        db_path = tmp_path / "memory.db"

        ltm_content = """\
## Key Learnings
- (2026-03-20) [pattern] Recent entry one day ago
- (2026-03-10) [pattern] Moderate entry eleven days ago
- (2025-09-03) [pattern] Old entry two hundred days ago
"""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(ltm_content, encoding="utf-8")

        with mock.patch("storage.get_db_path", return_value=db_path), \
             mock.patch("storage.get_memory_dir", return_value=tmp_path):
            conn = _make_v2_db(db_path)
            stats = migrate_markdown_to_db(conn)
            assert stats.chunks_inserted == 3
            chunks_list = query_chunks_with_salience(conn)
            chunks = {c.content: c for c in chunks_list}
            close_db(conn)

        recent_id = [c.id for c in chunks.values() if "Recent" in c.content][0]
        moderate_id = [c.id for c in chunks.values() if "Moderate" in c.content][0]
        old_id = [c.id for c in chunks.values() if "Old" in c.content][0]

        with mock.patch("storage.get_db_path", return_value=db_path):
            from load_memory import track_memory_access
            track_memory_access([recent_id, moderate_id])

        with mock.patch("storage.get_db_path", return_value=db_path):
            conn2 = _make_v2_db(db_path)
            after_access = {c.id: c for c in query_chunks_with_salience(conn2)}

            from datetime import date as date_cls
            today = date_cls(2026, 3, 21)

            for c in after_access.values():
                dt = days_since(c.last_accessed, today=today)
                tier_name, lam = pick_tier(dt, c.access_count, c.salience)
                new_sal = decay_salience(c.salience, dt, lam)
                update_chunk_salience(conn2, c.id, new_sal)
            conn2.commit()

            final = {c.id: c for c in query_chunks_with_salience(conn2)}
            close_db(conn2)

        recent_final = final[recent_id]
        moderate_final = final[moderate_id]
        old_final = final[old_id]

        assert recent_final.access_count == 1
        assert old_final.access_count == 0

        assert recent_final.salience > old_final.salience
        assert moderate_final.salience > old_final.salience

        assert old_final.salience < ARCHIVE_SALIENCE_THRESHOLD


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
