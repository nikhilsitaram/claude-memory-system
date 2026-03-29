#!/usr/bin/env python3
"""
Unit tests for load_memory.py

Run with: python -m pytest tests/test_load_memory.py -v
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from load_memory import (
    _batch_update_data_point_access,
    _build_embedded_files,
    _build_preextracted_prompt,
    _build_synthesis_instructions_v3,
    _build_synthesis_prompt,
    _cosine_similarity,
    _deserialize_vector,
    _filter_by_cosine_dedup,
    _load_from_db,
    COSINE_DEDUP_THRESHOLD,
    pre_extract_transcripts_incremental,
    should_synthesize,
    write_synthesis_prompt,
)
from storage import (
    SCHEMA_DDL,
    DataPointRow,
    EdgeRow,
    ensure_db,
    insert_data_point,
    insert_edge,
    invalidate_edge,
)


def _make_v2_db(db_path):
    """Create a v2 DB for testing access tracking operations."""
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA_DDL)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    return conn

# =============================================================================
# should_synthesize Tests
# =============================================================================


class TestShouldSynthesize:
    def _make_settings(self, interval_hours: int = 2) -> dict:
        return {"synthesis": {"intervalHours": interval_hours}}

    def test_true_when_no_file(self):
        """Returns True when .last-synthesis file doesn't exist."""
        with mock.patch("load_memory.get_last_synthesis_file") as mock_f:
            mock_f.return_value = Path("/nonexistent/.last-synthesis")
            assert should_synthesize(self._make_settings()) is True

    def test_true_on_new_day(self, tmp_path):
        """Returns True when last synthesis was on a different UTC day."""
        ts_file = tmp_path / ".last-synthesis"
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        ts_file.write_text(yesterday.isoformat())

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f:
            mock_f.return_value = ts_file
            assert should_synthesize(self._make_settings()) is True

    def test_false_within_interval(self, tmp_path):
        """Returns False when last synthesis is same day and within interval."""
        ts_file = tmp_path / ".last-synthesis"
        fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        thirty_min_ago = fixed_now - timedelta(minutes=30)
        ts_file.write_text(thirty_min_ago.isoformat())

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f, \
             mock.patch("load_memory.datetime") as mock_dt:
            mock_f.return_value = ts_file
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert should_synthesize(self._make_settings()) is False

    def test_true_after_interval(self, tmp_path):
        """Returns True when same day but past intervalHours."""
        ts_file = tmp_path / ".last-synthesis"
        fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        three_hours_ago = fixed_now - timedelta(hours=3)
        ts_file.write_text(three_hours_ago.isoformat())

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f, \
             mock.patch("load_memory.datetime") as mock_dt:
            mock_f.return_value = ts_file
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert should_synthesize(self._make_settings(interval_hours=2)) is True

    def test_true_on_invalid_file(self, tmp_path):
        """Returns True when file contains invalid content."""
        ts_file = tmp_path / ".last-synthesis"
        ts_file.write_text("not a valid timestamp")

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f:
            mock_f.return_value = ts_file
            assert should_synthesize(self._make_settings()) is True

    def test_respects_custom_interval(self, tmp_path):
        """Uses intervalHours from settings, not hardcoded default."""
        ts_file = tmp_path / ".last-synthesis"
        # Use fixed time to avoid UTC midnight edge case
        fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        three_hours_ago = fixed_now - timedelta(hours=3)
        ts_file.write_text(three_hours_ago.isoformat())

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f, \
             mock.patch("load_memory.datetime") as mock_dt:
            mock_f.return_value = ts_file
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            assert should_synthesize(self._make_settings(interval_hours=4)) is False

    def test_eager_timestamp_prevents_concurrent_synthesis(self, tmp_path):
        """Second caller sees eager timestamp and skips synthesis (race condition fix)."""
        ts_file = tmp_path / ".last-synthesis"
        fixed_now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

        with mock.patch("load_memory.get_last_synthesis_file") as mock_f, \
             mock.patch("load_memory.datetime") as mock_dt:
            mock_f.return_value = ts_file
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            # First caller: no file exists, should synthesize
            assert should_synthesize(self._make_settings()) is True

            # Simulate eager write (what main() now does after should_synthesize returns True)
            ts_file.write_text(fixed_now.isoformat())

            # Second caller moments later: sees fresh timestamp, should NOT synthesize
            assert should_synthesize(self._make_settings()) is False


# =============================================================================
# load_global_memory Tests
# =============================================================================


# =============================================================================
# Synthesis Prompt [routed] Marker Tests
# =============================================================================




# =============================================================================
# _build_preextracted_prompt Tests
# =============================================================================


class TestBuildPreextractedPrompt:
    """Tests for _build_preextracted_prompt with embedded content and structured output."""

    def test_embeds_transcript_content(self, tmp_path):
        """Transcript content should be embedded inline, not as file paths."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("SESSION DATA HERE")

        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": str(extract_file)},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={"transcripts": {"2026-02-22": "SESSION DATA HERE"}},
        )
        assert "SESSION DATA HERE" in prompt
        assert "===PROJECT:" in prompt  # structured output format
        assert "===END===" in prompt
        assert "===DAILY:" not in prompt  # old format removed
        assert "===ROUTE:" not in prompt  # old format removed
        assert "Step 1: Read" not in prompt  # no Read instructions

    def test_no_vector_memories_shows_placeholder(self, tmp_path):
        """Without vector memories, prompt shows '(no existing memories)' placeholder."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={
                "transcripts": {"2026-02-22": "transcript"},
            },
        )
        assert "(no existing memories)" in prompt

    def test_structured_output_instructions(self, tmp_path):
        """Prompt should include delivery instructions and prohibitions."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={"transcripts": {"2026-02-22": "data"}},
        )
        assert "synthesis.py apply" in prompt
        assert "Output only the structured format" in prompt
        assert "Only use the Write and Bash tools" in prompt

    def test_no_auto_extract_fallback(self):
        """_build_autoextract_prompt should no longer exist."""
        import load_memory
        assert not hasattr(load_memory, "_build_autoextract_prompt")

    def test_contains_synthesis_instructions(self, tmp_path):
        """Synthesis instructions block is included in the prompt."""
        extract = tmp_path / "extract.txt"
        extract.write_text("content\n")

        prompt = _build_preextracted_prompt(
            ["2026-02-01"],
            {"2026-02-01": str(extract)},
            "MY_CUSTOM_INSTRUCTIONS",
        )
        assert "MY_CUSTOM_INSTRUCTIONS" in prompt

    def test_contains_extract_references(self, tmp_path):
        """Prompt includes extract paths for synthesis.py apply (no sidecars)."""
        extract = tmp_path / "extract-2026-02-01.txt"
        extract.write_text("content\n")

        prompt = _build_preextracted_prompt(
            ["2026-02-01"],
            {"2026-02-01": str(extract)},
            "instructions",
        )
        assert str(extract) in prompt
        assert "synthesis.py apply" in prompt
        assert "--sidecars" not in prompt

    def test_contains_all_dates(self, tmp_path):
        """All pending dates appear in the prompt."""
        f1 = tmp_path / "extract-01.txt"
        f2 = tmp_path / "extract-02.txt"
        f1.write_text("a\n")
        f2.write_text("b\n")

        prompt = _build_preextracted_prompt(
            ["2026-02-01", "2026-02-02"],
            {"2026-02-01": str(f1), "2026-02-02": str(f2)},
            "instructions",
        )
        assert "2026-02-01" in prompt
        assert "2026-02-02" in prompt

    def test_reads_from_extract_file_when_not_embedded(self, tmp_path):
        """Falls back to reading extract file when transcripts not in embedded_files."""
        extract = tmp_path / "extract.txt"
        extract.write_text("CONTENT FROM FILE")

        prompt = _build_preextracted_prompt(
            ["2026-02-01"],
            {"2026-02-01": str(extract)},
            "instructions",
        )
        assert "CONTENT FROM FILE" in prompt

    def test_handles_unreadable_file(self):
        """Shows unavailable message when extract file doesn't exist and not embedded."""
        prompt = _build_preextracted_prompt(
            ["2026-02-01"],
            {"2026-02-01": "/nonexistent/file.txt"},
            "instructions",
        )
        assert "(transcript unavailable)" in prompt


