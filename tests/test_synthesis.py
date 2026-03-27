#!/usr/bin/env python3
"""
Unit tests for synthesis.py

Run with: python -m pytest tests/test_synthesis.py -v
"""

import sqlite3  # noqa: I001
from pathlib import Path  # noqa: F401

from synthesis import (
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
             patch("synthesis.prune_stale_state_entries"):
            run_post_processing(extract_paths=[str(extract)])

        assert not extract.exists()

    def test_prunes_stale_state(self):
        """Calls prune_stale_state_entries during post-processing."""
        with patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis.prune_stale_state_entries") as mock_prune:
            run_post_processing(extract_paths=[])

        mock_prune.assert_called_once()

    def test_updates_timestamp(self, tmp_path):
        """Writes .last-synthesis timestamp file."""
        with patch("synthesis.rebuild_projects_index_quiet"), \
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


    def test_calls_reindex_after_synthesis(self, tmp_path):
        """run_post_processing calls _reindex_after_synthesis."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet"), \
             patch("synthesis._reindex_after_synthesis") as mock_reindex:
            run_post_processing(extract_paths=[])
        mock_reindex.assert_called_once()

    def test_calls_rebuild_projects_index(self, tmp_path):
        """run_post_processing rebuilds projects index before decay."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.rebuild_projects_index_quiet") as mock_rebuild:
            run_post_processing(extract_paths=[])
        mock_rebuild.assert_called_once()




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
            properties TEXT,
            certainty INTEGER DEFAULT NULL,
            validity_context TEXT DEFAULT NULL
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
        from storage import query_data_point_by_id
        from synthesis import MemoryOp, apply_memory_ops_v3
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
        from synthesis import MemoryOp, apply_memory_ops_v3
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
        from storage import DataPointRow, insert_data_point, query_data_point_by_id
        from synthesis import MemoryOp, apply_memory_ops_v3
        conn = _make_v3_db_for_synthesis(tmp_path)
        insert_data_point(conn, DataPointRow(
            type="memory", content="old fact", scope="proj", id="dp_old"))
        conn.commit()
        ops = [MemoryOp(action="UPDATE", id="dp_old", fact="new fact", entities=["JWT"])]
        apply_memory_ops_v3(conn, ops)
        dp = query_data_point_by_id(conn, "dp_old")
        assert dp.content == "new fact"

    def test_delete_creates_provenance(self, tmp_path):
        from storage import DataPointRow, insert_data_point, query_data_point_by_id
        from synthesis import MemoryOp, apply_memory_ops_v3
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
        from storage import DataPointRow, insert_data_point, query_data_point_by_id
        from synthesis import MemoryOp, apply_memory_ops_v3
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
        import os

        from synthesis import MemoryOp, apply_memory_ops_v3
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
        from storage import query_data_point_by_id
        from synthesis import MemoryOp, apply_memory_ops_v3
        conn = _make_v3_db_for_synthesis(tmp_path)
        ops = [MemoryOp(action="ADD", fact="no salience", scope="proj")]
        results = apply_memory_ops_v3(conn, ops)
        dp = query_data_point_by_id(conn, results[0]["id"])
        assert dp.salience == 0.5

    def test_add_with_nonexistent_supersedes_skips_gracefully(self, tmp_path):
        """ADD with supersedes pointing to a nonexistent ID is skipped without crashing."""
        from storage import query_data_point_by_id
        from synthesis import MemoryOp, apply_memory_ops_v3
        conn = _make_v3_db_for_synthesis(tmp_path)
        ops = [MemoryOp(action="ADD", fact="new fact", scope="proj",
                        supersedes="nonexistent-dp-id", reason="replaced old")]
        results = apply_memory_ops_v3(conn, ops)
        assert results[0]["status"] == "inserted"
        dp = query_data_point_by_id(conn, results[0]["id"])
        assert dp is not None

    def test_add_with_self_supersedes_skips_gracefully(self, tmp_path):
        """ADD where supersedes == new dp id (self-reference) is skipped without crashing.

        This can't happen in practice (dp_id is generated after insert), but the
        except clause should only catch ValueError and sqlite3.IntegrityError.
        """

        from storage import DataPointRow, insert_data_point
        from synthesis import MemoryOp, _apply_add_v3

        conn = _make_v3_db_for_synthesis(tmp_path)

        insert_data_point(conn, DataPointRow(
            type="memory", content="existing", scope="proj", id="dp-self"))
        conn.commit()

        op = MemoryOp(action="ADD", fact="updated", scope="proj", supersedes="dp-self")
        result = _apply_add_v3(conn, op)
        assert result["status"] == "inserted"


# =============================================================================
# TestApplyAddV3ExceptionHandling — Issue 8: except clause specificity
# =============================================================================


