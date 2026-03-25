#!/usr/bin/env python3
"""Unit tests for scripts/session_import.py"""

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
from session_import import ImportResult, _get_current_prefix, detect_prefixes, import_sessions


class TestDetectPrefixes:
    def test_groups_by_prefix(self):
        names = ["-home-nsitaram-projectA", "-home-nsitaram-projectB", "-home-alice-projectC"]
        result = detect_prefixes(names)
        assert "-home-nsitaram-" in result
        assert len(result["-home-nsitaram-"]) == 2
        assert "-home-alice-" in result
        assert len(result["-home-alice-"]) == 1

    def test_empty_list(self):
        assert detect_prefixes([]) == {}


class TestImportSessions:
    def _create_source(self, tmp_path, prefix="-home-nsitaram-", projects=None):
        """Create a source directory with test .jsonl files."""
        source = tmp_path / "backup" / "projects"
        source.mkdir(parents=True)
        projects = projects or ["projectA"]
        for proj in projects:
            proj_dir = source / f"{prefix}{proj}"
            proj_dir.mkdir()
            session = proj_dir / "abc123.jsonl"
            session.write_text('{"type":"human"}\n')
            ts = datetime(2026, 3, 20, tzinfo=timezone.utc).timestamp()
            os.utime(session, (ts, ts))
        return source

    def test_prefix_remapping(self, tmp_path):
        """Source with different prefix gets remapped to current machine."""
        source = self._create_source(tmp_path, prefix="-home-nsitaram-")
        target = tmp_path / "target"
        target.mkdir()

        with mock.patch("session_import.Path.home", return_value=Path("/Users/nsitaram")):
            result = import_sessions(source, target)

        assert result.copied == 1
        assert result.projects == 1
        expected_dir = target / "-Users-nsitaram-projectA"
        assert expected_dir.exists()
        assert (expected_dir / "abc123.jsonl").exists()

    def test_same_prefix_no_remap(self, tmp_path):
        """Same prefix copies without renaming."""
        source = self._create_source(tmp_path, prefix="-Users-nsitaram-")
        target = tmp_path / "target"
        target.mkdir()

        with mock.patch("session_import.Path.home", return_value=Path("/Users/nsitaram")):
            result = import_sessions(source, target)

        assert result.copied == 1
        assert (target / "-Users-nsitaram-projectA" / "abc123.jsonl").exists()

    def test_dedup_by_session_uuid(self, tmp_path):
        """Existing session file skipped."""
        source = self._create_source(tmp_path, prefix="-Users-nsitaram-")
        target = tmp_path / "target"
        target_dir = target / "-Users-nsitaram-projectA"
        target_dir.mkdir(parents=True)
        (target_dir / "abc123.jsonl").write_text("existing")

        with mock.patch("session_import.Path.home", return_value=Path("/Users/nsitaram")):
            result = import_sessions(source, target)

        assert result.copied == 0
        assert result.skipped == 1

    def test_mtime_preserved(self, tmp_path):
        """Copied file preserves original mtime."""
        source = self._create_source(tmp_path, prefix="-Users-nsitaram-")
        target = tmp_path / "target"
        target.mkdir()
        expected_ts = datetime(2026, 3, 20, tzinfo=timezone.utc).timestamp()

        with mock.patch("session_import.Path.home", return_value=Path("/Users/nsitaram")):
            import_sessions(source, target)

        copied_file = target / "-Users-nsitaram-projectA" / "abc123.jsonl"
        actual_ts = copied_file.stat().st_mtime
        assert abs(actual_ts - expected_ts) < 2  # within 2 seconds

    def test_empty_folders_skipped(self, tmp_path):
        """Folders without .jsonl files are skipped."""
        source = tmp_path / "backup" / "projects"
        source.mkdir(parents=True)
        empty_dir = source / "-home-nsitaram-empty"
        empty_dir.mkdir()
        (empty_dir / "readme.txt").write_text("not a session")

        target = tmp_path / "target"
        target.mkdir()

        with mock.patch("session_import.Path.home", return_value=Path("/Users/nsitaram")):
            result = import_sessions(source, target)

        assert result.copied == 0
        assert result.projects == 0

    def test_fuzzy_match_by_project_suffix(self, tmp_path):
        """Fuzzy match uses last path segment when remapped folder doesn't exist."""
        source = self._create_source(tmp_path, prefix="-home-nsitaram-")
        target = tmp_path / "target"
        # Create a target folder with different prefix structure
        existing = target / "-Users-nsitaram-personal-projectA"
        existing.mkdir(parents=True)

        with mock.patch("session_import.Path.home", return_value=Path("/Users/nsitaram")):
            result = import_sessions(source, target)

        assert result.copied == 1
        assert len(result.mismatches) == 1
        assert "fuzzy match" in result.mismatches[0]
        # File should be in the existing folder, not a new one
        assert (existing / "abc123.jsonl").exists()

    def test_mixed_prefixes(self, tmp_path):
        """Multiple prefixes each remapped independently."""
        source = tmp_path / "backup" / "projects"
        source.mkdir(parents=True)
        for prefix, proj in [("-home-nsitaram-", "projA"), ("-home-alice-", "projB")]:
            d = source / f"{prefix}{proj}"
            d.mkdir()
            (d / "sess1.jsonl").write_text('{"type":"human"}\n')

        target = tmp_path / "target"
        target.mkdir()

        with mock.patch("session_import.Path.home", return_value=Path("/Users/nsitaram")):
            result = import_sessions(source, target)

        assert result.copied == 2
        assert result.projects == 2

    def test_nonexistent_source_returns_empty(self, tmp_path):
        result = import_sessions(tmp_path / "nonexistent")
        assert result.copied == 0
        assert result.projects == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
