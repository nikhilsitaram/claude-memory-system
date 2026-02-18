#!/usr/bin/env python3
"""
Unit tests for devtools.py — keyword dedup and validate-ltm.

Run with: python -m pytest tests/test_devtools.py -v
"""

import argparse
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Add scripts directory to path
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from memory_utils import (  # noqa: E402
    extract_entry_keywords,
    is_routed_match,
)

# ── Keyword dedup: real-world near-duplicates from the audit ──


class TestKeywordDedupRealWorldCases:
    """Test cases derived from actual duplicates found in global LTM audit."""

    def test_happy_mcp_duplicate_different_backtick_style(self):
        """Lines 82 and 90 were near-identical, differing only in backtick usage."""
        entry1 = "- (2026-02-15) [gotcha] Happy's MCP tools only available when launched via `happy` CLI — sessions started directly with `claude` don't have access to mcp__happy__change_title or mobile interface tools"
        entry2 = "- (2026-02-15) [gotcha] Happy's MCP tools only available when launched via `happy` CLI — sessions started directly with `claude` don't have access to `mcp__happy__change_title` or mobile interface tools"
        assert is_routed_match(entry1, entry2, threshold=0.6) is True

    def test_worktree_cwd_mismatch_triple_duplicate(self):
        """Lines 84, 94, 104 — same concept, different path examples."""
        entry1 = "- (2026-02-15) [gotcha] Running commands from wrong directory in worktree setups causes script/output mismatch — CWD was main repo but edits were in `.worktrees/`, so script used main's version instead of worktree's"
        entry2 = "- (2026-02-15) [gotcha] Running commands from wrong directory in worktree setups causes script/output mismatch — CWD was main repo but edits were in worktree, so script used main's version instead of worktree's"
        entry3 = "- (2026-02-15) [gotcha] Running commands from wrong directory in worktree setups causes script/output mismatch — CWD was main repo but edits were in `.worktrees/html-improvements/`, so `python3 skills/analyze/scripts/render_report.py` used main's version instead of worktree's"
        assert is_routed_match(entry1, entry2, threshold=0.6) is True
        assert is_routed_match(entry1, entry3, threshold=0.6) is True

    def test_session_restart_overlapping(self):
        """Lines 93 and 98 — overlapping 'restart required' learnings.

        These share the concept but vocabulary differs enough that keyword
        matching at 0.5 threshold doesn't catch it. This is a known
        limitation — the entries share ~6 keywords out of ~13, ratio ~0.46.
        At 0.4 threshold they match, demonstrating the concept overlap exists.
        """
        entry1 = "- (2026-02-14) [gotcha] Hook changes (pretooluse-allow-safe.sh, settings.json permissions) require session restart or `/clear` to take effect"
        entry2 = "- (2026-02-13) [gotcha] Settings changes don't apply to current session — requires restart or `/clear` to reload permissions/MCP configs"
        # Below 0.5 threshold — vocabulary too different despite conceptual overlap
        assert is_routed_match(entry1, entry2, threshold=0.5) is False
        # At 0.4 threshold, the shared keywords are enough
        assert is_routed_match(entry1, entry2, threshold=0.4) is True

    def test_different_concepts_not_matched(self):
        """Ensure genuinely different entries are NOT matched."""
        entry1 = "- (2026-02-16) [pattern] Happy architecture: wraps `claude` CLI with system prompt injection"
        entry2 = "- (2026-02-17) [pattern] Git merge strategies: squash (clean, one commit, loses granularity)"
        assert is_routed_match(entry1, entry2, threshold=0.6) is False

    def test_similar_topic_different_aspect_not_matched(self):
        """Two entries about git but different topics should NOT match."""
        entry1 = "- (2026-02-17) [pattern] Git merge strategies: squash, rebase, merge commit"
        entry2 = "- (2026-02-17) [pattern] Worktree workflow: branch from latest main, one per worktree"
        assert is_routed_match(entry1, entry2, threshold=0.6) is False


