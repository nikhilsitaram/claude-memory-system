#!/usr/bin/env python3
"""
Unit tests for synthesis.py

Run with: python -m pytest tests/test_synthesis.py -v
"""

from synthesis import (  # noqa: I001
    DailyFile,  # noqa: F401
    RouteEntry,  # noqa: F401
    SynthesisResult,  # noqa: F401
    append_to_ltm,
    mark_routed_entries,
    parse_synthesis_output,
    write_daily_files,
)


# =============================================================================
# parse_synthesis_output Tests
# =============================================================================


class TestParseSynthesisOutput:
    def test_single_daily_no_routes(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Did something
## Learnings
- [global/pattern] Learned something

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1
        assert result.dailies[0].date == "2026-02-22"
        assert "[global/implement] Did something" in result.dailies[0].content
        assert len(result.routes) == 0

    def test_multiple_dailies_with_routes(self):
        text = """===DAILY:2026-02-21===
# 2026-02-21
## Actions
- [proj/implement] Built feature

===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/pattern] A pattern

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] A pattern

===ROUTE:proj:Key Actions===
- (2026-02-21) [implement] Built feature

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 2
        assert result.dailies[0].date == "2026-02-21"
        assert result.dailies[1].date == "2026-02-22"
        assert len(result.routes) == 2
        assert result.routes[0].scope == "global"
        assert result.routes[0].section == "Key Learnings"
        assert len(result.routes[0].entries) == 1
        assert result.routes[1].scope == "proj"

    def test_text_before_delimiters_ignored(self):
        text = """I'll now generate the synthesis output.

===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1
        assert "I'll now generate" not in result.dailies[0].content

    def test_missing_end_marker_warns(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something"""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1  # still parsed
        assert result.warnings  # warning about missing ===END===

    def test_no_daily_blocks_returns_empty(self):
        text = "Just some random text with no structure"
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 0
        assert len(result.routes) == 0

    def test_empty_route_block_skipped(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===ROUTE:global:Key Learnings===

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.routes) == 0  # empty route skipped

    def test_route_entries_parsed_correctly(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/gotcha] A gotcha

===ROUTE:global:Key Learnings===
- (2026-02-22) [gotcha] A gotcha
- (2026-02-22) [pattern] A pattern

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.routes) == 1
        assert len(result.routes[0].entries) == 2
        assert "[gotcha] A gotcha" in result.routes[0].entries[0]


# =============================================================================
# mark_routed_entries Tests
# =============================================================================


class TestMarkRoutedEntries:
    def test_marks_matching_entries(self):
        daily = DailyFile(
            date="2026-02-22",
            content="""# 2026-02-22
## Actions
- [global/implement] Built feature A
## Learnings
- [global/pattern] Pattern X is useful
- [global/gotcha] Gotcha Y is tricky""",
        )
        routes = [
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-22) [pattern] Pattern X is useful"],
            ),
        ]
        result = mark_routed_entries([daily], routes)
        assert "[routed][global/pattern] Pattern X is useful" in result[0].content
        assert "[routed][global/implement]" not in result[0].content  # not routed
        assert "[routed][global/gotcha]" not in result[0].content  # not routed

    def test_no_routes_no_changes(self):
        daily = DailyFile(
            date="2026-02-22",
            content="## Actions\n- [global/implement] Something",
        )
        result = mark_routed_entries([daily], [])
        assert result[0].content == daily.content

    def test_already_routed_not_double_marked(self):
        daily = DailyFile(
            date="2026-02-22",
            content="## Learnings\n- [routed][global/pattern] Already marked",
        )
        routes = [
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-22) [pattern] Already marked"],
            ),
        ]
        result = mark_routed_entries([daily], routes)
        assert result[0].content.count("[routed]") == 1  # no double marking

    def test_multiple_dailies_marked_independently(self):
        d1 = DailyFile(date="2026-02-21", content="## Learnings\n- [proj/pattern] P1")
        d2 = DailyFile(
            date="2026-02-22", content="## Learnings\n- [global/pattern] P2"
        )
        routes = [
            RouteEntry(
                scope="proj",
                section="Key Learnings",
                entries=["- (2026-02-21) [pattern] P1"],
            ),
        ]
        result = mark_routed_entries([d1, d2], routes)
        assert "[routed]" in result[0].content
        assert "[routed]" not in result[1].content


# =============================================================================
# write_daily_files Tests
# =============================================================================


class TestWriteDailyFiles:
    def test_writes_daily_file(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        dailies = [DailyFile(date="2026-02-22", content="# 2026-02-22\n## Actions\n- something")]
        write_daily_files(dailies, daily_dir)
        written = (daily_dir / "2026-02-22.md").read_text()
        assert "# 2026-02-22" in written
        assert "- something" in written

    def test_overwrites_existing_daily(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        (daily_dir / "2026-02-22.md").write_text("old content")
        dailies = [DailyFile(date="2026-02-22", content="new content")]
        write_daily_files(dailies, daily_dir)
        assert (daily_dir / "2026-02-22.md").read_text().strip() == "new content"

    def test_creates_daily_dir_if_missing(self, tmp_path):
        daily_dir = tmp_path / "daily"
        dailies = [DailyFile(date="2026-02-22", content="content")]
        write_daily_files(dailies, daily_dir)
        assert (daily_dir / "2026-02-22.md").exists()

    def test_returns_written_paths(self, tmp_path):
        daily_dir = tmp_path / "daily"
        dailies = [
            DailyFile(date="2026-02-21", content="day 1"),
            DailyFile(date="2026-02-22", content="day 2"),
        ]
        result = write_daily_files(dailies, daily_dir)
        assert len(result) == 2
        assert str(daily_dir / "2026-02-21.md") in result
        assert str(daily_dir / "2026-02-22.md") in result

    def test_empty_list_writes_nothing(self, tmp_path):
        daily_dir = tmp_path / "daily"
        result = write_daily_files([], daily_dir)
        assert result == []
        assert daily_dir.exists()  # dir still created

    def test_atomic_write_via_tmp(self, tmp_path):
        """Verify no .tmp files linger after write."""
        daily_dir = tmp_path / "daily"
        dailies = [DailyFile(date="2026-02-22", content="content")]
        write_daily_files(dailies, daily_dir)
        tmp_files = list(daily_dir.glob("*.tmp"))
        assert tmp_files == []


# =============================================================================
# append_to_ltm Tests
# =============================================================================


class TestAppendToLtm:
    def test_appends_entries_to_section(self, tmp_path):
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Long-Term Memory\n\n"
            "## Key Learnings\n"
            "<!-- Subject to 30-day decay -->\n"
            "\n"
            "- (2026-02-01) [pattern] Existing entry\n"
        )
        routes = [
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-22) [gotcha] New entry"],
            ),
        ]
        append_to_ltm(routes, ltm_dir=tmp_path, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "- (2026-02-01) [pattern] Existing entry" in content
        assert "- (2026-02-22) [gotcha] New entry" in content

    def test_creates_project_file_from_template(self, tmp_path):
        template = tmp_path / "templates" / "project-long-term-memory.md"
        template.parent.mkdir()
        template.write_text("# {project}\n\n## Key Learnings\n<!-- decay -->\n")
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()

        routes = [
            RouteEntry(
                scope="my-project",
                section="Key Learnings",
                entries=["- (2026-02-22) [pattern] First entry"],
            ),
        ]
        append_to_ltm(
            routes,
            ltm_dir=proj_dir,
            global_file=tmp_path / "global.md",
            template_dir=template.parent,
        )
        proj_file = proj_dir / "my-project-long-term-memory.md"
        assert proj_file.exists()
        assert "First entry" in proj_file.read_text()

    def test_section_not_found_skips(self, tmp_path):
        ltm_file = tmp_path / "global.md"
        ltm_file.write_text("# Memory\n\n## About Me\nJust about me.\n")
        routes = [
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-22) [pattern] Orphaned"],
            ),
        ]
        # Should not crash, should skip
        warnings = append_to_ltm(routes, ltm_dir=tmp_path, global_file=ltm_file)
        assert any("Key Learnings" in w for w in warnings)

    def test_no_duplicate_append(self, tmp_path):
        ltm_file = tmp_path / "global.md"
        ltm_file.write_text(
            "## Key Learnings\n" "<!-- decay -->\n" "- (2026-02-22) [pattern] Already exists\n"
        )
        routes = [
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-22) [pattern] Already exists"],
            ),
        ]
        append_to_ltm(routes, ltm_dir=tmp_path, global_file=ltm_file)
        content = ltm_file.read_text()
        assert content.count("Already exists") == 1  # not duplicated

    def test_multiple_sections_same_file(self, tmp_path):
        ltm_file = tmp_path / "global.md"
        ltm_file.write_text(
            "# Memory\n\n"
            "## Key Learnings\n"
            "<!-- decay -->\n\n"
            "## Key Actions\n"
            "<!-- decay -->\n\n"
        )
        routes = [
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-22) [pattern] A pattern"],
            ),
            RouteEntry(
                scope="global",
                section="Key Actions",
                entries=["- (2026-02-22) [implement] An action"],
            ),
        ]
        append_to_ltm(routes, ltm_dir=tmp_path, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "A pattern" in content
        assert "An action" in content

    def test_no_template_no_file_warns(self, tmp_path):
        routes = [
            RouteEntry(
                scope="missing-project",
                section="Key Learnings",
                entries=["- (2026-02-22) [pattern] Entry"],
            ),
        ]
        warnings = append_to_ltm(
            routes,
            ltm_dir=tmp_path / "project-memory",
            global_file=tmp_path / "global.md",
            template_dir=tmp_path / "no-templates",
        )
        assert len(warnings) > 0


# =============================================================================
# apply_results / run_post_processing Tests
# =============================================================================


from unittest.mock import patch  # noqa: E402

from synthesis import apply_results, run_post_processing  # noqa: E402


class TestRunPostProcessing:
    def test_cleans_up_temp_files(self, tmp_path):
        """Removes extract temp files."""
        extract = tmp_path / "extract.txt"
        extract.write_text("data")

        with patch("synthesis.subprocess.run"), \
             patch("synthesis.prune_stale_state_entries"):
            run_post_processing(extract_paths=[str(extract)])

        assert not extract.exists()

    def test_prunes_stale_state(self):
        """Calls prune_stale_state_entries during post-processing."""
        with patch("synthesis.subprocess.run"), \
             patch("synthesis.prune_stale_state_entries") as mock_prune:
            run_post_processing(extract_paths=[])

        mock_prune.assert_called_once()

    def test_updates_timestamp(self, tmp_path):
        """Writes .last-synthesis timestamp file."""
        with patch("synthesis.subprocess.run"), \
             patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path):
            run_post_processing(extract_paths=[])

        ts_file = tmp_path / ".last-synthesis"
        assert ts_file.exists()
        content = ts_file.read_text()
        assert "T" in content  # ISO format


class TestApplyResults:
    def test_full_pipeline(self, tmp_path):
        """Integration test: parse -> mark_routed -> write -> append."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        global_ltm = tmp_path / "global-long-term-memory.md"
        global_ltm.write_text(
            "## Key Learnings\n<!-- decay -->\n\n"
        )

        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/pattern] Important pattern

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] Important pattern

