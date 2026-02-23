#!/usr/bin/env python3
"""
Unit tests for load_memory.py

Run with: python -m pytest tests/test_load_memory.py -v
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from load_memory import (
    _build_embedded_files,
    _build_preextracted_prompt,
    _build_synthesis_instructions,
    _build_synthesis_prompt,
    _get_project_names_str,
    load_daily_summaries,
    load_global_memory,
    load_project_history,
    load_project_memory,
    pre_extract_transcripts,
    should_synthesize,
)

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


class TestLoadGlobalMemory:
    def test_returns_content_when_exists(self, tmp_path):
        mem_file = tmp_path / "global-long-term-memory.md"
        mem_file.write_text("# Global Memory\nSome content here")

        with mock.patch("load_memory.get_global_memory_file") as mock_f:
            mock_f.return_value = mem_file
            content, size = load_global_memory()
            assert "Global Memory" in content
            assert size > 0

    def test_returns_empty_when_no_file(self):
        with mock.patch("load_memory.get_global_memory_file") as mock_f:
            mock_f.return_value = Path("/nonexistent/memory.md")
            content, size = load_global_memory()
            assert content == ""
            assert size == 0

    def test_returns_empty_on_io_error(self, tmp_path):
        mem_file = tmp_path / "memory.md"
        mem_file.write_text("content")
        # Make unreadable
        mem_file.chmod(0o000)

        with mock.patch("load_memory.get_global_memory_file") as mock_f:
            mock_f.return_value = mem_file
            content, size = load_global_memory()
            assert content == ""
            assert size == 0

        # Restore permissions for cleanup
        mem_file.chmod(0o644)


# =============================================================================
# load_project_memory Tests
# =============================================================================


class TestLoadProjectMemory:
    def test_returns_content(self, tmp_path):
        mem_file = tmp_path / "myproject-long-term-memory.md"
        mem_file.write_text("# myproject\nProject learnings")

        with mock.patch("load_memory.get_project_memory_dir") as mock_d:
            mock_d.return_value = tmp_path
            content, size = load_project_memory("myproject")
            assert "Project learnings" in content
            assert size > 0

    def test_returns_empty_when_missing(self, tmp_path):
        with mock.patch("load_memory.get_project_memory_dir") as mock_d:
            mock_d.return_value = tmp_path
            content, size = load_project_memory("nonexistent")
            assert content == ""
            assert size == 0

    def test_handles_special_chars_in_name(self, tmp_path):
        """Project names with special chars map to correct filenames."""
        # "My Project!" -> "my-project-long-term-memory.md"
        mem_file = tmp_path / "my-project-long-term-memory.md"
        mem_file.write_text("# My Project\nContent")

        with mock.patch("load_memory.get_project_memory_dir") as mock_d:
            mock_d.return_value = tmp_path
            content, size = load_project_memory("My Project!")
            assert "Content" in content


# =============================================================================
# load_daily_summaries Tests
# =============================================================================


SAMPLE_DAILY_GLOBAL = """# 2026-02-05
## Actions
- [global/implement] Set up new hooks
- [myproject/implement] Added feature X

## Learnings
- [global/pattern] Important global pattern
- [myproject/gotcha] Project-specific gotcha
"""

SAMPLE_DAILY_PROJECT = """# 2026-02-04
## Actions
- [global/document] Wrote docs
- [myproject/implement] Built the widget

