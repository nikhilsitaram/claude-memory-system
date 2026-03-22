#!/usr/bin/env python3
"""
Unit tests for synthesis.py

Run with: python -m pytest tests/test_synthesis.py -v
"""

import sqlite3  # noqa: I001
from pathlib import Path  # noqa: F401

from synthesis import (
    MemoryOp,
    SynthesisResult,  # noqa: F401
    parse_synthesis_output,
)

def _make_v2_db(db_path):
    """Create a v2 DB for testing synthesis operations."""
    from storage import SCHEMA_DDL

    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA_DDL)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    return conn


# =============================================================================
# parse_synthesis_output Tests (v3: MEMORY_OPS only)
# =============================================================================


class TestParseSynthesisOutput:
    def test_memory_ops_parsed(self):
        """parse_synthesis_output returns memory_ops from MEMORY_OPS block."""
        text = '''===MEMORY_OPS===
{"ops": [
  {"action": "ADD", "fact": "project uses gRPC", "scope": "proj", "section": "Key Decisions", "entities": ["gRPC"]}
]}
===END==='''
        result = parse_synthesis_output(text)
        assert len(result.memory_ops) == 1
        assert result.memory_ops[0].action == "ADD"
        assert result.memory_ops[0].fact == "project uses gRPC"

    def test_legacy_daily_blocks_skipped(self):
        """Legacy ===DAILY:=== blocks are silently skipped, not surfaced as fields."""
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Did something
## Learnings
- [global/pattern] Learned something