===END==="""

        output_file = tmp_path / "synthesis-output.txt"
        output_file.write_text(output_text)

        with patch("memory_utils.get_daily_dir", return_value=daily_dir), \
             patch("memory_utils.get_global_memory_file", return_value=global_ltm), \
             patch("memory_utils.get_project_memory_dir", return_value=tmp_path / "project-memory"), \
             patch("memory_utils.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"):
            apply_results(
                output_file=str(output_file),
                extract_paths=[],
            )

        # Daily file written with [routed] marking
        daily_content = (daily_dir / "2026-02-22.md").read_text()
        assert "[routed]" in daily_content

        # LTM updated
        ltm_content = global_ltm.read_text()
        assert "Important pattern" in ltm_content

    def test_no_dailies_skips_everything(self, tmp_path):
        """If output has no ===DAILY: blocks, nothing happens."""
        output_file = tmp_path / "bad-output.txt"
        output_file.write_text("just garbage text")

        with patch("synthesis.run_post_processing") as mock_post:
            apply_results(output_file=str(output_file), extract_paths=[])
            mock_post.assert_not_called()

    def test_warnings_printed_to_stderr(self, tmp_path, capsys):
        """Warnings from parsing are printed to stderr."""
        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something"""
        # No ===END=== marker

        output_file = tmp_path / "output.txt"
        output_file.write_text(output_text)

        with patch("memory_utils.get_daily_dir", return_value=tmp_path / "daily"), \
             patch("memory_utils.get_global_memory_file", return_value=tmp_path / "g.md"), \
             patch("memory_utils.get_project_memory_dir", return_value=tmp_path / "pm"), \
             patch("memory_utils.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"):
            apply_results(output_file=str(output_file), extract_paths=[])

        captured = capsys.readouterr()
        assert "Warning:" in captured.err


