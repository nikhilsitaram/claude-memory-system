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
    format_transcripts_for_output,
    parse_jsonl_file_from_line,
)

# =============================================================================
# get_recent_days Tests
# =============================================================================


class TestGetRecentDays:
    def test_returns_sorted_dates(self):
        from transcript_ops import get_recent_days

        sessions = [make_session_info("s1"), make_session_info("s2")]
        with mock.patch("transcript_ops.list_recent_sessions", return_value=sessions), \
             mock.patch("transcript_ops.get_session_date", side_effect=["2026-02-05", "2026-02-03"]):
            result = get_recent_days()
            assert result == ["2026-02-03", "2026-02-05"]

    def test_deduplicates_dates(self):
        from transcript_ops import get_recent_days

        sessions = [make_session_info("s1"), make_session_info("s2")]
        with mock.patch("transcript_ops.list_recent_sessions", return_value=sessions), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-05"):
            result = get_recent_days()
            assert result == ["2026-02-05"]

    def test_empty_when_none_recent(self):
        from transcript_ops import get_recent_days

        with mock.patch("transcript_ops.list_recent_sessions", return_value=[]):
            result = get_recent_days()
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
# should_skip_message Tests
# =============================================================================


class TestShouldSkipMessage:
    """Test synthesis artifact filtering in should_skip_message."""

    def test_skip_daily_format_artifacts(self):
        from transcript_ops import should_skip_message
        assert should_skip_message("===DAILY:2026-02-24===\n## Actions\n- stuff")

    def test_skip_route_format_artifacts(self):
        from transcript_ops import should_skip_message
        assert should_skip_message("===ROUTE:global:Key Lessons===\n- stuff")

    def test_skip_project_format_artifacts(self):
        from transcript_ops import should_skip_message
        assert should_skip_message("===PROJECT:swyfft===\n- [implement] stuff")

    def test_normal_message_not_skipped(self):
        from transcript_ops import should_skip_message
        assert not should_skip_message("I fixed the bug in the login page")


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

        with mock.patch("transcript_ops.list_recent_sessions", return_value=[session]), \
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

        with mock.patch("transcript_ops.list_recent_sessions", return_value=[session]), \
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

        with mock.patch("transcript_ops.list_recent_sessions", return_value=[session]), \
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

        with mock.patch("transcript_ops.list_recent_sessions", return_value=sessions), \
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

        with mock.patch("transcript_ops.list_recent_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        sess = result["2026-02-22"][0]
        assert "current_offset" in sess
        assert "current_lines" in sess
        assert sess["current_offset"] == transcript.stat().st_size
        assert sess["current_lines"] >= 1

    def test_exclude_session_id(self, tmp_path):
        """Exclude session ID is forwarded to list_recent_sessions."""
        from transcript_ops import extract_transcripts_incremental

        state = {"sessions": {}}

        with mock.patch("transcript_ops.list_recent_sessions", return_value=[]) as mock_lrs, \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            extract_transcripts_incremental(state, exclude_session_id="skip-me")

        mock_lrs.assert_called_once()
        call_kwargs = mock_lrs.call_args
        # Check exclude_session_id was passed
        assert call_kwargs[1].get("exclude_session_id") == "skip-me" or \
               (len(call_kwargs[0]) > 0 and call_kwargs[0][0] == "skip-me")

    def test_min_messages_skips_short_session(self, tmp_path):
        """Session with fewer messages than min_session_messages is excluded."""
        from memory_utils import DEFAULT_SETTINGS
        from transcript_ops import extract_transcripts_incremental

        threshold = DEFAULT_SETTINGS["synthesis"]["minSessionMessages"]
        transcript = tmp_path / "short.jsonl"
        # Create a session well below the threshold
        transcript.write_text(make_jsonl_content([
            ("assistant", "Hello"),
            ("assistant", "Bye"),
        ]))
        session = make_session_info(
            session_id="short-sess",
            transcript_path=transcript,
            file_size=transcript.stat().st_size,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {}}

        with mock.patch("transcript_ops.list_recent_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state, min_session_messages=threshold)

        assert result == {}

    def test_min_messages_keeps_long_session(self, tmp_path):
        """Session meeting min_session_messages threshold is included."""
        from memory_utils import DEFAULT_SETTINGS
        from transcript_ops import extract_transcripts_incremental

        threshold = DEFAULT_SETTINGS["synthesis"]["minSessionMessages"]
        messages = [("assistant", f"Message {i}") for i in range(threshold + 5)]
        transcript = tmp_path / "long.jsonl"
        transcript.write_text(make_jsonl_content(messages))
        session = make_session_info(
            session_id="long-sess",
            transcript_path=transcript,
            file_size=transcript.stat().st_size,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {}}

        with mock.patch("transcript_ops.list_recent_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state, min_session_messages=threshold)

        assert "2026-02-22" in result
        assert result["2026-02-22"][0]["session_id"] == "long-sess"

    def test_min_messages_zero_disables_filter(self, tmp_path):
        """min_session_messages=0 (default) includes all sessions."""
        from transcript_ops import extract_transcripts_incremental

        transcript = tmp_path / "tiny.jsonl"
        transcript.write_text(make_jsonl_content([("assistant", "Hi")]))
        session = make_session_info(
            session_id="tiny-sess",
            transcript_path=transcript,
            file_size=transcript.stat().st_size,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {}}

        with mock.patch("transcript_ops.list_recent_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state, min_session_messages=0)

        assert "2026-02-22" in result

    def test_min_messages_filters_mixed_sessions(self, tmp_path):
        """Only sessions above threshold survive; short ones are dropped."""
        from memory_utils import DEFAULT_SETTINGS
        from transcript_ops import extract_transcripts_incremental

        threshold = DEFAULT_SETTINGS["synthesis"]["minSessionMessages"]

        # Short session (2 messages — below threshold)
        t1 = tmp_path / "short.jsonl"
        t1.write_text(make_jsonl_content([("assistant", "A"), ("assistant", "B")]))

        # Long session (threshold + 2 messages — above threshold)
        t2 = tmp_path / "long.jsonl"
        t2.write_text(make_jsonl_content([("assistant", f"Msg {i}") for i in range(threshold + 2)]))

        sessions = [
            make_session_info("short", t1, t1.stat().st_size, created=datetime(2026, 2, 22, 10, 0, tzinfo=timezone.utc)),
            make_session_info("long", t2, t2.stat().st_size, created=datetime(2026, 2, 22, 11, 0, tzinfo=timezone.utc)),
        ]
        state = {"sessions": {}}

        with mock.patch("transcript_ops.list_recent_sessions", return_value=sessions), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state, min_session_messages=threshold)

        day_sessions = result["2026-02-22"]
        assert len(day_sessions) == 1
        assert day_sessions[0]["session_id"] == "long"


# =============================================================================
# format_transcripts_incremental Tests
# =============================================================================


class TestFormatTranscriptsIncremental:
    def _make_session(self, sid, mode, messages, content_lines=1):
        return {
            "session_id": sid,
            "filepath": "/tmp/test.jsonl",
            "project_path": None,
            "message_count": len(messages),
            "messages": [{"role": "assistant", "content": m} for m in messages],
            "mode": mode,
            "current_offset": 1000,
            "current_lines": 10,
        }

    def test_full_session_labeled(self):
        """Full sessions have standard session header."""
        from transcript_ops import format_transcripts_incremental

        data = {"2026-02-22": [self._make_session("s1", "full", ["Hello"])]}
        output = format_transcripts_incremental(data)
        assert "Session: s1" in output
        assert "(continued" not in output

    def test_delta_session_labeled(self):
        """Delta sessions have (continued) marker in header."""
        from transcript_ops import format_transcripts_incremental

        data = {"2026-02-22": [self._make_session("s1", "delta", ["New msg"])]}
        output = format_transcripts_incremental(data)
        assert "Session: s1" in output
        assert "(continued" in output

    def test_budget_applied(self):
        """Line budget still works with incremental format."""
        from transcript_ops import format_transcripts_incremental

        msgs = [f"Message {i}" for i in range(50)]
        data = {"2026-02-22": [self._make_session("s1", "full", msgs)]}
        output_no_budget = format_transcripts_incremental(data)
        output_with_budget = format_transcripts_incremental(data, total_line_budget=30)
        assert len(output_with_budget.split("\n")) < len(output_no_budget.split("\n"))

    def test_mixed_modes_in_day(self):
        """Both full and delta sessions in same day get correct labels."""
        from transcript_ops import format_transcripts_incremental

        data = {"2026-02-22": [
            self._make_session("s1", "full", ["First session"]),
            self._make_session("s2", "delta", ["Continued session"]),
        ]}
        output = format_transcripts_incremental(data)
        lines = output.split("\n")
        # Find session header lines
        s1_header = [ln for ln in lines if "Session: s1" in ln]
        s2_header = [ln for ln in lines if "Session: s2" in ln]
        assert len(s1_header) == 1
        assert len(s2_header) == 1
        assert "(continued" not in s1_header[0]
        assert "(continued" in s2_header[0]

    def test_day_header_present(self):
        """Output includes DAY header with session/message counts."""
        from transcript_ops import format_transcripts_incremental

        data = {"2026-02-22": [self._make_session("s1", "full", ["Hello", "World"])]}
        output = format_transcripts_incremental(data)
        assert "DAY: 2026-02-22" in output
        assert "1 sessions" in output
        assert "2 messages" in output


# =============================================================================
# format_transcripts_incremental Project Header Tests
# =============================================================================


class TestFormatTranscriptsIncrementalProjectHeader:
    def test_session_header_includes_project_name(self):
        from transcript_ops import format_transcripts_incremental

        daily_data = {
            "2026-02-23": [{
                "session_id": "abc123",
                "message_count": 1,
                "messages": [{"role": "assistant", "content": "hello"}],
                "mode": "full",
                "project_name": "cartwheel",
            }]
        }
        result = format_transcripts_incremental(daily_data)
        assert "[project: cartwheel]" in result

    def test_session_header_global_when_no_project(self):
        from transcript_ops import format_transcripts_incremental

        daily_data = {
            "2026-02-23": [{
                "session_id": "abc123",
                "message_count": 1,
                "messages": [{"role": "assistant", "content": "hello"}],
                "mode": "full",
                "project_name": None,
            }]
        }
        result = format_transcripts_incremental(daily_data)
        assert "[project: global]" in result


# =============================================================================
# _resolve_project_name Fallback Tests
# =============================================================================


class TestResolveProjectName:
    """Test _resolve_project_name with project_hash fallback."""

    def setup_method(self):
        """Clear the projects index cache before each test."""
        from memory_utils import _clear_projects_index_cache
        _clear_projects_index_cache()

    def _make_index(self, projects):
        return {"projects": projects, "version": 1}

    def test_resolves_via_project_path(self, tmp_path):
        """Direct project_path lookup (existing behavior)."""
        from transcript_ops import _resolve_project_name

        index = self._make_index({
            "/home/user/myproject": {
                "name": "myproject",
                "encodedPaths": ["-home-user-myproject"],
            }
        })
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name("/home/user/myproject")
        assert result == "myproject"

    def test_falls_back_to_project_hash(self, tmp_path):
        """When project_path is None, resolve via project_hash in encodedPaths."""
        from transcript_ops import _resolve_project_name

        index = self._make_index({
            "/home/user/myproject": {
                "name": "myproject",
                "encodedPaths": ["-home-user-myproject"],
            }
        })
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(None, project_hash="-home-user-myproject")
        assert result == "myproject"

    def test_hash_fallback_matches_worktree_encoded_path(self, tmp_path):
        """Worktree encoded paths are in the same project's encodedPaths list."""
        from transcript_ops import _resolve_project_name

        index = self._make_index({
            "/home/user/myproject": {
                "name": "myproject",
                "encodedPaths": [
                    "-home-user-myproject",
                    "-home-user-myproject--worktrees-feature-x",
                ],
            }
        })
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(None, project_hash="-home-user-myproject--worktrees-feature-x")
        assert result == "myproject"

    def test_returns_none_when_no_path_and_no_hash(self, tmp_path):
        """Both None returns None."""
        from transcript_ops import _resolve_project_name

        result = _resolve_project_name(None, project_hash=None)
        assert result is None

    def test_returns_none_when_hash_not_in_index(self, tmp_path):
        """Unknown project_hash returns None."""
        from transcript_ops import _resolve_project_name

        index = self._make_index({
            "/home/user/myproject": {
                "name": "myproject",
                "encodedPaths": ["-home-user-myproject"],
            }
        })
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(None, project_hash="-home-user-unknown")
        assert result is None

    def test_project_path_takes_precedence_over_hash(self, tmp_path):
        """When both are available, project_path wins."""
        from transcript_ops import _resolve_project_name

        index = self._make_index({
            "/home/user/alpha": {
                "name": "alpha",
                "encodedPaths": ["-home-user-alpha"],
            },
            "/home/user/beta": {
                "name": "beta",
                "encodedPaths": ["-home-user-beta"],
            },
        })
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name("/home/user/alpha", project_hash="-home-user-beta")
        assert result == "alpha"


# =============================================================================
# _resolve_project_name Worktree Prefix Fallback Tests
# =============================================================================


class TestResolveProjectNameWorktreePrefix:
    """Test that unindexed worktrees resolve via prefix match."""

    def setup_method(self):
        """Clear the projects index cache before each test."""
        from memory_utils import _clear_projects_index_cache
        _clear_projects_index_cache()

    def test_new_worktree_resolves_to_parent(self, tmp_path):
        """ts-phase-4 should resolve to 'investing' when ts-phase-1..3 are indexed."""
        from transcript_ops import _resolve_project_name
        index = {
            "projects": {
                "/home/user/investing": {
                    "name": "investing",
                    "encodedPaths": [
                        "-home-user-investing",
                        "-home-user-investing--worktrees-ts-phase-1",
                        "-home-user-investing--worktrees-ts-phase-3",
                    ],
                }
            }
        }
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(
                None, project_hash="-home-user-investing--worktrees-ts-phase-4"
            )
        assert result == "investing"

    def test_exact_match_takes_precedence_over_prefix(self, tmp_path):
        """If exact match exists, don't fall through to prefix."""
        from transcript_ops import _resolve_project_name
        index = {
            "projects": {
                "/home/user/investing": {
                    "name": "investing",
                    "encodedPaths": [
                        "-home-user-investing--worktrees-ts-phase-4",
                    ],
                }
            }
        }
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(
                None, project_hash="-home-user-investing--worktrees-ts-phase-4"
            )
        assert result == "investing"

    def test_no_prefix_match_returns_none(self, tmp_path):
        """Completely unrelated hash returns None."""
        from transcript_ops import _resolve_project_name
        index = {
            "projects": {
                "/home/user/investing": {
                    "name": "investing",
                    "encodedPaths": ["-home-user-investing"],
                }
            }
        }
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(
                None, project_hash="-home-user-totally-different"
            )
        assert result is None

    def test_worktree_prefix_matches_base_project(self, tmp_path):
        """Worktree hash shares base encoded path prefix with project."""
        from transcript_ops import _resolve_project_name
        index = {
            "projects": {
                "/home/user/myproject": {
                    "name": "myproject",
                    "encodedPaths": ["-home-user-myproject"],
                }
            }
        }
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(
                None, project_hash="-home-user-myproject--worktrees-feature-x"
            )
        assert result == "myproject"

    def test_non_worktree_hash_no_prefix_fallback(self, tmp_path):
        """Hash without --worktrees- does not trigger prefix matching."""
        from transcript_ops import _resolve_project_name
        index = {
            "projects": {
                "/home/user/myproject": {
                    "name": "myproject",
                    "encodedPaths": ["-home-user-myproject"],
                }
            }
        }
        with mock.patch("memory_utils.load_json_file", return_value=index), \
             mock.patch("memory_utils.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(
                None, project_hash="-home-user-myproject-subfolder"
            )
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