===END==="""
        result = parse_synthesis_output(text)
        assert result.memory_ops == []
        assert not hasattr(result, "dailies")

    def test_legacy_route_blocks_skipped(self):
        """Legacy ===ROUTE:=== blocks are silently skipped."""
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/pattern] A pattern

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] A pattern

===END==="""
        result = parse_synthesis_output(text)
        assert result.memory_ops == []
        assert not hasattr(result, "routes")

    def test_text_before_delimiters_ignored(self):
        """Preamble text before first delimiter is ignored."""
        text = """I'll now generate the synthesis output.

===MEMORY_OPS===
{"ops": [{"action": "ADD", "fact": "test fact", "scope": "global"}]}
===END==="""
        result = parse_synthesis_output(text)
        assert len(result.memory_ops) == 1

    def test_missing_end_marker_warns_for_legacy_formats(self):
        """Missing ===END=== produces a warning when legacy delimiters are present."""
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something"""
        result = parse_synthesis_output(text)
        assert result.warnings

    def test_no_blocks_returns_empty(self):
        """Text with no structured blocks returns empty result."""
        text = "Just some random text with no structure"
        result = parse_synthesis_output(text)
        assert result.memory_ops == []
        assert result.warnings == []

    def test_malformed_memory_ops_json_warns(self):
        """Invalid JSON in MEMORY_OPS block produces a warning."""
        text = """===MEMORY_OPS===
{this is not valid json}
===END==="""
        result = parse_synthesis_output(text)
        assert result.memory_ops == []
        assert any("MEMORY_OPS" in w for w in result.warnings)

    def test_multiple_ops_parsed(self):
        """Multiple ops in a single MEMORY_OPS block are all parsed."""
        text = """===MEMORY_OPS===
{"ops": [
  {"action": "ADD", "fact": "fact one", "scope": "global"},
  {"action": "NOOP", "id": "dp_abc", "reason": "still accurate"}
]}
===END==="""
        result = parse_synthesis_output(text)
        assert len(result.memory_ops) == 2
        assert result.memory_ops[0].action == "ADD"
        assert result.memory_ops[1].action == "NOOP"


# =============================================================================
# compute_offsets_from_extracts Tests
# =============================================================================


class TestComputeOffsetsFromExtracts:
    """Test computing session offsets directly from extract files and JSONL sources."""

    def test_finds_sessions_and_computes_offsets(self, tmp_path):
        """Parses session IDs from extract headers, finds JSONL files, returns offsets."""
        from synthesis import compute_offsets_from_extracts

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

        projects_dir = tmp_path / "projects"
        proj_dir = projects_dir / "encoded-path"
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / "abc-123.jsonl"
        jsonl.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n'
                         '{"type":"assistant","message":{"role":"assistant","content":"hello"}}\n'
                         '\n'
                         '{"type":"assistant","message":{"role":"assistant","content":"done"}}\n')

        from unittest.mock import patch
        with patch("synthesis.get_projects_dir", return_value=projects_dir):
            offsets = compute_offsets_from_extracts([str(extract)])

        assert "abc-123" in offsets
        assert offsets["abc-123"]["offset"] == jsonl.stat().st_size
        assert offsets["abc-123"]["lines"] == 3

    def test_multiple_sessions_across_extracts(self, tmp_path):
        """Handles multiple sessions across multiple extract files."""
        from unittest.mock import patch
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
        from unittest.mock import patch
        from synthesis import compute_offsets_from_extracts

        with patch("synthesis.get_projects_dir", return_value=tmp_path):
            assert compute_offsets_from_extracts([]) == {}

    def test_session_not_found_on_disk_skipped(self, tmp_path):
        """Session ID in extract but no matching JSONL returns empty for that session."""
        from unittest.mock import patch
        from synthesis import compute_offsets_from_extracts

        extract = tmp_path / "extract.txt"
        extract.write_text("Session: ghost-session [project: x]\n[CLAUDE]\ntext\n")

        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        (projects_dir / "somedir").mkdir()

        with patch("synthesis.get_projects_dir", return_value=projects_dir):
            offsets = compute_offsets_from_extracts([str(extract)])

        assert offsets == {}

    def test_missing_extract_file_skipped(self, tmp_path):
        """Missing extract file path is silently skipped."""
        from unittest.mock import patch
        from synthesis import compute_offsets_from_extracts

        with patch("synthesis.get_projects_dir", return_value=tmp_path):
            offsets = compute_offsets_from_extracts(["/nonexistent/file.txt"])

        assert offsets == {}


# =============================================================================
# run_post_processing Tests
# =============================================================================

from unittest.mock import patch  # noqa: E402

from synthesis import run_post_processing  # noqa: E402


class TestRunPostProcessing:
    def test_cleans_up_temp_files(self, tmp_path):
        """Removes extract temp files."""
        extract = tmp_path / "extract.txt"
        extract.write_text("data")

        with patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
             patch("synthesis.prune_stale_state_entries"):
            run_post_processing(extract_paths=[str(extract)])

        assert not extract.exists()

    def test_prunes_stale_state(self):
        """Calls prune_stale_state_entries during post-processing."""
        with patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
             patch("synthesis.prune_stale_state_entries") as mock_prune:
            run_post_processing(extract_paths=[])

        mock_prune.assert_called_once()

    def test_updates_timestamp(self, tmp_path):
        """Writes .last-synthesis timestamp file."""
        with patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
             patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path):
            run_post_processing(extract_paths=[])

        ts_file = tmp_path / ".last-synthesis"
        assert ts_file.exists()
        content = ts_file.read_text()
        assert "T" in content


class TestRunPostProcessingOffsetsCleanup:
    """Test that run_post_processing cleans up offsets file."""

    def test_cleans_up_offsets_file(self, tmp_path):
        """Offsets file is cleaned up during post-processing."""
        offsets_file = tmp_path / "offsets.json"
        offsets_file.write_text('{"s1": {"offset": 100}}')

        with patch("synthesis.rebuild_projects_index_quiet"), \
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
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
             patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path):
            run_post_processing(extract_paths=[])


class TestRunPostProcessingNoSubprocess:
    """Verify post-processing uses function calls, not subprocess.run."""

    def test_no_subprocess_import(self):
        """synthesis module should not import subprocess at all."""
        import synthesis

        assert not hasattr(synthesis, "subprocess"), \
            "synthesis.py should not import subprocess anymore"

    def test_calls_run_validate_ltm(self, tmp_path):
        """run_post_processing calls run_validate_ltm."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_validate_ltm") as mock_vl, \
             patch("synthesis.run_decay"):
            run_post_processing(extract_paths=[])
        mock_vl.assert_called_once()

    def test_calls_run_decay(self, tmp_path):
        """run_post_processing calls run_decay."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay") as mock_decay:
            run_post_processing(extract_paths=[])
        mock_decay.assert_called_once()

    def test_calls_reindex_after_synthesis(self, tmp_path):
        """run_post_processing calls _reindex_after_synthesis."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet"), \
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
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay", side_effect=track_decay):
            run_post_processing(extract_paths=[])

        assert call_order.index("rebuild") < call_order.index("decay")


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
        import re
        assert re.match(r"\d{4}-\d{2}-\d{2}", result)

    def test_empty_paths_falls_back_to_today(self):
        """Empty path list falls back to today's date."""
        import re
        result = _extract_date_from_extracts([])
        assert re.match(r"\d{4}-\d{2}-\d{2}", result)


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

        from storage import close_db, query_chunks_by_scope
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "ADD", "fact": "project uses gRPC for internal comms", "scope": "global", "section": "Key Actions", "type": "implement", "entities": ["gRPC"]}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")
            assert warnings == []
            conn = _make_v2_db(db_path)
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
        conn = _make_v2_db(db_path)

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
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

        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
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

        from storage import ChunkRow, close_db, insert_chunk
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)

        try:
            chunk = ChunkRow(content="project uses REST API for external", source_file="global-long-term-memory.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01")
            chunk_id = insert_chunk(conn, chunk)
            conn.commit()

            old_text = ltm_file.read_text()
            ltm_file.write_text(old_text + "- (2026-01-01) [implement] project uses REST API for external\n")

            with patch("storage.get_db_path", return_value=db_path), \
                 patch("storage.ensure_db", return_value=conn), \
                 patch("storage.close_db"), \
                 patch("synthesis.get_global_memory_file", return_value=ltm_file), \
                 patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
                ops = [{"action": "UPDATE", "id": chunk_id, "fact": "project uses gRPC for external", "entities": ["gRPC"]}]
                warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")
        finally:
            close_db(conn)

        assert not any("not found" in w for w in warnings)
        content = ltm_file.read_text()
        assert "gRPC for external" in content

    def test_update_db_only_when_markdown_not_found(self, tmp_path):
        """UPDATE: when no markdown match, applies DB change and logs warning."""
        from unittest.mock import patch

        from storage import ChunkRow, close_db, insert_chunk
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)

        with patch("storage.get_db_path", return_value=db_path):
            conn = _make_v2_db(db_path)
            try:
                chunk = ChunkRow(content="some unique content xyz", source_file="global-long-term-memory.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01")
                chunk_id = insert_chunk(conn, chunk)
                conn.commit()
            finally:
                pass

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "UPDATE", "id": chunk_id, "fact": "new content for this chunk"}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")
        close_db(conn)

        assert any("DB-only" in w or "not found" in w for w in warnings)

    def test_delete_sets_salience_zero_and_archives(self, tmp_path):
        """DELETE: sets salience=0, archives in markdown."""
        from unittest.mock import patch

        from storage import ChunkRow, close_db, insert_chunk, query_chunk_by_id
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)

        try:
            chunk = ChunkRow(content="deprecated fact to delete here", source_file="global-long-term-memory.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01")
            chunk_id = insert_chunk(conn, chunk)
            conn.commit()

            old = ltm_file.read_text()
            ltm_file.write_text(old + "- (2026-01-01) [implement] deprecated fact to delete here\n")

            with patch("storage.get_db_path", return_value=db_path), \
                 patch("storage.ensure_db", return_value=conn), \
                 patch("storage.close_db"), \
                 patch("synthesis.get_global_memory_file", return_value=ltm_file), \
                 patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
                ops = [{"action": "DELETE", "id": chunk_id, "reason": "Outdated"}]
                warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

            assert not any("not found" in w for w in warnings)
            result = query_chunk_by_id(conn, chunk_id)
            assert result.salience == 0.0
        finally:
            close_db(conn)
        content = ltm_file.read_text()
        assert "Archived" in content

    def test_noop_increments_evidence_count(self, tmp_path):
        """NOOP: increments evidence_count on the chunk."""
        from unittest.mock import patch

        from storage import ChunkRow, close_db, insert_chunk, query_chunk_by_id
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path):
            conn = _make_v2_db(db_path)
            try:
                chunk = ChunkRow(content="confirmed fact", source_file="test.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01", evidence_count=1)
                chunk_id = insert_chunk(conn, chunk)
                conn.commit()
            finally:
                pass

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "NOOP", "id": chunk_id, "reason": "Already accurate"}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")
        close_db(conn)

        assert warnings == []
        with patch("storage.get_db_path", return_value=db_path):
            conn = _make_v2_db(db_path)
            try:
                result = query_chunk_by_id(conn, chunk_id)
                assert result.evidence_count == 2
            finally:
                close_db(conn)

    def test_unknown_action_logged_as_warning(self, tmp_path):
        """Unrecognized action produces warning, does not crash."""
        from unittest.mock import patch

        from storage import close_db
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)
        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "MERGE", "fact": "something"}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")
        close_db(conn)

        assert any("MERGE" in w or "Unknown" in w for w in warnings)

    def test_missing_chunk_id_on_update_logged(self, tmp_path):
        """UPDATE with nonexistent chunk ID produces warning."""
        from unittest.mock import patch

        from storage import close_db
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)
        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "UPDATE", "id": "nonexistent-id", "fact": "new fact"}]
            warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")
        close_db(conn)

        assert any("not found" in w or "nonexistent" in w for w in warnings)