class TestKeywordDedupEdgeCases:
    """Edge cases for the keyword matching algorithm."""

    def test_empty_entries(self):
        assert is_routed_match("", "", threshold=0.5) is False
        assert is_routed_match("- some text", "", threshold=0.5) is False

    def test_only_stopwords(self):
        """Entries with only stopwords should not match anything."""
        entry1 = "- (2026-01-01) [tip] the is are was were"
        entry2 = "- (2026-01-01) [tip] a an the for with"
        # After removing stopwords and short tokens, keywords are empty
        assert is_routed_match(entry1, entry2, threshold=0.5) is False

    def test_single_keyword_overlap(self):
        """Short entries with one shared keyword — should depend on threshold."""
        entry1 = "- (2026-01-01) [gotcha] SQLAlchemy needs commit"
        entry2 = "- (2026-01-01) [gotcha] SQLAlchemy requires flush"
        # Both have "sqlalchemy", one has "commit", other has "flush"+"requires"
        assert "sqlalchemy" in extract_entry_keywords(entry1)
        assert "sqlalchemy" in extract_entry_keywords(entry2)
        # Low overlap ratio — only "sqlalchemy"+"needs"/"requires" overlap
        # Should not match at 0.6 threshold
        assert is_routed_match(entry1, entry2, threshold=0.6) is False

    def test_threshold_boundary(self):
        """Exact threshold boundary behavior."""
        entry1 = "- [tip] alpha bravo charlie delta"
        entry2 = "- [tip] alpha bravo echo foxtrot"
        # {alpha, bravo, charlie, delta} vs {alpha, bravo, echo, foxtrot}
        # overlap = 2 (alpha, bravo), smaller = 4, ratio = 0.5
        assert is_routed_match(entry1, entry2, threshold=0.5) is True
        assert is_routed_match(entry1, entry2, threshold=0.6) is False

    def test_routed_prefix_stripped(self):
        """[routed] prefix should not affect matching."""
        entry1 = "- [routed][global/gotcha] MCP servers can be disabled per-project"
        entry2 = "- (2026-01-22) [gotcha] MCP servers can be disabled per-project"
        assert is_routed_match(entry1, entry2, threshold=0.6) is True


# ── mark-routed keyword dedup integration ──


class TestMarkRoutedKeywordDedup:
    """Test that cmd_mark_routed removes near-duplicates within LTM files."""

    def _make_ltm_file(self, tmpdir, filename, content):
        p = Path(tmpdir) / filename
        p.write_text(content, encoding="utf-8")
        return p

    def test_exact_duplicates_removed(self):
        """Exact duplicate lines within LTM should be removed."""
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] First entry about something
- (2026-02-15) [gotcha] First entry about something
- (2026-02-16) [pattern] Different entry
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ltm = self._make_ltm_file(tmpdir, "global-long-term-memory.md", content)

            with mock.patch("memory_utils.get_global_memory_file", return_value=ltm), \
                 mock.patch("memory_utils.get_project_memory_dir", return_value=Path(tmpdir) / "project-memory"), \
                 mock.patch("memory_utils.get_daily_dir", return_value=Path(tmpdir) / "daily"):
                # Create empty dirs so they exist
                (Path(tmpdir) / "project-memory").mkdir()
                (Path(tmpdir) / "daily").mkdir()

                from devtools import cmd_mark_routed
                args = argparse.Namespace(dry_run=False)
                cmd_mark_routed(args)

                result = ltm.read_text(encoding="utf-8")
                # Should only have one copy of the duplicate
                assert result.count("First entry about something") == 1
                assert "Different entry" in result

    def test_near_duplicates_removed(self):
        """Near-duplicate lines (high keyword overlap above 0.7) should be removed."""
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] Running commands from wrong directory in worktree setups causes script and output mismatch with main repo version
- (2026-02-15) [gotcha] Running commands from wrong directory in worktree setups causes script and output mismatch using worktree edits instead
- (2026-02-16) [pattern] Something completely unrelated about databases
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ltm = self._make_ltm_file(tmpdir, "global-long-term-memory.md", content)

            with mock.patch("memory_utils.get_global_memory_file", return_value=ltm), \
                 mock.patch("memory_utils.get_project_memory_dir", return_value=Path(tmpdir) / "project-memory"), \
                 mock.patch("memory_utils.get_daily_dir", return_value=Path(tmpdir) / "daily"):
                (Path(tmpdir) / "project-memory").mkdir()
                (Path(tmpdir) / "daily").mkdir()

                from devtools import cmd_mark_routed
                args = argparse.Namespace(dry_run=False)
                cmd_mark_routed(args)

                result = ltm.read_text(encoding="utf-8")
                # First entry kept, near-dup removed, unrelated kept
                lines = [ln for ln in result.splitlines() if ln.startswith("- (")]
                assert len(lines) == 2
                assert "main repo" in lines[0]
                assert "databases" in lines[1]

    def test_different_entries_kept(self):
        """Genuinely different entries should all be kept."""
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] Happy MCP transport reuse error
- (2026-02-16) [pattern] Git merge strategies and rebase workflow
- (2026-02-17) [tip] WSL2 idle timeout configuration
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ltm = self._make_ltm_file(tmpdir, "global-long-term-memory.md", content)

            with mock.patch("memory_utils.get_global_memory_file", return_value=ltm), \
                 mock.patch("memory_utils.get_project_memory_dir", return_value=Path(tmpdir) / "project-memory"), \
                 mock.patch("memory_utils.get_daily_dir", return_value=Path(tmpdir) / "daily"):
                (Path(tmpdir) / "project-memory").mkdir()
                (Path(tmpdir) / "daily").mkdir()

                from devtools import cmd_mark_routed
                args = argparse.Namespace(dry_run=False)
                cmd_mark_routed(args)

                result = ltm.read_text(encoding="utf-8")
                lines = [ln for ln in result.splitlines() if ln.startswith("- (")]
                assert len(lines) == 3

    def test_dry_run_does_not_modify(self):
        """Dry run should not modify the file."""
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] First entry about something
- (2026-02-15) [gotcha] First entry about something
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ltm = self._make_ltm_file(tmpdir, "global-long-term-memory.md", content)
            original = ltm.read_text(encoding="utf-8")

            with mock.patch("memory_utils.get_global_memory_file", return_value=ltm), \
                 mock.patch("memory_utils.get_project_memory_dir", return_value=Path(tmpdir) / "project-memory"), \
                 mock.patch("memory_utils.get_daily_dir", return_value=Path(tmpdir) / "daily"):
                (Path(tmpdir) / "project-memory").mkdir()
                (Path(tmpdir) / "daily").mkdir()

                from devtools import cmd_mark_routed
                args = argparse.Namespace(dry_run=True)
                cmd_mark_routed(args)

                assert ltm.read_text(encoding="utf-8") == original


