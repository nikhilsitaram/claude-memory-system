#!/usr/bin/env python3
"""
Unit tests for synthesis.py

Run with: python -m pytest tests/test_synthesis.py -v
"""

from pathlib import Path  # noqa: F401, I001

from synthesis import (
    MIN_ROUTE_KEYWORDS,  # noqa: F401
    ROUTE_CAP,  # noqa: F401
    SECTION_ORDER,  # noqa: F401
    TYPE_TO_SECTION,  # noqa: F401
    DailyFile,
    MemoryOp,
    ProjectBlock,
    RouteEntry,  # noqa: F401
    SynthesisResult,  # noqa: F401
    append_to_ltm,
    build_dailies_from_project_blocks,
    extract_routes_from_project_blocks,  # noqa: F401
    inject_scopes,
    mark_routed_entries,
    merge_daily_sections,
    parse_daily_sections,
    parse_synthesis_output,
    write_daily_files,
)


# =============================================================================
# parse_daily_sections Tests
# =============================================================================


class TestParseDailySections:
    def test_all_four_sections(self):
        content = (
            "# 2026-02-23\n"
            "## Actions\n"
            "- [impl] Did A\n"
            "- [impl] Did B\n"
            "## Decisions\n"
            "- [design] Chose X\n"
            "## Learnings\n"
            "- [gotcha] Found bug\n"
            "## Lessons\n"
            "- [tip] Use Y\n"
        )
        result = parse_daily_sections(content)
        assert result["date"] == "2026-02-23"
        assert result["Actions"] == ["- [impl] Did A", "- [impl] Did B"]
        assert result["Decisions"] == ["- [design] Chose X"]
        assert result["Learnings"] == ["- [gotcha] Found bug"]
        assert result["Lessons"] == ["- [tip] Use Y"]

    def test_missing_sections_are_empty_lists(self):
        content = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        result = parse_daily_sections(content)
        assert result["Actions"] == ["- [impl] Did A"]
        assert result["Decisions"] == []
        assert result["Learnings"] == []
        assert result["Lessons"] == []

    def test_preserves_routed_prefix(self):
        content = "# 2026-02-23\n## Actions\n- [routed][proj/impl] Old entry\n- [proj/impl] New entry\n"
        result = parse_daily_sections(content)
        assert len(result["Actions"]) == 2
        assert "[routed]" in result["Actions"][0]

    def test_skips_html_comments(self):
        content = "# 2026-02-23\n## Actions\n<!-- template hint -->\n- [impl] Did A\n"
        result = parse_daily_sections(content)
        assert len(result["Actions"]) == 1

    def test_empty_content(self):
        result = parse_daily_sections("")
        assert result["date"] == ""
        assert all(result[s] == [] for s in ["Actions", "Decisions", "Learnings", "Lessons"])

    def test_no_date_header(self):
        content = "## Actions\n- [impl] Did A\n"
        result = parse_daily_sections(content)
        assert result["date"] == ""
        assert result["Actions"] == ["- [impl] Did A"]


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

    def test_merges_with_existing_daily(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        (daily_dir / "2026-02-22.md").write_text("# 2026-02-22\n## Actions\n- [impl] Old action\n")
        dailies = [DailyFile(date="2026-02-22", content="# 2026-02-22\n## Actions\n- [impl] New action")]
        write_daily_files(dailies, daily_dir)
        content = (daily_dir / "2026-02-22.md").read_text()
        assert "- [impl] Old action" in content
        assert "- [impl] New action" in content

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
# write_daily_files Merge Tests
# =============================================================================


class TestWriteDailyFilesMerge:
    def test_first_write_creates_file(self, tmp_path):
        dailies = [DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [impl] Did A")]
        written = write_daily_files(dailies, daily_dir=tmp_path)
        assert len(written) == 1
        assert "- [impl] Did A" in Path(written[0]).read_text()

    def test_second_write_merges_not_overwrites(self, tmp_path):
        # First write
        first = [DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [impl] Did A")]
        write_daily_files(first, daily_dir=tmp_path)

        # Second write with different entries
        second = [DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [impl] Did B")]
        write_daily_files(second, daily_dir=tmp_path)

        content = (tmp_path / "2026-02-23.md").read_text()
        assert "- [impl] Did A" in content
        assert "- [impl] Did B" in content

    def test_second_write_deduplicates(self, tmp_path):
        first = [DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [impl] Did A")]
        write_daily_files(first, daily_dir=tmp_path)

        # Same entry again
        second = [DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [impl] Did A")]
        write_daily_files(second, daily_dir=tmp_path)

        content = (tmp_path / "2026-02-23.md").read_text()
        assert content.count("- [impl] Did A") == 1


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
            "- (2026-02-01) [pattern] Existing entry about filesystem operations\n"
        )
        routes = [
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-22) [gotcha] Tailscale MTU black hole drops packets silently"],
            ),
        ]
        append_to_ltm(routes, ltm_dir=tmp_path, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "- (2026-02-01) [pattern] Existing entry about filesystem operations" in content
        assert "Tailscale MTU" in content

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
                entries=["- (2026-02-22) [pattern] pytest conftest shared fixtures improve isolation"],
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
        assert "pytest conftest" in proj_file.read_text()

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
                entries=["- (2026-02-22) [pattern] pytest conftest shared fixtures improve isolation"],
            ),
            RouteEntry(
                scope="global",
                section="Key Actions",
                entries=["- (2026-02-22) [implement] docker compose networking bridge configuration setup"],
            ),
        ]
        append_to_ltm(routes, ltm_dir=tmp_path, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "pytest conftest" in content
        assert "docker compose" in content

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


class TestAppendToLtmKeywordDedup:
    def test_rejects_near_duplicate_by_keyword_overlap(self, tmp_path):
        """Near-duplicate with different wording is rejected."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n## Key Learnings\n"
            "<!-- gotchas -->\n"
            "- (2026-02-20) [gotcha] Tailscale MTU black hole drops packets silently\n"
        )
        routes = [RouteEntry(
            scope="global",
            section="Key Learnings",
            entries=["- (2026-02-23) [gotcha] Tailscale MTU black hole silently drops packets"],
        )]
        append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        assert content.count("Tailscale MTU") == 1  # Not added

    def test_allows_genuinely_different_entry(self, tmp_path):
        """Different entry passes dedup check."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n## Key Learnings\n"
            "<!-- gotchas -->\n"
            "- (2026-02-20) [gotcha] Tailscale MTU black hole\n"
        )
        routes = [RouteEntry(
            scope="global",
            section="Key Learnings",
            entries=["- (2026-02-23) [pattern] pytest conftest.py shared fixtures"],
        )]
        append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "pytest conftest" in content

    def test_route_cap_enforced(self, tmp_path):
        """Max 5 entries per file per synthesis run."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text("# Global LTM\n## Key Lessons\n<!-- tips -->\n")
        # Use genuinely different entries with enough keywords to pass quality floor
        topics = [
            "pytest fixtures isolation improves test reliability",
            "docker compose networking requires bridge configuration",
            "git rebase workflow strategy preserves commit history",
            "tailscale subnet routing enables private network access",
            "redis caching invalidation prevents stale data serving",
            "nginx reverse proxy headers forwarding configuration",
            "kubernetes pod scheduling affinity node selection",
            "terraform state locking prevents concurrent modifications",
        ]
        entries = [f"- (2026-02-23) [tip] {t}" for t in topics]
        routes = [RouteEntry(scope="global", section="Key Lessons", entries=entries)]
        warnings = append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        # Only 5 should be added (route cap)
        added = sum(1 for t in topics if t in content)
        assert added == ROUTE_CAP
        assert any("cap" in w.lower() or "limit" in w.lower() for w in warnings)


# =============================================================================
# Cross-Section Dedup Tests
# =============================================================================


class TestAppendToLtmCrossSectionDedup:
    def test_learning_blocks_same_lesson(self, tmp_path):
        """Near-dup in Key Learnings prevents routing to Key Lessons."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n\n"
            "## Key Learnings\n"
            "<!-- decay -->\n"
            "- (2026-02-20) [pattern] pytest conftest shared fixtures improve test isolation\n\n"
            "## Key Lessons\n"
            "<!-- decay -->\n"
        )
        routes = [RouteEntry(
            scope="global",
            section="Key Lessons",
            entries=["- (2026-02-23) [tip] pytest conftest shared fixtures for test isolation"],
        )]
        append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        # The near-dup should NOT appear in Key Lessons
        assert "Key Lessons" in content
        assert content.count("conftest") == 1  # only the existing one in Key Learnings

    def test_pinned_blocks_route(self, tmp_path):
        """Entry in Pinned section prevents routing to Key Learnings."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n\n"
            "## Pinned\n"
            "- (2026-02-01) [design] Two-tier memory architecture with project and global tiers\n\n"
            "## Key Learnings\n"
            "<!-- decay -->\n"
        )
        routes = [RouteEntry(
            scope="global",
            section="Key Learnings",
            entries=["- (2026-02-23) [pattern] Two-tier memory architecture project and global tiers"],
        )]
        append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        # Should not be added to Key Learnings (pinned already covers it)
        assert content.count("Two-tier memory") == 1

    def test_different_sections_different_content_both_routed(self, tmp_path):
        """Genuinely different entries in different sections both pass."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n\n"
            "## Key Learnings\n"
            "<!-- decay -->\n\n"
            "## Key Lessons\n"
            "<!-- decay -->\n"
        )
        routes = [
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-23) [gotcha] Tailscale MTU black hole drops packets silently"],
            ),
            RouteEntry(
                scope="global",
                section="Key Lessons",
                entries=["- (2026-02-23) [tip] pytest conftest shared fixtures improve test isolation"],
            ),
        ]
        append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "Tailscale MTU" in content
        assert "pytest conftest" in content

    def test_intra_batch_cross_section_dedup(self, tmp_path):
        """Same concept routed to two sections in same batch — second rejected."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n\n"
            "## Key Learnings\n"
            "<!-- decay -->\n\n"
            "## Key Lessons\n"
            "<!-- decay -->\n"
        )
        routes = [
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-23) [pattern] Tailscale MTU black hole drops packets silently"],
            ),
            RouteEntry(
                scope="global",
                section="Key Lessons",
                entries=["- (2026-02-23) [tip] Tailscale MTU black hole silently drops packets"],
            ),
        ]
        append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        # Only the first one should be added
        assert content.count("Tailscale MTU") == 1


# =============================================================================
# Quality Floor Tests
# =============================================================================


class TestAppendToLtmQualityFloor:
    def test_thin_entry_rejected(self, tmp_path):
        """Entry with too few keywords is rejected with warning."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text("# Global LTM\n## Key Lessons\n<!-- decay -->\n")
        routes = [RouteEntry(
            scope="global",
            section="Key Lessons",
            entries=["- (2026-02-23) [tip] Use git stash"],
        )]
        warnings = append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "git stash" not in content
        assert any("quality floor" in w for w in warnings)

    def test_substantive_entry_passes(self, tmp_path):
        """Entry with enough keywords is accepted."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text("# Global LTM\n## Key Learnings\n<!-- decay -->\n")
        routes = [RouteEntry(
            scope="global",
            section="Key Learnings",
            entries=["- (2026-02-23) [gotcha] Tailscale MTU black hole drops packets silently on WSL2"],
        )]
        warnings = append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "Tailscale MTU" in content
        assert not any("quality floor" in w for w in warnings)

    def test_quality_floor_does_not_count_toward_route_cap(self, tmp_path):
        """Rejected entries don't consume route cap slots."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text("# Global LTM\n## Key Lessons\n<!-- decay -->\n")
        # First 4 entries are thin (below quality floor), then 5 substantive
        thin_entries = [f"- (2026-02-23) [tip] Do {chr(65 + i)}" for i in range(4)]
        topics = [
            "pytest fixtures provide test isolation automatically",
            "docker compose networking requires bridge configuration",
            "git rebase workflow strategy preserves commit history",
            "tailscale subnet routing enables private network access",
            "redis caching invalidation prevents stale data serving",
        ]
        good_entries = [f"- (2026-02-23) [tip] {t}" for t in topics]
        routes = [RouteEntry(
            scope="global",
            section="Key Lessons",
            entries=thin_entries + good_entries,
        )]
        append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        # All 5 good entries should be added (thin ones don't count toward cap)
        added = sum(1 for t in topics if t in content)
        assert added == ROUTE_CAP

    def test_boundary_exactly_min_keywords_passes(self, tmp_path):
        """Entry with exactly MIN_ROUTE_KEYWORDS keywords passes."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text("# Global LTM\n## Key Learnings\n<!-- decay -->\n")
        # Construct entry with exactly MIN_ROUTE_KEYWORDS meaningful keywords
        # Keywords: "pytest", "fixtures", "shared", "isolation" (4 keywords)
        routes = [RouteEntry(
            scope="global",
            section="Key Learnings",
            entries=["- (2026-02-23) [pattern] pytest fixtures shared isolation"],
        )]
        from memory_utils import extract_entry_keywords
        kw = extract_entry_keywords(routes[0].entries[0])
        assert len(kw) >= MIN_ROUTE_KEYWORDS, f"Test setup: expected >= {MIN_ROUTE_KEYWORDS} keywords, got {len(kw)}: {kw}"

        warnings = append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "pytest fixtures" in content
        assert not any("quality floor" in w for w in warnings)

    def test_combined_quality_floor_and_cross_section_dedup(self, tmp_path):
        """Both quality floor and cross-section dedup work together."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n\n"
            "## Pinned\n"
            "- (2026-02-01) [design] Two-tier memory architecture with project and global tiers\n\n"
            "## Key Learnings\n"
            "<!-- decay -->\n\n"
            "## Key Lessons\n"
            "<!-- decay -->\n"
        )
        routes = [
            # Thin entry — rejected by quality floor
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-23) [tip] Use git stash"],
            ),
            # Near-dup of Pinned — rejected by cross-section dedup
            RouteEntry(
                scope="global",
                section="Key Lessons",
                entries=["- (2026-02-23) [insight] Two-tier memory architecture project and global tiers"],
            ),
            # Genuinely new and substantive — accepted
            RouteEntry(
                scope="global",
                section="Key Learnings",
                entries=["- (2026-02-23) [gotcha] Tailscale MTU black hole drops packets silently on WSL2"],
            ),
        ]
        warnings = append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        # Only the genuinely new entry should be added
        assert "Tailscale MTU" in content
        assert "git stash" not in content
        assert content.count("Two-tier memory") == 1  # only the pinned one
        assert any("quality floor" in w for w in warnings)


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

        with patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
             patch("synthesis.prune_stale_state_entries"):
            run_post_processing(extract_paths=[str(extract)])

        assert not extract.exists()

    def test_prunes_stale_state(self):
        """Calls prune_stale_state_entries during post-processing."""
        with patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
             patch("synthesis.prune_stale_state_entries") as mock_prune:
            run_post_processing(extract_paths=[])

        mock_prune.assert_called_once()

    def test_updates_timestamp(self, tmp_path):
        """Writes .last-synthesis timestamp file."""
        with patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
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
- [global/pattern] Tailscale MTU black hole drops packets silently on WSL2

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] Tailscale MTU black hole drops packets silently on WSL2

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
        assert "Tailscale MTU" in ltm_content

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
# compute_offsets_from_extracts Tests
# =============================================================================


