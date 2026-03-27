#!/usr/bin/env python3
"""
Unit tests for scripts/injection_log.py (injection logging and rotation)

Run with: python3 -m pytest tests/test_injection_log.py -v
"""

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


class TestGetLogPath:
    """Tests for get_log_path()."""

    def test_returns_path_in_memory_dir(self, tmp_path):
        from injection_log import LOG_FILENAME, get_log_path
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            path = get_log_path()
        assert path == tmp_path / LOG_FILENAME

    def test_path_is_pathlib(self, tmp_path):
        from injection_log import get_log_path
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            path = get_log_path()
        assert isinstance(path, Path)


class TestIsEnabled:
    """Tests for _is_enabled() settings check."""

    def test_enabled_by_default(self):
        from injection_log import _is_enabled
        with patch("injection_log.load_settings", return_value={}):
            assert _is_enabled() is True

    def test_explicitly_enabled(self):
        from injection_log import _is_enabled
        with patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": True}}):
            assert _is_enabled() is True

    def test_explicitly_disabled(self):
        from injection_log import _is_enabled
        with patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": False}}):
            assert _is_enabled() is False

    def test_exception_returns_true(self):
        from injection_log import _is_enabled
        with patch("injection_log.load_settings", side_effect=Exception("boom")):
            assert _is_enabled() is True


class TestLogSessionStart:
    """Tests for log_session_start()."""

    def test_writes_jsonl_line(self, tmp_path):
        from injection_log import log_session_start
        with patch("injection_log.get_memory_dir", return_value=tmp_path), \
             patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": True}}):
            tiers = [
                {"name": "profile", "count": 1, "tokens_est": 200, "ids": ["dp-1"]},
                {"name": "project", "count": 2, "tokens_est": 500, "ids": ["dp-2", "dp-3"]},
            ]
            log_session_start(
                session_id="sess-123",
                project_scope="/home/user/project",
                tiers=tiers,
                latency_ms=42.567,
            )

        from injection_log import LOG_FILENAME
        log_file = tmp_path / LOG_FILENAME
        assert log_file.exists()
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["session_id"] == "sess-123"
        assert entry["hook"] == "SessionStart"
        assert entry["project_scope"] == "/home/user/project"
        assert entry["total_items"] == 3
        assert entry["total_tokens_est"] == 700
        assert entry["latency_ms"] == 42.6
        assert entry["health_alerts"] == []
        assert "ts" in entry
        assert len(entry["tiers"]) == 2

    def test_includes_health_alerts(self, tmp_path):
        from injection_log import log_session_start
        with patch("injection_log.get_memory_dir", return_value=tmp_path), \
             patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": True}}):
            log_session_start(
                session_id="sess-456",
                project_scope="/proj",
                tiers=[],
                latency_ms=10.0,
                health_alerts=["low salience", "stale data"],
            )

        from injection_log import LOG_FILENAME
        log_file = tmp_path / LOG_FILENAME
        entry = json.loads(log_file.read_text().strip())
        assert entry["health_alerts"] == ["low salience", "stale data"]

    def test_disabled_writes_nothing(self, tmp_path):
        from injection_log import LOG_FILENAME, log_session_start
        with patch("injection_log.get_memory_dir", return_value=tmp_path), \
             patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": False}}):
            log_session_start(
                session_id="sess-789",
                project_scope="/proj",
                tiers=[],
                latency_ms=10.0,
            )
        log_file = tmp_path / LOG_FILENAME
        assert not log_file.exists()

    def test_exception_silenced(self, tmp_path):
        from injection_log import log_session_start
        with patch("injection_log.get_memory_dir", side_effect=Exception("boom")), \
             patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": True}}):
            log_session_start(
                session_id="sess-err",
                project_scope="/proj",
                tiers=[],
                latency_ms=10.0,
            )