# ── validate-ltm integration ──


class TestValidateLtm:
    """Test the validate-ltm command."""

    def test_clean_file_no_issues(self):
        """A clean LTM file should report no issues."""
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] MCP resource handlers block server initialization if slow
- (2026-02-16) [pattern] Git merge strategies include squash rebase and merge commit
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ltm = Path(tmpdir) / "global-long-term-memory.md"
            ltm.write_text(content, encoding="utf-8")
            project_dir = Path(tmpdir) / "project-memory"
            project_dir.mkdir()
            index_file = Path(tmpdir) / "projects-index.json"
            index_file.write_text('{"projects": {}}', encoding="utf-8")

            with mock.patch("memory_utils.get_global_memory_file", return_value=ltm), \
                 mock.patch("memory_utils.get_project_memory_dir", return_value=project_dir), \
                 mock.patch("memory_utils.get_projects_index_file", return_value=index_file):
                from devtools import cmd_validate_ltm
                args = argparse.Namespace()
                result = cmd_validate_ltm(args)
                assert result == 0

    def test_exact_duplicate_detected(self):
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] Same entry here
- (2026-02-15) [gotcha] Same entry here
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ltm = Path(tmpdir) / "global-long-term-memory.md"
            ltm.write_text(content, encoding="utf-8")
            project_dir = Path(tmpdir) / "project-memory"
            project_dir.mkdir()
            index_file = Path(tmpdir) / "projects-index.json"
            index_file.write_text('{"projects": {}}', encoding="utf-8")

            with mock.patch("memory_utils.get_global_memory_file", return_value=ltm), \
                 mock.patch("memory_utils.get_project_memory_dir", return_value=project_dir), \
                 mock.patch("memory_utils.get_projects_index_file", return_value=index_file):
                from devtools import cmd_validate_ltm
                args = argparse.Namespace()
                result = cmd_validate_ltm(args)
                assert result == 1  # Issues found

    def test_unregistered_project_detected(self):
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] Some entry
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            ltm = Path(tmpdir) / "global-long-term-memory.md"
            ltm.write_text("# Empty\n", encoding="utf-8")
            project_dir = Path(tmpdir) / "project-memory"
            project_dir.mkdir()
            # Create a project file that's NOT in the index
            orphan = project_dir / "fake-project-long-term-memory.md"
            orphan.write_text(content, encoding="utf-8")
            index_file = Path(tmpdir) / "projects-index.json"
            index_file.write_text('{"projects": {}}', encoding="utf-8")

            with mock.patch("memory_utils.get_global_memory_file", return_value=ltm), \
                 mock.patch("memory_utils.get_project_memory_dir", return_value=project_dir), \
                 mock.patch("memory_utils.get_projects_index_file", return_value=index_file):
                from devtools import cmd_validate_ltm
                args = argparse.Namespace()
                result = cmd_validate_ltm(args)
                assert result == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
