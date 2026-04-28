#!/usr/bin/env python3
"""Unit tests for devtools.py — keyword dedup and validate-ltm."""

import argparse
import json
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from devtools import cmd_mark_routed, cmd_stats, cmd_validate_ltm
from memory_utils import (
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
        assert "sqlalchemy" in extract_entry_keywords(entry1)
        assert "sqlalchemy" in extract_entry_keywords(entry2)
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

    def _run_mark_routed(self, tmp_path, ltm_content, dry_run=False):
        """Write LTM content, run cmd_mark_routed, return result text."""
        ltm = tmp_path / "global-long-term-memory.md"
        ltm.write_text(ltm_content, encoding="utf-8")
        (tmp_path / "project-memory").mkdir(exist_ok=True)
        (tmp_path / "daily").mkdir(exist_ok=True)
        with mock.patch("memory_utils.get_global_memory_file", return_value=ltm), \
             mock.patch("memory_utils.get_project_memory_dir", return_value=tmp_path / "project-memory"), \
             mock.patch("memory_utils.get_daily_dir", return_value=tmp_path / "daily"):
            args = argparse.Namespace(dry_run=dry_run)
            cmd_mark_routed(args)
        return ltm.read_text(encoding="utf-8")

    def test_exact_duplicates_removed(self, tmp_path):
        """Exact duplicate lines within LTM should be removed."""
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] First entry about something
- (2026-02-15) [gotcha] First entry about something
- (2026-02-16) [pattern] Different entry
"""
        result = self._run_mark_routed(tmp_path, content)
        assert result.count("First entry about something") == 1
        assert "Different entry" in result

    def test_near_duplicates_removed(self, tmp_path):
        """Near-duplicate lines (high keyword overlap above 0.7) should be removed."""
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] Running commands from wrong directory in worktree setups causes script and output mismatch with main repo version
- (2026-02-15) [gotcha] Running commands from wrong directory in worktree setups causes script and output mismatch using worktree edits instead
- (2026-02-16) [pattern] Something completely unrelated about databases
"""
        result = self._run_mark_routed(tmp_path, content)
        lines = [ln for ln in result.splitlines() if ln.startswith("- (")]
        assert len(lines) == 2
        assert "main repo" in lines[0]
        assert "databases" in lines[1]

    def test_different_entries_kept(self, tmp_path):
        """Genuinely different entries should all be kept."""
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] Happy MCP transport reuse error
- (2026-02-16) [pattern] Git merge strategies and rebase workflow
- (2026-02-17) [tip] WSL2 idle timeout configuration
"""
        result = self._run_mark_routed(tmp_path, content)
        lines = [ln for ln in result.splitlines() if ln.startswith("- (")]
        assert len(lines) == 3

    def test_dry_run_does_not_modify(self, tmp_path):
        """Dry run should not modify the file."""
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] First entry about something
- (2026-02-15) [gotcha] First entry about something
"""
        ltm = tmp_path / "global-long-term-memory.md"
        ltm.write_text(content, encoding="utf-8")
        result = self._run_mark_routed(tmp_path, content, dry_run=True)
        assert result == content


# ── validate-ltm integration ──


class TestValidateLtm:
    """Test the validate-ltm command."""

    def _validate(self, tmp_path, global_content, project_files=None):
        """Set up LTM environment and run validate, return exit code."""
        ltm = tmp_path / "global-long-term-memory.md"
        ltm.write_text(global_content, encoding="utf-8")
        project_dir = tmp_path / "project-memory"
        project_dir.mkdir(exist_ok=True)
        if project_files:
            for name, file_content in project_files.items():
                (project_dir / name).write_text(file_content, encoding="utf-8")
        index_file = tmp_path / "projects-index.json"
        index_file.write_text('{"projects": {}}', encoding="utf-8")
        with mock.patch("memory_utils.get_global_memory_file", return_value=ltm), \
             mock.patch("memory_utils.get_project_memory_dir", return_value=project_dir), \
             mock.patch("memory_utils.get_projects_index_file", return_value=index_file):
            args = argparse.Namespace()
            return cmd_validate_ltm(args)

    def test_clean_file_no_issues(self, tmp_path):
        """A clean LTM file should report no issues."""
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] MCP resource handlers block server initialization if slow
- (2026-02-16) [pattern] Git merge strategies include squash rebase and merge commit
"""
        assert self._validate(tmp_path, content) == 0

    def test_exact_duplicate_detected(self, tmp_path):
        content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] Same entry here
- (2026-02-15) [gotcha] Same entry here
"""
        assert self._validate(tmp_path, content) == 1

    def test_unregistered_project_detected(self, tmp_path):
        project_content = """# Test
## Key Learnings

- (2026-02-15) [gotcha] Some entry
"""
        assert self._validate(
            tmp_path,
            "# Empty\n",
            project_files={"fake-project-long-term-memory.md": project_content},
        ) == 1