# =============================================================================
# _build_synthesis_prompt integration Tests
# =============================================================================


class TestBuildSynthesisPromptIntegration:
    """Test the orchestrator dispatches correctly to _build_preextracted_prompt."""

    def test_uses_v3_instructions(self, tmp_path):
        """Prompt includes v3 MEMORY_OPS instructions (not v2 PROJECT blocks)."""
        transcript = tmp_path / "extract.txt"
        transcript.write_text("content\n")

        prompt = _build_synthesis_prompt(
            ["2026-02-01"],
            extracted_files={"2026-02-01": str(transcript)},
        )
        # v3 instructions use MEMORY_OPS format
        assert "MEMORY_OPS" in prompt
        assert "salience" in prompt.lower()

    def test_passes_embedded_files(self, tmp_path):
        """embedded_files are forwarded to _build_preextracted_prompt."""
        transcript = tmp_path / "extract.txt"
        transcript.write_text("content\n")

        prompt = _build_synthesis_prompt(
            ["2026-02-01"],
            extracted_files={"2026-02-01": str(transcript)},
            embedded_files={
                "transcripts": {"2026-02-01": "EMBEDDED TRANSCRIPT DATA"},
            },
        )
        assert "EMBEDDED TRANSCRIPT DATA" in prompt

    def test_structured_output_format(self, tmp_path):
        """Prompt includes structured output delimiters (v3 uses MEMORY_OPS + END)."""
        transcript = tmp_path / "extract.txt"
        transcript.write_text("content\n")

        prompt = _build_synthesis_prompt(
            ["2026-02-01"],
            extracted_files={"2026-02-01": str(transcript)},
        )
        assert "===MEMORY_OPS===" in prompt
        assert "===END===" in prompt
        assert "===DAILY:" not in prompt
        assert "===ROUTE:" not in prompt


# =============================================================================
# _build_embedded_files Tests
# =============================================================================


class TestBuildEmbeddedFiles:
    """Tests for _build_embedded_files helper (transcript-only)."""

    def test_reads_transcript_files(self, tmp_path):
        """Transcript extract files are read into embedded dict."""
        extract = tmp_path / "extract-2026-02-01.txt"
        extract.write_text("transcript content")

        result = _build_embedded_files({"2026-02-01": str(extract)})

        assert result["transcripts"]["2026-02-01"] == "transcript content"

    def test_handles_missing_transcript_file(self, tmp_path):
        """Missing transcript files are silently skipped."""
        result = _build_embedded_files({"2026-02-01": "/nonexistent/file.txt"})

        assert "2026-02-01" not in result["transcripts"]

    def test_returns_only_transcripts_key(self, tmp_path):
        """Result dict has only 'transcripts' key (no global_ltm, project_ltms)."""
        extract = tmp_path / "extract.txt"
        extract.write_text("content")

        result = _build_embedded_files({"2026-02-01": str(extract)})

        assert set(result.keys()) == {"transcripts"}

    def test_empty_input(self):
        """Empty input returns empty transcripts dict."""
        result = _build_embedded_files({})
        assert result == {"transcripts": {}}



# =============================================================================
# Output filename PID fix Tests
# =============================================================================


class TestOutputFilenamePid:
    """Verify delivery instructions use deterministic PID-based filename."""

    def test_prompt_uses_pid_not_dollar_dollar(self, tmp_path):
        """Output filename should use os.getpid(), not $$."""
        extract = tmp_path / "extract.txt"
        extract.write_text("content\n")

        prompt = _build_preextracted_prompt(
            ["2026-02-01"],
            {"2026-02-01": str(extract)},
            "instructions",
        )
        # Should NOT contain literal $$
        assert "/tmp/synthesis-output-$$" not in prompt
        # Should contain the actual PID
        import os
        assert f"/tmp/synthesis-output-{os.getpid()}.txt" in prompt

    def test_write_and_bash_use_same_filename(self, tmp_path):
        """Both Write and Bash instructions reference identical filename."""
        extract = tmp_path / "extract.txt"
        extract.write_text("content\n")

        prompt = _build_preextracted_prompt(
            ["2026-02-01"],
            {"2026-02-01": str(extract)},
            "instructions",
        )
        import os
        expected_filename = f"/tmp/synthesis-output-{os.getpid()}.txt"
        # Should appear exactly twice: once in Write, once in Bash
        assert prompt.count(expected_filename) == 2


# =============================================================================
# pre_extract_transcripts_incremental Tests
# =============================================================================