# =============================================================================
# apply_results --offsets-json Tests
# =============================================================================


class TestApplyResultsWithOffsets:
    """Test state update when --offsets-json is provided."""

    def test_updates_synthesis_state(self, tmp_path):
        """apply_results calls update_synthesis_state when offsets_json provided."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        global_ltm = tmp_path / "global-long-term-memory.md"
        global_ltm.write_text("## Key Learnings\n<!-- decay -->\n\n")

        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/pattern] Something

===END==="""

        output_file = tmp_path / "output.txt"
        output_file.write_text(output_text)

        offsets_file = tmp_path / "offsets.json"
        offsets_file.write_text('{"s1": {"offset": 500, "lines": 10}}')

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=global_ltm), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "pm"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"), \
             patch("synthesis.update_synthesis_state") as mock_update:
            apply_results(
                output_file=str(output_file),
                extract_paths=[],
                offsets_json=str(offsets_file),
            )

        mock_update.assert_called_once_with({"s1": {"offset": 500, "lines": 10}})

    def test_no_offsets_no_state_update(self, tmp_path):
        """apply_results does not call update_synthesis_state when no offsets."""
        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===END==="""

        output_file = tmp_path / "output.txt"
        output_file.write_text(output_text)

        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "g.md"), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "pm"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"), \
             patch("synthesis.update_synthesis_state") as mock_update:
            apply_results(output_file=str(output_file), extract_paths=[])

        mock_update.assert_not_called()

    def test_offsets_file_missing_warns(self, tmp_path):
        """apply_results warns if offsets file doesn't exist."""
        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===END==="""

        output_file = tmp_path / "output.txt"
        output_file.write_text(output_text)

        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "g.md"), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "pm"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"), \
             patch("synthesis.update_synthesis_state") as mock_update:
            # Should not crash even with missing file
            apply_results(
                output_file=str(output_file),
                extract_paths=[],
                offsets_json=str(tmp_path / "missing.json"),
            )

        mock_update.assert_not_called()

    def test_offsets_file_invalid_json_warns(self, tmp_path, capsys):
        """apply_results warns if offsets file has invalid JSON."""
        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===END==="""

        output_file = tmp_path / "output.txt"
        output_file.write_text(output_text)

        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        offsets_file = tmp_path / "bad.json"
        offsets_file.write_text("{invalid json")

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "g.md"), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "pm"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"), \
             patch("synthesis.update_synthesis_state") as mock_update:
            apply_results(
                output_file=str(output_file),
                extract_paths=[],
                offsets_json=str(offsets_file),
            )

        mock_update.assert_not_called()
        captured = capsys.readouterr()
        assert "Could not update synthesis state" in captured.err