# =============================================================================
# C5: Bi-temporal edge handling tests
# =============================================================================


class TestBitemporalEdges:
    """Tests for bi-temporal edge invalidation on DELETE operations."""

    def _setup_db(self, tmp_path):
        import json
        from unittest.mock import patch

        from storage import (
            ChunkRow,
            EdgeRow,
            NodeRow,
            close_db,
            insert_chunk,
            insert_edge,
            insert_node,
            query_node_by_name_and_type,
        )

        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path):
            conn = _make_v2_db(db_path)
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

        from storage import close_db, query_current_edges
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        ltm_file.write_text(ltm_file.read_text() + "- (2026-01-01) [implement] project uses gRPC instead of REST\n")
        db_path, chunk_id, edge_id, _, _ = self._setup_db(tmp_path)
        conn = _make_v2_db(db_path)

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "DELETE", "id": chunk_id, "reason": "Contradicted: no longer uses gRPC"}]
            apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        with patch("storage.get_db_path", return_value=db_path):
            conn = _make_v2_db(db_path)
            try:
                current = query_current_edges(conn)
                current_ids = [e.id for e in current]
                assert edge_id not in current_ids
            finally:
                close_db(conn)

    def test_delete_chunk_with_no_edges_is_safe(self, tmp_path):
        """DELETE on a chunk with no associated edges completes without error."""
        import json
        from unittest.mock import patch

        from storage import ChunkRow, close_db, insert_chunk, query_chunk_by_id
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)

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

            old_text = ltm_file.read_text()
            ltm_file.write_text(old_text + "- (2026-01-01) [implement] fact with no edges\n")

            with patch("storage.get_db_path", return_value=db_path), \
                 patch("storage.ensure_db", return_value=conn), \
                 patch("storage.close_db"), \
                 patch("synthesis.get_global_memory_file", return_value=ltm_file), \
                 patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
                ops = [{"action": "DELETE", "id": chunk_id, "reason": "Outdated"}]
                warnings = apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

            assert not any("error" in w.lower() for w in warnings)
            result = query_chunk_by_id(conn, chunk_id)
            assert result.salience == 0.0
        finally:
            close_db(conn)

    def test_delete_only_invalidates_chunk_related_edges(self, tmp_path):
        """Only edges where both source AND target are chunk entities get invalidated."""
        import json
        from unittest.mock import patch

        from storage import (
            ChunkRow,
            EdgeRow,
            NodeRow,
            close_db,
            insert_chunk,
            insert_edge,
            insert_node,
            query_current_edges,
            query_node_by_name_and_type,
        )
        from synthesis import apply_memory_ops

        ltm_file = _make_ltm_file(tmp_path, "global")
        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)

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

            old_text = ltm_file.read_text()
            ltm_file.write_text(old_text + "- (2026-01-01) [implement] fact about entity-a and entity-b\n")

            with patch("storage.get_db_path", return_value=db_path), \
                 patch("storage.ensure_db", return_value=conn), \
                 patch("storage.close_db"), \
                 patch("synthesis.get_global_memory_file", return_value=ltm_file), \
                 patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
                ops = [{"action": "DELETE", "id": chunk_id, "reason": "Outdated"}]
                apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

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
        import json
        from unittest.mock import patch

        from storage import close_db, query_chunks_by_scope
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)

        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "ADD", "fact": "uses gRPC for comms", "scope": "global", "section": "Key Actions", "entities": ["gRPC", "myproject", "internal services"]}]
            apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        with patch("storage.get_db_path", return_value=db_path):
            conn = _make_v2_db(db_path)
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
        import json
        from unittest.mock import patch

        from storage import ChunkRow, close_db, insert_chunk, query_chunk_by_id
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        ltm_file = _make_ltm_file(tmp_path, "global")
        conn = _make_v2_db(db_path)

        try:
            chunk = ChunkRow(content="old lib usage", source_file="global-long-term-memory.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01", entities=json.dumps(["old-lib"]))
            chunk_id = insert_chunk(conn, chunk)
            conn.commit()

            old_text = ltm_file.read_text()
            ltm_file.write_text(old_text + "- (2026-01-01) [implement] old lib usage\n")

            with patch("storage.get_db_path", return_value=db_path), \
                 patch("storage.ensure_db", return_value=conn), \
                 patch("storage.close_db"), \
                 patch("synthesis.get_global_memory_file", return_value=ltm_file), \
                 patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
                ops = [{"action": "UPDATE", "id": chunk_id, "fact": "new lib api-client usage", "entities": ["new-lib", "api-client"]}]
                apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

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

        from storage import close_db, query_chunks_by_scope
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)

        ltm_file = _make_ltm_file(tmp_path, "global")

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "ADD", "fact": "simple fact without entities", "scope": "global", "section": "Key Actions"}]
            apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        with patch("storage.get_db_path", return_value=db_path):
            conn = _make_v2_db(db_path)
            try:
                chunks = query_chunks_by_scope(conn, "global")
                assert chunks[0].entities is None
            finally:
                close_db(conn)

    def test_entities_roundtrip_json(self, tmp_path):
        """Entities survive JSON encode/decode roundtrip."""
        import json
        from unittest.mock import patch

        from storage import close_db, query_chunks_by_scope
        from synthesis import apply_memory_ops

        db_path = tmp_path / "memory.db"
        conn = _make_v2_db(db_path)

        ltm_file = _make_ltm_file(tmp_path, "global")
        entities = ["Python 3.13", "pytest", "https://example.com", "2026-03-21"]

        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.ensure_db", return_value=conn), patch("storage.close_db"), \
             patch("synthesis.get_global_memory_file", return_value=ltm_file), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"):
            ops = [{"action": "ADD", "fact": "uses various entities", "scope": "global", "section": "Key Actions", "entities": entities}]
            apply_memory_ops(ops, "2026-03-21", global_file=ltm_file, ltm_dir=tmp_path / "project-memory")

        with patch("storage.get_db_path", return_value=db_path):
            conn = _make_v2_db(db_path)
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

    def test_project_blocks_silently_skipped(self):
        """PROJECT blocks are skipped (no longer parsed into result fields)."""
        text = '''===PROJECT:myproject===
- [implement] Built API endpoints
===MEMORY_OPS===
{"ops": [{"action": "ADD", "fact": "test", "scope": "myproject", "section": "Key Actions", "entities": []}]}
===END==='''
        result = parse_synthesis_output(text)
        assert len(result.memory_ops) == 1
        assert not hasattr(result, "project_blocks")

    def test_missing_memory_ops_backward_compat(self):
        """Output without MEMORY_OPS returns empty memory_ops list."""
        text = '''===PROJECT:myproject===
- [implement] Built API endpoints
===END==='''
        result = parse_synthesis_output(text)
        assert result.memory_ops == []

    def test_malformed_json_produces_warning(self):
        """Invalid JSON in MEMORY_OPS produces warning, result is otherwise empty."""
        text = '''===PROJECT:myproject===
- [implement] Built API endpoints
===MEMORY_OPS===
{this is not valid json}
===END==='''
        result = parse_synthesis_output(text)
        assert result.memory_ops == []
        assert any("MEMORY_OPS" in w for w in result.warnings)

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
        """MEMORY_OPS can appear before PROJECT blocks (both silently processed)."""
        text = '''===MEMORY_OPS===
{"ops": [{"action": "ADD", "fact": "test", "scope": "proj", "section": "Key Actions", "entities": []}]}
===PROJECT:proj===
- [implement] Something
===END==='''
        result = parse_synthesis_output(text)
        assert len(result.memory_ops) == 1

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


