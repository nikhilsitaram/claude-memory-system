#!/usr/bin/env python3
"""
Unit tests for load_memory.py

Run with: python -m pytest tests/test_load_memory.py -v
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from load_memory import (
    _build_embedded_files,
    _build_preextracted_prompt,
    _build_synthesis_instructions,
    _build_synthesis_prompt,
    _find_projects_in_extracts,
    _get_project_names_str,
    _strip_profile_sections,
    load_daily_summaries,
    load_global_memory,
    load_project_history,
    load_project_memory,
    pre_extract_transcripts_incremental,
    should_synthesize,
    write_synthesis_prompt,
)

# =============================================================================
# _strip_profile_sections Tests
# =============================================================================


SAMPLE_GLOBAL_LTM = """# Long-Term Memory

## About Me
- **Name**: Test User
- **Role**: Developer

## Current Projects
- **ProjectA**: Building stuff

## Technical Environment
- **OS**: Linux
- **Tools**: vim, git

## Patterns & Preferences
- Prefers tabs over spaces

## Pinned
- (2026-01-01) [pattern] Important pinned item

## Key Actions
- (2026-02-01) [implement] Built feature X

## Key Decisions
- (2026-02-01) [design] Chose architecture Y

## Key Learnings
- (2026-02-01) [gotcha] Watch out for Z

## Key Lessons
- (2026-02-01) [tip] Use command W
"""


class TestStripProfileSections:
    def test_strips_profile_sections(self):
        """About Me, Current Projects, Technical Environment, Patterns & Preferences removed."""
        result = _strip_profile_sections(SAMPLE_GLOBAL_LTM)
        assert "## About Me" not in result
        assert "Test User" not in result
        assert "## Current Projects" not in result
        assert "ProjectA" not in result
        assert "## Technical Environment" not in result
        assert "## Patterns & Preferences" not in result
        assert "tabs over spaces" not in result

    def test_keeps_key_sections(self):
        """Key Actions/Decisions/Learnings/Lessons preserved."""
        result = _strip_profile_sections(SAMPLE_GLOBAL_LTM)
        assert "## Key Actions" in result
        assert "Built feature X" in result
        assert "## Key Decisions" in result
        assert "Chose architecture Y" in result
        assert "## Key Learnings" in result
        assert "Watch out for Z" in result
        assert "## Key Lessons" in result
        assert "Use command W" in result

    def test_keeps_pinned(self):
        """Pinned section preserved."""
        result = _strip_profile_sections(SAMPLE_GLOBAL_LTM)
        assert "## Pinned" in result
        assert "Important pinned item" in result

    def test_empty_content(self):
        """Empty string returns empty."""
        assert _strip_profile_sections("") == ""

    def test_project_ltm_unchanged(self):
        """Content without profile sections passes through unchanged."""
        project_content = """# Project Memory

## Pinned
- Important

## Key Learnings
- (2026-02-01) [pattern] Something useful
"""
        assert _strip_profile_sections(project_content) == project_content


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
        """Project LTM files are read when project found in extracts."""
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        (proj_dir / "myproject-long-term-memory.md").write_text("project content")
        extract = tmp_path / "extract.txt"
        extract.write_text("transcript data")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd, \
             mock.patch("load_memory._find_projects_in_extracts", return_value={"myproject"}):
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = proj_dir
            result = _build_embedded_files({"2026-02-01": str(extract)})

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
        """Multiple project LTM files are read when found in extracts."""
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        (proj_dir / "alpha-long-term-memory.md").write_text("alpha content")
        (proj_dir / "beta-long-term-memory.md").write_text("beta content")
        extract = tmp_path / "extract.txt"
        extract.write_text("transcript data")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd, \
             mock.patch("load_memory._find_projects_in_extracts", return_value={"alpha", "beta"}):
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = proj_dir
            result = _build_embedded_files({"2026-02-01": str(extract)})

        assert result["project_ltms"]["alpha"] == "alpha content"
        assert result["project_ltms"]["beta"] == "beta content"

    def test_filters_unmentioned_projects(self, tmp_path):
        """Project LTMs not in extracts are excluded."""
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        (proj_dir / "mentioned-long-term-memory.md").write_text("included")
        (proj_dir / "unrelated-long-term-memory.md").write_text("excluded")
        extract = tmp_path / "extract.txt"
        extract.write_text("transcript data")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd, \
             mock.patch("load_memory._find_projects_in_extracts", return_value={"mentioned"}):
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = proj_dir
            result = _build_embedded_files({"2026-02-01": str(extract)})

        assert "mentioned" in result["project_ltms"]
        assert "unrelated" not in result["project_ltms"]


# =============================================================================
# _find_projects_in_extracts Tests
# =============================================================================


class TestFindProjectsInExtracts:
    """Tests for _find_projects_in_extracts helper."""

    def test_maps_project_path_to_name(self, tmp_path):
        index = {"projects": {
            "/home/user/myproject": {"name": "myproject", "encodedPaths": ["-home-user-myproject"]}
        }}
        with mock.patch("load_memory.get_projects_index_file") as mock_idx:
            mock_idx.return_value = tmp_path / "index.json"
            (tmp_path / "index.json").write_text(json.dumps(index))
            result = _find_projects_in_extracts({
                "2026-02-01": [{"session_id": "s1", "project_path": "/home/user/myproject"}]
            })
        assert result == {"myproject"}

    def test_multiple_projects(self, tmp_path):
        index = {"projects": {
            "/proj/a": {"name": "alpha", "encodedPaths": []},
            "/proj/b": {"name": "beta", "encodedPaths": []},
        }}
        with mock.patch("load_memory.get_projects_index_file") as mock_idx:
            mock_idx.return_value = tmp_path / "index.json"
            (tmp_path / "index.json").write_text(json.dumps(index))
            result = _find_projects_in_extracts({
                "2026-02-01": [
                    {"session_id": "s1", "project_path": "/proj/a"},
                    {"session_id": "s2", "project_path": "/proj/b"},
                ]
            })
        assert result == {"alpha", "beta"}

    def test_empty_data_returns_empty(self):
        with mock.patch("load_memory.get_projects_index_file") as mock_idx:
            mock_idx.return_value = Path("/nonexistent/index.json")
            result = _find_projects_in_extracts({})
        assert result == set()

    def test_unknown_project_skipped(self, tmp_path):
        index = {"projects": {}}
        with mock.patch("load_memory.get_projects_index_file") as mock_idx:
            mock_idx.return_value = tmp_path / "index.json"
            (tmp_path / "index.json").write_text(json.dumps(index))
            result = _find_projects_in_extracts({
                "2026-02-01": [{"session_id": "s1", "project_path": "/unknown/path"}]
            })
        assert result == set()


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


# =============================================================================
# _build_embedded_files with dailies Tests
# =============================================================================


class TestBuildEmbeddedFilesWithDailies:
    """Test that _build_embedded_files includes existing daily files as merge context."""

    def test_includes_existing_daily_when_available(self, tmp_path):
        """Existing daily file for a date is read into embedded dict."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        (daily_dir / "2026-02-22.md").write_text("## Actions\n- [global/implement] Old stuff")

        extract = tmp_path / "extract.txt"
        extract.write_text("transcript data")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd, \
             mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory._find_projects_in_extracts", return_value=set()):
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            mock_dd.return_value = daily_dir
            result = _build_embedded_files(
                {"2026-02-22": str(extract)},
                include_dailies=True,
            )

        assert "existing_dailies" in result
        assert "2026-02-22" in result["existing_dailies"]
        assert "Old stuff" in result["existing_dailies"]["2026-02-22"]

    def test_no_daily_returns_empty(self, tmp_path):
        """When no daily file exists for a date, it's not in embedded dict."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd, \
             mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory._find_projects_in_extracts", return_value=set()):
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            mock_dd.return_value = daily_dir
            result = _build_embedded_files({}, include_dailies=True)

        assert result.get("existing_dailies", {}) == {}

    def test_include_dailies_false_skips(self, tmp_path):
        """include_dailies=False (default) does not read daily files."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        (daily_dir / "2026-02-22.md").write_text("## Actions\n- stuff")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd, \
             mock.patch("load_memory._find_projects_in_extracts", return_value=set()):
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            result = _build_embedded_files({"2026-02-22": str(tmp_path / "x.txt")})

        assert "existing_dailies" not in result or result["existing_dailies"] == {}


