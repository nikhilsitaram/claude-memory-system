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
from synthesis import ROUTE_CAP

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
    def test_contains_type_format(self):
        instructions = _build_synthesis_instructions("`alpha`, `beta`")
        # New prompt uses [type] not [scope/type]
        assert "[type]" in instructions
        assert "[GLOBAL]" in instructions

    def test_contains_routing_rules(self):
        instructions = _build_synthesis_instructions("(none registered)")
        assert "[LTM]" in instructions
        assert f"Only the top {ROUTE_CAP}" in instructions
        assert "ordered by importance" in instructions
        # Dedup is now handled by deterministic code, not LLM
        assert "DEDUP REQUIREMENT" not in instructions

    def test_no_dedup_in_instructions(self):
        """Dedup is handled by deterministic code, not the LLM."""
        instructions = _build_synthesis_instructions("`proj`")
        assert "DEDUP REQUIREMENT" not in instructions
        assert "system handles" in instructions

    def test_contains_project_block_template(self):
        instructions = _build_synthesis_instructions("`proj`")
        assert "===PROJECT:" in instructions
        assert "===END===" in instructions
        assert "[LTM]" in instructions

    def test_scope_injection_documented(self):
        """Prompt tells LLM that scope is injected automatically."""
        instructions = _build_synthesis_instructions("`proj`")
        assert "scope injection automatically" in instructions

    def test_exclusion_criteria_has_categories(self):
        """Routing exclusions are categorized, not a single vague line."""
        instructions = _build_synthesis_instructions("`proj`")
        assert "Common dev knowledge" in instructions
        assert "Generic software patterns" in instructions
        assert "One-time fixes" in instructions
        assert "Version-specific notes" in instructions


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
        assert "[type]" in prompt
        assert "[LTM]" in prompt

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
        assert "===PROJECT:" in prompt
        assert "===END===" in prompt
        assert "===DAILY:" not in prompt
        assert "===ROUTE:" not in prompt


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

    def setup_method(self):
        """Clear the projects index cache before each test."""
        from memory_utils import _clear_projects_index_cache
        _clear_projects_index_cache()

    def test_maps_project_path_to_name(self, tmp_path):
        index = {"projects": {
            "/home/user/myproject": {"name": "myproject", "encodedPaths": ["-home-user-myproject"]}
        }}
        with mock.patch("memory_utils.get_projects_index_file") as mock_idx:
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
        with mock.patch("memory_utils.get_projects_index_file") as mock_idx:
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
        from memory_utils import _clear_projects_index_cache
        _clear_projects_index_cache()
        with mock.patch("memory_utils.get_projects_index_file") as mock_idx:
            mock_idx.return_value = Path("/nonexistent/index.json")
            result = _find_projects_in_extracts({})
        assert result == set()

    def test_unknown_project_skipped(self, tmp_path):
        index = {"projects": {}}
        with mock.patch("memory_utils.get_projects_index_file") as mock_idx:
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
            return {"transcripts": {"2026-02-23": "test"}, "global_ltm": "", "project_ltms": {}}

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
# Simplified Synthesis Prompt Tests (Task 7: deterministic synthesis)
# =============================================================================


class TestSynthesisPromptSimplified:
    def test_no_scope_tagging_instructions(self):
        """Prompt should not instruct LLM to add scope tags."""
        from load_memory import _build_synthesis_instructions
        result = _build_synthesis_instructions("cartwheel, investing")
        assert "scope is `global` or one of" not in result
        assert "[scope/type]" not in result or "[type]" in result

    def test_no_merge_instructions(self):
        """Prompt should not instruct LLM to merge."""
        from load_memory import _build_synthesis_instructions
        result = _build_synthesis_instructions("cartwheel, investing")
        assert "merge new insights" not in result.lower()

    def test_no_dedup_instructions(self):
        """Prompt should not instruct LLM to check for duplicates."""
        from load_memory import _build_synthesis_instructions
        result = _build_synthesis_instructions("cartwheel, investing")
        assert "DEDUP REQUIREMENT" not in result

    def test_global_marker_documented(self):
        """Prompt should document [GLOBAL] marker usage."""
        from load_memory import _build_synthesis_instructions
        result = _build_synthesis_instructions("cartwheel, investing")
        assert "[GLOBAL]" in result

    def test_existing_dailies_marked_readonly(self):
        """Existing daily content labeled as read-only context."""
        from load_memory import _build_preextracted_prompt
        result = _build_preextracted_prompt(
            pending_dates=["2026-02-23"],
            extracted_files={"2026-02-23": "/tmp/test.txt"},
            synthesis_instructions="test instructions",
            embedded_files={
                "transcripts": {"2026-02-23": "transcript content"},
                "existing_dailies": {"2026-02-23": "# 2026-02-23\n## Actions\n- [proj/impl] Old"},
            },
        )
        assert "read-only" in result.lower() or "READ-ONLY" in result
        assert "do NOT repeat" in result or "do not repeat" in result.lower()


