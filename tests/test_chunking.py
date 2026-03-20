"""Tests for chunking.py — text chunking pipeline for memory files."""

import hashlib

from chunking import (
    DEFAULT_OVERLAP_RATIO,
    MAX_OVERLAP_PARAGRAPHS,
    Chunk,
    chunk_daily_file,
    chunk_ltm_file,
)
from simhash import compute_simhash


class TestChunkDataclass:
    """Tests for the Chunk dataclass."""

    def test_chunk_is_frozen(self):
        chunk = Chunk(
            content="test",
            source_file="/tmp/test.md",
            source_type="ltm",
            section="## Test",
            chunk_index=0,
            created_at="2026-01-15",
            content_hash="abc123",
            simhash=42,
            scope="global",
            entry_type="pattern",
        )
        try:
            chunk.content = "modified"  # type: ignore[misc]
            assert False, "Chunk should be frozen"
        except AttributeError:
            pass

    def test_chunk_has_all_fields(self):
        chunk = Chunk(
            content="test",
            source_file="/tmp/test.md",
            source_type="ltm",
            section="## Test",
            chunk_index=0,
            created_at="2026-01-15",
            content_hash="abc",
            simhash=42,
            scope="global",
            entry_type="pattern",
        )
        assert chunk.content == "test"
        assert chunk.source_file == "/tmp/test.md"
        assert chunk.source_type == "ltm"
        assert chunk.section == "## Test"
        assert chunk.chunk_index == 0
        assert chunk.created_at == "2026-01-15"
        assert chunk.content_hash == "abc"
        assert chunk.simhash == 42
        assert chunk.scope == "global"
        assert chunk.entry_type == "pattern"