class TestComputeOffsetsFromExtracts:
    """Test computing session offsets directly from extract files and JSONL sources."""

    def test_finds_sessions_and_computes_offsets(self, tmp_path):
        """Parses session IDs from extract headers, finds JSONL files, returns offsets."""
        from synthesis import compute_offsets_from_extracts

        # Create a fake extract file with session headers
        extract = tmp_path / "extract-2026-02-22.txt"
        extract.write_text(
            "======\n"
            "DAY: 2026-02-22\n"
            "======\n"
            "──────\n"
            "Session: abc-123 [project: myproject]\n"
            "──────\n"
            "[CLAUDE]\nDid some work\n"
        )

        # Create a fake JSONL file for that session
        projects_dir = tmp_path / "projects"
        proj_dir = projects_dir / "encoded-path"
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / "abc-123.jsonl"
        jsonl.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n'
                         '{"type":"assistant","message":{"role":"assistant","content":"hello"}}\n'
                         '\n'  # blank line (should not count)
                         '{"type":"assistant","message":{"role":"assistant","content":"done"}}\n')

        with patch("synthesis.get_projects_dir", return_value=projects_dir):
            offsets = compute_offsets_from_extracts([str(extract)])

        assert "abc-123" in offsets
        assert offsets["abc-123"]["offset"] == jsonl.stat().st_size
        assert offsets["abc-123"]["lines"] == 3  # 3 non-blank lines

    def test_multiple_sessions_across_extracts(self, tmp_path):
        """Handles multiple sessions across multiple extract files."""
        from synthesis import compute_offsets_from_extracts

        extract1 = tmp_path / "e1.txt"
        extract1.write_text("Session: sess-1 [project: p1]\n[CLAUDE]\nstuff\n")

        extract2 = tmp_path / "e2.txt"
        extract2.write_text("Session: sess-2 [project: p2]\n[CLAUDE]\nmore\n")

        projects_dir = tmp_path / "projects"
        p1 = projects_dir / "p1"
        p1.mkdir(parents=True)
        p1_jsonl = p1 / "sess-1.jsonl"
        p1_jsonl.write_text('{"type":"user"}\n{"type":"assistant"}\n')

        p2 = projects_dir / "p2"
        p2.mkdir(parents=True)
        p2_jsonl = p2 / "sess-2.jsonl"
        p2_jsonl.write_text('{"type":"user"}\n')

        with patch("synthesis.get_projects_dir", return_value=projects_dir):
            offsets = compute_offsets_from_extracts([str(extract1), str(extract2)])

        assert len(offsets) == 2
        assert offsets["sess-1"]["lines"] == 2
        assert offsets["sess-2"]["lines"] == 1

    def test_empty_extracts_returns_empty(self, tmp_path):
        """No extract paths returns empty dict."""
        from synthesis import compute_offsets_from_extracts

        with patch("synthesis.get_projects_dir", return_value=tmp_path):
            assert compute_offsets_from_extracts([]) == {}

    def test_session_not_found_on_disk_skipped(self, tmp_path):
        """Session ID in extract but no matching JSONL returns empty for that session."""
        from synthesis import compute_offsets_from_extracts

        extract = tmp_path / "extract.txt"
        extract.write_text("Session: ghost-session [project: x]\n[CLAUDE]\ntext\n")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "somedir").mkdir()  # No JSONL inside

        with patch("synthesis.get_projects_dir", return_value=projects_dir):
            offsets = compute_offsets_from_extracts([str(extract)])

        assert offsets == {}

    def test_missing_extract_file_skipped(self, tmp_path):
        """Missing extract file path is silently skipped."""
        from synthesis import compute_offsets_from_extracts

        with patch("synthesis.get_projects_dir", return_value=tmp_path):
            offsets = compute_offsets_from_extracts(["/nonexistent/file.txt"])

        assert offsets == {}