class TestLogPromptRecall:
    """Tests for log_prompt_recall()."""

    def test_writes_jsonl_line(self, tmp_path):
        from injection_log import log_prompt_recall
        with patch("injection_log.get_memory_dir", return_value=tmp_path), \
             patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": True}}):
            log_prompt_recall(
                session_id="sess-abc",
                prompt_preview="How do I configure Redis for caching?",
                candidates=5,
                injected=["dp-10", "dp-11"],
                filtered=["dp-12"],
                latency_ms=15.321,
            )

        from injection_log import LOG_FILENAME
        log_file = tmp_path / LOG_FILENAME
        assert log_file.exists()
        entry = json.loads(log_file.read_text().strip())
        assert entry["session_id"] == "sess-abc"
        assert entry["hook"] == "UserPromptSubmit"
        assert entry["prompt_preview"] == "How do I configure Redis for caching?"
        assert entry["candidates"] == 5
        assert entry["injected"] == ["dp-10", "dp-11"]
        assert entry["filtered"] == ["dp-12"]
        assert entry["latency_ms"] == 15.3
        assert "ts" in entry

    def test_truncates_long_prompt(self, tmp_path):
        from injection_log import CONTENT_PREVIEW_LENGTH, log_prompt_recall
        long_prompt = "x" * 200
        with patch("injection_log.get_memory_dir", return_value=tmp_path), \
             patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": True}}):
            log_prompt_recall(
                session_id="sess-trunc",
                prompt_preview=long_prompt,
                candidates=0,
                injected=[],
                filtered=[],
                latency_ms=1.0,
            )

        from injection_log import LOG_FILENAME
        log_file = tmp_path / LOG_FILENAME
        entry = json.loads(log_file.read_text().strip())
        assert len(entry["prompt_preview"]) == CONTENT_PREVIEW_LENGTH

    def test_disabled_writes_nothing(self, tmp_path):
        from injection_log import LOG_FILENAME, log_prompt_recall
        with patch("injection_log.get_memory_dir", return_value=tmp_path), \
             patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": False}}):
            log_prompt_recall(
                session_id="sess-off",
                prompt_preview="test",
                candidates=0,
                injected=[],
                filtered=[],
                latency_ms=1.0,
            )
        log_file = tmp_path / LOG_FILENAME
        assert not log_file.exists()

    def test_exception_silenced(self, tmp_path):
        from injection_log import log_prompt_recall
        with patch("injection_log.get_memory_dir", side_effect=Exception("boom")), \
             patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": True}}):
            log_prompt_recall(
                session_id="sess-err",
                prompt_preview="test",
                candidates=0,
                injected=[],
                filtered=[],
                latency_ms=1.0,
            )


class TestRotateLog:
    """Tests for rotate_log()."""

    def test_no_file_no_error(self, tmp_path):
        from injection_log import rotate_log
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            rotate_log()

    def test_under_max_no_truncation(self, tmp_path):
        from injection_log import LOG_FILENAME, MAX_LINES, rotate_log
        log_file = tmp_path / LOG_FILENAME
        lines = [json.dumps({"i": i}) + "\n" for i in range(MAX_LINES - 10)]
        log_file.write_text("".join(lines))
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            rotate_log()
        assert len(log_file.read_text().strip().split("\n")) == MAX_LINES - 10

    def test_over_max_truncates_to_keep(self, tmp_path):
        from injection_log import KEEP_LINES, LOG_FILENAME, MAX_LINES, rotate_log
        log_file = tmp_path / LOG_FILENAME
        lines = [json.dumps({"i": i}) + "\n" for i in range(MAX_LINES + 50)]
        log_file.write_text("".join(lines))
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            rotate_log()
        remaining = log_file.read_text().strip().split("\n")
        assert len(remaining) == KEEP_LINES
        last_entry = json.loads(remaining[-1])
        assert last_entry["i"] == MAX_LINES + 49

    def test_custom_limits(self, tmp_path):
        from injection_log import LOG_FILENAME, rotate_log
        log_file = tmp_path / LOG_FILENAME
        lines = [json.dumps({"i": i}) + "\n" for i in range(20)]
        log_file.write_text("".join(lines))
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            rotate_log(max_lines=10, keep_lines=5)
        remaining = log_file.read_text().strip().split("\n")
        assert len(remaining) == 5
        last_entry = json.loads(remaining[-1])
        assert last_entry["i"] == 19

    def test_exception_silenced(self, tmp_path):
        from injection_log import rotate_log
        with patch("injection_log.get_memory_dir", side_effect=Exception("boom")):
            rotate_log()


