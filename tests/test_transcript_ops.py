#!/usr/bin/env python3
"""
Unit tests for transcript_ops.py

Run with: python -m pytest tests/test_transcript_ops.py -v
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

# Add scripts directory to path
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from indexing import SessionInfo
from transcript_ops import (
    extract_transcripts,
    format_transcripts_for_output,
    get_pending_days,
)


# =============================================================================
# Helpers
# =============================================================================


def make_session_info(
    session_id: str = "test-session",
    transcript_path: Path = Path("/tmp/test.jsonl"),
    file_size: int = 2000,
    project_path: str | None = None,
    created: datetime | None = None,
    file_mtime: datetime | None = None,
) -> SessionInfo:
    return SessionInfo(
        session_id=session_id,
        transcript_path=transcript_path,
        project_hash="test-hash",
        file_mtime=file_mtime or datetime.now(timezone.utc),
        file_size=file_size,
        project_path=project_path,
        created=created,
    )


def make_jsonl_content(messages: list[tuple[str, str]]) -> str:
    """Create JSONL content from (role, text) pairs."""
    lines = []
    for role, text in messages:
        lines.append(json.dumps({
            "type": role,
            "message": {"role": role, "content": text},
        }))
    return "\n".join(lines) + "\n"


# =============================================================================
# extract_transcripts Tests
# =============================================================================


class TestExtractTranscripts:
    def test_extracts_specific_day(self):
        """Filters to only the requested day."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a transcript file with assistant content
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(make_jsonl_content([
                ("assistant", "I found the bug in the code"),
            ]))

            session = make_session_info(
                session_id="s1",
                transcript_path=transcript,
                created=datetime(2026, 2, 5, 12, 0, tzinfo=timezone.utc),
            )

            with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
                 mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
                 mock.patch("transcript_ops.get_session_date", return_value="2026-02-05"):
                result = extract_transcripts(specific_day="2026-02-05")
                assert "2026-02-05" in result
                assert len(result["2026-02-05"]) == 1
                assert result["2026-02-05"][0]["session_id"] == "s1"

    def test_excludes_non_matching_day(self):
        """Sessions from other days are excluded when specific_day is set."""
        session = make_session_info(session_id="s1")

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-06"):
            result = extract_transcripts(specific_day="2026-02-05")
            assert result == {}

    def test_empty_when_no_pending(self):
        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[]):
            result = extract_transcripts()
            assert result == {}


# =============================================================================
# get_pending_days Tests
# =============================================================================


class TestGetPendingDays:
    def test_returns_sorted_dates(self):
        sessions = [
            make_session_info("s1"),
            make_session_info("s2"),
        ]

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=sessions), \
             mock.patch("transcript_ops.get_session_date", side_effect=["2026-02-05", "2026-02-03"]):
            result = get_pending_days()
            assert result == ["2026-02-03", "2026-02-05"]

    def test_deduplicates_dates(self):
        """Multiple sessions on same day produce one date entry."""
        sessions = [
            make_session_info("s1"),
            make_session_info("s2"),
        ]

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=sessions), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-05"):
            result = get_pending_days()
            assert result == ["2026-02-05"]

    def test_empty_when_all_captured(self):
        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[]):
            result = get_pending_days()
            assert result == []


# =============================================================================
# format_transcripts_for_output — Line Budget Tests
# =============================================================================


class TestFormatWithLineBudget:
    def _make_daily_data(self, num_messages: int, content_lines: int = 1) -> dict:
        """Create daily_data with one session containing N messages."""
        msg_content = "\n".join(f"Line {i}" for i in range(content_lines))
        messages = [
            {"role": "assistant", "content": msg_content}
            for _ in range(num_messages)
        ]
        return {
            "2026-02-05": [{
                "session_id": "s1",
                "filepath": "/tmp/test.jsonl",
                "project_path": None,
                "message_count": num_messages,
                "messages": messages,
            }]
        }

    def test_truncation_applied(self):
        """Sessions exceeding per-session budget get head/tail treatment."""
        # Create a session that produces many output lines
        daily_data = self._make_daily_data(num_messages=20, content_lines=10)
        output_no_budget = format_transcripts_for_output(daily_data)
        output_with_budget = format_transcripts_for_output(daily_data, total_line_budget=30)

        # Budget version should be shorter
        assert len(output_with_budget.split("\n")) < len(output_no_budget.split("\n"))
        assert "truncated" in output_with_budget

    def test_small_sessions_untouched(self):
        """Sessions under budget pass through without truncation."""
        daily_data = self._make_daily_data(num_messages=2, content_lines=1)
        output = format_transcripts_for_output(daily_data, total_line_budget=500)
        assert "truncated" not in output

    def test_budget_floor_of_15(self):
        """Even with tiny total budget, per-session floor is 15 lines."""
        daily_data = self._make_daily_data(num_messages=50, content_lines=5)
        output = format_transcripts_for_output(daily_data, total_line_budget=5)
        # Output should still have content (not completely empty)
        lines = output.strip().split("\n")
        assert len(lines) >= 15


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