# =============================================================================
# apply_results offset computation Tests
# =============================================================================


class TestApplyResultsComputesOffsets:
    """Test that apply_results computes offsets from extracts when --offsets-json not passed."""

    def test_computes_offsets_when_no_offsets_json(self, tmp_path):
        """apply_results calls compute_offsets_from_extracts and updates state."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/pattern] Something

===END==="""

        output_file = tmp_path / "output.txt"
        output_file.write_text(output_text)

        computed = {"s1": {"offset": 5000, "lines": 50}}

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "g.md"), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "pm"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"), \
             patch("synthesis.compute_offsets_from_extracts", return_value=computed) as mock_compute, \
             patch("synthesis.update_synthesis_state") as mock_update:
            apply_results(
                output_file=str(output_file),
                extract_paths=["/tmp/e1.txt"],
            )

        mock_compute.assert_called_once_with(["/tmp/e1.txt"])
        mock_update.assert_called_once_with(computed)

    def test_skips_state_update_when_no_offsets_computed(self, tmp_path):
        """No offsets computed (empty extracts) means no state update."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===END==="""

        output_file = tmp_path / "output.txt"
        output_file.write_text(output_text)

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "g.md"), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "pm"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"), \
             patch("synthesis.compute_offsets_from_extracts", return_value={}), \
             patch("synthesis.update_synthesis_state") as mock_update:
            apply_results(output_file=str(output_file), extract_paths=[])

        mock_update.assert_not_called()

    def test_offsets_json_overrides_computed(self, tmp_path):
        """Explicit --offsets-json takes precedence over computed offsets."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===END==="""

        output_file = tmp_path / "output.txt"
        output_file.write_text(output_text)

        cli_offsets = tmp_path / "offsets.json"
        cli_offsets.write_text('{"s1": {"offset": 9999, "lines": 99}}')

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "g.md"), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "pm"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"), \
             patch("synthesis.compute_offsets_from_extracts") as mock_compute, \
             patch("synthesis.update_synthesis_state") as mock_update:
            apply_results(
                output_file=str(output_file),
                extract_paths=[],
                offsets_json=str(cli_offsets),
            )

        # Should not compute if CLI offsets provided
        mock_compute.assert_not_called()
        mock_update.assert_called_once_with({"s1": {"offset": 9999, "lines": 99}})


# =============================================================================
# apply_results --offsets-json Tests (legacy)
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

        with patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
             patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path):
            run_post_processing(
                extract_paths=[],
                offsets_json=str(offsets_file),
            )

        assert not offsets_file.exists()

    def test_no_offsets_no_error(self, tmp_path):
        """run_post_processing works fine without offsets_json."""
        with patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
             patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path):
            run_post_processing(extract_paths=[])

        # Should not raise


class TestRunPostProcessingNoSubprocess:
    """Verify post-processing uses function calls, not subprocess.run."""

    def test_no_subprocess_import(self):
        """synthesis module should not import subprocess at all."""
        import synthesis

        assert not hasattr(synthesis, "subprocess"), \
            "synthesis.py should not import subprocess anymore"

    def test_calls_run_mark_routed(self, tmp_path):
        """run_post_processing calls run_mark_routed."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_mark_routed") as mock_mr, \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"):
            run_post_processing(extract_paths=[])
        mock_mr.assert_called_once()

    def test_calls_run_validate_ltm(self, tmp_path):
        """run_post_processing calls run_validate_ltm."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm") as mock_vl, \
             patch("synthesis.run_decay"):
            run_post_processing(extract_paths=[])
        mock_vl.assert_called_once()

    def test_calls_run_decay(self, tmp_path):
        """run_post_processing calls run_decay."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay") as mock_decay:
            run_post_processing(extract_paths=[])
        mock_decay.assert_called_once()

    def test_calls_reindex_after_synthesis(self, tmp_path):
        """run_post_processing calls _reindex_after_synthesis."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
             patch("synthesis._reindex_after_synthesis") as mock_reindex:
            run_post_processing(extract_paths=[])
        mock_reindex.assert_called_once()

    def test_calls_rebuild_projects_index(self, tmp_path):
        """run_post_processing rebuilds projects index before decay."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet") as mock_rebuild, \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"):
            run_post_processing(extract_paths=[])
        mock_rebuild.assert_called_once()

    def test_rebuild_before_decay_ordering(self, tmp_path):
        """Projects index rebuild runs before decay."""
        call_order = []

        def track_rebuild():
            call_order.append("rebuild")

        def track_decay():
            call_order.append("decay")

        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet", side_effect=track_rebuild), \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay", side_effect=track_decay):
            run_post_processing(extract_paths=[])

        assert call_order.index("rebuild") < call_order.index("decay")


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
- [global/implement] Refactored authentication module with OAuth integration
## Learnings
- [global/pattern] Tailscale MTU black hole drops packets silently on WSL2

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] Tailscale MTU black hole drops packets silently on WSL2

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
        assert "Tailscale MTU" in global_ltm.read_text()

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
- [myproj/implement] Built OAuth integration with refresh token rotation
## Learnings
- [global/pattern] Tailscale MTU black hole drops packets silently on WSL2

===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Refactored authentication module with session management
## Learnings
- [myproj/gotcha] SQLAlchemy connection pool exhaustion under concurrent requests

===ROUTE:global:Key Learnings===
- (2026-02-21) [pattern] Tailscale MTU black hole drops packets silently on WSL2

===ROUTE:global:Key Actions===
- (2026-02-22) [implement] Refactored authentication module with session management

===ROUTE:myproj:Key Learnings===
- (2026-02-22) [gotcha] SQLAlchemy connection pool exhaustion under concurrent requests

===ROUTE:myproj:Key Actions===
- (2026-02-21) [implement] Built OAuth integration with refresh token rotation

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
        assert "Tailscale MTU" in global_content
        assert "Refactored authentication" in global_content

        # Project LTM was created from template and has both entries
        proj_file = proj_dir / "myproj-long-term-memory.md"
        assert proj_file.exists()
        proj_content = proj_file.read_text()
        assert "SQLAlchemy connection pool" in proj_content
        assert "OAuth integration" in proj_content


# =============================================================================
# merge_daily_sections Tests
# =============================================================================


# =============================================================================
# inject_scopes Tests
# =============================================================================


class TestInjectScopes:
    def test_type_only_gets_project_scope(self):
        """[type] becomes [project/type] when session has project."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Actions\n- [implement] Built OAuth\n"
        )
        session_projects = {"2026-02-23": "cartwheel"}
        result = inject_scopes([daily], session_projects)
        assert "- [cartwheel/implement] Built OAuth" in result[0].content

    def test_global_marker_creates_dual_scope(self):
        """[GLOBAL][type] becomes [global|project/type]."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Learnings\n- [GLOBAL][gotcha] MTU issue\n"
        )
        session_projects = {"2026-02-23": "cartwheel"}
        result = inject_scopes([daily], session_projects)
        assert "- [global|cartwheel/gotcha] MTU issue" in result[0].content

    def test_no_project_defaults_to_global(self):
        """[type] becomes [global/type] when no project match."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Lessons\n- [tip] Use stash\n"
        )
        session_projects = {"2026-02-23": None}
        result = inject_scopes([daily], session_projects)
        assert "- [global/tip] Use stash" in result[0].content

    def test_global_marker_no_project_stays_global(self):
        """[GLOBAL][type] with no project becomes [global/type] (not [global|global/type])."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Lessons\n- [GLOBAL][tip] Use stash\n"
        )
        session_projects = {"2026-02-23": None}
        result = inject_scopes([daily], session_projects)
        assert "- [global/tip] Use stash" in result[0].content

    def test_already_scoped_entries_unchanged(self):
        """Entries with existing [scope/type] format pass through unchanged."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Actions\n- [cartwheel/implement] Built OAuth\n"
        )
        session_projects = {"2026-02-23": "cartwheel"}
        result = inject_scopes([daily], session_projects)
        assert "- [cartwheel/implement] Built OAuth" in result[0].content

    def test_placeholder_name_gets_rescoped(self):
        """LLM-leaked {name} placeholder is treated as unscoped and gets project injection."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Actions\n- [{name}/implement] Built OAuth\n"
        )
        session_projects = {"2026-02-23": "cartwheel"}
        result = inject_scopes([daily], session_projects)
        assert "- [cartwheel/implement] Built OAuth" in result[0].content
        assert "{name}" not in result[0].content

    def test_placeholder_name_no_project_gets_global(self):
        """LLM-leaked {name} placeholder defaults to global when no project."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Learnings\n- [{name}/gotcha] Bad thing\n"
        )
        session_projects = {"2026-02-23": None}
        result = inject_scopes([daily], session_projects)
        assert "- [global/gotcha] Bad thing" in result[0].content

    def test_multiple_sessions_different_projects(self):
        """Different dates can have different projects."""
        dailies = [
            DailyFile(date="2026-02-22", content="# 2026-02-22\n## Actions\n- [implement] Did A\n"),
            DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [implement] Did B\n"),
        ]
        session_projects = {"2026-02-22": "cartwheel", "2026-02-23": "investing"}
        result = inject_scopes(dailies, session_projects)
        assert "- [cartwheel/implement] Did A" in result[0].content
        assert "- [investing/implement] Did B" in result[1].content