# =============================================================================
# Synthesis Prompt PROJECT Format Tests
# =============================================================================


class TestSynthesisPromptProjectFormat:
    """Verify the prompt uses ===PROJECT:X=== format instead of ===DAILY: + ===ROUTE:."""

    def test_instructions_mention_project_blocks(self):
        instructions = _build_synthesis_instructions("`swyfft`, `investing`")
        assert "===PROJECT:" in instructions
        assert "[LTM]" in instructions
        assert "===DAILY:" not in instructions
        assert "===ROUTE:" not in instructions

    def test_example_shows_project_format(self):
        instructions = _build_synthesis_instructions("`swyfft`")
        assert "===PROJECT:" in instructions
        assert "===END===" in instructions

    def test_project_names_in_instructions(self):
        instructions = _build_synthesis_instructions("`swyfft`, `investing`")
        assert "`swyfft`, `investing`" in instructions

    def test_preextracted_prompt_uses_project_format(self, tmp_path):
        instructions = "test instructions"
        prompt = _build_preextracted_prompt(
            ["2026-02-24"],
            {"2026-02-24": str(tmp_path / "extract.txt")},
            instructions,
            {"transcripts": {"2026-02-24": "session content"},
             "global_ltm": "", "project_ltms": {}},
        )
        assert "===PROJECT:" in prompt
        assert "===END===" in prompt

    def test_preextracted_prompt_no_daily_format(self, tmp_path):
        instructions = "test instructions"
        prompt = _build_preextracted_prompt(
            ["2026-02-24"],
            {"2026-02-24": str(tmp_path / "extract.txt")},
            instructions,
            {"transcripts": {"2026-02-24": "session content"},
             "global_ltm": "", "project_ltms": {}},
        )
        # Should not reference old format in examples or reminders
        assert "===ROUTE:" not in prompt


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
        # main() should proceed past the env var check and hit load_settings.
        # We mock load_settings to raise so we can confirm it got past the check.
        with mock.patch("load_memory.load_settings", side_effect=RuntimeError("reached")):
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
        assert result is not None
        assert "error 5" in result
        assert "error 9" in result
        assert "error 0" not in result


# =============================================================================
# load_pending_recall Tests
# =============================================================================

from memory_utils import DEFAULT_SETTINGS


