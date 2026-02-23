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
    parse_jsonl_file_from_line,
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


# =============================================================================
# parse_jsonl_file_from_line Tests
# =============================================================================


class TestParseJsonlFileFromLine:
    def test_full_parse_when_start_line_zero(self, tmp_path):
        """start_line=0 reads all messages (same as parse_jsonl_file)."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("assistant", "First message"),
            ("assistant", "Second message"),
        ]))
        messages, total_lines = parse_jsonl_file_from_line(transcript, start_line=0)
        assert len(messages) == 2
        assert total_lines == 2

    def test_delta_from_line(self, tmp_path):
        """start_line=1 skips first line, parses remainder."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("assistant", "Old message"),
            ("assistant", "New message"),
        ]))
        messages, total_lines = parse_jsonl_file_from_line(transcript, start_line=1)
        assert len(messages) == 1
        assert "New message" in messages[0]["content"]
        assert total_lines == 2

    def test_start_line_beyond_file(self, tmp_path):
        """start_line past EOF returns empty messages and current line count."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("assistant", "Only message"),
        ]))
        messages, total_lines = parse_jsonl_file_from_line(transcript, start_line=100)
        assert messages == []
        assert total_lines == 1

    def test_filters_skippable_messages(self, tmp_path):
        """should_skip_message filter still applies to delta messages."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("assistant", "Old message"),
            ("assistant", "<system-reminder>skip this</system-reminder>"),
            ("assistant", "New real message"),
        ]))
        messages, total_lines = parse_jsonl_file_from_line(transcript, start_line=1)
        assert len(messages) == 1
        assert "New real message" in messages[0]["content"]
        assert total_lines == 3

    def test_returns_total_lines_not_parsed_lines(self, tmp_path):
        """total_lines counts non-blank JSONL lines only."""
        transcript = tmp_path / "session.jsonl"
        content = make_jsonl_content([("assistant", "msg1"), ("assistant", "msg2")])
        content += "\n"  # trailing blank line
        transcript.write_text(content)
        _, total_lines = parse_jsonl_file_from_line(transcript, start_line=0)
        # total_lines = non-blank JSONL lines (blank lines are skipped in line count)
        assert total_lines == 2


# =============================================================================
# extract_transcripts_incremental Tests
# =============================================================================


class TestExtractTranscriptsIncremental:
    def test_new_session_full_extract(self, tmp_path):
        """Session not in state gets mode='full' with all messages."""
        from transcript_ops import extract_transcripts_incremental

        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("assistant", "Hello"),
            ("assistant", "World"),
        ]))
        session = make_session_info(
            session_id="new-sess",
            transcript_path=transcript,
            file_size=transcript.stat().st_size,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {}}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        assert "2026-02-22" in result
        sess = result["2026-02-22"][0]
        assert sess["mode"] == "full"
        assert sess["message_count"] == 2

    def test_unchanged_session_skipped(self, tmp_path):
        """Session with same file size as state is skipped entirely."""
        from transcript_ops import extract_transcripts_incremental

        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([("assistant", "Hello")]))
        fsize = transcript.stat().st_size

        session = make_session_info(
            session_id="old-sess",
            transcript_path=transcript,
            file_size=fsize,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {"old-sess": {"offset": fsize, "lines": 1, "last_synthesized": "2026-02-22T10:00:00Z"}}}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        assert result == {}  # no content to process

    def test_grown_session_delta_extract(self, tmp_path):
        """Session that grew since state gets mode='delta' with only new messages."""
        from transcript_ops import extract_transcripts_incremental

        transcript = tmp_path / "session.jsonl"
        # Write initial content
        initial_content = make_jsonl_content([("assistant", "Old message")])
        transcript.write_text(initial_content)
        initial_size = transcript.stat().st_size

        # Append new content
        with open(transcript, "a") as f:
            f.write(make_jsonl_content([("assistant", "New message")]))
        new_size = transcript.stat().st_size

        session = make_session_info(
            session_id="grown-sess",
            transcript_path=transcript,
            file_size=new_size,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {"grown-sess": {"offset": initial_size, "lines": 1, "last_synthesized": "2026-02-22T10:00:00Z"}}}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        assert "2026-02-22" in result
        sess = result["2026-02-22"][0]
        assert sess["mode"] == "delta"
        assert sess["message_count"] == 1  # only new message
        assert "New message" in sess["messages"][0]["content"]

    def test_mixed_sessions_same_day(self, tmp_path):
        """Mix of new, unchanged, and grown sessions on same day."""
        from transcript_ops import extract_transcripts_incremental

        # Unchanged session
        t1 = tmp_path / "s1.jsonl"
        t1.write_text(make_jsonl_content([("assistant", "Old")]))

        # Grown session
        t2 = tmp_path / "s2.jsonl"
        initial = make_jsonl_content([("assistant", "Was here")])
        t2.write_text(initial)
        initial_size = t2.stat().st_size
        with open(t2, "a") as f:
            f.write(make_jsonl_content([("assistant", "Am new")]))

        # New session
        t3 = tmp_path / "s3.jsonl"
        t3.write_text(make_jsonl_content([("assistant", "Brand new")]))

        sessions = [
            make_session_info("s1", t1, t1.stat().st_size, created=datetime(2026, 2, 22, 10, 0, tzinfo=timezone.utc)),
            make_session_info("s2", t2, t2.stat().st_size, created=datetime(2026, 2, 22, 11, 0, tzinfo=timezone.utc)),
            make_session_info("s3", t3, t3.stat().st_size, created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc)),
        ]

        state = {"sessions": {
            "s1": {"offset": t1.stat().st_size, "lines": 1, "last_synthesized": "2026-02-22T10:00:00Z"},
            "s2": {"offset": initial_size, "lines": 1, "last_synthesized": "2026-02-22T10:00:00Z"},
        }}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=sessions), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        day_sessions = result["2026-02-22"]
        modes = {s["session_id"]: s["mode"] for s in day_sessions}
        assert "s1" not in modes  # unchanged, skipped
        assert modes["s2"] == "delta"
        assert modes["s3"] == "full"

    def test_returns_session_offsets(self, tmp_path):
        """Result includes session_offsets dict for state update."""
        from transcript_ops import extract_transcripts_incremental

        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([("assistant", "Hello")]))

        session = make_session_info(
            session_id="sess-1",
            transcript_path=transcript,
            file_size=transcript.stat().st_size,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {}}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        sess = result["2026-02-22"][0]
        assert "current_offset" in sess
        assert "current_lines" in sess
        assert sess["current_offset"] == transcript.stat().st_size
        assert sess["current_lines"] >= 1

    def test_exclude_session_id(self, tmp_path):
        """Exclude session ID is forwarded to list_pending_sessions."""
        from transcript_ops import extract_transcripts_incremental

        state = {"sessions": {}}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[]) as mock_lps, \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            extract_transcripts_incremental(state, exclude_session_id="skip-me")

        mock_lps.assert_called_once()
        call_kwargs = mock_lps.call_args
        # Check exclude_session_id was passed
        assert call_kwargs[1].get("exclude_session_id") == "skip-me" or \
               (len(call_kwargs[0]) > 1 and call_kwargs[0][1] == "skip-me")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