class TestMergeDailySections:
    def test_no_existing_returns_new(self):
        new = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        result = merge_daily_sections("", new)
        assert "- [impl] Did A" in result

    def test_appends_new_entries_to_existing(self):
        existing = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        new = "# 2026-02-23\n## Actions\n- [impl] Did B\n"
        result = merge_daily_sections(existing, new)
        assert "- [impl] Did A" in result
        assert "- [impl] Did B" in result

    def test_dedup_rejects_near_duplicate(self):
        existing = "# 2026-02-23\n## Learnings\n- [gotcha] Tailscale MTU black hole drops packets\n"
        new = "# 2026-02-23\n## Learnings\n- [gotcha] Tailscale MTU black hole silently drops packets\n"
        result = merge_daily_sections(existing, new)
        # Should only have one entry (near-duplicate rejected)
        assert result.count("Tailscale MTU") == 1

    def test_preserves_existing_when_new_is_empty(self):
        existing = "# 2026-02-23\n## Actions\n- [impl] Did A\n## Lessons\n- [tip] Use X\n"
        new = "# 2026-02-23\n"
        result = merge_daily_sections(existing, new)
        assert "- [impl] Did A" in result
        assert "- [tip] Use X" in result

    def test_new_section_not_in_existing(self):
        existing = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        new = "# 2026-02-23\n## Lessons\n- [tip] Use Y\n"
        result = merge_daily_sections(existing, new)
        assert "- [impl] Did A" in result
        assert "- [tip] Use Y" in result

    def test_preserves_section_order(self):
        existing = "# 2026-02-23\n## Lessons\n- [tip] Use X\n"
        new = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        result = merge_daily_sections(existing, new)
        actions_pos = result.index("## Actions")
        lessons_pos = result.index("## Lessons")
        assert actions_pos < lessons_pos

    def test_preserves_routed_entries(self):
        existing = "# 2026-02-23\n## Actions\n- [routed][proj/impl] Old entry\n"
        new = "# 2026-02-23\n## Actions\n- [impl] New entry\n"
        result = merge_daily_sections(existing, new)
        assert "[routed]" in result
        assert "New entry" in result

    def test_uses_existing_date_header(self):
        existing = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        new = "## Actions\n- [impl] Did B\n"
        result = merge_daily_sections(existing, new)
        assert result.startswith("# 2026-02-23")


# =============================================================================
# Full Pipeline Integration Tests
# =============================================================================


class TestFullPipelineIntegration:
    def test_end_to_end_with_scope_injection_and_merge(self, tmp_path):
        """Full pipeline: parse -> inject scopes -> mark routed -> merge -> write -> LTM."""
        # Setup: existing daily file from earlier synthesis
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        existing_daily = daily_dir / "2026-02-23.md"
        existing_daily.write_text(
            "# 2026-02-23\n## Actions\n- [cartwheel/implement] Built OAuth\n"
        )

        # Setup: existing LTM
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n## Pinned\n\n## Key Learnings\n<!-- gotchas -->\n\n"
            "## Key Lessons\n<!-- tips -->\n"
        )

        # LLM output (simplified format -- no scopes, just types)
        llm_output = """===DAILY:2026-02-23===
# 2026-02-23
## Actions
- [implement] Added rate limiting

## Learnings
- [GLOBAL][gotcha] Tailscale MTU black hole on WSL2

===ROUTE:global:Key Learnings===
- (2026-02-23) [gotcha] Tailscale MTU black hole on WSL2

===END==="""

        # Parse
        result = parse_synthesis_output(llm_output)
        assert len(result.dailies) == 1

        # Inject scopes
        session_projects = {"2026-02-23": "cartwheel"}
        scoped = inject_scopes(result.dailies, session_projects)
        assert "- [cartwheel/implement] Added rate limiting" in scoped[0].content
        assert "- [global|cartwheel/gotcha] Tailscale MTU" in scoped[0].content

        # Mark routed
        marked = mark_routed_entries(scoped, result.routes)

        # Write (merges with existing)
        write_daily_files(marked, daily_dir=daily_dir)
        daily_content = existing_daily.read_text()
        assert "- [cartwheel/implement] Built OAuth" in daily_content  # preserved
        assert "- [cartwheel/implement] Added rate limiting" in daily_content  # merged
        assert "Tailscale MTU" in daily_content  # merged

        # Append to LTM
        append_to_ltm(result.routes, global_file=ltm_file)
        ltm_content = ltm_file.read_text()
        assert "Tailscale MTU" in ltm_content

    def test_dedup_across_merge_and_ltm(self, tmp_path):
        """Dedup prevents duplicates in both daily merge and LTM append."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        existing = daily_dir / "2026-02-23.md"
        existing.write_text(
            "# 2026-02-23\n## Learnings\n- [global/gotcha] Tailscale MTU drops packets\n"
        )

        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n## Key Learnings\n<!-- gotchas -->\n"
            "- (2026-02-20) [gotcha] Tailscale MTU black hole drops packets\n"
        )

        # LLM outputs near-duplicate
        llm_output = """===DAILY:2026-02-23===
# 2026-02-23
## Learnings
- [gotcha] Tailscale MTU black hole silently drops packets

===ROUTE:global:Key Learnings===
- (2026-02-23) [gotcha] Tailscale MTU black hole silently drops packets

===END==="""

        result = parse_synthesis_output(llm_output)
        session_projects = {"2026-02-23": None}
        scoped = inject_scopes(result.dailies, session_projects)
        marked = mark_routed_entries(scoped, result.routes)
        write_daily_files(marked, daily_dir=daily_dir)
        append_to_ltm(result.routes, global_file=ltm_file)

        # Daily: should have only 1 Tailscale entry (dedup rejected near-dupe)
        daily_content = existing.read_text()
        assert daily_content.count("Tailscale MTU") == 1

        # LTM: should have only 1 Tailscale entry (keyword dedup rejected)
        ltm_content = ltm_file.read_text()
        assert ltm_content.count("Tailscale MTU") == 1


# =============================================================================
# TYPE_TO_SECTION Tests
# =============================================================================


class TestTypeToSection:
    def test_action_types(self):
        assert TYPE_TO_SECTION["implement"] == "Actions"
        assert TYPE_TO_SECTION["improve"] == "Actions"
        assert TYPE_TO_SECTION["document"] == "Actions"
        assert TYPE_TO_SECTION["analyze"] == "Actions"

    def test_decision_types(self):
        assert TYPE_TO_SECTION["design"] == "Decisions"
        assert TYPE_TO_SECTION["tradeoff"] == "Decisions"
        assert TYPE_TO_SECTION["scope"] == "Decisions"

    def test_learning_types(self):
        assert TYPE_TO_SECTION["gotcha"] == "Learnings"
        assert TYPE_TO_SECTION["pitfall"] == "Learnings"
        assert TYPE_TO_SECTION["pattern"] == "Learnings"

    def test_lesson_types(self):
        assert TYPE_TO_SECTION["insight"] == "Lessons"
        assert TYPE_TO_SECTION["tip"] == "Lessons"
        assert TYPE_TO_SECTION["workaround"] == "Lessons"

    def test_all_types_covered(self):
        assert set(TYPE_TO_SECTION.values()) == set(SECTION_ORDER)


# =============================================================================
# ProjectBlock Tests
# =============================================================================


class TestProjectBlock:
    """Test the new ProjectBlock dataclass."""

    def test_basic_creation(self):
        block = ProjectBlock(project="swyfft", entries=[
            "- [implement] Did something",
            "- [LTM][gotcha] Found a bug",
        ])
        assert block.project == "swyfft"
        assert len(block.entries) == 2

    def test_empty_entries_default(self):
        block = ProjectBlock(project="global")
        assert block.project == "global"
        assert block.entries == []


# =============================================================================
# Parse ===PROJECT:X=== Format Tests
# =============================================================================


class TestParseProjectFormat:
    """Test parsing the new ===PROJECT:X=== format."""

    def test_single_project(self):
        text = """===PROJECT:swyfft===