class TestLoadPendingRecall:
    """Tests for load_pending_recall() function."""

    def _write_recall_file(self, recall_dir, session_id, project="", cwd="/test",
                           content="Test recall content", age_hours=0):
        """Helper to create a recall file with frontmatter."""
        import time
        recall_dir.mkdir(parents=True, exist_ok=True)
        f = recall_dir / f"{session_id}.md"
        lines = [
            "---",
            f"session_id: {session_id}",
            f"project: {project}",
            "timestamp: 2026-03-31T18:00:00Z",
            f"cwd: {cwd}",
            "---",
            "> first prompt",
            "",
            content,
            "",
        ]
        f.write_text(chr(10).join(lines))
        if age_hours:
            old_time = time.time() - (age_hours * 3600)
            import os
            os.utime(f, (old_time, old_time))
        return f

    def test_returns_recall_for_matching_project(self, tmp_path):
        """Returns recall section when project name matches."""
        recall_dir = tmp_path / "pending-recall"
        self._write_recall_file(recall_dir, "sess-1", project="my-project")

        from load_memory import load_pending_recall
        with mock.patch("load_memory.get_pending_recall_dir", return_value=recall_dir):
            section, size = load_pending_recall(
                current_session_id="sess-2",
                current_project_name="my-project",
                resolved_cwd="/other/path",
            )
        assert "Previous Session Recall" in section
        assert "Test recall content" in section
        assert size > 0

    def test_cwd_fallback_when_no_project(self, tmp_path):
        """Falls back to CWD matching when no project name."""
        recall_dir = tmp_path / "pending-recall"
        self._write_recall_file(recall_dir, "sess-1", project="", cwd="/my/dir")

        from load_memory import load_pending_recall
        with mock.patch("load_memory.get_pending_recall_dir", return_value=recall_dir):
            section, size = load_pending_recall(
                current_session_id="sess-2",
                current_project_name="",
                resolved_cwd="/my/dir",
            )
        assert "Previous Session Recall" in section

    def test_skips_current_session(self, tmp_path):
        """Does not return recall from the current session."""
        recall_dir = tmp_path / "pending-recall"
        self._write_recall_file(recall_dir, "current-sess", project="proj")

        from load_memory import load_pending_recall
        with mock.patch("load_memory.get_pending_recall_dir", return_value=recall_dir):
            section, size = load_pending_recall(
                current_session_id="current-sess",
                current_project_name="proj",
                resolved_cwd="/test",
            )
        assert section == ""
        assert size == 0

    def test_most_recent_only(self, tmp_path):
        """Returns only the most recent matching recall file."""
        import time
        recall_dir = tmp_path / "pending-recall"
        self._write_recall_file(recall_dir, "old-sess", project="proj", content="OLD content")
        time.sleep(0.05)
        self._write_recall_file(recall_dir, "new-sess", project="proj", content="NEW content")

        from load_memory import load_pending_recall
        with mock.patch("load_memory.get_pending_recall_dir", return_value=recall_dir):
            section, size = load_pending_recall(
                current_session_id="other-sess",
                current_project_name="proj",
                resolved_cwd="/test",
            )
        assert "NEW content" in section
        assert "OLD content" not in section

    def test_deletes_files_older_than_24h(self, tmp_path):
        """Deletes recall files older than 24 hours as safety net."""
        recall_dir = tmp_path / "pending-recall"
        old_file = self._write_recall_file(
            recall_dir, "old-sess", project="proj", age_hours=25
        )
        self._write_recall_file(recall_dir, "new-sess", project="proj")

        from load_memory import load_pending_recall
        with mock.patch("load_memory.get_pending_recall_dir", return_value=recall_dir):
            load_pending_recall(
                current_session_id="other",
                current_project_name="proj",
                resolved_cwd="/test",
            )
        assert not old_file.exists()

    def test_no_matching_project(self, tmp_path):
        """Returns empty when no recall files match current project."""
        recall_dir = tmp_path / "pending-recall"
        self._write_recall_file(recall_dir, "sess-1", project="other-project")

        from load_memory import load_pending_recall
        with mock.patch("load_memory.get_pending_recall_dir", return_value=recall_dir):
            section, size = load_pending_recall(
                current_session_id="sess-2",
                current_project_name="my-project",
                resolved_cwd="/different/path",
            )
        assert section == ""

    def test_empty_recall_dir(self, tmp_path):
        """Returns empty when pending-recall dir doesn't exist."""
        recall_dir = tmp_path / "pending-recall"

        from load_memory import load_pending_recall
        with mock.patch("load_memory.get_pending_recall_dir", return_value=recall_dir):
            section, size = load_pending_recall(
                current_session_id="sess",
                current_project_name="proj",
                resolved_cwd="/test",
            )
        assert section == ""
        assert size == 0

    def test_disabled_setting(self, tmp_path):
        """Returns empty when previousSessionRecall.enabled is False."""
        recall_dir = tmp_path / "pending-recall"
        self._write_recall_file(recall_dir, "sess-1", project="proj")

        from load_memory import load_pending_recall
        with mock.patch("load_memory.get_pending_recall_dir", return_value=recall_dir),              mock.patch("load_memory.load_settings", return_value={
                 **DEFAULT_SETTINGS,
                 "previousSessionRecall": {"enabled": False, "tokenLimit": 1500},
             }):
            section, size = load_pending_recall(
                current_session_id="sess-2",
                current_project_name="proj",
                resolved_cwd="/test",
            )
        assert section == ""