## Learnings
- [myproject/pattern] Widget must be initialized first
"""


def _setup_daily_dir(tmp_path, include_global_only_day=False):
    daily_dir = tmp_path / "daily"
    daily_dir.mkdir()
    (daily_dir / "2026-02-05.md").write_text(SAMPLE_DAILY_GLOBAL)
    (daily_dir / "2026-02-04.md").write_text(SAMPLE_DAILY_PROJECT)
    if include_global_only_day:
        (daily_dir / "2026-02-03.md").write_text(
            "# 2026-02-03\n## Actions\n- [global/implement] Only global\n"
        )
    return daily_dir


class TestLoadDailySummaries:
    def test_global_scope_filtering(self, tmp_path):
        daily_dir = _setup_daily_dir(tmp_path)
        with mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory.get_working_days") as mock_wd:
            mock_dd.return_value = daily_dir
            mock_wd.return_value = ["2026-02-05", "2026-02-04"]

            summaries, total_bytes = load_daily_summaries(2, scope="global")
            all_content = " ".join(content for _, content in summaries)
            assert "[global/" in all_content
            assert "[myproject/" not in all_content

    def test_project_scope_filtering(self, tmp_path):
        daily_dir = _setup_daily_dir(tmp_path)
        with mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory.get_working_days") as mock_wd:
            mock_dd.return_value = daily_dir
            mock_wd.return_value = ["2026-02-05", "2026-02-04"]

            summaries, total_bytes = load_daily_summaries(2, scope="myproject")
            all_content = " ".join(content for _, content in summaries)
            assert "[myproject/" in all_content
            assert "[global/" not in all_content

    def test_respects_days_limit(self, tmp_path):
        """get_working_days already limits, so only those dates are loaded."""
        daily_dir = _setup_daily_dir(tmp_path)
        with mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory.get_working_days") as mock_wd:
            mock_dd.return_value = daily_dir
            mock_wd.return_value = ["2026-02-05"]  # Only 1 day

            summaries, _ = load_daily_summaries(1, scope="global")
            dates = [d for d, _ in summaries]
            assert "2026-02-04" not in dates

    def test_empty_when_no_matching_content(self, tmp_path):
        daily_dir = _setup_daily_dir(tmp_path)
        with mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory.get_working_days") as mock_wd:
            mock_dd.return_value = daily_dir
            mock_wd.return_value = ["2026-02-05"]

            summaries, total_bytes = load_daily_summaries(1, scope="other-project")
            assert summaries == []
            assert total_bytes == 0


# =============================================================================
# load_project_history Tests
# =============================================================================


class TestLoadProjectHistory:
    def test_loads_project_entries(self, tmp_path):
        daily_dir = _setup_daily_dir(tmp_path, include_global_only_day=True)
        with mock.patch("load_memory.get_daily_dir") as mock_dd:
            mock_dd.return_value = daily_dir
            project = {"name": "myproject"}
            summaries, total_bytes = load_project_history(project, days_limit=10)

            assert len(summaries) == 2  # Feb 4 and Feb 5 have myproject entries
            all_content = " ".join(content for _, content in summaries)
            assert "[myproject/" in all_content
            assert "[global/" not in all_content
            assert total_bytes > 0

    def test_oldest_first_ordering(self, tmp_path):
        """Output should be chronological (oldest first)."""
        daily_dir = _setup_daily_dir(tmp_path, include_global_only_day=True)
        with mock.patch("load_memory.get_daily_dir") as mock_dd:
            mock_dd.return_value = daily_dir
            project = {"name": "myproject"}
            summaries, _ = load_project_history(project, days_limit=10)
            dates = [d for d, _ in summaries]
            assert dates == sorted(dates)

    def test_respects_day_limit(self, tmp_path):
        daily_dir = _setup_daily_dir(tmp_path, include_global_only_day=True)
        with mock.patch("load_memory.get_daily_dir") as mock_dd:
            mock_dd.return_value = daily_dir
            project = {"name": "myproject"}
            summaries, _ = load_project_history(project, days_limit=1)
            assert len(summaries) == 1

    def test_empty_project_name(self):
        project = {"name": ""}
        summaries, total_bytes = load_project_history(project, days_limit=10)
        assert summaries == []
        assert total_bytes == 0


# =============================================================================
# Synthesis Prompt [routed] Marker Tests
# =============================================================================


class TestPreExtractTranscripts:
    """Tests for pre_extract_transcripts helper."""

    def _mock_daily_data(self, session_id="s1"):
        """Build a minimal extract_transcripts return value."""
        return {
            "project-a": [
                {
                    "session_id": session_id,
                    "messages": [{"role": "assistant", "content": "hello"}],
                }
            ]
        }

    def test_returns_extracted_files_dict(self, tmp_path):
        """pre_extract_transcripts returns dict mapping date -> path."""
        with mock.patch("load_memory.extract_transcripts", return_value=self._mock_daily_data()), \
             mock.patch("load_memory.format_transcripts_for_output", return_value="formatted output"):
            result = pre_extract_transcripts(
                ["2026-02-18"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        assert "2026-02-18" in result
        assert Path(result["2026-02-18"]).exists()

    def test_creates_sidecar_file(self, tmp_path):
        """Sidecar .sessions file is created alongside the output file."""
        with mock.patch("load_memory.extract_transcripts", return_value=self._mock_daily_data("sess-abc")), \
             mock.patch("load_memory.format_transcripts_for_output", return_value="formatted"):
            result = pre_extract_transcripts(
                ["2026-02-18"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        sidecar = Path(result["2026-02-18"]).with_suffix(".sessions")
        assert sidecar.exists()
        assert "sess-abc" in sidecar.read_text()

    def test_skips_dates_with_no_data(self, tmp_path):
        """Dates where extract_transcripts returns empty dict are skipped."""
        with mock.patch("load_memory.extract_transcripts", return_value={}), \
             mock.patch("load_memory.format_transcripts_for_output", return_value=""):
            result = pre_extract_transcripts(
                ["2026-02-18"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        assert result == {}

    def test_handles_extraction_error_gracefully(self, tmp_path):
        """Extraction errors for one date don't prevent other dates."""
        def side_effect(date, exclude_session_id=None):
            if date == "2026-02-17":
                raise RuntimeError("disk full")
            return self._mock_daily_data()

        with mock.patch("load_memory.extract_transcripts", side_effect=side_effect), \
             mock.patch("load_memory.format_transcripts_for_output", return_value="formatted"):
            result = pre_extract_transcripts(
                ["2026-02-17", "2026-02-18"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        assert "2026-02-17" not in result
        assert "2026-02-18" in result

    def test_passes_exclude_session_id(self, tmp_path):
        """exclude_session_id is forwarded to extract_transcripts."""
        with mock.patch("load_memory.extract_transcripts", return_value={}) as mock_extract, \
             mock.patch("load_memory.format_transcripts_for_output", return_value=""):
            pre_extract_transcripts(
                ["2026-02-18"], exclude_session_id="sess-xyz", output_dir=str(tmp_path)
            )
        mock_extract.assert_called_once_with("2026-02-18", exclude_session_id="sess-xyz")

    def test_multiple_dates(self, tmp_path):
        """Multiple dates each get their own output and sidecar files."""
        with mock.patch("load_memory.extract_transcripts", return_value=self._mock_daily_data()), \
             mock.patch("load_memory.format_transcripts_for_output", return_value="formatted"):
            result = pre_extract_transcripts(
                ["2026-02-17", "2026-02-18"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        assert len(result) == 2
        assert result["2026-02-17"] != result["2026-02-18"]
        for path in result.values():
            assert Path(path).exists()
            assert Path(path).with_suffix(".sessions").exists()


class TestSynthesisPromptRoutedMarker:
    """Verify [routed] marking is handled by synthesis.py, not subagent."""

    def test_prompt_does_not_contain_routed_instruction(self, tmp_path):
        """Subagent should NOT be told to mark [routed] -- synthesis.py handles it."""
        extract = tmp_path / "extract.txt"
        extract.write_text("content\n")
        prompt = _build_synthesis_prompt(
            ["2026-02-01"], {"2026-02-01": str(extract)}
        )
        assert "Dedup marking" not in prompt

    def test_prompt_contains_synthesis_apply(self, tmp_path):
        """Prompt should instruct subagent to run synthesis.py apply."""
        extract = tmp_path / "extract.txt"
        extract.write_text("content\n")
        prompt = _build_synthesis_prompt(
            ["2026-02-01"], {"2026-02-01": str(extract)}
        )
        assert "synthesis.py apply" in prompt


# =============================================================================
# _get_project_names_str Tests
# =============================================================================


class TestGetProjectNamesStr:
    def test_returns_formatted_names(self):
        index = {"projects": {
            "/path/a": {"name": "alpha"},
            "/path/b": {"name": "beta"},
        }}
        with mock.patch("load_memory.load_json_file", return_value=index):
            result = _get_project_names_str()
        assert "`alpha`" in result
        assert "`beta`" in result
        # Sorted order
        assert result.index("`alpha`") < result.index("`beta`")

    def test_returns_none_registered_when_empty(self):
        with mock.patch("load_memory.load_json_file", return_value={}):
            result = _get_project_names_str()
        assert result == "(none registered)"

    def test_deduplicates_names(self):
        index = {"projects": {
            "/path/a": {"name": "same"},
            "/path/b": {"name": "same"},
        }}
        with mock.patch("load_memory.load_json_file", return_value=index):
            result = _get_project_names_str()
        assert result.count("`same`") == 1

    def test_skips_entries_without_name(self):
        index = {"projects": {
            "/path/a": {"name": "valid"},
            "/path/b": {},
            "/path/c": {"name": ""},
        }}
        with mock.patch("load_memory.load_json_file", return_value=index):
            result = _get_project_names_str()
        assert "`valid`" in result
        assert result.count("`") == 2  # only `valid`


# =============================================================================
# _build_synthesis_instructions Tests
# =============================================================================


class TestBuildSynthesisInstructions:
    def test_contains_tag_format(self):
        instructions = _build_synthesis_instructions("`alpha`, `beta`")
        assert "[scope/type]" in instructions
        assert "`alpha`, `beta`" in instructions

    def test_contains_routing_rules(self):
        instructions = _build_synthesis_instructions("(none registered)")
        assert "Long-term routing" in instructions
        assert "DEDUP REQUIREMENT" in instructions
        assert "GRANULARITY CAP" in instructions

    def test_contains_dedup_requirement(self):
        instructions = _build_synthesis_instructions("`proj`")
        assert "DEDUP REQUIREMENT" in instructions

    def test_contains_daily_summary_template(self):
        instructions = _build_synthesis_instructions("`proj`")
        assert "## Actions" in instructions
        assert "## Decisions" in instructions
        assert "## Learnings" in instructions
        assert "## Lessons" in instructions


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
        assert "===DAILY:" in prompt  # structured output format
        assert "===ROUTE:" in prompt
        assert "===END===" in prompt
        assert "Step 1: Read" not in prompt  # no Read instructions

    def test_embeds_ltm_content(self, tmp_path):
        """Global and project LTM content should be embedded inline."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={
                "transcripts": {"2026-02-22": "transcript"},
                "global_ltm": "## Key Learnings\n- existing",
                "project_ltms": {"proj": "## Key Learnings\n- proj existing"},
            },
        )
        assert "## Key Learnings" in prompt
        assert "- existing" in prompt
        assert "- proj existing" in prompt

    def test_structured_output_instructions(self, tmp_path):
        """Prompt should include delivery instructions and prohibitions."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={"transcripts": {"2026-02-22": "data"}},
        )
        assert "synthesis.py apply" in prompt
        assert "Do NOT generate a summary" in prompt
        assert "Do NOT use any other tools" in prompt

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

    def test_contains_sidecar_references(self, tmp_path):
        """Prompt includes sidecar paths for synthesis.py apply."""
        extract = tmp_path / "extract-2026-02-01.txt"
        extract.write_text("content\n")

        prompt = _build_preextracted_prompt(
            ["2026-02-01"],
            {"2026-02-01": str(extract)},
            "instructions",
        )
        sidecar = str(extract).rsplit(".", 1)[0] + ".sessions"
        assert sidecar in prompt
        assert "synthesis.py apply" in prompt

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

    def test_includes_synthesis_instructions(self, tmp_path):
        """Prompt includes shared synthesis instructions from _build_synthesis_instructions."""
        transcript = tmp_path / "extract.txt"
        transcript.write_text("content\n")

        prompt = _build_synthesis_prompt(
            ["2026-02-01"],
            extracted_files={"2026-02-01": str(transcript)},
        )
        # Should contain instructions generated by _build_synthesis_instructions
        assert "[scope/type]" in prompt
        assert "Long-term routing" in prompt

    def test_passes_embedded_files(self, tmp_path):
        """embedded_files are forwarded to _build_preextracted_prompt."""
        transcript = tmp_path / "extract.txt"
        transcript.write_text("content\n")

        prompt = _build_synthesis_prompt(
            ["2026-02-01"],
            extracted_files={"2026-02-01": str(transcript)},
            embedded_files={
                "transcripts": {"2026-02-01": "EMBEDDED TRANSCRIPT DATA"},
                "global_ltm": "GLOBAL LTM CONTENT",
            },
        )
        assert "EMBEDDED TRANSCRIPT DATA" in prompt
        assert "GLOBAL LTM CONTENT" in prompt

    def test_structured_output_format(self, tmp_path):
        """Prompt includes structured output delimiters."""
        transcript = tmp_path / "extract.txt"
        transcript.write_text("content\n")

        prompt = _build_synthesis_prompt(
            ["2026-02-01"],
            extracted_files={"2026-02-01": str(transcript)},
        )
        assert "===DAILY:" in prompt
        assert "===ROUTE:" in prompt
        assert "===END===" in prompt


# =============================================================================
# _build_embedded_files Tests
# =============================================================================


class TestBuildEmbeddedFiles:
    """Tests for _build_embedded_files helper."""

    def test_reads_transcript_files(self, tmp_path):
        """Transcript extract files are read into embedded dict."""
        extract = tmp_path / "extract-2026-02-01.txt"
        extract.write_text("transcript content")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd:
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            result = _build_embedded_files({"2026-02-01": str(extract)})

        assert result["transcripts"]["2026-02-01"] == "transcript content"

    def test_reads_global_ltm(self, tmp_path):
        """Global LTM file content is read into embedded dict."""
        ltm = tmp_path / "global-long-term-memory.md"
        ltm.write_text("## Key Learnings\n- something")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd:
            mock_gm.return_value = ltm
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            result = _build_embedded_files({})

        assert "## Key Learnings" in result["global_ltm"]

    def test_reads_project_ltms(self, tmp_path):
        """Project LTM files are read and keyed by project name."""
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        (proj_dir / "myproject-long-term-memory.md").write_text("project content")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd:
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = proj_dir
            result = _build_embedded_files({})

        assert result["project_ltms"]["myproject"] == "project content"

    def test_handles_missing_transcript_file(self, tmp_path):
        """Missing transcript files are silently skipped."""
        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd:
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            result = _build_embedded_files({"2026-02-01": "/nonexistent/file.txt"})

        assert "2026-02-01" not in result["transcripts"]

    def test_handles_missing_global_ltm(self, tmp_path):
        """Missing global LTM returns empty string."""
        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd:
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            result = _build_embedded_files({})

        assert result["global_ltm"] == ""

    def test_handles_missing_project_dir(self, tmp_path):
        """Missing project memory dir returns empty project_ltms."""
        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd:
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            result = _build_embedded_files({})

        assert result["project_ltms"] == {}

    def test_multiple_projects(self, tmp_path):
        """Multiple project LTM files are all read."""
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        (proj_dir / "alpha-long-term-memory.md").write_text("alpha content")
        (proj_dir / "beta-long-term-memory.md").write_text("beta content")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd:
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = proj_dir
            result = _build_embedded_files({})

        assert result["project_ltms"]["alpha"] == "alpha content"
        assert result["project_ltms"]["beta"] == "beta content"


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