class TestRunPostProcessingOffsetsCleanup:
    """Test that run_post_processing cleans up offsets file."""

    def test_cleans_up_offsets_file(self, tmp_path):
        """Offsets file is cleaned up during post-processing."""
        offsets_file = tmp_path / "offsets.json"
        offsets_file.write_text('{"s1": {"offset": 100}}')

        with patch("synthesis.subprocess.run"), \
             patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path):
            run_post_processing(
                extract_paths=[],
                offsets_json=str(offsets_file),
            )

        assert not offsets_file.exists()

    def test_no_offsets_no_error(self, tmp_path):
        """run_post_processing works fine without offsets_json."""
        with patch("synthesis.subprocess.run"), \
             patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path):
            run_post_processing(extract_paths=[])

        # Should not raise


# =============================================================================
# End-to-End Integration Tests
# =============================================================================


class TestEndToEnd:
    """Full pipeline integration test with real file operations."""

    def test_cli_apply(self, tmp_path):
        """Test the CLI entry point with a realistic output file."""
        # Set up directory structure
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        template_dir.joinpath("project-long-term-memory.md").write_text(
            "# {project}\n\n## Key Learnings\n<!-- decay -->\n\n## Key Lessons\n<!-- decay -->\n"
        )

        global_ltm = memory_dir / "global-long-term-memory.md"
        global_ltm.write_text(
            "# Global LTM\n\n## Key Learnings\n<!-- decay -->\n\n## Key Lessons\n<!-- decay -->\n"
        )

        output_file = tmp_path / "output.txt"
        output_file.write_text("""===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Built something cool
## Learnings
- [global/pattern] Pattern is useful

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] Pattern is useful

===END===
""")

        # Verify the parser works end-to-end
        result = parse_synthesis_output(output_file.read_text())
        assert len(result.dailies) == 1
        assert len(result.routes) == 1

        marked = mark_routed_entries(result.dailies, result.routes)
        assert "[routed]" in marked[0].content

        write_daily_files(marked, daily_dir)
        assert (daily_dir / "2026-02-22.md").exists()
        assert "[routed]" in (daily_dir / "2026-02-22.md").read_text()

        warnings = append_to_ltm(
            result.routes,
            ltm_dir=proj_dir,
            global_file=global_ltm,
            template_dir=template_dir,
        )
        assert len(warnings) == 0
        assert "Pattern is useful" in global_ltm.read_text()

    def test_multi_day_multi_scope(self, tmp_path):
        """Test pipeline with multiple days and both global + project routes."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        template_dir.joinpath("project-long-term-memory.md").write_text(
            "# {project}\n\n## Key Learnings\n<!-- decay -->\n\n## Key Actions\n<!-- decay -->\n"
        )

        global_ltm = tmp_path / "global-long-term-memory.md"
        global_ltm.write_text(
            "# Global LTM\n\n## Key Learnings\n<!-- decay -->\n\n## Key Actions\n<!-- decay -->\n"
        )

        output_text = """===DAILY:2026-02-21===