- [implement] Rewrote SQL
- [gotcha] Tableau mislabeled

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.project_blocks) == 1
        assert result.project_blocks[0].project == "swyfft"
        assert len(result.project_blocks[0].entries) == 2

    def test_multiple_projects(self):
        text = """===PROJECT:swyfft===
- [implement] Rewrote SQL

===PROJECT:investing===
- [implement] Started Phase 4

===PROJECT:global===
- [analyze] Benchmarked Python vs TS

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.project_blocks) == 3
        projects = [b.project for b in result.project_blocks]
        assert projects == ["swyfft", "investing", "global"]

    def test_ltm_and_global_flags_preserved(self):
        text = """===PROJECT:swyfft===
- [LTM][gotcha] Important bug
- [GLOBAL][pattern] Cross-project pattern
- [LTM][GLOBAL][tip] Global LTM tip

===END==="""
        result = parse_synthesis_output(text)
        entries = result.project_blocks[0].entries
        assert entries[0] == "- [LTM][gotcha] Important bug"
        assert entries[1] == "- [GLOBAL][pattern] Cross-project pattern"
        assert entries[2] == "- [LTM][GLOBAL][tip] Global LTM tip"

    def test_skips_non_entry_lines(self):
        text = """===PROJECT:swyfft===
Some preamble text
- [implement] Real entry

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.project_blocks[0].entries) == 1

    def test_empty_project_block_skipped(self):
        text = """===PROJECT:swyfft===

===PROJECT:investing===
- [implement] Real entry

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.project_blocks) == 1
        assert result.project_blocks[0].project == "investing"

    def test_missing_end_marker_warns(self):
        text = """===PROJECT:swyfft===
- [implement] Did something"""
        result = parse_synthesis_output(text)
        assert len(result.project_blocks) == 1
        assert any("END" in w for w in result.warnings)

    def test_old_daily_format_still_works(self):
        """Backwards compatibility: ===DAILY=== format still parses."""
        text = """===DAILY:2026-02-25===
## Actions
- [implement] Did something

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1
        assert result.dailies[0].date == "2026-02-25"

    def test_mixed_project_and_daily_rejected(self):
        """If both formats present, both are parsed (downstream decides)."""
        text = """===DAILY:2026-02-25===
## Actions
- [implement] Did something

===PROJECT:swyfft===
- [implement] Rewrote SQL

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1
        assert len(result.project_blocks) == 1


# =============================================================================
# build_dailies_from_project_blocks Tests
# =============================================================================


class TestBuildDailiesFromProjectBlocks:
    def test_single_project_sections(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [implement] Rewrote SQL",
            "- [gotcha] Tableau mislabeled",
            "- [design] Use bind date",
            "- [tip] Rename GWP column",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert len(dailies) == 1
        content = dailies[0].content
        assert dailies[0].date == "2026-02-24"
        assert "## Actions\n- [swyfft/implement] Rewrote SQL" in content
        assert "## Learnings\n- [swyfft/gotcha] Tableau mislabeled" in content
        assert "## Decisions\n- [swyfft/design] Use bind date" in content
        assert "## Lessons\n- [swyfft/tip] Rename GWP column" in content

    def test_global_project_scope(self):
        blocks = [ProjectBlock(project="global", entries=[
            "- [analyze] Benchmarked Python vs TS",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert "- [global/analyze] Benchmarked Python vs TS" in dailies[0].content

    def test_global_flag_produces_pipe_scope(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [GLOBAL][pattern] Cross-project pattern",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert "- [global|swyfft/pattern] Cross-project pattern" in dailies[0].content

    def test_global_flag_on_global_project_stays_global(self):
        blocks = [ProjectBlock(project="global", entries=[
            "- [GLOBAL][tip] Some tip",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert "- [global/tip] Some tip" in dailies[0].content

    def test_ltm_flag_stripped_from_daily(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [LTM][gotcha] Important bug",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert "[LTM]" not in dailies[0].content
        assert "- [swyfft/gotcha] Important bug" in dailies[0].content

    def test_multiple_projects_merge_into_one_daily(self):
        blocks = [
            ProjectBlock(project="swyfft", entries=["- [implement] Swyfft work"]),
            ProjectBlock(project="investing", entries=["- [implement] Investing work"]),
        ]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert len(dailies) == 1
        content = dailies[0].content
        assert "- [swyfft/implement] Swyfft work" in content
        assert "- [investing/implement] Investing work" in content

    def test_unknown_type_goes_to_actions(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [investigate] Something new",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        content = dailies[0].content
        assert "## Actions" in content
        assert "- [swyfft/investigate] Something new" in content

    def test_section_order_is_standard(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [tip] A lesson",
            "- [implement] An action",
            "- [gotcha] A learning",
            "- [design] A decision",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        content = dailies[0].content
        for section in SECTION_ORDER:
            assert f"## {section}" in content
        # Verify order: Actions before Decisions before Learnings before Lessons
        assert content.index("## Actions") < content.index("## Decisions")
        assert content.index("## Decisions") < content.index("## Learnings")
        assert content.index("## Learnings") < content.index("## Lessons")

    def test_empty_blocks_returns_empty(self):
        dailies = build_dailies_from_project_blocks([], "2026-02-24")
        assert len(dailies) == 1
        # Should just have the date header, no sections
        assert dailies[0].date == "2026-02-24"
        assert "## Actions" not in dailies[0].content

    def test_entries_without_type_tag_skipped(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- No tag here",
            "- [implement] Valid entry",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert "No tag here" not in dailies[0].content
        assert "- [swyfft/implement] Valid entry" in dailies[0].content

    def test_ltm_and_global_combined(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [LTM][GLOBAL][pattern] Important cross-project pattern",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        content = dailies[0].content
        assert "[LTM]" not in content
        assert "- [global|swyfft/pattern] Important cross-project pattern" in content

    def test_reversed_flag_order(self):
        """[GLOBAL][LTM][type] should work the same as [LTM][GLOBAL][type]."""
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [GLOBAL][LTM][tip] Reversed flag order tip",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        content = dailies[0].content
        assert "[LTM]" not in content
        assert "[GLOBAL]" not in content
        assert "- [global|swyfft/tip] Reversed flag order tip" in content

    def test_bare_ltm_flag_skipped(self):
        """- [LTM] bare entry with no type tag should be skipped."""
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [LTM] Only LTM flag no type",
            "- [implement] Valid entry",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        content = dailies[0].content
        assert "Only LTM flag no type" not in content
        assert "- [swyfft/implement] Valid entry" in content


# =============================================================================
# extract_routes_from_project_blocks Tests
# =============================================================================


class TestExtractRoutesFromProjectBlocks:
    def test_ltm_entries_become_routes(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [implement] Normal entry",
            "- [LTM][gotcha] Important bug",
            "- [LTM][tip] Useful command",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        assert len(routes) == 2
        # gotcha -> Key Learnings, tip -> Key Lessons
        scopes = {(r.scope, r.section) for r in routes}
        assert ("swyfft", "Key Learnings") in scopes
        assert ("swyfft", "Key Lessons") in scopes

    def test_date_prefix_added(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [LTM][gotcha] Important bug",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        assert routes[0].entries[0] == "- (2026-02-24) [gotcha] Important bug"

    def test_global_project_routes_to_global(self):
        blocks = [ProjectBlock(project="global", entries=[
            "- [LTM][tip] Global tip",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        assert routes[0].scope == "global"

    def test_global_flag_routes_to_both(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [LTM][GLOBAL][pattern] Cross-project pattern",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        scopes = {r.scope for r in routes}
        assert "swyfft" in scopes
        assert "global" in scopes

    def test_no_ltm_entries_no_routes(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [implement] Normal entry",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        assert routes == []

    def test_routes_grouped_by_scope_and_section(self):
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [LTM][gotcha] Bug one",
            "- [LTM][gotcha] Bug two",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        # Both gotchas grouped into one RouteEntry for swyfft:Key Learnings
        assert len(routes) == 1
        assert len(routes[0].entries) == 2

    def test_multiple_projects(self):
        """Entries from different projects produce separate routes."""
        blocks = [
            ProjectBlock(project="swyfft", entries=[
                "- [LTM][gotcha] Swyfft bug",
            ]),
            ProjectBlock(project="memory", entries=[
                "- [LTM][gotcha] Memory bug",
            ]),
        ]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        scopes = {r.scope for r in routes}
        assert "swyfft" in scopes
        assert "memory" in scopes

    def test_empty_project_defaults_to_global(self):
        """Empty project name should route to global."""
        blocks = [ProjectBlock(project="", entries=[
            "- [LTM][tip] Some tip",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        assert routes[0].scope == "global"

    def test_reversed_flag_order(self):
        """[GLOBAL][LTM][type] should work the same as [LTM][GLOBAL][type]."""
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [GLOBAL][LTM][tip] Reversed flag tip",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        scopes = {r.scope for r in routes}
        assert "swyfft" in scopes
        assert "global" in scopes
        # Check the formatted entry has no flags
        for r in routes:
            for e in r.entries:
                assert "[LTM]" not in e
                assert "[GLOBAL]" not in e

    def test_all_type_sections_mapped(self):
        """Verify all TYPE_TO_SECTION types map to correct Key sections."""
        for entry_type, section in TYPE_TO_SECTION.items():
            blocks = [ProjectBlock(project="test", entries=[
                f"- [LTM][{entry_type}] Test entry for {entry_type}",
            ])]
            routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
            assert len(routes) == 1, f"No route for type {entry_type}"
            assert routes[0].section == f"Key {section}", (
                f"Type {entry_type} mapped to {routes[0].section}, "
                f"expected Key {section}"
            )


# =============================================================================
# _extract_date_from_extracts Tests
# =============================================================================

from synthesis import _extract_date_from_extracts  # noqa: E402


class TestExtractDateFromExtracts:
    """Test date extraction from extract file paths."""

    def test_extracts_date_from_filename(self, tmp_path):
        """Extract date from standard extract filename."""
        f = tmp_path / "extract-2026-02-24.txt"
        f.write_text("content")
        assert _extract_date_from_extracts([str(f)]) == "2026-02-24"

    def test_uses_first_match(self, tmp_path):
        """Returns date from first matching file."""
        f1 = tmp_path / "extract-2026-02-20.txt"
        f2 = tmp_path / "extract-2026-02-21.txt"
        f1.write_text("c1")
        f2.write_text("c2")
        assert _extract_date_from_extracts([str(f1), str(f2)]) == "2026-02-20"

    def test_no_date_in_filename_falls_back_to_today(self):
        """Falls back to today's date if no date found in filenames."""
        result = _extract_date_from_extracts(["/tmp/nodatehere.txt"])
        # Should be a valid ISO date
        import re
        assert re.match(r"\d{4}-\d{2}-\d{2}", result)

    def test_empty_paths_falls_back_to_today(self):
        """Empty path list falls back to today's date."""
        import re
        result = _extract_date_from_extracts([])
        assert re.match(r"\d{4}-\d{2}-\d{2}", result)


