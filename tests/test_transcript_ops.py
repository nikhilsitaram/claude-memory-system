#!/usr/bin/env python3
"""
Unit tests for transcript_ops.py

Run with: python -m pytest tests/test_transcript_ops.py -v
"""

from datetime import datetime, timezone
from unittest import mock

import pytest
from helpers import make_jsonl_content, make_session_info  # noqa: I001
from transcript_ops import (
    extract_transcripts,
    format_transcripts_for_output,
    get_pending_days,
)

# =============================================================================
# extract_transcripts Tests
# =============================================================================


class TestExtractTranscripts:
    def test_extracts_specific_day(self, tmp_path):
        """Filters to only the requested day."""
        # Create a transcript file with assistant content
        transcript = tmp_path / "session.jsonl"
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
    def _make_daily_data(self, num_messages, content_lines=1):
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


# =============================================================================
# format_transcripts_for_output — Consistency Tests
# =============================================================================


class TestFormatTranscriptsConsistency:
    def test_no_budget_matches_large_budget(self):
        """No budget and a large budget (no truncation) should produce identical output."""
        messages = [{"role": "assistant", "content": f"Line {i}"} for i in range(5)]
        data = {"2026-01-01": [{"session_id": "s1", "filepath": "/tmp/t.jsonl",
                                "project_path": None, "message_count": 5, "messages": messages}]}

        full = format_transcripts_for_output(data)
        with_budget = format_transcripts_for_output(data, total_line_budget=10000)

        assert full == with_budget, "Budget that doesn't trigger truncation should produce identical output"

    def test_multiline_content_no_budget_matches_large_budget(self):
        """Multi-line message content should also be identical across code paths."""
        messages = [
            {"role": "assistant", "content": "First line\nSecond line\nThird line"},
            {"role": "assistant", "content": "Another\nmulti-line\nblock"},
        ]
        data = {"2026-01-01": [{"session_id": "s1", "filepath": "/tmp/t.jsonl",
                                "project_path": None, "message_count": 2, "messages": messages}]}

        full = format_transcripts_for_output(data)
        with_budget = format_transcripts_for_output(data, total_line_budget=10000)

        assert full == with_budget


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