# 2026-02-21
## Actions
- [myproj/implement] Built feature X
## Learnings
- [global/pattern] Global insight A

===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Refactored module Y
## Learnings
- [myproj/gotcha] Watch out for edge case Z

===ROUTE:global:Key Learnings===
- (2026-02-21) [pattern] Global insight A

===ROUTE:global:Key Actions===
- (2026-02-22) [implement] Refactored module Y

===ROUTE:myproj:Key Learnings===
- (2026-02-22) [gotcha] Watch out for edge case Z

===ROUTE:myproj:Key Actions===
- (2026-02-21) [implement] Built feature X

===END===
"""

        result = parse_synthesis_output(output_text)
        assert len(result.dailies) == 2
        assert len(result.routes) == 4

        marked = mark_routed_entries(result.dailies, result.routes)
        # All tagged entries should be routed
        assert "[routed][global/pattern]" in marked[0].content
        assert "[routed][myproj/implement]" in marked[0].content
        assert "[routed][global/implement]" in marked[1].content
        assert "[routed][myproj/gotcha]" in marked[1].content

        write_daily_files(marked, daily_dir)
        assert (daily_dir / "2026-02-21.md").exists()
        assert (daily_dir / "2026-02-22.md").exists()

        warnings = append_to_ltm(
            result.routes,
            ltm_dir=proj_dir,
            global_file=global_ltm,
            template_dir=template_dir,
        )
        assert len(warnings) == 0

        # Global LTM has both entries
        global_content = global_ltm.read_text()
        assert "Global insight A" in global_content
        assert "Refactored module Y" in global_content

        # Project LTM was created from template and has both entries
        proj_file = proj_dir / "myproj-long-term-memory.md"
        assert proj_file.exists()
        proj_content = proj_file.read_text()
        assert "Watch out for edge case Z" in proj_content
        assert "Built feature X" in proj_content