class TestPreExtractTranscriptsIncremental:
    def _mock_daily_data(self, session_id="s1", mode="full"):
        """Build a minimal extract_transcripts_incremental return value."""
        return {
            "2026-02-22": [
                {
                    "session_id": session_id,
                    "filepath": "/tmp/test.jsonl",
                    "project_path": "project-a",
                    "messages": [{"role": "assistant", "content": "hello"}],
                    "message_count": 1,
                    "mode": mode,
                    "current_offset": 500,
                    "current_lines": 5,
                }
            ]
        }

    def test_returns_extracted_files_offsets_and_daily_data(self, tmp_path):
        """Returns extracted_files dict, session_offsets dict, and daily_data."""
        with mock.patch("load_memory.extract_transcripts_incremental", return_value=self._mock_daily_data()), \
             mock.patch("load_memory.format_transcripts_incremental", return_value="formatted output"), \
             mock.patch("load_memory.load_synthesis_state", return_value={"sessions": {}}):
            extracted, offsets, daily_data = pre_extract_transcripts_incremental(
                ["2026-02-22"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        assert "2026-02-22" in extracted
        assert Path(extracted["2026-02-22"]).exists()
        assert offsets["s1"]["offset"] == 500
        assert offsets["s1"]["lines"] == 5
        assert "2026-02-22" in daily_data

    def test_skips_empty_dates(self, tmp_path):
        """Dates with no incremental content are excluded."""
        with mock.patch("load_memory.extract_transcripts_incremental", return_value={}), \
             mock.patch("load_memory.load_synthesis_state", return_value={"sessions": {}}):
            extracted, offsets, daily_data = pre_extract_transcripts_incremental(
                ["2026-02-22"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        assert extracted == {}
        assert offsets == {}
        assert daily_data == {}

    def test_collects_offsets_from_multiple_sessions(self, tmp_path):
        """Multiple sessions across dates accumulate in offsets dict."""
        multi = {
            "2026-02-21": [
                {"session_id": "s1", "filepath": "/tmp/t1", "project_path": None,
                 "messages": [{"role": "assistant", "content": "a"}], "message_count": 1,
                 "mode": "full", "current_offset": 100, "current_lines": 2},
            ],
            "2026-02-22": [
                {"session_id": "s2", "filepath": "/tmp/t2", "project_path": None,
                 "messages": [{"role": "assistant", "content": "b"}], "message_count": 1,
                 "mode": "delta", "current_offset": 300, "current_lines": 8},
            ],
        }
        with mock.patch("load_memory.extract_transcripts_incremental", return_value=multi), \
             mock.patch("load_memory.format_transcripts_incremental", return_value="data"), \
             mock.patch("load_memory.load_synthesis_state", return_value={"sessions": {}}):
            _, offsets, _ = pre_extract_transcripts_incremental(
                ["2026-02-21", "2026-02-22"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        assert offsets["s1"]["offset"] == 100
        assert offsets["s2"]["offset"] == 300

    def test_passes_min_session_messages_from_settings(self, tmp_path):
        """pre_extract passes synthesis.minSessionMessages to extract_transcripts_incremental."""
        from memory_utils import DEFAULT_SETTINGS

        threshold = DEFAULT_SETTINGS["synthesis"]["minSessionMessages"]

        with mock.patch("load_memory.extract_transcripts_incremental", return_value={}) as mock_extract, \
             mock.patch("load_memory.load_synthesis_state", return_value={"sessions": {}}), \
             mock.patch("load_memory.load_settings", return_value=DEFAULT_SETTINGS):
            pre_extract_transcripts_incremental(
                ["2026-02-22"], exclude_session_id=None, output_dir=str(tmp_path)
            )

        mock_extract.assert_called_once()
        assert mock_extract.call_args[1].get("min_session_messages") == threshold




# =============================================================================
# Incremental Wiring Tests
# =============================================================================


class TestIncrementalWiring:
    """Verify incremental extraction is wired into the prompt."""

    def test_no_offsets_arg_in_prompt(self):
        """Prompt command never includes --offsets-json (offsets computed at apply time)."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={
                "transcripts": {"2026-02-22": "data"},
            },
        )
        assert "--offsets-json" not in prompt
        assert "synthesis.py apply" in prompt


# =============================================================================
# write_synthesis_prompt (file output) Tests
# =============================================================================


class TestSynthesisPromptFileOutput:
    """Test that --synthesis-prompt writes prompt to file, not stdout."""

    def test_writes_prompt_to_temp_file(self, tmp_path, monkeypatch):
        """Prompt content goes to temp file, stdout gets only model + path."""
        monkeypatch.setattr("load_memory.get_recent_days", lambda **kw: ["2026-02-23"])
        monkeypatch.setattr(
            "load_memory.pre_extract_transcripts_incremental",
            lambda dates, **kw: (
                {"2026-02-23": "/tmp/extract.txt"},
                {"sid1": {"offset": 100, "lines": 10}},
                {"2026-02-23": [{"session_id": "sid1", "project_path": "/test", "messages": ["hi"]}]},
            ),
        )
        monkeypatch.setattr(
            "load_memory._build_embedded_files",
            lambda *a, **kw: {"transcripts": {"2026-02-23": "test"}},
        )
        monkeypatch.setattr(
            "load_memory._build_synthesis_prompt",
            lambda *a, **kw: "FAKE_PROMPT_CONTENT_HERE",
        )
        monkeypatch.setattr("load_memory.load_settings", lambda: {"synthesis": {"model": "haiku"}})
        monkeypatch.setattr("load_memory.SYNTHESIS_PROMPT_DIR", str(tmp_path))

        import io
        captured = io.StringIO()
        monkeypatch.setattr("sys.stdout", captured)

        write_synthesis_prompt(exclude_session_id=None)

        output = captured.getvalue()
        lines = output.strip().split("\n")

        assert lines[0] == "model=haiku"
        assert lines[1].startswith("prompt_file=")
        prompt_path = lines[1].split("=", 1)[1]
        assert Path(prompt_path).exists()
        assert Path(prompt_path).read_text() == "FAKE_PROMPT_CONTENT_HERE"

    def test_no_pending_transcripts(self, tmp_path, monkeypatch):
        """Prints 'No pending transcripts.' when no dates available."""
        monkeypatch.setattr("load_memory.get_recent_days", lambda **kw: [])
        monkeypatch.setattr("load_memory.load_settings", lambda: {"synthesis": {"model": "haiku"}})

        import io
        captured = io.StringIO()
        monkeypatch.setattr("sys.stdout", captured)

        write_synthesis_prompt(exclude_session_id=None)

        assert "No pending transcripts." in captured.getvalue()

    def test_no_extracted_content(self, tmp_path, monkeypatch):
        """Prints 'No pending transcripts with content.' when extraction yields nothing."""
        monkeypatch.setattr("load_memory.get_recent_days", lambda **kw: ["2026-02-23"])
        monkeypatch.setattr(
            "load_memory.pre_extract_transcripts_incremental",
            lambda dates, **kw: ({}, {}, {}),
        )
        monkeypatch.setattr("load_memory.load_settings", lambda: {"synthesis": {"model": "haiku"}})

        import io
        captured = io.StringIO()
        monkeypatch.setattr("sys.stdout", captured)

        write_synthesis_prompt(exclude_session_id=None)

        assert "No pending transcripts with content." in captured.getvalue()

    def test_no_offsets_file_written(self, tmp_path, monkeypatch):
        """Offsets are no longer written to temp files (computed at apply time instead)."""
        monkeypatch.setattr("load_memory.get_recent_days", lambda **kw: ["2026-02-23"])
        monkeypatch.setattr(
            "load_memory.pre_extract_transcripts_incremental",
            lambda dates, **kw: (
                {"2026-02-23": "/tmp/extract.txt"},
                {"sid1": {"offset": 200, "lines": 5}},
                {"2026-02-23": [{"session_id": "sid1", "project_path": "/test", "messages": ["hi"]}]},
            ),
        )
        # Capture what's passed to _build_synthesis_prompt
        captured_embedded = {}

        def mock_build_embedded(*a, **kw):
            return {"transcripts": {"2026-02-23": "test"}}

        def mock_build_prompt(*a, **kw):
            if len(a) >= 3:
                captured_embedded.update(a[2] or {})
            elif "embedded_files" in kw:
                captured_embedded.update(kw["embedded_files"] or {})
            return "PROMPT"

        monkeypatch.setattr("load_memory._build_embedded_files", mock_build_embedded)
        monkeypatch.setattr("load_memory._build_synthesis_prompt", mock_build_prompt)
        monkeypatch.setattr("load_memory.load_settings", lambda: {"synthesis": {"model": "haiku"}})
        monkeypatch.setattr("load_memory.SYNTHESIS_PROMPT_DIR", str(tmp_path))

        import io
        captured_out = io.StringIO()
        monkeypatch.setattr("sys.stdout", captured_out)

        write_synthesis_prompt(exclude_session_id=None)

        # offsets_path should NOT be in embedded files (computed at apply time now)
        assert "offsets_path" not in captured_embedded


# =============================================================================
# Synthesis Model Default Tests
# =============================================================================


class TestSynthesisModelDefault:
    def test_default_model_is_sonnet(self):
        """Default synthesis model should be sonnet for reliable tool use."""
        from memory_utils import DEFAULT_SETTINGS
        assert DEFAULT_SETTINGS["synthesis"]["model"] == "sonnet"


# =============================================================================
# Worktree Project Detection Tests
# =============================================================================


class TestWorktreeProjectDetection:
    """Verify load_memory resolves worktree paths before project lookup."""

    def test_worktree_path_resolved_before_project_lookup(self):
        """CWD in a worktree should resolve to main repo for project matching."""
        from load_memory import resolve_session_path as imported
        assert imported is not None




# =============================================================================
# Skip Memory Env Var Tests
# =============================================================================


class TestSkipMemory:
    def test_skip_memory_env_var_suppresses_output(self, capsys, monkeypatch):
        """CLAUDE_SKIP_MEMORY=1 causes main() to exit with no output."""
        from load_memory import main

        monkeypatch.setenv("CLAUDE_SKIP_MEMORY", "1")
        main()
        assert capsys.readouterr().out == ""

    def test_no_skip_without_env_var(self, monkeypatch):
        """Without CLAUDE_SKIP_MEMORY, main() proceeds past the env check."""
        from load_memory import main

        monkeypatch.delenv("CLAUDE_SKIP_MEMORY", raising=False)
        # main() should proceed past the env var check and hit check_synthesis_errors.
        # We mock it to raise so we can confirm it got past the check.
        with mock.patch("load_memory.check_synthesis_errors", side_effect=RuntimeError("reached")):
            with pytest.raises(RuntimeError, match="reached"):
                main()




class TestCheckSynthesisErrors:
    """Tests for check_synthesis_errors()."""

    def test_returns_none_when_no_log(self, tmp_path):
        """No error log file -> None."""
        from load_memory import check_synthesis_errors
        with mock.patch("load_memory.SYNTHESIS_ERROR_LOG", tmp_path / ".synthesis-errors.log"):
            assert check_synthesis_errors() is None

    def test_returns_none_when_empty_log(self, tmp_path):
        """Empty error log file -> None."""
        from load_memory import check_synthesis_errors
        error_log = tmp_path / ".synthesis-errors.log"
        error_log.write_text("")
        with mock.patch("load_memory.SYNTHESIS_ERROR_LOG", error_log):
            assert check_synthesis_errors() is None

    def test_returns_alert_with_errors(self, tmp_path):
        """Error log with content -> alert text."""
        from load_memory import check_synthesis_errors
        error_log = tmp_path / ".synthesis-errors.log"
        error_log.write_text("[2026-03-01T14:00:00Z] FileNotFoundError: claude\n")
        with mock.patch("load_memory.SYNTHESIS_ERROR_LOG", error_log):
            result = check_synthesis_errors()
        assert result is not None
        assert "Synthesis Error Alert" in result
        assert "FileNotFoundError" in result

    def test_clears_log_after_reading(self, tmp_path):
        """Error log should be deleted after surfacing."""
        from load_memory import check_synthesis_errors
        error_log = tmp_path / ".synthesis-errors.log"
        error_log.write_text("[2026-03-01T14:00:00Z] test error\n")
        with mock.patch("load_memory.SYNTHESIS_ERROR_LOG", error_log):
            check_synthesis_errors()
        assert not error_log.exists()

    def test_limits_to_last_5_errors(self, tmp_path):
        """Should only show the last 5 errors."""
        from load_memory import check_synthesis_errors
        error_log = tmp_path / ".synthesis-errors.log"
        lines = [f"[2026-03-01T{i:02d}:00:00Z] error {i}\n" for i in range(10)]
        error_log.write_text("".join(lines))
        with mock.patch("load_memory.SYNTHESIS_ERROR_LOG", error_log):
            result = check_synthesis_errors()
        # Should contain errors 5-9 (last 5), not 0-4
        assert "error 5" in result
        assert "error 9" in result
        assert "error 0" not in result


# ============================================================================
# C7: CRUD-aware synthesis prompt
# ============================================================================


class TestSynthesisPromptCrud:
    """Tests for CRUD-aware synthesis prompt generation."""

    def test_prompt_contains_memory_ops_format_spec(self):
        """Prompt includes ===MEMORY_OPS=== output format with examples."""
        instructions = _build_synthesis_instructions_v3()
        assert "===MEMORY_OPS===" in instructions
        assert "ADD" in instructions
        assert "UPDATE" in instructions
        assert "DELETE" in instructions
        assert "NOOP" in instructions

    def test_prompt_includes_crud_action_descriptions(self):
        """Prompt describes what each CRUD action means and ID referencing."""
        instructions = _build_synthesis_instructions_v3()
        assert "id" in instructions.lower()

    def test_prompt_includes_existing_memories_with_ids(self, tmp_path):
        """When vector memories available, prompt has Existing Memories with chunk IDs."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("test transcript")
        vector_memories = [
            {"chunk_id": "abc123", "content": "project uses REST API"},
            {"chunk_id": "def456", "content": "prefers Python for scripting"},
        ]
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-03-21"],
            extracted_files={"2026-03-21": str(extract_file)},
            synthesis_instructions="test instructions",
            embedded_files={"transcripts": {"2026-03-21": "test transcript"}},
            vector_memories=vector_memories,
        )
        assert "abc123" in prompt
        assert "def456" in prompt
        assert "Existing Memories" in prompt

    def test_prompt_shows_placeholder_without_vector(self, tmp_path):
        """Without vector memories, prompt shows placeholder text."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("test transcript")
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-03-21"],
            extracted_files={"2026-03-21": str(extract_file)},
            synthesis_instructions="test instructions",
            embedded_files={
                "transcripts": {"2026-03-21": "test transcript"},
            },
        )
        assert "(no existing memories)" in prompt


# ============================================================================
# C2: Pre-retrieval context in synthesis prompts
# ============================================================================


class TestPreRetrievalPrompt:
    """Tests for vector-retrieved memory context in _build_preextracted_prompt."""

    def test_prompt_includes_existing_memories_section(self, tmp_path):
        """Synthesis prompt has '## Existing Memories' with chunk IDs."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("test transcript")
        vector_memories = [
            {"chunk_id": "abc123", "content": "project uses REST API"},
            {"chunk_id": "def456", "content": "prefers Python for scripting"},
        ]
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-03-21"],
            extracted_files={"2026-03-21": str(extract_file)},
            synthesis_instructions="test instructions",
            embedded_files={"transcripts": {"2026-03-21": "test transcript"}},
            vector_memories=vector_memories,
        )
        assert "Existing Memories" in prompt
        assert "abc123" in prompt
        assert "def456" in prompt

    def test_placeholder_when_vec_unavailable(self, tmp_path):
        """When vector_memories is None, shows '(no existing memories)' placeholder."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("test transcript")
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-03-21"],
            extracted_files={"2026-03-21": str(extract_file)},
            synthesis_instructions="test instructions",
            embedded_files={
                "transcripts": {"2026-03-21": "test transcript"},
            },
        )
        assert "(no existing memories)" in prompt

    def test_vector_results_formatted_with_chunk_ids(self, tmp_path):
        """Each retrieved memory includes its chunk ID for CRUD reference."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("test transcript")
        vector_memories = [
            {"chunk_id": "chunk_abc123", "content": "content text here"},
        ]
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-03-21"],
            extracted_files={"2026-03-21": str(extract_file)},
            synthesis_instructions="instructions",
            embedded_files={"transcripts": {"2026-03-21": "test transcript"}},
            vector_memories=vector_memories,
        )
        assert "[chunk_abc123] content text here" in prompt

    def test_empty_vector_memories_shows_placeholder(self, tmp_path):
        """Empty vector_memories list shows '(no existing memories)' placeholder."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("test transcript")
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-03-21"],
            extracted_files={"2026-03-21": str(extract_file)},
            synthesis_instructions="instructions",
            embedded_files={
                "transcripts": {"2026-03-21": "test transcript"},
            },
            vector_memories=[],
        )
        assert "(no existing memories)" in prompt


# =============================================================================
# _build_synthesis_instructions_v3 Tests
# =============================================================================


class TestSynthesisPromptV3:
    def test_prompt_requests_memory_ops_only(self):
        """V3 prompt should request MEMORY_OPS format, not PROJECT blocks."""
        instructions = _build_synthesis_instructions_v3()
        assert "MEMORY_OPS" in instructions
        assert "===PROJECT:" not in instructions

    def test_prompt_includes_salience_guidance(self):
        """Prompt explains salience spectrum (0.3-0.5 transient, 0.7-0.9 important)."""
        instructions = _build_synthesis_instructions_v3()
        assert "salience" in instructions.lower()
        assert "0.3" in instructions or "transient" in instructions

    def test_prompt_documents_three_scopes(self):
        """Prompt explains user, global, and project scopes."""
        instructions = _build_synthesis_instructions_v3()
        assert "user" in instructions
        assert "global" in instructions

    def test_prompt_includes_provenance_fields(self):
        """Prompt explains supersedes, contradicts, etc. relationship types."""
        instructions = _build_synthesis_instructions_v3()
        assert "supersedes" in instructions

    def test_prompt_includes_entity_extraction(self):
        """Prompt requests entities array in each operation."""
        instructions = _build_synthesis_instructions_v3()
        assert "entities" in instructions


# =============================================================================
# _load_from_db Tests (E1 + E2)
# =============================================================================


def _make_v3_db(tmp_path):
    """Create a minimal v3 DB for testing smart loading.

    Uses ensure_db() via patched get_db_path so vec0 errors are handled
    gracefully (same as production code).
    """
    from storage import ensure_db

    db_path = tmp_path / "test.db"
    with mock.patch("storage.get_db_path", return_value=db_path):
        conn = ensure_db()
    return conn


class TestSmartLoading:
    def test_returns_none_for_v2_db(self, tmp_path):
        """Returns None (signal legacy) when DB is v2."""
        from storage import SCHEMA_DDL

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.executescript(SCHEMA_DDL)
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()
        assert result is None

    def test_returns_empty_tuple_when_no_db(self, tmp_path):
        """Returns empty tuple (not None) when DB file doesn't exist."""
        with mock.patch("storage.get_db", side_effect=FileNotFoundError):
            result = _load_from_db("myproject")
        assert result == ("", [], [])

    def test_graceful_fallback_no_db(self, tmp_path):
        """If DB doesn't exist, loading returns empty tuple gracefully."""
        with mock.patch("storage.get_db", side_effect=FileNotFoundError):
            result = _load_from_db("")
        assert result == ("", [], [])

    def test_loads_user_profile(self, tmp_path):
        """User profile data_points (scope='user') are loaded."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        insert_data_point(
            conn, DataPointRow(type="profile", content="Senior Python dev", scope="user", salience=1.0)
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        assert result is not None
        text, tiers, alerts = result
        assert "Senior Python dev" in text

    def test_loads_project_memories(self, tmp_path):
        """Project memories with salience > 0.4 are loaded; below threshold excluded from Tier 3."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        insert_data_point(
            conn,
            DataPointRow(type="memory", content="Uses gRPC", scope="myproject", salience=0.8),
        )
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat().replace("+00:00", "Z")
        insert_data_point(
            conn,
            DataPointRow(
                type="memory",
                content="Low salience old",
                scope="myproject",
                salience=0.2,
                created_at=old_date,
            ),
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        assert result is not None
        text, tiers, alerts = result
        assert "Uses gRPC" in text
        assert "Low salience old" not in text

    def test_loads_global_knowledge(self, tmp_path):
        """Global memories with salience > 0.6 are loaded."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        insert_data_point(
            conn,
            DataPointRow(type="memory", content="SQLite WAL mode", scope="global", salience=0.9),
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("")
            mock_conn.close()

        assert result is not None
        text, tiers, alerts = result
        assert "SQLite WAL mode" in text

    def test_dedup_across_tiers(self, tmp_path):
        """Same data_point doesn't appear twice across query tiers."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        insert_data_point(
            conn,
            DataPointRow(
                id="dp_cross",
                type="memory",
                content="Cross-tier fact",
                scope="global",
                salience=0.9,
                created_at=now_iso,
            ),
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("")
            mock_conn.close()

        assert result is not None
        text, tiers, alerts = result
        assert text.count("Cross-tier fact") == 1

    def test_access_tracking_fires(self, tmp_path):
        """All served data_point IDs have access_count incremented."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        dp_id = insert_data_point(
            conn,
            DataPointRow(type="memory", content="tracked", scope="global", salience=0.9),
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            _load_from_db("")
            mock_conn.close()

        verify_conn = sqlite3.connect(str(tmp_path / "test.db"))
        row = verify_conn.execute(
            "SELECT access_count FROM data_points WHERE id=?", (dp_id,)
        ).fetchone()
        verify_conn.close()
        assert row is not None
        assert row[0] > 0


class TestSessionContinuity:
    def test_shows_last_session_work(self, tmp_path):
        """Output includes 'Last Session' section when context exists."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        recent = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        insert_data_point(
            conn,
            DataPointRow(
                type="session_context",
                content="Working on auth",
                scope="myproject",
                salience=0.8,
                created_at=recent,
            ),
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        assert result is not None
        text, tiers, alerts = result
        assert "Last Session" in text
        assert "Working on auth" in text

    def test_no_section_when_no_context(self, tmp_path):
        """No 'Last Session' section when no session_context exists for project."""
        conn = _make_v3_db(tmp_path)
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        assert result is not None
        text, tiers, alerts = result
        assert "Last Session" not in text

    def test_status_from_properties(self, tmp_path):
        """Status field from properties JSON is displayed in output."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        insert_data_point(
            conn,
            DataPointRow(
                type="session_context",
                content="Auth work",
                scope="myproject",
                salience=0.8,
                created_at=recent,
                properties=json.dumps({"status": "in_progress"}),
            ),
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        assert result is not None
        text, tiers, alerts = result
        assert "in_progress" in text

    def test_stale_context_not_shown(self, tmp_path):
        """Session context older than 7 days is not shown."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        stale = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat().replace("+00:00", "Z")
        insert_data_point(
            conn,
            DataPointRow(
                type="session_context",
                content="Old work",
                scope="myproject",
                salience=0.8,
                created_at=stale,
            ),
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        assert result is not None
        text, tiers, alerts = result
        assert "Last Session" not in text

    def test_entities_from_context_for_edges(self, tmp_path):
        """Entities connected via context_for edges are listed in output."""
        from storage import DataPointRow, EdgeRow, insert_data_point, insert_edge

        conn = _make_v3_db(tmp_path)
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        ctx_id = insert_data_point(
            conn,
            DataPointRow(
                type="session_context",
                content="Auth work",
                scope="myproject",
                salience=0.8,
                created_at=recent,
            ),
        )
        entity_id = insert_data_point(
            conn,
            DataPointRow(type="entity", name="JWT", scope="myproject", salience=0.7),
        )
        insert_edge(
            conn,
            EdgeRow(
                source=ctx_id,
                target=entity_id,
                type="context_for",
                created_at=recent,
            ),
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch(
            "storage.close_db"
        ):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        assert result is not None
        text, tiers, alerts = result
        assert "JWT" in text


# =============================================================================
# _load_from_db Tiers Metadata Tests (B1)
# =============================================================================


class TestLoadFromDbTiersMetadata:
    """Tests for _load_from_db returning (text, tiers_metadata, health_alerts) tuple."""

    def test_returns_tuple_with_three_elements(self, tmp_path):
        """v3 DB returns a 3-tuple (text, tiers, alerts)."""
        conn = _make_v3_db(tmp_path)
        insert_data_point(
            conn, DataPointRow(type="memory", content="Some fact", scope="global", salience=0.9)
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch("storage.close_db"):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("")
            mock_conn.close()

        assert result is not None
        assert isinstance(result, tuple)
        assert len(result) == 3
        text, tiers, alerts = result
        assert isinstance(text, str)
        assert isinstance(tiers, list)
        assert isinstance(alerts, list)

    def test_tier_dicts_have_required_keys(self, tmp_path):
        """Each tier metadata dict has name, count, tokens_est, ids."""
        conn = _make_v3_db(tmp_path)
        insert_data_point(
            conn, DataPointRow(type="profile", content="Dev profile", scope="user", salience=1.0)
        )
        insert_data_point(
            conn, DataPointRow(type="memory", content="Global fact", scope="global", salience=0.9)
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch("storage.close_db"):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        text, tiers, alerts = result
        required_keys = {"name", "count", "tokens_est", "ids"}
        for tier in tiers:
            assert required_keys.issubset(tier.keys()), f"Tier {tier.get('name')} missing keys: {required_keys - tier.keys()}"

    def test_token_estimation_uses_char_div_4(self, tmp_path):
        """Token estimate is sum(len(content) // 4) for each tier's rows."""
        conn = _make_v3_db(tmp_path)
        content = "A" * 100
        insert_data_point(
            conn, DataPointRow(type="profile", content=content, scope="user", salience=1.0)
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch("storage.close_db"):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("")
            mock_conn.close()

        text, tiers, alerts = result
        profile_tier = next(t for t in tiers if t["name"] == "Profile")
        assert profile_tier["tokens_est"] == len(content) // 4

    def test_returns_none_for_v2_db(self, tmp_path):
        """v2 DB returns None (unchanged behavior)."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.executescript(SCHEMA_DDL)
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch("storage.close_db"):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()
        assert result is None

    def test_empty_db_returns_tuple_with_zero_counts(self, tmp_path):
        """Empty v3 DB returns tuple with all tier counts at zero."""
        conn = _make_v3_db(tmp_path)
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch("storage.close_db"):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("")
            mock_conn.close()

        assert result is not None
        text, tiers, alerts = result
        assert all(t["count"] == 0 for t in tiers)
        assert all(t["tokens_est"] == 0 for t in tiers)

    def test_tier_ids_populated(self, tmp_path):
        """Tier ids list contains the actual data_point IDs served."""
        conn = _make_v3_db(tmp_path)
        dp_id = insert_data_point(
            conn, DataPointRow(type="memory", content="Tracked fact", scope="global", salience=0.9)
        )
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch("storage.close_db"):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("")
            mock_conn.close()

        text, tiers, alerts = result
        global_tier = next(t for t in tiers if t["name"] == "Global")
        assert dp_id in global_tier["ids"]

    def test_five_tiers_always_present(self, tmp_path):
        """All 5 tier names are always present in metadata, even with no data."""
        conn = _make_v3_db(tmp_path)
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch("storage.close_db"):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        text, tiers, alerts = result
        tier_names = [t["name"] for t in tiers]
        assert "Profile" in tier_names
        assert "Session" in tier_names
        assert "Project" in tier_names
        assert "Global" in tier_names
        assert "Recent" in tier_names


# =============================================================================
# Salience Reinforcement Tests (A1)
# =============================================================================


class TestSalienceReinforcement:
    """Tests for salience reinforcement in _batch_update_data_point_access."""

    def _make_v3_db(self, tmp_path):
        """Create a v3 DB for testing data_point access tracking."""
        from unittest.mock import patch
        with patch("storage.get_db_path", return_value=tmp_path / "memory.db"), \
             patch("memory_utils.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        return conn

    def test_salience_increases_on_access(self, tmp_path):
        """Accessing a data_point increases its salience via diminishing returns."""
        conn = self._make_v3_db(tmp_path)
        dp = DataPointRow(type="memory", content="test fact", scope="global", salience=0.5)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        from load_memory import REINFORCEMENT_ETA, _batch_update_data_point_access
        _batch_update_data_point_access(conn, [dp_id])
        conn.commit()

        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        expected = min(1.0, 0.5 + REINFORCEMENT_ETA * (1.0 - 0.5))
        assert abs(row[0] - expected) < 0.001
        conn.close()

    def test_salience_capped_at_one(self, tmp_path):
        """Salience cannot exceed 1.0 even after repeated reinforcement."""
        conn = self._make_v3_db(tmp_path)
        dp = DataPointRow(type="memory", content="test fact", scope="global", salience=0.95)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        from load_memory import _batch_update_data_point_access
        for _ in range(10):
            _batch_update_data_point_access(conn, [dp_id])
            conn.commit()

        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        assert row[0] <= 1.0
        conn.close()

    def test_associative_boost_propagates_to_entities(self, tmp_path):
        """Accessing a memory boosts salience of connected entity data_points."""
        conn = self._make_v3_db(tmp_path)
        memory_dp = DataPointRow(type="memory", content="Use Redis for caching", scope="global", salience=0.6)
        memory_id = insert_data_point(conn, memory_dp)
        entity_dp = DataPointRow(type="entity", name="Redis", content="Redis", scope="global", salience=0.4)
        entity_id = insert_data_point(conn, entity_dp)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        insert_edge(conn, EdgeRow(source=memory_id, target=entity_id, type="mentions", weight=1.0, created_at=now))
        conn.commit()

        from load_memory import _batch_update_data_point_access
        _batch_update_data_point_access(conn, [memory_id])
        conn.commit()

        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (entity_id,)).fetchone()
        assert row[0] > 0.4, "Entity salience should increase via associative boost"
        conn.close()

    def test_no_boost_to_invalidated_edges(self, tmp_path):
        """Entities connected via invalidated edges (valid_to IS NOT NULL) are not boosted."""
        conn = self._make_v3_db(tmp_path)
        memory_dp = DataPointRow(type="memory", content="test", scope="global", salience=0.6)
        memory_id = insert_data_point(conn, memory_dp)
        entity_dp = DataPointRow(type="entity", name="test", content="test", scope="global", salience=0.4)
        entity_id = insert_data_point(conn, entity_dp)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        edge_row = EdgeRow(source=memory_id, target=entity_id, type="mentions", weight=1.0, created_at=now)
        insert_edge(conn, edge_row)
        edge_id = conn.execute("SELECT id FROM edges ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        invalidate_edge(conn, edge_id, valid_to=now, expired_at=now)
        conn.commit()

        original_salience = conn.execute("SELECT salience FROM data_points WHERE id = ?", (entity_id,)).fetchone()[0]
        from load_memory import _batch_update_data_point_access
        _batch_update_data_point_access(conn, [memory_id])
        conn.commit()

        new_salience = conn.execute("SELECT salience FROM data_points WHERE id = ?", (entity_id,)).fetchone()[0]
        assert new_salience == original_salience, "Invalidated edge should not cause boost"
        conn.close()


# =============================================================================
# Passive vs Active Reinforcement Tests (A5)
# =============================================================================


class TestPassiveReinforcement:
    """Tests for passive parameter in _batch_update_data_point_access."""

    def _make_v3_db(self, tmp_path):
        """Create a v3 DB for testing passive reinforcement."""
        with mock.patch("storage.get_db_path", return_value=tmp_path / "memory.db"), \
             mock.patch("memory_utils.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        return conn

    def test_passive_load_skips_salience_reinforcement(self, tmp_path):
        """Passive access updates access_count but does not change salience."""
        conn = self._make_v3_db(tmp_path)
        dp = DataPointRow(type="memory", content="test fact", scope="global", salience=0.7)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        _batch_update_data_point_access(conn, [dp_id], passive=True)
        conn.commit()

        row = conn.execute(
            "SELECT salience, access_count FROM data_points WHERE id = ?", (dp_id,)
        ).fetchone()
        assert row[0] == 0.7, "Salience should remain unchanged for passive access"
        assert row[1] == 1, "access_count should still be incremented"
        conn.close()

    def test_active_load_applies_salience_reinforcement(self, tmp_path):
        """Active access (passive=False) increases salience via reinforcement."""
        conn = self._make_v3_db(tmp_path)
        dp = DataPointRow(type="memory", content="test fact", scope="global", salience=0.7)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        _batch_update_data_point_access(conn, [dp_id], passive=False)
        conn.commit()

        row = conn.execute(
            "SELECT salience, access_count FROM data_points WHERE id = ?", (dp_id,)
        ).fetchone()
        assert row[0] > 0.7, "Salience should increase for active access"
        assert row[1] == 1, "access_count should be incremented"
        conn.close()

    def test_passive_load_skips_neighbor_boosting(self, tmp_path):
        """Passive access does not boost salience of connected entity data_points."""
        conn = self._make_v3_db(tmp_path)
        memory_dp = DataPointRow(
            type="memory", content="Use Redis for caching", scope="global", salience=0.6
        )
        memory_id = insert_data_point(conn, memory_dp)
        entity_dp = DataPointRow(
            type="entity", name="Redis", content="Redis", scope="global", salience=0.4
        )
        entity_id = insert_data_point(conn, entity_dp)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        insert_edge(
            conn,
            EdgeRow(source=memory_id, target=entity_id, type="mentions", weight=1.0, created_at=now),
        )
        conn.commit()

        original_salience = conn.execute(
            "SELECT salience FROM data_points WHERE id = ?", (entity_id,)
        ).fetchone()[0]

        _batch_update_data_point_access(conn, [memory_id], passive=True)
        conn.commit()

        new_salience = conn.execute(
            "SELECT salience FROM data_points WHERE id = ?", (entity_id,)
        ).fetchone()[0]
        assert new_salience == original_salience, "Entity salience should not change for passive access"
        conn.close()


# =============================================================================
# Working-Day Loading Tests (B2)
# =============================================================================


class TestWorkingDayLoading:
    """Test Tier 2 and Tier 5 working-day integration."""

    def test_tier2_uses_project_working_days(self, tmp_path):
        """Tier 2 uses get_project_working_days for cutoff."""
        conn = _make_v3_db(tmp_path)
        conn.execute(
            "INSERT INTO data_points (id, type, scope, content, created_at, salience) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("sc1", "session_context", "myproject", "Working on tests", "2026-03-13T10:00:00Z", 1.0),
        )
        conn.commit()

        # Mock at memory_utils level since load_memory imports lazily
        with mock.patch("memory_utils.get_project_working_days",
                        return_value=["2026-03-25", "2026-03-20", "2026-03-13"]):
            from memory_utils import get_project_working_days

            working_days = get_project_working_days("myproject", 5)
            cutoff = working_days[-1] + "T00:00:00Z"

            row = conn.execute(
                "SELECT id, content, properties FROM data_points "
                "WHERE type='session_context' AND scope=? AND created_at > ? "
                "ORDER BY created_at DESC LIMIT 1",
                ("myproject", cutoff),
            ).fetchone()

        assert row is not None, "Session context from Mar 13 should be found with working-day cutoff"
        assert row[1] == "Working on tests"
        conn.close()

    def test_tier2_falls_back_to_calendar(self):
        """Tier 2 falls back to 7 calendar days when no working days."""
        with mock.patch("memory_utils.get_project_working_days", return_value=[]):
            from memory_utils import get_project_working_days

            working_days = get_project_working_days("myproject", 5)
            assert working_days == []

    def test_tier5_uses_global_working_days(self, tmp_path):
        """Tier 5 uses get_global_working_days for cutoff."""
        conn = _make_v3_db(tmp_path)
        conn.execute(
            "INSERT INTO data_points (id, type, scope, content, created_at, salience) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("m1", "memory", "global", "Important fact", "2026-03-15T10:00:00Z", 0.8),
        )
        conn.commit()

        with mock.patch("memory_utils.get_global_working_days",
                        return_value=["2026-03-25", "2026-03-20", "2026-03-15"]):
            from memory_utils import get_global_working_days

            global_working = get_global_working_days(3)
            cutoff = global_working[-1] + "T00:00:00Z"

            rows = conn.execute(
                "SELECT id, content FROM data_points "
                "WHERE scope IN ('global', ?) AND type='memory' "
                "AND created_at > ? "
                "ORDER BY created_at DESC LIMIT 15",
                ("global", cutoff),
            ).fetchall()

        assert len(rows) == 1
        assert rows[0][1] == "Important fact"
        conn.close()

    def test_tier5_falls_back_to_calendar(self):
        """Tier 5 falls back to 3 calendar days when no working days."""
        with mock.patch("memory_utils.get_global_working_days", return_value=[]):
            from memory_utils import get_global_working_days

            assert get_global_working_days(3) == []


class TestEntityCompaction:
    """Tests for entity tag compaction in Profile tier."""

    def test_entity_rows_rendered_as_tags_line(self, tmp_path):
        """Entity-type data_points in Profile tier render as a single Tags: line."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        insert_data_point(conn, DataPointRow(
            type="entity", name="Python-3.13", content="Python-3.13",
            scope="user", salience=1.0,
        ))
        insert_data_point(conn, DataPointRow(
            type="entity", name="claude-code", content="claude-code",
            scope="user", salience=0.9,
        ))
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch("storage.close_db"):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        text, tiers, alerts = result
        assert "Tags:" in text
        assert "Python-3.13" in text
        assert "claude-code" in text
        lines = text.strip().splitlines()
        tag_lines = [l for l in lines if "Tags:" in l]
        assert len(tag_lines) == 1
        assert "Python-3.13" in tag_lines[0] and "claude-code" in tag_lines[0]

    def test_memory_rows_still_render_normally(self, tmp_path):
        """Memory-type data_points in Profile tier still render as separate lines."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        insert_data_point(conn, DataPointRow(
            type="profile", content="Senior Python developer",
            scope="user", salience=1.0,
        ))
        insert_data_point(conn, DataPointRow(
            type="entity", name="Python", content="Python",
            scope="user", salience=0.8,
        ))
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch("storage.close_db"):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        text, tiers, alerts = result
        assert "Senior Python developer" in text
        assert "Tags:" in text
        assert "Python" in text

    def test_no_tags_line_when_no_entities(self, tmp_path):
        """When no entity rows exist, no Tags: line appears."""
        from storage import DataPointRow, insert_data_point

        conn = _make_v3_db(tmp_path)
        insert_data_point(conn, DataPointRow(
            type="profile", content="Uses vim", scope="user", salience=1.0,
        ))
        conn.commit()
        conn.close()

        with mock.patch("storage.get_db") as mock_get_db, mock.patch("storage.close_db"):
            mock_conn = sqlite3.connect(str(tmp_path / "test.db"))
            mock_conn.execute("PRAGMA user_version=3")
            mock_get_db.return_value = mock_conn
            result = _load_from_db("myproject")
            mock_conn.close()

        text, tiers, alerts = result
        assert "Uses vim" in text
        assert "Tags:" not in text


class TestCosineDedup:
    """Tests for injection-time cosine dedup in Tiers 3-5."""

    def test_deserialize_vector_roundtrip(self):
        """Deserialize a packed float32 vector and get back the original values."""
        import struct
        from embeddings import EMBEDDING_DIM
        original = [0.1, 0.2, 0.3] + [0.0] * (EMBEDDING_DIM - 1 - 2)
        blob = struct.pack(f"{len(original)}f", *original)
        result = _deserialize_vector(blob)
        assert len(result) == EMBEDDING_DIM
        assert abs(result[0] - 0.1) < 1e-6
        assert abs(result[1] - 0.2) < 1e-6

    def test_cosine_similarity_identical_vectors(self):
        """Identical vectors have cosine similarity of 1.0."""
        from embeddings import EMBEDDING_DIM
        vec = [1.0, 0.0, 0.0] + [0.0] * (EMBEDDING_DIM - 3)
        assert abs(_cosine_similarity(vec, vec) - 1.0) < 1e-6

    def test_cosine_similarity_orthogonal_vectors(self):
        """Orthogonal vectors have cosine similarity of 0.0."""
        from embeddings import EMBEDDING_DIM
        a = [1.0, 0.0, 0.0] + [0.0] * (EMBEDDING_DIM - 3)
        b = [0.0, 1.0, 0.0] + [0.0] * (EMBEDDING_DIM - 3)
        assert abs(_cosine_similarity(a, b)) < 1e-6

    def test_filter_removes_similar_candidates(self):
        """Candidates similar to seen embeddings are filtered out."""
        from embeddings import EMBEDDING_DIM

        vec_a = [1.0] + [0.0] * (EMBEDDING_DIM - 1)
        vec_b = [0.99] + [0.01] * (EMBEDDING_DIM - 1)  # very similar to vec_a
        vec_c = [0.0] * (EMBEDDING_DIM - 1) + [1.0]     # orthogonal to vec_a

        seen_embeddings = [vec_a]
        candidates = [("id_b", "similar content"), ("id_c", "different content")]
        embeddings_map = {"id_b": vec_b, "id_c": vec_c}

        accepted, new_seen = _filter_by_cosine_dedup(
            candidates, embeddings_map, seen_embeddings
        )

        accepted_ids = [r[0] for r in accepted]
        assert "id_b" not in accepted_ids
        assert "id_c" in accepted_ids
        assert len(new_seen) == 2  # original vec_a + new vec_c


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
