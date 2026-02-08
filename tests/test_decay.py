#!/usr/bin/env python3
"""
Unit tests for decay.py

Run with: python -m pytest tests/test_decay.py -v
"""

import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

# Add scripts directory to path
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from decay import (
    ARCHIVE_HEADER_PATTERN,
    AUTO_PINNED_SECTIONS,
    DATE_PATTERN,
    DECAY_ELIGIBLE_SECTIONS,
    append_to_archive,
    decay_file,
    is_decay_eligible,
    is_protected_section,
    parse_learning_date,
    parse_learnings,
    parse_sections,
    purge_old_archives,
)


# =============================================================================
# Date Parsing Tests
# =============================================================================


class TestParseLearningDate:
    def test_valid_date(self):
        line = "- (2026-01-15) [pattern] Some learning"
        result = parse_learning_date(line)
        assert result == date(2026, 1, 15)

    def test_no_date(self):
        line = "- [pattern] No date here"
        assert parse_learning_date(line) is None

    def test_invalid_date(self):
        line = "- (2026-13-45) [pattern] Invalid date"
        assert parse_learning_date(line) is None

    def test_date_in_middle(self):
        line = "- Some text (2026-06-15) more text"
        result = parse_learning_date(line)
        assert result == date(2026, 6, 15)


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


class TestSectionClassification:
    def test_protected_sections(self):
        assert is_protected_section("## About Me")
        assert is_protected_section("## Pinned")
        assert is_protected_section("## Current Projects")

    def test_non_protected(self):
        assert not is_protected_section("## Key Learnings")
        assert not is_protected_section("## Random")

    def test_decay_eligible(self):
        assert is_decay_eligible("## Key Actions")
        assert is_decay_eligible("## Key Decisions")
        assert is_decay_eligible("## Key Learnings")
        assert is_decay_eligible("## Key Lessons")

    def test_not_decay_eligible(self):
        assert not is_decay_eligible("## About Me")
        assert not is_decay_eligible("## Pinned")
        assert not is_decay_eligible("## Random Section")


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


class TestDecayFile:
    def _make_memory_file(self, tmpdir: str, content: str) -> Path:
        filepath = Path(tmpdir) / "test-memory.md"
        filepath.write_text(content)
        return filepath

    def test_decay_old_learning(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        content = f"""## Pinned
- Permanent item

## Key Learnings
<!-- Subject to decay -->
- ({old_date}) [pattern] Old learning that should be archived
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._make_memory_file(tmpdir, content)
            count, archived = decay_file(filepath, age_days=30)
            assert count == 1
            assert "Old learning" in archived[0]

            # Verify file was updated
            new_content = filepath.read_text()
            assert "Old learning" not in new_content
            # Pinned section preserved
            assert "Permanent item" in new_content

    def test_keep_recent_learning(self):
        recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        content = f"""## Key Learnings
- ({recent_date}) [pattern] Recent learning
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._make_memory_file(tmpdir, content)
            count, archived = decay_file(filepath, age_days=30)
            assert count == 0
            assert filepath.read_text() == content

    def test_pinned_section_protected(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        content = f"""## Pinned
- ({old_date}) [pattern] Old but pinned

## Key Learnings
- ({old_date}) [pattern] Old and eligible
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._make_memory_file(tmpdir, content)
            count, _ = decay_file(filepath, age_days=30)
            assert count == 1  # Only the Key Learnings entry
            new_content = filepath.read_text()
            assert "Old but pinned" in new_content

    def test_undated_learning_protected(self):
        content = """## Key Learnings
- [pattern] No date means no decay
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._make_memory_file(tmpdir, content)
            count, _ = decay_file(filepath, age_days=30)
            assert count == 0

    def test_dry_run_no_changes(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        content = f"""## Key Learnings
- ({old_date}) [pattern] Should be archived
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._make_memory_file(tmpdir, content)
            count, archived = decay_file(filepath, age_days=30, dry_run=True)
            assert count == 1
            # File should NOT be modified
            assert filepath.read_text() == content

    def test_nonexistent_file(self):
        count, archived = decay_file(Path("/nonexistent/file.md"), age_days=30)
        assert count == 0
        assert archived == []

    def test_multiple_sections_decayed(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
        content = f"""## Key Actions
- ({old_date}) [implement] Old action

## Key Learnings
- ({old_date}) [pattern] Old learning

## Key Lessons
- ({old_date}) [insight] Old lesson
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            filepath = self._make_memory_file(tmpdir, content)
            count, _ = decay_file(filepath, age_days=30)
            assert count == 3


# =============================================================================
# Archive Tests
# =============================================================================


class TestAppendToArchive:
    def test_creates_new_archive(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("decay.get_memory_dir") as mock_md:
                mock_md.return_value = Path(tmpdir)
                append_to_archive(["- (2026-01-01) [pattern] Test learning"])
                archive = Path(tmpdir) / ".decay-archive.md"
                assert archive.exists()
                content = archive.read_text()
                assert "Test learning" in content
                assert "# Decay Archive" in content

    def test_appends_to_existing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / ".decay-archive.md"
            archive.write_text("# Decay Archive\n\n## Archived 2026-01-01\nOld entry\n")

            with mock.patch("decay.get_memory_dir") as mock_md:
                mock_md.return_value = Path(tmpdir)
                append_to_archive(["- (2026-02-01) [pattern] New learning"])
                content = archive.read_text()
                assert "Old entry" in content
                assert "New learning" in content

    def test_dry_run_no_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("decay.get_memory_dir") as mock_md:
                mock_md.return_value = Path(tmpdir)
                append_to_archive(["- test"], dry_run=True)
                assert not (Path(tmpdir) / ".decay-archive.md").exists()


class TestPurgeOldArchives:
    def test_purge_old_sections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            archive = Path(tmpdir) / ".decay-archive.md"
            # Create archive with old and recent sections
            old_date = (datetime.now(timezone.utc) - timedelta(days=400)).strftime(
                "%Y-%m-%d"
            )
            recent_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            archive.write_text(
                f"# Decay Archive\n\n"
                f"## Archived {recent_date}\nRecent entry\n\n"
                f"## Archived {old_date}\nOld entry\n"
            )

            with mock.patch("decay.get_memory_dir") as mock_md:
                mock_md.return_value = Path(tmpdir)
                purged = purge_old_archives(retention_days=365)
                assert purged == 1
                content = archive.read_text()
                assert "Recent entry" in content
                assert "Old entry" not in content

    def test_no_archive_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch("decay.get_memory_dir") as mock_md:
                mock_md.return_value = Path(tmpdir)
                purged = purge_old_archives(retention_days=365)
                assert purged == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
