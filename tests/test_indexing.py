#!/usr/bin/env python3
"""
Unit tests for indexing.py

Run with: python -m pytest tests/test_indexing.py -v
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

# Add scripts directory to path
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from indexing import (
    MIN_SESSION_SIZE_BYTES,
    SessionInfo,
    get_session_date,
    has_assistant_message,
    list_pending_sessions,
)
from transcript_ops import (
    extract_text_content,
    format_transcripts_for_output,
    parse_jsonl_file,
    should_skip_message,
)


# =============================================================================
# Helper to create JSONL content
# =============================================================================


def make_jsonl_line(role: str, content: str) -> str:
    return json.dumps({
        "type": role,
        "message": {"role": role, "content": content},
    })


def make_session_info(
    session_id: str = "test-session",
    transcript_path: Path = Path("/tmp/test.jsonl"),
    file_size: int = 2000,
    project_path: str | None = None,
    created: datetime | None = None,
) -> SessionInfo:
    return SessionInfo(
        session_id=session_id,
        transcript_path=transcript_path,
        project_hash="test-hash",
        file_mtime=datetime.now(timezone.utc),
        file_size=file_size,
        project_path=project_path,
        created=created,
    )


# =============================================================================
# Content Extraction Tests
# =============================================================================


class TestExtractTextContent:
    def test_string_content(self):
        assert extract_text_content("hello world") == "hello world"

    def test_list_content(self):
        content = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]
        result = extract_text_content(content)
        assert "first" in result
        assert "second" in result

    def test_list_with_non_text(self):
        content = [
            {"type": "text", "text": "keep"},
            {"type": "image", "url": "http://example.com"},
        ]
        result = extract_text_content(content)
        assert "keep" in result
        assert "example.com" not in result

    def test_none_content(self):
        assert extract_text_content(None) == ""

    def test_int_content(self):
        assert extract_text_content(42) == ""

    def test_empty_list(self):
        assert extract_text_content([]) == ""


# =============================================================================
# Message Filter Tests
# =============================================================================


class TestShouldSkipMessage:
    def test_skill_injection(self):
        assert should_skip_message("Base directory for this skill: /home/user/.claude/skills/test")

    def test_command_name_tag(self):
        assert should_skip_message("<command-name>/synthesize</command-name> some content")

    def test_system_reminder(self):
        assert should_skip_message("Some text with <system-reminder> embedded")

    def test_user_interruption(self):
        assert should_skip_message("[Request interrupted by user]")

    def test_normal_content(self):
        assert not should_skip_message("I've analyzed the codebase and found the following patterns")

    def test_empty_content(self):
        assert not should_skip_message("")


# =============================================================================
# has_assistant_message Tests
# =============================================================================


class TestHasAssistantMessage:
    def test_with_assistant_message(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(make_jsonl_line("user", "hello") + "\n")
            f.write(make_jsonl_line("assistant", "hi there") + "\n")
            f.flush()
            try:
                assert has_assistant_message(Path(f.name)) is True
            finally:
                os.unlink(f.name)

    def test_without_assistant_message(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(make_jsonl_line("user", "hello") + "\n")
            f.flush()
            try:
                assert has_assistant_message(Path(f.name)) is False
            finally:
                os.unlink(f.name)

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write("")
            f.flush()
            try:
                assert has_assistant_message(Path(f.name)) is False
            finally:
                os.unlink(f.name)

    def test_metadata_only(self):
        """Sessions with only metadata (file-history-snapshot, progress) should return False."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(json.dumps({"type": "progress", "data": {}}) + "\n")
            f.write(json.dumps({"type": "file-history-snapshot", "files": []}) + "\n")
            f.flush()
            try:
                assert has_assistant_message(Path(f.name)) is False
            finally:
                os.unlink(f.name)

    def test_nonexistent_file(self):
        assert has_assistant_message(Path("/nonexistent/file.jsonl")) is False

    def test_invalid_json_lines(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write("not json\n")
            f.write(make_jsonl_line("assistant", "valid") + "\n")
            f.flush()
            try:
                assert has_assistant_message(Path(f.name)) is True
            finally:
                os.unlink(f.name)


# =============================================================================
# parse_jsonl_file Tests
# =============================================================================


class TestParseJsonlFile:
    def test_parses_assistant_messages(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(make_jsonl_line("assistant", "I found the bug") + "\n")
            f.flush()
            try:
                messages = parse_jsonl_file(Path(f.name))
                assert len(messages) == 1
                assert messages[0]["role"] == "assistant"
                assert "bug" in messages[0]["content"]
            finally:
                os.unlink(f.name)

    def test_skips_user_messages(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(make_jsonl_line("user", "Fix the bug") + "\n")
            f.write(make_jsonl_line("assistant", "I found the issue") + "\n")
            f.flush()
            try:
                messages = parse_jsonl_file(Path(f.name))
                assert len(messages) == 1
                assert messages[0]["role"] == "assistant"
            finally:
                os.unlink(f.name)

    def test_skips_system_reminders(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(make_jsonl_line("assistant", "Normal response") + "\n")
            f.write(make_jsonl_line("assistant", "Response with <system-reminder> injected") + "\n")
            f.flush()
            try:
                messages = parse_jsonl_file(Path(f.name))
                assert len(messages) == 1
                assert "Normal response" in messages[0]["content"]
            finally:
                os.unlink(f.name)

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.flush()
            try:
                messages = parse_jsonl_file(Path(f.name))
                assert messages == []
            finally:
                os.unlink(f.name)

    def test_nonexistent_file(self):
        messages = parse_jsonl_file(Path("/nonexistent/file.jsonl"))
        assert messages == []


# =============================================================================
# list_pending_sessions Tests
# =============================================================================


class TestListPendingSessions:
    def _make_sessions(self) -> list[SessionInfo]:
        return [
            make_session_info("captured-1", file_size=2000),
            make_session_info("pending-1", file_size=2000),
            make_session_info("small-1", file_size=500),
            make_session_info("pending-2", file_size=3000),
        ]

    def test_filters_captured(self):
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_pending_sessions(captured={"captured-1"})
            ids = {s.session_id for s in result}
            assert "captured-1" not in ids
            assert "pending-1" in ids

    def test_filters_small_sessions(self):
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_pending_sessions(captured=set())
            ids = {s.session_id for s in result}
            assert "small-1" not in ids

    def test_excludes_session_id(self):
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_pending_sessions(
                captured=set(), exclude_session_id="pending-1"
            )
            ids = {s.session_id for s in result}
            assert "pending-1" not in ids
            assert "pending-2" in ids

    def test_min_session_size_constant(self):
        assert MIN_SESSION_SIZE_BYTES == 1000


# =============================================================================
# get_session_date Tests
# =============================================================================


class TestGetSessionDate:
    def test_prefers_created(self):
        session = make_session_info(
            created=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        )
        assert get_session_date(session) == "2026-01-15"

    def test_falls_back_to_mtime(self):
        session = make_session_info(created=None)
        # mtime is set to now in the helper
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert get_session_date(session) == today


# =============================================================================
# Format Output Tests
# =============================================================================


class TestFormatTranscriptsForOutput:
    def test_basic_formatting(self):
        daily_data = {
            "2026-02-01": [
                {
                    "session_id": "abc123",
                    "filepath": "/tmp/test.jsonl",
                    "project_path": None,
                    "message_count": 2,
                    "messages": [
                        {"role": "assistant", "content": "Hello world"},
                        {"role": "assistant", "content": "Goodbye"},
                    ],
                }
            ]
        }
        output = format_transcripts_for_output(daily_data)
        assert "2026-02-01" in output
        assert "abc123" in output
        assert "Hello world" in output
        assert "CLAUDE" in output

    def test_empty_data(self):
        assert format_transcripts_for_output({}) == ""

    def test_multiple_days_sorted(self):
        daily_data = {
            "2026-02-03": [
                {
                    "session_id": "s1",
                    "filepath": "/tmp/a.jsonl",
                    "project_path": None,
                    "message_count": 1,
                    "messages": [{"role": "assistant", "content": "Day 3"}],
                }
            ],
            "2026-02-01": [
                {
                    "session_id": "s2",
                    "filepath": "/tmp/b.jsonl",
                    "project_path": None,
                    "message_count": 1,
                    "messages": [{"role": "assistant", "content": "Day 1"}],
                }
            ],
        }
        output = format_transcripts_for_output(daily_data)
        # Day 1 should appear before Day 3
        assert output.index("2026-02-01") < output.index("2026-02-03")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