# =============================================================================
# apply_results with project blocks Tests
# =============================================================================


class TestApplyResultsProjectBlocks:
    """Integration test: apply_results with new ===PROJECT=== format."""

    def test_project_blocks_produce_scoped_daily(self, tmp_path):
        """Project blocks produce a daily file with scoped entries."""
        output_file = tmp_path / "output.txt"
        output_file.write_text("""===PROJECT:swyfft===
- [implement] Rewrote SQL query for performance
- [LTM][gotcha] Tableau dashboard mislabeled metric column
- [design] Use bind date for policy lookup

===PROJECT:global===
- [analyze] Benchmarked Python vs TypeScript serialization

===END===
""")
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        extract_file = tmp_path / "extract-2026-02-24.txt"
        extract_file.write_text("Session: abc123 [project: swyfft]\n")

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "global-ltm.md"), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.compute_offsets_from_extracts", return_value={}), \
             patch("synthesis.update_synthesis_state"), \
             patch("synthesis.run_post_processing"):
            apply_results(str(output_file), [str(extract_file)])

        daily_file = daily_dir / "2026-02-24.md"
        assert daily_file.exists()
        content = daily_file.read_text()
        assert "[swyfft/implement]" in content
        assert "[swyfft/gotcha]" in content
        assert "[swyfft/design]" in content
        assert "[global/analyze]" in content

    def test_project_blocks_ltm_routing(self, tmp_path):
        """LTM entries from project blocks get routed correctly."""
        output_file = tmp_path / "output.txt"
        output_file.write_text("""===PROJECT:swyfft===
- [LTM][gotcha] Important bug found in the data processing pipeline

===END===
""")
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        proj_memory_dir = tmp_path / "project-memory"
        proj_memory_dir.mkdir()
        # Create existing LTM file
        ltm_file = proj_memory_dir / "swyfft-long-term-memory.md"
        ltm_file.write_text("# swyfft\n\n## Pinned\n\n## Key Learnings\n")

        extract_file = tmp_path / "extract-2026-02-24.txt"
        extract_file.write_text("Session: abc123 [project: swyfft]\n")

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "global-ltm.md"), \
             patch("synthesis.get_project_memory_dir", return_value=proj_memory_dir), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.compute_offsets_from_extracts", return_value={}), \
             patch("synthesis.update_synthesis_state"), \
             patch("synthesis.run_post_processing"):
            apply_results(str(output_file), [str(extract_file)])

        ltm_content = ltm_file.read_text()
        assert "(2026-02-24) [gotcha] Important bug found" in ltm_content

    def test_old_format_still_works(self, tmp_path):
        """Backwards compatibility: ===DAILY=== format still processes."""
        output_file = tmp_path / "output.txt"
        output_file.write_text("""===DAILY:2026-02-24===
# 2026-02-24
## Actions
- [swyfft/implement] Did something

===END===
""")
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        extract_file = tmp_path / "extract-2026-02-24.txt"
        extract_file.write_text("Session: abc123 [project: swyfft]\n")

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "global-ltm.md"), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.compute_offsets_from_extracts", return_value={}), \
             patch("synthesis.update_synthesis_state"), \
             patch("synthesis.run_post_processing"):
            apply_results(str(output_file), [str(extract_file)])

        content = (daily_dir / "2026-02-24.md").read_text()
        assert "[swyfft/implement] Did something" in content

    def test_no_blocks_no_dailies_prints_error(self, tmp_path, capsys):
        """When neither project blocks nor dailies found, print error."""
        output_file = tmp_path / "output.txt"
        output_file.write_text("just garbage text with no blocks")

        with patch("synthesis.run_post_processing") as mock_post:
            apply_results(str(output_file), [])
            mock_post.assert_not_called()

        captured = capsys.readouterr()
        assert "No daily or project blocks found" in captured.err

    def test_project_blocks_with_global_flag_dual_routes(self, tmp_path):
        """[GLOBAL] flag routes to both project and global LTM."""
        output_file = tmp_path / "output.txt"
        output_file.write_text("""===PROJECT:swyfft===
- [LTM][GLOBAL][gotcha] Cross-project bug found in shared data pipeline

===END===
""")
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        proj_memory_dir = tmp_path / "project-memory"
        proj_memory_dir.mkdir()
        ltm_file = proj_memory_dir / "swyfft-long-term-memory.md"
        ltm_file.write_text("# swyfft\n\n## Pinned\n\n## Key Learnings\n")
        global_ltm = tmp_path / "global-ltm.md"
        global_ltm.write_text("## Key Learnings\n<!-- decay -->\n")

        extract_file = tmp_path / "extract-2026-02-24.txt"
        extract_file.write_text("Session: abc123 [project: swyfft]\n")

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=global_ltm), \
             patch("synthesis.get_project_memory_dir", return_value=proj_memory_dir), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.compute_offsets_from_extracts", return_value={}), \
             patch("synthesis.update_synthesis_state"), \
             patch("synthesis.run_post_processing"):
            apply_results(str(output_file), [str(extract_file)])

        # Routed to project LTM
        proj_content = ltm_file.read_text()
        assert "(2026-02-24) [gotcha] Cross-project bug found" in proj_content
        # Routed to global LTM
        global_content = global_ltm.read_text()
        assert "(2026-02-24) [gotcha] Cross-project bug found" in global_content

    def test_project_blocks_mark_routed_in_daily(self, tmp_path):
        """LTM entries get [routed] marker in daily file."""
        output_file = tmp_path / "output.txt"
        output_file.write_text("""===PROJECT:swyfft===
- [implement] Normal entry that should not be routed
- [LTM][gotcha] Routed entry should get marker in daily output

===END===
""")
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        proj_memory_dir = tmp_path / "project-memory"
        proj_memory_dir.mkdir()
        ltm_file = proj_memory_dir / "swyfft-long-term-memory.md"
        ltm_file.write_text("# swyfft\n\n## Pinned\n\n## Key Learnings\n")

        extract_file = tmp_path / "extract-2026-02-24.txt"
        extract_file.write_text("Session: abc123 [project: swyfft]\n")

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "global-ltm.md"), \
             patch("synthesis.get_project_memory_dir", return_value=proj_memory_dir), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.compute_offsets_from_extracts", return_value={}), \
             patch("synthesis.update_synthesis_state"), \
             patch("synthesis.run_post_processing"):
            apply_results(str(output_file), [str(extract_file)])

        content = (daily_dir / "2026-02-24.md").read_text()
        # The LTM entry should be marked as routed
        assert "[routed]" in content
        # The normal entry should NOT be marked as routed
        for line in content.split("\n"):
            if "Normal entry" in line:
                assert "[routed]" not in line


# =============================================================================
# C4: CRUD apply logic tests
# =============================================================================


def _make_ltm_file(tmp_path, scope="global"):
    """Create a minimal LTM file with standard sections."""
    content = f"""# {scope} Long-Term Memory

## Key Actions
<!-- recent actions -->

## Key Decisions

## Key Learnings

"""
    if scope == "global":
        f = tmp_path / "global-long-term-memory.md"
    else:
        ltm_dir = tmp_path / "project-memory"
        ltm_dir.mkdir(exist_ok=True)
        f = ltm_dir / f"{scope}-long-term-memory.md"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    return f


def _make_db(tmp_path):
    """Create an in-memory DB with proper patching."""
    from unittest.mock import patch
    db_path = tmp_path / "memory.db"
    return patch("storage.get_db_path", return_value=db_path)