class TestChunkLtmFile:
    """Tests for chunk_ltm_file()."""

    def test_empty_content(self):
        chunks = chunk_ltm_file("", "/tmp/test.md")
        assert chunks == []

    def test_single_section_single_entry(self):
        content = (
            "## Key Actions\n"
            "- (2026-01-15) [implement] Added new chunking pipeline\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", scope="global")
        assert len(chunks) == 1
        assert chunks[0].section == "## Key Actions"
        assert chunks[0].chunk_index == 0
        assert chunks[0].created_at == "2026-01-15"
        assert chunks[0].entry_type == "implement"
        assert chunks[0].scope == "global"
        assert chunks[0].source_type == "ltm"

    def test_multiple_entries_in_section(self):
        content = (
            "## Key Learnings\n"
            "- (2026-01-10) [gotcha] SQLite WAL mode needs busy_timeout\n"
            "\n"
            "- (2026-01-12) [pattern] Use dataclasses for structured data\n"
            "\n"
            "- (2026-01-15) [pitfall] Avoid global mutable state in modules\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", scope="global", overlap_ratio=0)
        assert len(chunks) == 3
        assert chunks[0].chunk_index == 0
        assert chunks[1].chunk_index == 1
        assert chunks[2].chunk_index == 2
        assert chunks[0].entry_type == "gotcha"
        assert chunks[1].entry_type == "pattern"
        assert chunks[2].entry_type == "pitfall"

    def test_section_boundary_respected(self):
        content = (
            "## Key Actions\n"
            "- (2026-01-10) [implement] Feature A\n"
            "\n"
            "## Key Decisions\n"
            "- (2026-01-11) [design] Architecture B\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", scope="global", overlap_ratio=0)
        assert len(chunks) == 2
        assert chunks[0].section == "## Key Actions"
        assert chunks[1].section == "## Key Decisions"
        # Chunk indices reset per section
        assert chunks[0].chunk_index == 0
        assert chunks[1].chunk_index == 0

    def test_overlap_adds_preceding_paragraph(self):
        content = (
            "## Key Actions\n"
            "- (2026-01-10) [implement] First entry\n"
            "\n"
            "- (2026-01-11) [implement] Second entry\n"
            "\n"
            "- (2026-01-12) [implement] Third entry\n"
        )
        chunks = chunk_ltm_file(
            content, "/tmp/ltm.md", scope="global",
            overlap_ratio=DEFAULT_OVERLAP_RATIO,
        )
        assert len(chunks) == 3
        # First chunk has no overlap (nothing precedes it)
        assert "First entry" in chunks[0].content
        assert "Second entry" not in chunks[0].content
        # Second chunk should include first as overlap
        assert "First entry" in chunks[1].content
        assert "Second entry" in chunks[1].content
        # Third chunk should include second as overlap
        assert "Second entry" in chunks[2].content
        assert "Third entry" in chunks[2].content

    def test_no_overlap_when_ratio_zero(self):
        content = (
            "## Key Actions\n"
            "- (2026-01-10) [implement] First entry\n"
            "\n"
            "- (2026-01-11) [implement] Second entry\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", overlap_ratio=0)
        assert len(chunks) == 2
        assert "First entry" not in chunks[1].content

    def test_overlap_does_not_cross_sections(self):
        content = (
            "## Key Actions\n"
            "- (2026-01-10) [implement] Action entry\n"
            "\n"
            "## Key Decisions\n"
            "- (2026-01-11) [design] Decision entry\n"
        )
        chunks = chunk_ltm_file(
            content, "/tmp/ltm.md",
            overlap_ratio=DEFAULT_OVERLAP_RATIO,
        )
        # Decision chunk should NOT contain Action entry (different section)
        decision_chunks = [c for c in chunks if c.section == "## Key Decisions"]
        assert len(decision_chunks) == 1
        assert "Action entry" not in decision_chunks[0].content

    def test_overlap_capped_at_max(self):
        """Overlap count never exceeds MAX_OVERLAP_PARAGRAPHS even with many paragraphs."""
        entries = []
        for i in range(20):
            entries.append(f"- (2026-01-{i+1:02d}) [implement] Entry number {i}\n")
        content = "## Key Actions\n" + "\n".join(entries)
        chunks = chunk_ltm_file(
            content, "/tmp/ltm.md",
            overlap_ratio=DEFAULT_OVERLAP_RATIO,
        )
        # Last chunk should contain at most MAX_OVERLAP_PARAGRAPHS preceding entries
        last = chunks[-1]
        overlap_entries = [
            line for line in last.content.split("\n")
            if line.startswith("- (") and "Entry number" in line
        ]
        # Current paragraph + at most MAX_OVERLAP_PARAGRAPHS overlap paragraphs
        assert len(overlap_entries) <= MAX_OVERLAP_PARAGRAPHS + 1

    def test_comments_skipped(self):
        content = (
            "## Key Actions\n"
            "<!-- Subject to 30-day decay -->\n"
            "- (2026-01-15) [implement] Real entry\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md")
        assert len(chunks) == 1
        assert "Subject to 30-day decay" not in chunks[0].content

    def test_footer_separator_skipped(self):
        """The --- footer separator is not chunked as content."""
        content = (
            "## Key Learnings\n"
            "- (2026-01-15) [gotcha] Real entry\n"
            "\n"
            "---\n"
            "*Last synthesized: 2026-01-15*\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", overlap_ratio=0)
        assert len(chunks) == 1
        assert "---" not in chunks[0].content
        assert "Last synthesized" not in chunks[0].content

    def test_content_hash_deterministic(self):
        content = "## Test\n- (2026-01-15) [pattern] Same content\n"
        chunks_a = chunk_ltm_file(content, "/tmp/a.md")
        chunks_b = chunk_ltm_file(content, "/tmp/b.md")
        assert chunks_a[0].content_hash == chunks_b[0].content_hash

    def test_content_hash_is_sha256(self):
        content = "## Test\n- (2026-01-15) [pattern] Test content\n"
        chunks = chunk_ltm_file(content, "/tmp/test.md")
        # Verify it is a valid SHA-256 hex string
        assert len(chunks[0].content_hash) == 64
        # Verify it matches direct SHA-256
        expected = hashlib.sha256(chunks[0].content.encode("utf-8")).hexdigest()
        assert chunks[0].content_hash == expected

    def test_simhash_populated(self):
        content = "## Test\n- (2026-01-15) [pattern] Test content for simhash\n"
        chunks = chunk_ltm_file(content, "/tmp/test.md")
        assert chunks[0].simhash == compute_simhash(chunks[0].content)
        assert isinstance(chunks[0].simhash, int)

    def test_scope_passed_through(self):
        content = "## Test\n- (2026-01-15) [pattern] Entry\n"
        chunks = chunk_ltm_file(content, "/tmp/test.md", scope="my-project")
        assert chunks[0].scope == "my-project"

    def test_scope_none_when_not_provided(self):
        content = "## Test\n- (2026-01-15) [pattern] Entry\n"
        chunks = chunk_ltm_file(content, "/tmp/test.md")
        assert chunks[0].scope is None

    def test_pinned_section_chunked(self):
        content = (
            "## Pinned\n"
            "- (2026-01-01) [tip] Important pinned item\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/test.md")
        assert len(chunks) == 1
        assert chunks[0].section == "## Pinned"

    def test_preamble_content_chunked(self):
        """Content before the first ## header gets section=''."""
        content = (
            "# Long-Term Memory\n"
            "\n"
            "Some preamble text here.\n"
            "\n"
            "## Key Actions\n"
            "- (2026-01-15) [implement] Entry\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/test.md", overlap_ratio=0)
        preamble_chunks = [c for c in chunks if c.section == ""]
        assert len(preamble_chunks) >= 1

    def test_source_file_stored_as_string(self):
        from pathlib import Path
        content = "## Test\n- (2026-01-15) [pattern] Entry\n"
        chunks = chunk_ltm_file(content, Path("/tmp/test.md"))
        assert chunks[0].source_file == "/tmp/test.md"
        assert isinstance(chunks[0].source_file, str)

    def test_non_entry_paragraphs_in_profile_sections(self):
        """Profile sections (About Me, etc.) contain free-form text, not entries."""
        content = (
            "## About Me\n"
            "Senior engineer working on insurance systems.\n"
            "Prefer concise, direct communication.\n"
            "\n"
            "## Current Projects\n"
            "Working on Claude memory system refactor.\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/test.md", overlap_ratio=0)
        about_chunks = [c for c in chunks if c.section == "## About Me"]
        assert len(about_chunks) >= 1
        # No date or entry_type for free-form text
        assert about_chunks[0].created_at is None
        assert about_chunks[0].entry_type is None