# =============================================================================
# TestMemoryOpsV3 — C2: Enhanced MEMORY_OPS parser with salience and provenance
# =============================================================================


class TestMemoryOpsV3:
    def test_memory_ops_only_output(self):
        """Parser handles output with MEMORY_OPS but no PROJECT blocks."""
        text = """===MEMORY_OPS===
{"ops": [
  {"action": "ADD", "fact": "gRPC for internal services", "scope": "myproject",
   "type": "design", "salience": 0.8, "entities": ["gRPC"]}
]}
===END==="""
        result = parse_synthesis_output(text)
        assert len(result.memory_ops) == 1
        assert result.memory_ops[0].salience == 0.8

    def test_salience_field_parsed(self):
        """MemoryOp.salience populated from JSON."""
        text = '===MEMORY_OPS===\n{"ops": [{"action": "ADD", "fact": "test", "salience": 0.6}]}\n===END==='
        result = parse_synthesis_output(text)
        assert result.memory_ops[0].salience == 0.6

    def test_salience_defaults_to_none(self):
        """Missing salience in JSON yields None on MemoryOp."""
        text = '===MEMORY_OPS===\n{"ops": [{"action": "ADD", "fact": "test"}]}\n===END==='
        result = parse_synthesis_output(text)
        assert result.memory_ops[0].salience is None

    def test_backward_compat_mixed_format(self):
        """Mixed PROJECT + MEMORY_OPS: MEMORY_OPS parsed, PROJECT block skipped."""
        text = """===PROJECT:myproject===
- [implement] Added retry logic
===MEMORY_OPS===
{"ops": [{"action": "ADD", "fact": "retry logic added", "salience": 0.5}]}
===END==="""
        result = parse_synthesis_output(text)
        assert len(result.memory_ops) == 1
        assert result.memory_ops[0].salience == 0.5

    def test_supersedes_field_parsed(self):
        """supersedes field extracted from ADD ops."""
        text = '===MEMORY_OPS===\n{"ops": [{"action": "ADD", "fact": "new", "supersedes": "dp_abc", "reason": "updated"}]}\n===END==='
        result = parse_synthesis_output(text)
        assert result.memory_ops[0].supersedes == "dp_abc"