class TestApplyCrudOps:
    """Tests for CRUD operation application from MEMORY_OPS."""

    def test_add_inserts_chunk_and_appends_to_ltm(self, tmp_path):
        """ADD: creates DB chunk and appends entry to LTM markdown."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, query_chunks_by_scope
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "ADD", "fact": "project uses gRPC for internal comms", "scope": "global", "section": "Key Actions", "type": "implement", "entities": ["gRPC"]}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")
            assert warnings == []
            conn = ensure_db()
            try:
                chunks = query_chunks_by_scope(conn, "global")
                assert any("gRPC" in c.content for c in chunks)
            finally:
                close_db(conn)

        content = ltm_file.read_text()
        assert "gRPC for internal comms" in content

    def test_add_with_design_type_produces_design_tag(self, tmp_path):
        """ADD with type='design' should produce [design] in markdown, not [implement]."""
        from unittest.mock import patch
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "ADD", "fact": "JWT over sessions for statelessness", "scope": "global", "section": "Key Decisions", "type": "design", "entities": ["JWT"]}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")
            assert warnings == []

        content = ltm_file.read_text()
        assert "[design] JWT over sessions" in content
        assert "[implement]" not in content or "JWT" not in content.split("[implement]")[-1]

    def test_add_with_memoryop_type_produces_correct_tag(self, tmp_path):
        """ADD via MemoryOp dataclass with type='design' should produce [design] tag."""
        from unittest.mock import patch
        from synthesis import MemoryOp, apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [MemoryOp(action="ADD", fact="chose gRPC over REST", scope="global", section="Key Decisions", type="design", entities=["gRPC", "REST"])]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")
            assert warnings == []

        content = ltm_file.read_text()
        assert "- (2026-03-21) [design] chose gRPC over REST" in content

    def test_update_modifies_chunk_and_markdown_line(self, tmp_path):
        """UPDATE: modifies DB chunk content and updates markdown line."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, insert_chunk, ChunkRow, query_chunk_by_id
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                chunk = ChunkRow(content="project uses REST API for external", source_file="global-long-term-memory.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01")
                chunk_id = insert_chunk(conn, chunk)
                conn.commit()
            finally:
                close_db(conn)

        # Add the old content to the LTM file
        old_text = ltm_file.read_text()
        ltm_file.write_text(old_text + "- (2026-01-01) [implement] project uses REST API for external\n")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "UPDATE", "id": chunk_id, "fact": "project uses gRPC for external", "entities": ["gRPC"]}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        assert not any("not found" in w for w in warnings)
        content = ltm_file.read_text()
        assert "gRPC for external" in content

    def test_update_db_only_when_markdown_not_found(self, tmp_path):
        """UPDATE: when no markdown match, applies DB change and logs warning."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, insert_chunk, ChunkRow, query_chunk_by_id
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                chunk = ChunkRow(content="some unique content xyz", source_file="global-long-term-memory.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01")
                chunk_id = insert_chunk(conn, chunk)
                conn.commit()
            finally:
                close_db(conn)

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "UPDATE", "id": chunk_id, "fact": "new content for this chunk"}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        assert any("DB-only" in w or "not found" in w for w in warnings)

    def test_delete_sets_salience_zero_and_archives(self, tmp_path):
        """DELETE: sets salience=0, archives in markdown."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, insert_chunk, ChunkRow, query_chunk_by_id
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                chunk = ChunkRow(content="deprecated fact to delete here", source_file="global-long-term-memory.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01")
                chunk_id = insert_chunk(conn, chunk)
                conn.commit()
            finally:
                close_db(conn)

        # Add the content to the LTM
        old = ltm_file.read_text()
        ltm_file.write_text(old + "- (2026-01-01) [implement] deprecated fact to delete here\n")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "DELETE", "id": chunk_id, "reason": "Outdated"}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        assert not any("not found" in w for w in warnings)
        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                result = query_chunk_by_id(conn, chunk_id)
                assert result.salience == 0.0
            finally:
                close_db(conn)
        content = ltm_file.read_text()
        assert "Archived" in content

    def test_noop_increments_evidence_count(self, tmp_path):
        """NOOP: increments evidence_count on the chunk."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, insert_chunk, ChunkRow, query_chunk_by_id
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                chunk = ChunkRow(content="confirmed fact", source_file="test.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01", evidence_count=1)
                chunk_id = insert_chunk(conn, chunk)
                conn.commit()
            finally:
                close_db(conn)

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "NOOP", "id": chunk_id, "reason": "Already accurate"}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        assert warnings == []
        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                result = query_chunk_by_id(conn, chunk_id)
                assert result.evidence_count == 2
            finally:
                close_db(conn)

    def test_unknown_action_logged_as_warning(self, tmp_path):
        """Unrecognized action produces warning, does not crash."""
        from unittest.mock import patch
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "MERGE", "fact": "something"}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        assert any("MERGE" in w or "Unknown" in w for w in warnings)

    def test_missing_chunk_id_on_update_logged(self, tmp_path):
        """UPDATE with nonexistent chunk ID produces warning."""
        from unittest.mock import patch
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "UPDATE", "id": "nonexistent-id", "fact": "new fact"}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        assert any("not found" in w or "nonexistent" in w for w in warnings)


# =============================================================================
# C5: Bi-temporal edge handling tests
# =============================================================================


class TestBitemporalEdges:
    """Tests for bi-temporal edge invalidation on DELETE operations."""

    def _setup_db(self, tmp_path):
        from unittest.mock import patch
        from storage import ensure_db, close_db, insert_chunk, insert_node, insert_edge, ChunkRow, NodeRow, EdgeRow, query_node_by_name_and_type
        import json

        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            insert_node(conn, NodeRow(name="gRPC", type="entity", scope="global", created_at="2026-01-01"))
            insert_node(conn, NodeRow(name="REST", type="entity", scope="global", created_at="2026-01-01"))
            src = query_node_by_name_and_type(conn, "gRPC", "entity")
            tgt = query_node_by_name_and_type(conn, "REST", "entity")
            edge_id = insert_edge(conn, EdgeRow(source=src.id, target=tgt.id, type="replaces", created_at="2026-01-01"))
            chunk = ChunkRow(
                content="project uses gRPC instead of REST",
                source_file="global-long-term-memory.md",
                source_type="ltm",
                scope="global",
                chunk_index=0,
                created_at="2026-01-01",
                entities=json.dumps(["gRPC", "REST"]),
            )
            chunk_id = insert_chunk(conn, chunk)
            conn.commit()
            close_db(conn)
        return db_path, chunk_id, edge_id, src.id, tgt.id

    def test_delete_invalidates_edges_on_chunk(self, tmp_path):
        """DELETE sets valid_to on all edges connected to the chunk's entity nodes."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, query_current_edges
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        ltm_file.write_text(ltm_file.read_text() + "- (2026-01-01) [implement] project uses gRPC instead of REST\n")
        db_path, chunk_id, edge_id, _, _ = self._setup_db(tmp_path)

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "DELETE", "id": chunk_id, "reason": "Contradicted: no longer uses gRPC"}]
            apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                current = query_current_edges(conn)
                current_ids = [e.id for e in current]
                assert edge_id not in current_ids
            finally:
                close_db(conn)

    def test_delete_chunk_with_no_edges_is_safe(self, tmp_path):
        """DELETE on a chunk with no associated edges completes without error."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, insert_chunk, ChunkRow, query_chunk_by_id
        from synthesis import apply_memory_ops
        import json

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                chunk = ChunkRow(
                    content="fact with no edges",
                    source_file="global-long-term-memory.md",
                    source_type="ltm",
                    scope="global",
                    chunk_index=0,
                    created_at="2026-01-01",
                    entities=json.dumps([]),
                )
                chunk_id = insert_chunk(conn, chunk)
                conn.commit()
            finally:
                close_db(conn)

        ltm_file.write_text(ltm_file.read_text() + "- (2026-01-01) [implement] fact with no edges\n")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "DELETE", "id": chunk_id, "reason": "Outdated"}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        assert not any("error" in w.lower() for w in warnings)
        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                result = query_chunk_by_id(conn, chunk_id)
                assert result.salience == 0.0
            finally:
                close_db(conn)

    def test_delete_only_invalidates_chunk_related_edges(self, tmp_path):
        """Only edges where both source AND target are chunk entities get invalidated.

        Generic entities like "Python" appear across many memories, so we
        require both endpoints of an edge to be in the deleted chunk's entity
        set before invalidating.
        """
        from unittest.mock import patch
        from storage import ensure_db, close_db, insert_chunk, insert_node, insert_edge, ChunkRow, NodeRow, EdgeRow, query_current_edges, query_node_by_name_and_type
        from synthesis import apply_memory_ops
        import json

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                insert_node(conn, NodeRow(name="entity-a", type="entity", scope="global", created_at="2026-01-01"))
                insert_node(conn, NodeRow(name="entity-b", type="entity", scope="global", created_at="2026-01-01"))
                insert_node(conn, NodeRow(name="unrelated-entity", type="entity", scope="global", created_at="2026-01-01"))
                node_a = query_node_by_name_and_type(conn, "entity-a", "entity")
                node_b = query_node_by_name_and_type(conn, "entity-b", "entity")
                unrelated_node = query_node_by_name_and_type(conn, "unrelated-entity", "entity")
                both_in_chunk_edge_id = insert_edge(conn, EdgeRow(source=node_a.id, target=node_b.id, type="uses", created_at="2026-01-01"))
                one_outside_edge_id = insert_edge(conn, EdgeRow(source=node_a.id, target=unrelated_node.id, type="uses", created_at="2026-01-01"))
                fully_unrelated_edge_id = insert_edge(conn, EdgeRow(source=unrelated_node.id, target=unrelated_node.id, type="self", created_at="2026-01-01"))
                chunk = ChunkRow(
                    content="fact about entity-a and entity-b",
                    source_file="global-long-term-memory.md",
                    source_type="ltm",
                    scope="global",
                    chunk_index=0,
                    created_at="2026-01-01",
                    entities=json.dumps(["entity-a", "entity-b"]),
                )
                chunk_id = insert_chunk(conn, chunk)
                conn.commit()
            finally:
                close_db(conn)

        ltm_file.write_text(ltm_file.read_text() + "- (2026-01-01) [implement] fact about entity-a and entity-b\n")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "DELETE", "id": chunk_id, "reason": "Outdated"}]
            apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                current = query_current_edges(conn)
                current_ids = [e.id for e in current]
                assert both_in_chunk_edge_id not in current_ids, "edge between two chunk entities should be invalidated"
                assert one_outside_edge_id in current_ids, "edge with one non-chunk endpoint should be kept"
                assert fully_unrelated_edge_id in current_ids, "fully unrelated edge should be kept"
            finally:
                close_db(conn)


# =============================================================================
# C6: Entity extraction tests
# =============================================================================


class TestEntityExtraction:
    """Tests for entity extraction via CRUD operations."""

    def test_add_stores_entities_on_chunk(self, tmp_path):
        """ADD op with entities array stores them in chunk's entities JSON column."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, query_chunks_by_scope
        from synthesis import apply_memory_ops
        import json

        db_path = tmp_path / "memory.db"
        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "ADD", "fact": "uses gRPC for comms", "scope": "global", "section": "Key Actions", "entities": ["gRPC", "myproject", "internal services"]}]
            apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                chunks = query_chunks_by_scope(conn, "global")
                assert len(chunks) == 1
                stored_entities = json.loads(chunks[0].entities)
                assert "gRPC" in stored_entities
                assert "myproject" in stored_entities
            finally:
                close_db(conn)

    def test_update_replaces_entities(self, tmp_path):
        """UPDATE op with new entities replaces existing entities on chunk."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, insert_chunk, ChunkRow, query_chunk_by_id
        from synthesis import apply_memory_ops
        import json

        db_path = tmp_path / "memory.db"
        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                chunk = ChunkRow(content="old lib usage", source_file="global-long-term-memory.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01", entities=json.dumps(["old-lib"]))
                chunk_id = insert_chunk(conn, chunk)
                conn.commit()
            finally:
                close_db(conn)

        ltm_file.write_text(ltm_file.read_text() + "- (2026-01-01) [implement] old lib usage\n")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "UPDATE", "id": chunk_id, "fact": "new lib api-client usage", "entities": ["new-lib", "api-client"]}]
            apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                result = query_chunk_by_id(conn, chunk_id)
                updated = json.loads(result.entities)
                assert "new-lib" in updated
                assert "api-client" in updated
                assert "old-lib" not in updated
            finally:
                close_db(conn)

    def test_add_without_entities_stores_null(self, tmp_path):
        """ADD op without entities key stores NULL (not empty array)."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, query_chunks_by_scope
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "ADD", "fact": "simple fact without entities", "scope": "global", "section": "Key Actions"}]
            apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                chunks = query_chunks_by_scope(conn, "global")
                assert chunks[0].entities is None
            finally:
                close_db(conn)

    def test_entities_roundtrip_json(self, tmp_path):
        """Entities survive JSON encode/decode roundtrip."""
        from unittest.mock import patch
        from storage import ensure_db, close_db, query_chunks_by_scope
        from synthesis import apply_memory_ops
        import json

        db_path = tmp_path / "memory.db"
        ltm_file = _make_ltm_file(tmp_path, "global")
        entities = ["Python 3.13", "pytest", "https://example.com", "2026-03-21"]

        with patch("storage.get_db_path", return_value=db_path), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "ADD", "fact": "uses various entities", "scope": "global", "section": "Key Actions", "entities": entities}]
            apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        with patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            try:
                chunks = query_chunks_by_scope(conn, "global")
                roundtripped = json.loads(chunks[0].entities)
                assert roundtripped == entities
            finally:
                close_db(conn)

    def test_synthesis_instructions_mention_entities(self):
        """Synthesis prompt includes entity extraction guidance."""
        from load_memory import _build_synthesis_instructions
        instructions = _build_synthesis_instructions("test-project")
        assert "entities" in instructions.lower()