class TestChunkDailyFile:
    """Tests for chunk_daily_file()."""

    def test_empty_content(self):
        chunks = chunk_daily_file("", "/tmp/2026-01-15.md")
        assert chunks == []

    def test_single_entry(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [my-project/implement] Added new feature\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1
        assert chunks[0].source_type == "daily"
        assert chunks[0].section == "## Actions"
        assert chunks[0].scope == "my-project"
        assert chunks[0].entry_type == "implement"
        assert chunks[0].created_at == "2026-01-15"

    def test_multiple_entries_across_sections(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [project-a/implement] Feature one\n"
            "- [project-b/improve] Bug fix\n"
            "## Decisions\n"
            "- [project-a/design] Architecture choice\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 3
        assert chunks[0].section == "## Actions"
        assert chunks[1].section == "## Actions"
        assert chunks[2].section == "## Decisions"

    def test_multi_scope_tag(self):
        content = (
            "# 2026-01-15\n"
            "## Learnings\n"
            "- [global|my-project/gotcha] Shared learning\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1
        assert chunks[0].scope == "global|my-project"
        assert chunks[0].entry_type == "gotcha"

    def test_routed_entry_included(self):
        """Routed entries are included — filtering is the consumer's job."""
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [routed][project/implement] Already routed to LTM\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1
        assert "routed" in chunks[0].content

    def test_comments_skipped(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "<!-- implement: new features -->\n"
            "- [project/implement] Real entry\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1
        assert "implement: new features" not in chunks[0].content

    def test_blank_lines_skipped(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "\n"
            "- [project/implement] Entry\n"
            "\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1

    def test_date_from_header(self):
        content = (
            "# 2026-03-01\n"
            "## Actions\n"
            "- [project/implement] Entry\n"
        )
        chunks = chunk_daily_file(content, "/tmp/daily.md")
        assert chunks[0].created_at == "2026-03-01"

    def test_date_from_parameter(self):
        content = (
            "## Actions\n"
            "- [project/implement] Entry\n"
        )
        chunks = chunk_daily_file(content, "/tmp/daily.md", file_date="2026-05-20")
        assert chunks[0].created_at == "2026-05-20"

    def test_date_parameter_overrides_header(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [project/implement] Entry\n"
        )
        chunks = chunk_daily_file(
            content, "/tmp/daily.md", file_date="2026-05-20"
        )
        assert chunks[0].created_at == "2026-05-20"

    def test_date_from_filename(self):
        content = (
            "## Actions\n"
            "- [project/implement] Entry\n"
        )
        chunks = chunk_daily_file(content, "/tmp/daily/2026-02-28.md")
        assert chunks[0].created_at == "2026-02-28"

    def test_chunk_index_sequential(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [p/implement] One\n"
            "- [p/implement] Two\n"
            "## Decisions\n"
            "- [p/design] Three\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        # chunk_index is sequential across the entire file
        assert [c.chunk_index for c in chunks] == [0, 1, 2]

    def test_each_entry_is_one_chunk(self):
        """Every entry line produces exactly one chunk."""
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [a/implement] First\n"
            "- [b/implement] Second\n"
            "- [c/implement] Third\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 3

    def test_content_hash_and_simhash_populated(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [project/implement] Test entry for hashing\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks[0].content_hash) == 64
        assert isinstance(chunks[0].simhash, int)

    def test_source_file_stored(self):
        content = "## Actions\n- [p/implement] Entry\n"
        chunks = chunk_daily_file(content, "/home/user/daily/2026-01-15.md")
        assert chunks[0].source_file == "/home/user/daily/2026-01-15.md"

    def test_non_list_lines_skipped(self):
        """Lines that don't start with '-' are not entries."""
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "Some non-entry text\n"
            "- [project/implement] Real entry\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1


class TestChunkingIntegration:
    """End-to-end tests with realistic file content."""

    REALISTIC_LTM = (
        "# Long-Term Memory\n"
        "\n"
        "## About Me\n"
        "Senior engineer working on insurance and memory systems.\n"
        "Prefer concise communication, no emojis.\n"
        "\n"
        "## Pinned\n"
        "- (2026-01-01) [tip] Always use WAL mode for SQLite concurrent access\n"
        "\n"
        "## Key Actions\n"
        "<!-- Subject to 30-day decay -->\n"
        "- (2026-03-10) [implement] Built text chunking pipeline with paragraph-level splitting\n"
        "\n"
        "- (2026-03-12) [implement] Added SimHash fingerprinting for near-duplicate detection\n"
        "\n"
        "- (2026-03-15) [improve] Optimized batch embedding with content hash skip\n"
        "\n"
        "## Key Decisions\n"
        "- (2026-03-09) [design] Chose sqlite-vec over FAISS for single-file portability\n"
        "\n"
        "## Key Learnings\n"
        "- (2026-03-11) [gotcha] parse_markdown_sections returns preamble with empty header\n"
        "\n"
        "---\n"
        "*Last synthesized: 2026-03-15*\n"
    )

    REALISTIC_DAILY = (
        "# 2026-03-15\n"
        "## Actions\n"
        "- [claude-memory/implement] Built paragraph chunking with configurable overlap\n"
        "- [claude-memory/implement] Added SimHash module with 64-bit fingerprints\n"
        "- [global|claude-memory/improve] Improved token estimation accuracy\n"
        "## Decisions\n"
        "- [claude-memory/design] Chose dataclass over dict for chunk representation\n"
        "## Learnings\n"
        "- [claude-memory/gotcha] SimHash Hamming distance 3 catches typo-level edits\n"
    )

    def test_ltm_produces_chunks_for_all_sections(self):
        chunks = chunk_ltm_file(
            self.REALISTIC_LTM, "/tmp/global-ltm.md",
            scope="global", overlap_ratio=0,
        )
        sections = {c.section for c in chunks}
        assert "## About Me" in sections
        assert "## Pinned" in sections
        assert "## Key Actions" in sections
        assert "## Key Decisions" in sections
        assert "## Key Learnings" in sections

    def test_ltm_chunk_count(self):
        chunks = chunk_ltm_file(
            self.REALISTIC_LTM, "/tmp/global-ltm.md",
            scope="global", overlap_ratio=0,
        )
        # Preamble: 1 ("# Long-Term Memory"), About Me: 1 (2 lines as 1 paragraph),
        # Pinned: 1, Key Actions: 3, Key Decisions: 1, Key Learnings: 1
        # Footer ("---" + "*Last synthesized*") is filtered by _is_content_line
        assert len(chunks) == 8

    def test_ltm_all_chunks_have_valid_hashes(self):
        chunks = chunk_ltm_file(
            self.REALISTIC_LTM, "/tmp/global-ltm.md", scope="global",
        )
        for chunk in chunks:
            assert len(chunk.content_hash) == 64
            assert isinstance(chunk.simhash, int)
            assert chunk.simhash >= 0

    def test_daily_produces_one_chunk_per_entry(self):
        chunks = chunk_daily_file(
            self.REALISTIC_DAILY, "/tmp/2026-03-15.md",
        )
        assert len(chunks) == 5

    def test_daily_scopes_extracted(self):
        chunks = chunk_daily_file(
            self.REALISTIC_DAILY, "/tmp/2026-03-15.md",
        )
        scopes = [c.scope for c in chunks]
        assert "claude-memory" in scopes
        assert "global|claude-memory" in scopes

    def test_near_duplicate_entries_have_close_simhash(self):
        from simhash import hamming_distance, DEFAULT_HAMMING_THRESHOLD

        content = (
            "## Key Actions\n"
            "- (2026-03-10) [implement] Built text chunking pipeline with paragraph splitting\n"
            "\n"
            "- (2026-03-10) [implement] Built text chunking pipeline with paragraph-level splitting\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", overlap_ratio=0)
        assert len(chunks) == 2
        dist = hamming_distance(chunks[0].simhash, chunks[1].simhash)
        # Near-duplicate entries should have small distance
        # (generous bound: 3x threshold since these are short texts)
        assert dist <= DEFAULT_HAMMING_THRESHOLD * 3

    def test_different_entries_have_distant_simhash(self):
        from simhash import hamming_distance, DEFAULT_HAMMING_THRESHOLD

        content = (
            "## Key Actions\n"
            "- (2026-03-10) [implement] Built SQLite storage layer with WAL mode and migration pipeline\n"
            "\n"
            "## Key Learnings\n"
            "- (2026-03-11) [gotcha] Python pathlib resolves symlinks which breaks worktree detection\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", overlap_ratio=0)
        assert len(chunks) == 2
        dist = hamming_distance(chunks[0].simhash, chunks[1].simhash)
        assert dist > DEFAULT_HAMMING_THRESHOLD