# =============================================================================
# TestApplyV3 — C3: DB-only apply pipeline
# =============================================================================


def _make_v3_db_for_synthesis(tmp_path):
    """Create a minimal v3 schema DB (data_points + edges) without vec0."""
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS data_points (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT,
            content TEXT,
            scope TEXT,
            entry_type TEXT,
            source_type TEXT,
            source_sessions TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            salience REAL DEFAULT 1.0,
            access_count INTEGER DEFAULT 0,
            last_accessed TEXT,
            evidence_count INTEGER DEFAULT 1,
            consolidated INTEGER DEFAULT 0,
            content_hash TEXT,
            simhash INTEGER,
            entities TEXT,
            properties TEXT
        );
        CREATE TABLE IF NOT EXISTS edges (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL REFERENCES data_points(id),
            target TEXT NOT NULL REFERENCES data_points(id),
            type TEXT NOT NULL,
            reason TEXT,
            fact TEXT,
            properties TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            valid_from TEXT,
            valid_to TEXT,
            expired_at TEXT,
            weight REAL DEFAULT 1.0,
            source_sessions TEXT
        );
    """)
    conn.commit()
    return conn


class TestApplyV3:
    def test_add_creates_data_point(self, tmp_path):
        from synthesis import apply_memory_ops_v3, MemoryOp
        from storage import query_data_point_by_id
        conn = _make_v3_db_for_synthesis(tmp_path)
        ops = [MemoryOp(action="ADD", fact="gRPC for services", scope="proj",
                        type="design", salience=0.8, entities=["gRPC"])]
        results = apply_memory_ops_v3(conn, ops)
        assert len(results) == 1
        dp = query_data_point_by_id(conn, results[0]["id"])
        assert dp is not None
        assert dp.content == "gRPC for services"
        assert dp.salience == 0.8
        assert dp.type == "memory"

    def test_add_creates_entity_data_points(self, tmp_path):
        from synthesis import apply_memory_ops_v3, MemoryOp
        conn = _make_v3_db_for_synthesis(tmp_path)
        ops = [MemoryOp(action="ADD", fact="uses gRPC", scope="proj",
                        entities=["gRPC", "internal services"])]
        apply_memory_ops_v3(conn, ops)
        entities = conn.execute(
            "SELECT name FROM data_points WHERE type='entity'"
        ).fetchall()
        names = {e[0] for e in entities}
        assert "gRPC" in names
        assert "internal services" in names

    def test_update_modifies_content(self, tmp_path):
        from synthesis import apply_memory_ops_v3, MemoryOp
        from storage import DataPointRow, insert_data_point, query_data_point_by_id
        conn = _make_v3_db_for_synthesis(tmp_path)
        dp_id = insert_data_point(conn, DataPointRow(
            type="memory", content="old fact", scope="proj", id="dp_old"))
        conn.commit()
        ops = [MemoryOp(action="UPDATE", id="dp_old", fact="new fact", entities=["JWT"])]
        apply_memory_ops_v3(conn, ops)
        dp = query_data_point_by_id(conn, "dp_old")
        assert dp.content == "new fact"

    def test_delete_creates_provenance(self, tmp_path):
        from synthesis import apply_memory_ops_v3, MemoryOp
        from storage import DataPointRow, insert_data_point, query_data_point_by_id
        conn = _make_v3_db_for_synthesis(tmp_path)
        insert_data_point(conn, DataPointRow(
            type="memory", content="wrong fact", scope="proj", id="dp_wrong"))
        conn.commit()
        ops = [MemoryOp(action="DELETE", id="dp_wrong", reason="Contradicted")]
        apply_memory_ops_v3(conn, ops)
        dp = query_data_point_by_id(conn, "dp_wrong")
        assert dp.salience == 0.0
        edges = conn.execute(
            "SELECT type FROM edges WHERE target='dp_wrong'"
        ).fetchall()
        assert any(e[0] == "supersedes" for e in edges)

    def test_noop_increments_evidence(self, tmp_path):
        from synthesis import apply_memory_ops_v3, MemoryOp
        from storage import DataPointRow, insert_data_point, query_data_point_by_id
        conn = _make_v3_db_for_synthesis(tmp_path)
        insert_data_point(conn, DataPointRow(
            type="memory", content="confirmed", scope="proj", id="dp_ok"))
        conn.commit()
        ops = [MemoryOp(action="NOOP", id="dp_ok", reason="Still correct")]
        apply_memory_ops_v3(conn, ops)
        dp = query_data_point_by_id(conn, "dp_ok")
        assert dp.evidence_count == 2

    def test_no_filesystem_writes(self, tmp_path):
        """V3 apply does not write to any markdown files."""
        from synthesis import apply_memory_ops_v3, MemoryOp
        import os
        conn = _make_v3_db_for_synthesis(tmp_path)
        ops = [MemoryOp(action="ADD", fact="test", scope="proj")]
        md_files_before = set()
        for root, dirs, files in os.walk(str(tmp_path)):
            md_files_before.update(f for f in files if f.endswith(".md"))
        apply_memory_ops_v3(conn, ops)
        md_files_after = set()
        for root, dirs, files in os.walk(str(tmp_path)):
            md_files_after.update(f for f in files if f.endswith(".md"))
        assert md_files_before == md_files_after

    def test_add_default_salience(self, tmp_path):
        """ADD without salience defaults to 0.5."""
        from synthesis import apply_memory_ops_v3, MemoryOp
        from storage import query_data_point_by_id
        conn = _make_v3_db_for_synthesis(tmp_path)
        ops = [MemoryOp(action="ADD", fact="no salience", scope="proj")]
        results = apply_memory_ops_v3(conn, ops)
        dp = query_data_point_by_id(conn, results[0]["id"])
        assert dp.salience == 0.5