# =============================================================================
# C3: MEMORY_OPS parsing tests
# =============================================================================


class TestMemoryOpsParsing:
    """Tests for ===MEMORY_OPS=== block parsing in synthesis output."""

    def test_parses_valid_memory_ops_json(self):
        """Parses MEMORY_OPS JSON block into SynthesisResult.memory_ops."""
        text = '''===PROJECT:myproject===
- [implement] Built API endpoints
===MEMORY_OPS===
{"ops": [
  {"action": "ADD", "fact": "project uses gRPC", "scope": "myproject", "section": "Key Decisions", "entities": ["gRPC"]},
  {"action": "UPDATE", "id": "chunk_abc123", "fact": "API client has retry logic", "entities": ["API client"]},
  {"action": "DELETE", "id": "chunk_def456", "reason": "Contradicted: no longer uses REST"},
  {"action": "NOOP", "id": "chunk_ghi789", "reason": "Already captured"}
]}
===END==='''
        result = parse_synthesis_output(text)
        assert len(result.memory_ops) == 4
        assert result.memory_ops[0].action == "ADD"
        assert result.memory_ops[1].action == "UPDATE"
        assert result.memory_ops[2].action == "DELETE"
        assert result.memory_ops[3].action == "NOOP"

    def test_project_blocks_still_parsed_alongside_memory_ops(self):
        """PROJECT blocks are parsed normally when MEMORY_OPS is present."""
        text = '''===PROJECT:myproject===
- [implement] Built API endpoints
===MEMORY_OPS===
{"ops": [{"action": "ADD", "fact": "test", "scope": "myproject", "section": "Key Actions", "entities": []}]}
===END==='''
        result = parse_synthesis_output(text)
        assert len(result.project_blocks) == 1
        assert result.project_blocks[0].project == "myproject"

    def test_missing_memory_ops_backward_compat(self):
        """Output without MEMORY_OPS returns empty memory_ops list."""
        text = '''===PROJECT:myproject===
- [implement] Built API endpoints
===END==='''
        result = parse_synthesis_output(text)
        assert result.memory_ops == []

    def test_malformed_json_produces_warning(self):
        """Invalid JSON in MEMORY_OPS produces warning, rest of output still parsed."""
        text = '''===PROJECT:myproject===
- [implement] Built API endpoints
===MEMORY_OPS===
{this is not valid json}
===END==='''
        result = parse_synthesis_output(text)
        assert result.memory_ops == []
        assert any("MEMORY_OPS" in w for w in result.warnings)
        assert len(result.project_blocks) == 1

    def test_memory_ops_with_missing_optional_fields(self):
        """Ops with missing optional fields (id, reason, entities) still parse."""
        text = '''===MEMORY_OPS===
{"ops": [{"action": "ADD", "fact": "simple fact"}]}
===END==='''
        result = parse_synthesis_output(text)
        assert len(result.memory_ops) == 1
        assert result.memory_ops[0].id is None
        assert result.memory_ops[0].entities is None

    def test_memory_ops_before_project_blocks(self):
        """MEMORY_OPS can appear before PROJECT blocks."""
        text = '''===MEMORY_OPS===
{"ops": [{"action": "ADD", "fact": "test", "scope": "proj", "section": "Key Actions", "entities": []}]}
===PROJECT:proj===
- [implement] Something
===END==='''
        result = parse_synthesis_output(text)
        assert len(result.memory_ops) == 1
        assert len(result.project_blocks) == 1

    def test_memory_op_fields_mapped_correctly(self):
        """All MemoryOp fields are populated from JSON."""
        text = '''===MEMORY_OPS===
{"ops": [{"action": "ADD", "fact": "uses gRPC", "scope": "proj", "section": "Key Decisions", "type": "design", "entities": ["gRPC", "proj"], "reason": "new info"}]}
===END==='''
        result = parse_synthesis_output(text)
        op = result.memory_ops[0]
        assert op.action == "ADD"
        assert op.fact == "uses gRPC"
        assert op.scope == "proj"
        assert op.section == "Key Decisions"
        assert op.type == "design"
        assert op.entities == ["gRPC", "proj"]
        assert op.reason == "new info"

    def test_memory_op_type_defaults_to_none(self):
        """MemoryOp.type is None when not provided in JSON."""
        text = '''===MEMORY_OPS===
{"ops": [{"action": "ADD", "fact": "simple fact"}]}
===END==='''
        result = parse_synthesis_output(text)
        assert result.memory_ops[0].type is None