class TestApplyAddV3ExceptionHandling:
    """Tests that _apply_add_v3 only catches expected exceptions from provenance."""

    def test_value_error_from_provenance_is_silenced(self, tmp_path):
        """ValueError from create_provenance_edge (e.g., self-ref) is silenced."""
        from unittest.mock import patch

        from synthesis import MemoryOp, _apply_add_v3

        conn = _make_v3_db_for_synthesis(tmp_path)
        op = MemoryOp(action="ADD", fact="new fact", scope="proj", supersedes="dp-old")

        with patch("storage.create_provenance_edge", side_effect=ValueError("self-ref")):
            result = _apply_add_v3(conn, op)

        assert result["status"] == "inserted"

    def test_integrity_error_from_provenance_is_silenced(self, tmp_path):
        """sqlite3.IntegrityError from create_provenance_edge (FK miss) is silenced."""
        import sqlite3
        from unittest.mock import patch

        from synthesis import MemoryOp, _apply_add_v3

        conn = _make_v3_db_for_synthesis(tmp_path)
        op = MemoryOp(action="ADD", fact="new fact", scope="proj", supersedes="dp-old")

        with patch("storage.create_provenance_edge",
                   side_effect=sqlite3.IntegrityError("FK violation")):
            result = _apply_add_v3(conn, op)

        assert result["status"] == "inserted"

    def test_unexpected_exception_from_provenance_propagates(self, tmp_path):
        """Unexpected exceptions from create_provenance_edge are NOT silenced."""
        from unittest.mock import patch

        import pytest
        from synthesis import MemoryOp, _apply_add_v3

        conn = _make_v3_db_for_synthesis(tmp_path)
        op = MemoryOp(action="ADD", fact="new fact", scope="proj", supersedes="dp-old")

        with patch("storage.create_provenance_edge", side_effect=RuntimeError("unexpected")):
            with pytest.raises(RuntimeError, match="unexpected"):
                _apply_add_v3(conn, op)


class TestCertaintyInMemoryOps:
    """Tests for certainty field in MemoryOp and synthesis pipeline."""

    def test_memory_op_parses_certainty(self):
        from synthesis import parse_synthesis_output
        text = '===MEMORY_OPS===\n{"ops": [{"action": "ADD", "fact": "test", "scope": "global", "certainty": 4}]}\n===END==='
        result = parse_synthesis_output(text)
        assert result.memory_ops[0].certainty == 4

    def test_memory_op_certainty_default_none(self):
        from synthesis import parse_synthesis_output
        text = '===MEMORY_OPS===\n{"ops": [{"action": "ADD", "fact": "test", "scope": "global"}]}\n===END==='
        result = parse_synthesis_output(text)
        assert result.memory_ops[0].certainty is None

    def test_apply_add_v3_writes_certainty(self, tmp_path):
        from storage import query_data_point_by_id
        from synthesis import MemoryOp, _apply_add_v3
        conn = _make_v3_db_for_synthesis(tmp_path)
        op = MemoryOp(action="ADD", fact="certain fact", scope="global", certainty=4)
        result = _apply_add_v3(conn, op)
        dp = query_data_point_by_id(conn, result["id"])
        assert dp.certainty == 4

    def test_apply_add_v3_defaults_certainty_to_2(self, tmp_path):
        from storage import query_data_point_by_id
        from synthesis import MemoryOp, _apply_add_v3
        conn = _make_v3_db_for_synthesis(tmp_path)
        op = MemoryOp(action="ADD", fact="no certainty", scope="global")
        result = _apply_add_v3(conn, op)
        dp = query_data_point_by_id(conn, result["id"])
        assert dp.certainty == 2


# =============================================================================
# Entity dedup idempotency — two ADDs mentioning the same entity
# =============================================================================


class TestEntityDedupIdempotency:
    """Two ADD ops referencing the same entity should produce exactly 1 entity data_point."""

    def test_two_adds_same_entity_produce_one_entity(self, tmp_path):
        from synthesis import MemoryOp, apply_memory_ops_v3

        conn = _make_v3_db_for_synthesis(tmp_path)
        ops = [
            MemoryOp(action="ADD", fact="gRPC used for internal services", scope="proj",
                     entities=["gRPC"]),
            MemoryOp(action="ADD", fact="gRPC requires protobuf definitions", scope="proj",
                     entities=["gRPC"]),
        ]
        apply_memory_ops_v3(conn, ops)

        # 2 memory data_points created (one per ADD)
        memory_rows = conn.execute(
            "SELECT id FROM data_points WHERE type='memory'"
        ).fetchall()
        assert len(memory_rows) == 2

        # Exactly 1 entity with name "gRPC"
        entity_rows = conn.execute(
            "SELECT id FROM data_points WHERE type='entity' AND name='gRPC'"
        ).fetchall()
        assert len(entity_rows) == 1

        # 2 "mentions" edges, both pointing to the same entity
        entity_id = entity_rows[0][0]
        mention_edges = conn.execute(
            "SELECT source, target FROM edges WHERE type='mentions' AND target=?",
            (entity_id,),
        ).fetchall()
        assert len(mention_edges) == 2

        # Each edge source is a different memory data_point
        edge_sources = {e[0] for e in mention_edges}
        memory_ids = {r[0] for r in memory_rows}
        assert edge_sources == memory_ids