# =============================================================================
# Prompt merge context Tests
# =============================================================================


class TestPromptMergeContext:
    """Test that prompts include merge instructions when existing dailies present."""

    def test_prompt_includes_existing_daily(self):
        """When existing_dailies in embedded, prompt includes merge context section."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={
                "transcripts": {"2026-02-22": "new transcript data"},
                "existing_dailies": {"2026-02-22": "## Actions\n- [global/implement] Old stuff"},
            },
        )
        assert "Existing daily summary" in prompt
        assert "Old stuff" in prompt
        assert "merge" in prompt.lower()

    def test_prompt_no_merge_when_no_dailies(self):
        """Without existing_dailies, no merge section in prompt."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={
                "transcripts": {"2026-02-22": "transcript data"},
            },
        )
        assert "Existing daily summary" not in prompt


# =============================================================================
# Incremental Wiring Tests
# =============================================================================


class TestIncrementalWiring:
    """Verify incremental extraction is wired into the prompt."""

    def test_offsets_arg_in_prompt_when_offsets_present(self):
        """When offsets_path is in embedded_files, apply command includes --offsets-json."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={
                "transcripts": {"2026-02-22": "data"},
                "offsets_path": "/tmp/synthesis-offsets-123.json",
            },
        )
        assert "--offsets-json /tmp/synthesis-offsets-123.json" in prompt

    def test_no_offsets_arg_when_no_offsets(self):
        """Without offsets_path, apply command has no --offsets-json."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={
                "transcripts": {"2026-02-22": "data"},
            },
        )
        assert "--offsets-json" not in prompt


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
            lambda *a, **kw: {"transcripts": {"2026-02-23": "test"}, "global_ltm": "", "project_ltms": {}},
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

    def test_offsets_file_written_when_session_offsets(self, tmp_path, monkeypatch):
        """Session offsets are written to a temp JSON file and embedded in the prompt args."""
        monkeypatch.setattr("load_memory.get_recent_days", lambda **kw: ["2026-02-23"])
        monkeypatch.setattr(
            "load_memory.pre_extract_transcripts_incremental",
            lambda dates, **kw: (
                {"2026-02-23": "/tmp/extract.txt"},
                {"sid1": {"offset": 200, "lines": 5}},
                {"2026-02-23": [{"session_id": "sid1", "project_path": "/test", "messages": ["hi"]}]},
            ),
        )
        # Capture what's passed to _build_embedded_files
        captured_embedded = {}

        def mock_build_embedded(*a, **kw):
            result = {"transcripts": {"2026-02-23": "test"}, "global_ltm": "", "project_ltms": {}}
            return result

        def mock_build_prompt(*a, **kw):
            # Capture the embedded arg passed to _build_synthesis_prompt
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

        # Verify offsets_path was added to embedded files
        assert "offsets_path" in captured_embedded
        offsets_file = Path(captured_embedded["offsets_path"])
        assert offsets_file.exists()
        offsets_data = json.loads(offsets_file.read_text())
        assert offsets_data["sid1"]["offset"] == 200


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