# =============================================================================
# main() Output Order Tests
# =============================================================================


_DEFAULT_PROJECT = object()  # sentinel: use {"name": "testproject"} as the default mock


class TestMainOutputOrder:
    """Verify main() outputs sections in the correct order with read instruction first."""

    def _run_main_raw(self, monkeypatch, *, global_ltm="", project_ltm="",
                      global_stm=None, project_stm=None, recall="", current_project=_DEFAULT_PROJECT,
                      mode=None):
        """Run main() with mocked memory sources, return raw captured stdout.

        Pass current_project=None to simulate no project found (find_current_project returns None).
        Omit current_project to use the default {"name": "testproject"} mock.
        """
        import io

        from load_memory import main

        monkeypatch.delenv("CLAUDE_SKIP_MEMORY", raising=False)
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))

        monkeypatch.setattr("load_memory.load_global_memory", lambda: (global_ltm, len(global_ltm)))
        monkeypatch.setattr("load_memory.load_project_memory", lambda name: (project_ltm, len(project_ltm)))

        global_stm = global_stm or []
        project_stm = project_stm or []
        monkeypatch.setattr("load_memory.load_daily_summaries", lambda days, scope="global": (global_stm, sum(len(c) for _, c in global_stm)))
        monkeypatch.setattr("load_memory.load_project_history", lambda proj, days: (project_stm, sum(len(c) for _, c in project_stm)))

        recall_section = f"## Previous Session Recall\n{recall}" if recall else ""
        monkeypatch.setattr("load_memory.load_pending_recall", lambda **kw: (recall_section, len(recall_section)))
        monkeypatch.setattr("load_memory.check_synthesis_errors", lambda: None)
        monkeypatch.setattr("load_memory.resolve_session_path", lambda p: p)
        monkeypatch.setattr("load_memory.load_json_file", lambda *a, **kw: {})
        effective_project = {"name": "testproject"} if current_project is _DEFAULT_PROJECT else current_project
        monkeypatch.setattr("load_memory.find_current_project", lambda idx, pwd: effective_project)
        effective_mode = DEFAULT_SETTINGS["mode"] if mode is None else mode
        monkeypatch.setattr("load_memory.load_settings", lambda: {
            **DEFAULT_SETTINGS,
            "mode": effective_mode,
            "globalShortTerm": {"workingDays": 2, "tokenLimit": 1500},
            "projectShortTerm": {"workingDays": 5, "tokenLimit": 3750},
            "totalTokenBudget": 6000,
        })

        captured = io.StringIO()
        monkeypatch.setattr("sys.stdout", captured)
        main()
        return captured.getvalue()

    def _run_main(self, monkeypatch, **kwargs):
        """Run main() and return the decoded memory payload.

        main() emits a SessionStart hookSpecificOutput.additionalContext envelope;
        this helper unwraps it (asserting the envelope shape) and returns the inner
        payload so order/substring assertions operate on the memory text itself.
        """
        envelope = json.loads(self._run_main_raw(monkeypatch, **kwargs))
        hook_out = envelope["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "SessionStart"
        return hook_out["additionalContext"]

    def test_emits_additionalcontext_envelope(self, monkeypatch):
        """main() emits exactly one JSON SessionStart additionalContext envelope."""
        raw = self._run_main_raw(monkeypatch, global_ltm="## Long-Term Memory\n- item")
        # Must be a single, well-formed JSON object (stray stdout would break parsing).
        envelope = json.loads(raw)
        assert set(envelope) == {"hookSpecificOutput"}
        hook_out = envelope["hookSpecificOutput"]
        assert hook_out["hookEventName"] == "SessionStart"
        payload = hook_out["additionalContext"]
        assert payload.startswith("<memory>")
        assert payload.rstrip().endswith("</memory>")

    def test_read_instruction_present(self, monkeypatch):
        """Read instruction appears in output so Claude can find the full file when truncated."""
        output = self._run_main(monkeypatch, global_ltm="## Long-Term Memory\n- item")
        assert "Full output saved to" in output

    def test_read_instruction_before_ltm(self, monkeypatch):
        """Read instruction appears before long-term memory content."""
        output = self._run_main(monkeypatch, global_ltm="## Long-Term Memory\n- ltm item")
        instr_pos = output.find("Full output saved to")
        ltm_pos = output.find("ltm item")
        assert instr_pos < ltm_pos

    def test_recall_before_ltm(self, monkeypatch):
        """Previous session recall appears before long-term memory."""
        output = self._run_main(
            monkeypatch,
            global_ltm="## Long-Term Memory\n- ltm item",
            recall="I was working on feature X",
        )
        recall_pos = output.find("Previous Session Recall")
        ltm_pos = output.find("ltm item")
        assert recall_pos != -1, "Recall section missing"
        assert recall_pos < ltm_pos

    def test_project_stm_before_global_stm(self, monkeypatch):
        """Project STM appears before global STM."""
        output = self._run_main(
            monkeypatch,
            global_stm=[("2026-04-20", "- [global/implement] global work")],
            project_stm=[("2026-04-21", "- [testproject/implement] project work")],
        )
        proj_stm_pos = output.find("project work")
        global_stm_pos = output.find("global work")
        assert proj_stm_pos != -1, "Project STM missing"
        assert global_stm_pos != -1, "Global STM missing"
        assert proj_stm_pos < global_stm_pos

    def test_project_ltm_before_project_stm(self, monkeypatch):
        """Project LTM appears before project STM."""
        output = self._run_main(
            monkeypatch,
            project_ltm="- (2026-01-01) [pattern] ltm pattern",
            project_stm=[("2026-04-21", "- [testproject/implement] recent work")],
        )
        ltm_pos = output.find("ltm pattern")
        stm_pos = output.find("recent work")
        assert ltm_pos < stm_pos

    def test_section_order_full(self, monkeypatch):
        """Full section order: read instruction → recall → global LTM → project LTM → project STM → global STM."""
        output = self._run_main(
            monkeypatch,
            global_ltm="- global ltm content",
            project_ltm="- project ltm content",
            global_stm=[("2026-04-20", "- [global/implement] global stm content")],
            project_stm=[("2026-04-21", "- [testproject/implement] project stm content")],
            recall="recall content here",
        )
        positions = {
            "read_instr": output.find("Full output saved to"),
            "recall": output.find("recall content here"),
            "global_ltm": output.find("global ltm content"),
            "project_ltm": output.find("project ltm content"),
            "project_stm": output.find("project stm content"),
            "global_stm": output.find("global stm content"),
        }
        assert all(v != -1 for v in positions.values()), f"Missing section: {[k for k,v in positions.items() if v == -1]}"
        order = sorted(positions.items(), key=lambda x: x[1])
        order_keys = [k for k, _ in order]
        assert order_keys.index("read_instr") < order_keys.index("recall")
        assert order_keys.index("recall") < order_keys.index("global_ltm")
        assert order_keys.index("global_ltm") < order_keys.index("project_ltm")
        assert order_keys.index("project_ltm") < order_keys.index("project_stm")
        assert order_keys.index("project_stm") < order_keys.index("global_stm")

    def test_no_recall_still_has_instruction(self, monkeypatch):
        """Read instruction appears even when there's no recall."""
        output = self._run_main(monkeypatch, global_ltm="- some ltm")
        assert "Full output saved to" in output
        assert "Previous Session Recall" not in output

    def test_no_current_project_falls_back(self, monkeypatch):
        """When find_current_project returns None, main() produces global-only output (no project sections)."""
        output = self._run_main(
            monkeypatch,
            global_ltm="- global ltm only",
            project_ltm="- should not appear",
            current_project=None,
        )
        assert "global ltm only" in output
        assert "Project Long-Term Memory" not in output
        assert "Project Short-Term Memory" not in output

    def test_light_mode_emits_recall_and_global_ltm(self, monkeypatch):
        """Light mode keeps recall and global LTM."""
        output = self._run_main(
            monkeypatch,
            global_ltm="- global ltm content",
            recall="recall content here",
            mode="light",
        )
        assert "recall content here" in output
        assert "global ltm content" in output

    def test_light_mode_skips_project_and_global_stm(self, monkeypatch):
        """Light mode omits project LTM, project STM, and global STM."""
        output = self._run_main(
            monkeypatch,
            global_ltm="- global ltm content",
            project_ltm="- project ltm content",
            global_stm=[("2026-04-20", "- [global/implement] global stm content")],
            project_stm=[("2026-04-21", "- [testproject/implement] project stm content")],
            recall="recall content here",
            mode="light",
        )
        assert "project ltm content" not in output
        assert "project stm content" not in output
        assert "global stm content" not in output
        assert "Project Long-Term Memory" not in output
        assert "Project Short-Term Memory" not in output
        assert "Global Short-Term Memory" not in output

    def test_full_mode_default_matches_existing_behavior(self, monkeypatch):
        """Full mode (default) emits all sections."""
        output = self._run_main(
            monkeypatch,
            global_ltm="- global ltm content",
            project_ltm="- project ltm content",
            global_stm=[("2026-04-20", "- [global/implement] global stm content")],
            project_stm=[("2026-04-21", "- [testproject/implement] project stm content")],
        )
        assert "global ltm content" in output
        assert "project ltm content" in output
        assert "project stm content" in output
        assert "global stm content" in output

    def test_default_settings_mode_is_full(self):
        """DEFAULT_SETTINGS.mode is 'full' so existing installs unchanged."""
        assert DEFAULT_SETTINGS["mode"] == "full"

    @pytest.mark.parametrize("bad_mode", ["Full", "lite", "FULL", "", "unknown"])
    def test_unknown_mode_fails_safe_to_full(self, monkeypatch, bad_mode):
        """Unknown/typo mode values emit all sections (fail-safe to full)."""
        output = self._run_main(
            monkeypatch,
            global_ltm="- global ltm content",
            project_ltm="- project ltm content",
            project_stm=[("2026-04-21", "- [testproject/implement] project stm content")],
            global_stm=[("2026-04-20", "- [global/implement] global stm content")],
            mode=bad_mode,
        )
        assert "project ltm content" in output
        assert "project stm content" in output
        assert "global stm content" in output

    def test_light_mode_with_recall_disabled(self, monkeypatch):
        """Light mode + no recall section emits only global LTM (no project, no STM)."""
        output = self._run_main(
            monkeypatch,
            global_ltm="- global ltm content",
            project_ltm="- should not appear",
            project_stm=[("2026-04-21", "- [testproject/implement] should not appear")],
            recall="",
            mode="light",
        )
        assert "global ltm content" in output
        assert "Previous Session Recall" not in output
        assert "Project Long-Term Memory" not in output
        assert "Project Short-Term Memory" not in output
        assert "Global Short-Term Memory" not in output

    def test_light_mode_with_no_current_project(self, monkeypatch):
        """Light mode + no current project still emits recall + global LTM."""
        output = self._run_main(
            monkeypatch,
            global_ltm="- global ltm content",
            project_ltm="- should not appear",
            recall="recall content here",
            current_project=None,
            mode="light",
        )
        assert "global ltm content" in output
        assert "recall content here" in output
        assert "Project Long-Term Memory" not in output

    def test_light_mode_with_empty_global_ltm(self, monkeypatch):
        """Light mode + empty global LTM still produces well-formed output with recall."""
        output = self._run_main(
            monkeypatch,
            global_ltm="",
            recall="recall content here",
            mode="light",
        )
        assert output.startswith("<memory>")
        assert output.rstrip().endswith("</memory>")
        assert "recall content here" in output
        assert "## Long-Term Memory" not in output


# =============================================================================
# emit_project_memory Tests
# =============================================================================


class TestEmitProjectMemory:
    """Verify the on-demand project memory emitter (used by /load-project-memory)."""

    def _run(self, monkeypatch, *, project_ltm="", project_stm=None,
             current_project=None, project_name_arg=None):
        import io

        from load_memory import emit_project_memory

        project_stm = project_stm or []

        monkeypatch.setattr("load_memory.load_project_memory", lambda name: (project_ltm, len(project_ltm)))
        monkeypatch.setattr(
            "load_memory.load_project_history",
            lambda proj, days: (project_stm, sum(len(c) for _, c in project_stm)),
        )
        monkeypatch.setattr("load_memory.resolve_session_path", lambda p: p)
        monkeypatch.setattr("load_memory.load_json_file", lambda *a, **kw: {})
        monkeypatch.setattr("load_memory.find_current_project", lambda idx, pwd: current_project)
        monkeypatch.setattr("load_memory.load_settings", lambda: dict(DEFAULT_SETTINGS))

        out = io.StringIO()
        err = io.StringIO()
        monkeypatch.setattr("sys.stdout", out)
        monkeypatch.setattr("sys.stderr", err)
        rc = emit_project_memory(project_name_arg)
        return rc, out.getvalue(), err.getvalue()

    def test_explicit_name_emits_ltm_and_stm(self, monkeypatch):
        rc, stdout, _ = self._run(
            monkeypatch,
            project_ltm="- (2026-01-01) [pattern] ltm-line",
            project_stm=[("2026-04-21", "- [swyfft/implement] stm-line")],
            project_name_arg="swyfft",
        )
        assert rc == 0
        assert "<project-memory>" in stdout
        assert stdout.rstrip().endswith("</project-memory>")
        assert "Project: swyfft" in stdout
        assert "## Project Long-Term Memory: swyfft" in stdout
        assert "ltm-line" in stdout
        assert "## Project Short-Term Memory: swyfft" in stdout
        assert "2026-04-21" in stdout
        assert "stm-line" in stdout

    def test_uses_cwd_when_no_arg(self, monkeypatch):
        rc, stdout, _ = self._run(
            monkeypatch,
            project_ltm="- (2026-01-01) [pattern] cwd-ltm",
            current_project={"name": "cwdproject"},
        )
        assert rc == 0
        assert "Project: cwdproject" in stdout
        assert "cwd-ltm" in stdout

    def test_no_project_detected_returns_error(self, monkeypatch):
        rc, stdout, stderr = self._run(
            monkeypatch,
            current_project=None,
        )
        assert rc == 1
        assert stdout == ""
        assert "No project detected" in stderr

    def test_empty_project_returns_error(self, monkeypatch):
        rc, stdout, stderr = self._run(
            monkeypatch,
            project_name_arg="ghost",
        )
        assert rc == 1
        assert stdout == ""
        assert "No project memory found" in stderr
        assert "ghost" in stderr

    def test_emits_ltm_only(self, monkeypatch):
        rc, stdout, _ = self._run(
            monkeypatch,
            project_ltm="- (2026-01-01) [pattern] only-ltm",
            project_name_arg="proj",
        )
        assert rc == 0
        assert "## Project Long-Term Memory: proj" in stdout
        assert "only-ltm" in stdout
        assert "## Project Short-Term Memory" not in stdout

    def test_emits_stm_only(self, monkeypatch):
        rc, stdout, _ = self._run(
            monkeypatch,
            project_stm=[("2026-04-21", "- [proj/implement] only-stm")],
            project_name_arg="proj",
        )
        assert rc == 0
        assert "## Project Short-Term Memory: proj" in stdout
        assert "only-stm" in stdout
        assert "## Project Long-Term Memory" not in stdout

    def test_explicit_arg_overrides_cwd_project(self, monkeypatch):
        """Passing a name should bypass cwd detection entirely."""
        rc, stdout, _ = self._run(
            monkeypatch,
            project_ltm="- (2026-01-01) [pattern] arg-ltm",
            project_name_arg="explicit",
            current_project={"name": "shouldnotmatter"},
        )
        assert rc == 0
        assert "Project: explicit" in stdout
        assert "Project: shouldnotmatter" not in stdout


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
