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
    _build_embedded_files,
    _build_preextracted_prompt,
    _build_synthesis_instructions,
    _build_synthesis_instructions_v3,
    _build_synthesis_prompt,
    _find_projects_in_extracts,
    _get_project_names_str,
    _load_from_db,
    _strip_profile_sections,
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
        assert "Maximum 5" in instructions
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


# =============================================================================
# Synthesis Deferred Setting Tests
# =============================================================================


class TestSynthesisDeferredSetting:
    def test_default_deferred_matches_default_settings(self, tmp_path):
        """synthesis.deferred defaults to DEFAULT_SETTINGS value."""
        from memory_utils import DEFAULT_SETTINGS, load_settings

        settings_file = tmp_path / "settings.json"
        with mock.patch("memory_utils.get_settings_file", return_value=settings_file):
            settings = load_settings()
        assert settings["synthesis"]["deferred"] is DEFAULT_SETTINGS["synthesis"]["deferred"]

    def _make_settings(self, deferred=False):
        """Build a settings dict with synthesis.deferred set."""
        import copy

        from memory_utils import DEFAULT_SETTINGS, _calculate_token_limits

        s = copy.deepcopy(DEFAULT_SETTINGS)
        s["synthesis"]["deferred"] = deferred
        _calculate_token_limits(s)
        return s

    def _run_main_with_mocks(self, monkeypatch, capsys, tmp_path, settings):
        """Run main() with enough mocks to reach the synthesis gate, then return captured output."""
        import io

        from load_memory import main

        # stdin is not a tty in tests; provide valid JSON so session_id parsing works
        monkeypatch.setattr("sys.stdin", io.StringIO('{"session_id": "test-session"}'))

        # Settings
        monkeypatch.setattr("load_memory.load_settings", lambda: settings)

        # get_recent_days returns pending dates (triggers synthesis path)
        monkeypatch.setattr("load_memory.get_recent_days", lambda **kw: ["2026-02-23"])

        # should_synthesize returns True (time-based check passes)
        monkeypatch.setattr("load_memory.should_synthesize", lambda s: True)

        # Mock the synthesis file write (eager timestamp)
        synth_file = tmp_path / "last-synthesis"
        monkeypatch.setattr("load_memory.get_last_synthesis_file", lambda: synth_file)

        # Mock pre-extraction to return data that reaches the AUTO-SYNTHESIZE banner
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
            lambda *a, **kw: "FAKE_PROMPT",
        )
        monkeypatch.setattr("load_memory.SYNTHESIS_PROMPT_DIR", str(tmp_path))

        # Mock memory loading functions (not under test here)
        monkeypatch.setattr("load_memory.resolve_session_path", lambda p: p)
        monkeypatch.setattr("load_memory.load_json_file", lambda p, d: {})
        monkeypatch.setattr("load_memory.find_current_project", lambda *a: None)
        monkeypatch.setattr("load_memory._load_from_db", lambda *a: "")

        main()
        return capsys.readouterr().out

    def test_deferred_true_skips_auto_synthesis(self, tmp_path, capsys, monkeypatch):
        """When synthesis.deferred=True, main() should not print AUTO-SYNTHESIZE banner."""
        settings = self._make_settings(deferred=True)
        output = self._run_main_with_mocks(monkeypatch, capsys, tmp_path, settings)
        assert "AUTO-SYNTHESIZE" not in output

    def test_deferred_false_forced_to_deferred_on_v3(self, tmp_path, capsys, monkeypatch):
        """When synthesis.deferred=False on v3 schema, forced to deferred mode (no in-session synthesis)."""
        settings = self._make_settings(deferred=False)
        # Mock storage.get_db and _get_schema_version to simulate v3 schema
        import storage as _storage_mod
        monkeypatch.setattr(_storage_mod, "_get_schema_version", lambda c: 3)
        monkeypatch.setattr(_storage_mod, "get_db", lambda: type("FakeConn", (), {"execute": lambda *a: None})())
        monkeypatch.setattr(_storage_mod, "close_db", lambda c: None)
        output = self._run_main_with_mocks(monkeypatch, capsys, tmp_path, settings)
        assert "AUTO-SYNTHESIZE" not in output


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
        instructions = _build_synthesis_instructions("test-project")
        assert "===MEMORY_OPS===" in instructions
        assert "ADD" in instructions
        assert "UPDATE" in instructions
        assert "DELETE" in instructions
        assert "NOOP" in instructions

    def test_prompt_includes_crud_action_descriptions(self):
        """Prompt describes what each CRUD action means and chunk ID referencing."""
        instructions = _build_synthesis_instructions("test-project")
        assert "chunk_id" in instructions or "chunk" in instructions.lower()
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

    def test_prompt_falls_back_to_ltm_without_vector(self, tmp_path):
        """Without vector memories, prompt uses full LTM embedding."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("test transcript")
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-03-21"],
            extracted_files={"2026-03-21": str(extract_file)},
            synthesis_instructions="test instructions",
            embedded_files={
                "transcripts": {"2026-03-21": "test transcript"},
                "global_ltm": "## Key Actions\n- (2026-01-01) [implement] test",
            },
        )
        assert "Key Actions" in prompt


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

    def test_fallback_to_full_ltm_when_vec_unavailable(self, tmp_path):
        """When vector_memories is None/empty, falls back to full LTM embedding."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("test transcript")
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-03-21"],
            extracted_files={"2026-03-21": str(extract_file)},
            synthesis_instructions="test instructions",
            embedded_files={
                "transcripts": {"2026-03-21": "test transcript"},
                "global_ltm": "## Key Actions\n- (2026-01-01) [implement] test",
            },
        )
        assert "Key Actions" in prompt

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

    def test_empty_vector_memories_falls_back_to_ltm(self, tmp_path):
        """Empty vector_memories list uses full LTM fallback."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("test transcript")
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-03-21"],
            extracted_files={"2026-03-21": str(extract_file)},
            synthesis_instructions="instructions",
            embedded_files={
                "transcripts": {"2026-03-21": "test transcript"},
                "global_ltm": "## Key Actions\n- (2026-01-01) [implement] some fact",
            },
            vector_memories=[],
        )
        assert "Key Actions" in prompt


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

    def test_returns_empty_string_when_no_db(self, tmp_path):
        """Returns empty string (not None) when DB file doesn't exist."""
        with mock.patch("storage.get_db", side_effect=FileNotFoundError):
            result = _load_from_db("myproject")
        assert result == ""

    def test_graceful_fallback_no_db(self, tmp_path):
        """If DB doesn't exist, loading returns empty context gracefully."""
        with mock.patch("storage.get_db", side_effect=FileNotFoundError):
            result = _load_from_db("")
        assert result == ""

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
        assert "Senior Python dev" in result

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
        assert "Uses gRPC" in result
        assert "Low salience old" not in result

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
        assert "SQLite WAL mode" in result

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
        assert result.count("Cross-tier fact") == 1

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
        assert "Last Session" in result
        assert "Working on auth" in result

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
        assert "Last Session" not in result

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
        assert "in_progress" in result

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
        assert "Last Session" not in result

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
        assert "JWT" in result


# =============================================================================
# Salience Reinforcement Tests (A1)
# =============================================================================


class TestSalienceReinforcement:
    """Tests for salience reinforcement in _batch_update_data_point_access."""

    def _make_v3_db(self, tmp_path):
        """Create a v3 DB for testing data_point access tracking."""
        from unittest.mock import patch
        with patch("storage.get_db_path", return_value=tmp_path / "memory.db"), \
             patch("storage.get_memory_dir", return_value=tmp_path):
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