# =============================================================================
# A3: SimHash dedup gate in _apply_add_v3
# =============================================================================


class TestApplyAddV3SimhashDedup:
    """Tests for SimHash-based deduplication in _apply_add_v3."""

    def test_near_duplicate_deduped(self, shared_db):
        """Near-duplicate ADD is deduped: bumps evidence_count, no new row."""
        from datetime import datetime, timezone

        from simhash import compute_simhash
        from storage import DataPointRow, insert_data_point
        from synthesis import MemoryOp, _apply_add_v3, _simhash_to_sqlite

        content = "Always use git config global init defaultBranch main for setting up new repositories in any project that needs version control"
        dp_id = insert_data_point(shared_db, DataPointRow(
            type="memory", content=content, scope="global",
            salience=0.7, simhash=_simhash_to_sqlite(compute_simhash(content)),
            source_sessions='["sess-1"]',
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        ))
        shared_db.commit()

        op = MemoryOp(
            action="ADD",
            fact="Always use git config global init defaultBranch main for setting up new repositories in any project that needs versions control",
            scope="global",
        )
        result = _apply_add_v3(shared_db, op)

        assert result["status"] == "deduped"

        # evidence_count should be incremented
        row = shared_db.execute(
            "SELECT evidence_count FROM data_points WHERE id=?", (dp_id,)
        ).fetchone()
        assert row[0] == 2

        # No new data_point inserted
        count = shared_db.execute(
            "SELECT COUNT(*) FROM data_points WHERE type='memory' AND scope='global'"
        ).fetchone()[0]
        assert count == 1

    def test_no_duplicate_inserts_normally(self, shared_db):
        """Non-duplicate ADD inserts normally with status='inserted'."""
        from synthesis import MemoryOp, _apply_add_v3

        op = MemoryOp(
            action="ADD",
            fact="pytest -x stops on first failure",
            scope="global",
        )
        result = _apply_add_v3(shared_db, op)

        assert result["status"] == "inserted"

        count = shared_db.execute(
            "SELECT COUNT(*) FROM data_points WHERE type='memory' AND scope='global'"
        ).fetchone()[0]
        assert count == 1

    def test_dedup_preserves_source_sessions(self, shared_db):
        """Dedup preserves existing source_sessions on the original data_point."""
        from datetime import datetime, timezone

        from simhash import compute_simhash
        from storage import DataPointRow, insert_data_point
        from synthesis import MemoryOp, _apply_add_v3, _simhash_to_sqlite

        content = "Always use git config global init defaultBranch main for setting up new repositories in anys project that needs version control"
        dp_id = insert_data_point(shared_db, DataPointRow(
            type="memory", content=content, scope="global",
            salience=0.7, simhash=_simhash_to_sqlite(compute_simhash(content)),
            source_sessions='["sess-1"]',
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        ))
        shared_db.commit()

        op = MemoryOp(
            action="ADD",
            fact="Always use git config global init defaultBranch main for setting up new repositories in any project that needs version control",
            scope="global",
        )
        _apply_add_v3(shared_db, op)

        row = shared_db.execute(
            "SELECT source_sessions FROM data_points WHERE id=?", (dp_id,)
        ).fetchone()
        import json
        sessions = json.loads(row[0])
        assert "sess-1" in sessions

    def test_dedup_replaces_content_when_new_is_longer(self, shared_db):
        """Dedup replaces content when new content is >2x longer."""
        from datetime import datetime, timezone

        from simhash import compute_simhash
        from storage import DataPointRow, insert_data_point
        from synthesis import MemoryOp, _apply_add_v3, _simhash_to_sqlite

        long_fact = (
            "Always use git config global init defaultBranch main for setting "
            "up new repositories in any project that needs version control"
        )
        long_simhash = _simhash_to_sqlite(compute_simhash(long_fact))

        short_content = "git config defaultBranch"
        dp_id = insert_data_point(shared_db, DataPointRow(
            type="memory", content=short_content, scope="global",
            salience=0.7, simhash=long_simhash,
            source_sessions="[]",
            created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        ))
        shared_db.commit()

        op = MemoryOp(
            action="ADD",
            fact=long_fact,
            scope="global",
        )
        result = _apply_add_v3(shared_db, op)

        assert result["status"] == "deduped"

        row = shared_db.execute(
            "SELECT content FROM data_points WHERE id=?", (dp_id,)
        ).fetchone()
        assert row[0] == long_fact