class TestStatsCommand:
    """Tests for devtools.py stats subcommand."""

    def _write_stats(self, tmp_path, records: list[dict]) -> None:
        stats_file = tmp_path / ".synthesis-stats.jsonl"
        lines = [json.dumps(r) for r in records]
        stats_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _make_record(self, hours_ago: float = 1, status: str = "ok",
                     input_tokens: int = 1000, output_tokens: int = 100,
                     duration_s: float = 10.0) -> dict:
        ts = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        return {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "prompt": "synthesis-prompt-test",
            "model": "sonnet",
            "duration_s": duration_s,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "status": status,
        }

    def test_missing_file(self, tmp_path, capsys):
        stats_file = tmp_path / ".synthesis-stats.jsonl"
        with mock.patch("memory_utils.get_synthesis_stats_file", return_value=stats_file):
            result = cmd_stats(argparse.Namespace())
        assert result == 0
        output = capsys.readouterr().out
        assert "No synthesis stats" in output

    def test_empty_file(self, tmp_path, capsys):
        stats_file = tmp_path / ".synthesis-stats.jsonl"
        stats_file.write_text("")
        with mock.patch("memory_utils.get_synthesis_stats_file", return_value=stats_file):
            result = cmd_stats(argparse.Namespace())
        assert result == 0
        output = capsys.readouterr().out
        assert "Synthesis stats" in output

    def test_24h_records_aggregated(self, tmp_path, capsys):
        records = [
            self._make_record(hours_ago=2, input_tokens=5000, output_tokens=400, duration_s=12.0),
            self._make_record(hours_ago=6, input_tokens=3000, output_tokens=200, duration_s=8.0),
        ]
        self._write_stats(tmp_path, records)
        stats_file = tmp_path / ".synthesis-stats.jsonl"
        with mock.patch("memory_utils.get_synthesis_stats_file", return_value=stats_file):
            result = cmd_stats(argparse.Namespace())
        assert result == 0
        output = capsys.readouterr().out
        assert "8,000" in output
        assert "600" in output

    def test_7d_records_separate_from_24h(self, tmp_path, capsys):
        records = [
            self._make_record(hours_ago=2, input_tokens=1000, output_tokens=100),
            self._make_record(hours_ago=48, input_tokens=2000, output_tokens=200),
        ]
        self._write_stats(tmp_path, records)
        stats_file = tmp_path / ".synthesis-stats.jsonl"
        with mock.patch("memory_utils.get_synthesis_stats_file", return_value=stats_file):
            result = cmd_stats(argparse.Namespace())
        assert result == 0
        output = capsys.readouterr().out
        runs_line = next(line for line in output.strip().splitlines() if line.strip().startswith("Runs:"))
        parts = runs_line.split()
        assert parts[1] == "1"
        assert parts[2] == "2"

    def test_error_records_counted(self, tmp_path, capsys):
        records = [
            self._make_record(hours_ago=1, status="ok"),
            self._make_record(hours_ago=2, status="error"),
            self._make_record(hours_ago=3, status="error"),
        ]
        self._write_stats(tmp_path, records)
        stats_file = tmp_path / ".synthesis-stats.jsonl"
        with mock.patch("memory_utils.get_synthesis_stats_file", return_value=stats_file):
            result = cmd_stats(argparse.Namespace())
        assert result == 0
        output = capsys.readouterr().out
        errors_line = next(line for line in output.strip().splitlines() if line.strip().startswith("Errors:"))
        parts = errors_line.split()
        assert parts[1] == "2"
        assert parts[2] == "2"

    def test_records_older_than_7d_excluded(self, tmp_path, capsys):
        records = [
            self._make_record(hours_ago=2, input_tokens=1000, output_tokens=100),
            self._make_record(hours_ago=200, input_tokens=9999, output_tokens=999),
        ]
        self._write_stats(tmp_path, records)
        stats_file = tmp_path / ".synthesis-stats.jsonl"
        with mock.patch("memory_utils.get_synthesis_stats_file", return_value=stats_file):
            result = cmd_stats(argparse.Namespace())
        assert result == 0
        output = capsys.readouterr().out
        runs_line = next(line for line in output.strip().splitlines() if line.strip().startswith("Runs:"))
        parts = runs_line.split()
        assert parts[1] == "1"
        assert parts[2] == "1"
        assert "9,999" not in output

    def test_malformed_lines_skipped(self, tmp_path, capsys):
        stats_file = tmp_path / ".synthesis-stats.jsonl"
        good_record = self._make_record(hours_ago=1, input_tokens=500, output_tokens=50)
        stats_file.write_text(
            "not valid json\n"
            + json.dumps(good_record) + "\n"
            + '{"ts": "invalid-date"}\n',
            encoding="utf-8",
        )
        with mock.patch("memory_utils.get_synthesis_stats_file", return_value=stats_file):
            result = cmd_stats(argparse.Namespace())
        assert result == 0
        output = capsys.readouterr().out
        assert "500" in output


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