class TestReadLog:
    """Tests for read_log()."""

    def test_no_file_returns_empty(self, tmp_path):
        from injection_log import read_log
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            result = read_log()
        assert result == []

    def test_reads_recent_entries(self, tmp_path):
        from injection_log import LOG_FILENAME, read_log
        log_file = tmp_path / LOG_FILENAME
        now = datetime.now(timezone.utc)
        entries = [
            {"ts": (now - timedelta(minutes=30)).isoformat(), "hook": "SessionStart", "session_id": "s1"},
            {"ts": (now - timedelta(minutes=10)).isoformat(), "hook": "UserPromptSubmit", "session_id": "s1"},
        ]
        log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            result = read_log()
        assert len(result) == 2

    def test_filters_by_session_id(self, tmp_path):
        from injection_log import LOG_FILENAME, read_log
        log_file = tmp_path / LOG_FILENAME
        now = datetime.now(timezone.utc)
        entries = [
            {"ts": (now - timedelta(minutes=30)).isoformat(), "hook": "SessionStart", "session_id": "s1"},
            {"ts": (now - timedelta(minutes=10)).isoformat(), "hook": "SessionStart", "session_id": "s2"},
        ]
        log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            result = read_log(session_id="s1")
        assert len(result) == 1
        assert result[0]["session_id"] == "s1"

    def test_filters_by_since(self, tmp_path):
        from injection_log import LOG_FILENAME, read_log
        log_file = tmp_path / LOG_FILENAME
        now = datetime.now(timezone.utc)
        entries = [
            {"ts": (now - timedelta(hours=3)).isoformat(), "hook": "SessionStart", "session_id": "old"},
            {"ts": (now - timedelta(minutes=10)).isoformat(), "hook": "SessionStart", "session_id": "new"},
        ]
        log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            result = read_log(since=now - timedelta(hours=1))
        assert len(result) == 1
        assert result[0]["session_id"] == "new"

    def test_skips_malformed_lines(self, tmp_path):
        from injection_log import LOG_FILENAME, read_log
        log_file = tmp_path / LOG_FILENAME
        now = datetime.now(timezone.utc)
        content = "not json\n" + json.dumps({"ts": now.isoformat(), "hook": "SessionStart", "session_id": "ok"}) + "\n"
        log_file.write_text(content)
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            result = read_log()
        assert len(result) == 1
        assert result[0]["session_id"] == "ok"

    def test_max_entries_limit(self, tmp_path):
        from injection_log import LOG_FILENAME, read_log
        log_file = tmp_path / LOG_FILENAME
        now = datetime.now(timezone.utc)
        entries = [
            {"ts": (now - timedelta(minutes=i)).isoformat(), "hook": "SessionStart", "session_id": f"s{i}"}
            for i in range(10)
        ]
        log_file.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
        with patch("injection_log.get_memory_dir", return_value=tmp_path):
            result = read_log(max_entries=3)
        assert len(result) == 3


class TestEnabledToggle:
    """Tests that enabled/disabled toggle works correctly across all log functions."""

    def test_session_start_respects_disabled(self, tmp_path):
        from injection_log import LOG_FILENAME, log_session_start
        with patch("injection_log.get_memory_dir", return_value=tmp_path), \
             patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": False}}):
            log_session_start("s1", "/proj", [], 10.0)
        assert not (tmp_path / LOG_FILENAME).exists()

    def test_prompt_recall_respects_disabled(self, tmp_path):
        from injection_log import LOG_FILENAME, log_prompt_recall
        with patch("injection_log.get_memory_dir", return_value=tmp_path), \
             patch("injection_log.load_settings", return_value={"injectionLog": {"enabled": False}}):
            log_prompt_recall("s1", "test", 0, [], [], 1.0)
        assert not (tmp_path / LOG_FILENAME).exists()

    def test_default_settings_has_injection_log_enabled(self):
        from memory_utils import DEFAULT_SETTINGS
        assert "injectionLog" in DEFAULT_SETTINGS
        assert DEFAULT_SETTINGS["injectionLog"]["enabled"] is True
