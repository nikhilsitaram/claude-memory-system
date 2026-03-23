#!/usr/bin/env python3
"""
Unit tests for scripts/backfill.py entity re-extraction.

Tests cover: idempotent skip, progress reporting, Sonnet model selection,
batch processing, dry-run mode, and LLM failure handling.

Run with: python3 -m pytest tests/test_backfill.py -v
"""

import json
from unittest import mock

import pytest
from backfill import (
    BATCH_SIZE,
    MODEL,
    build_extraction_prompt,
    extract_entities_batch,
    get_chunks_needing_entities,
    run_backfill,
)
from storage import ChunkRow, close_db, insert_chunk, query_chunk_by_id

# ============================================================================
# Fixtures
# ============================================================================


def _make_v2_db(db_path):
    """Create a v2 DB for testing backfill operations."""
    import sqlite3

    from storage import SCHEMA_DDL

    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA_DDL)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    return conn


@pytest.fixture
def db_dir(tmp_path):
    """Provide a temporary directory for the DB and patch get_db_path."""
    db_path = tmp_path / "memory.db"
    with mock.patch("storage.get_db_path", return_value=db_path), \
         mock.patch("backfill.ensure_db") as mock_ensure, \
         mock.patch("backfill.close_db"):
        conn = _make_v2_db(db_path)
        mock_ensure.return_value = conn
        yield tmp_path, conn
    close_db(conn)


@pytest.fixture
def db(db_dir):
    """Return the connection from db_dir fixture."""
    return db_dir[1]


def _insert_test_chunk(conn, chunk_id, content, entities=None):
    """Helper to insert a chunk with specific id and optional entities."""
    chunk = ChunkRow(
        id=chunk_id,
        content=content,
        source_file="test.md",
        source_type="ltm",
        scope="global",
        entities=entities,
    )
    insert_chunk(conn, chunk)
    conn.commit()


def _make_llm_response(chunk_entity_map):
    """Build a mock LLM JSON response from a dict of {chunk_id: [entities]}."""
    results = [
        {"chunk_id": cid, "entities": ents}
        for cid, ents in chunk_entity_map.items()
    ]
    return json.dumps({"results": results})


# ============================================================================
# get_chunks_needing_entities
# ============================================================================


class TestGetChunksNeedingEntities:
    """Tests for get_chunks_needing_entities query."""

    def test_returns_only_null_entities(self, db_dir):
        db = db_dir[1]
        _insert_test_chunk(db, "c1", "chunk one", entities=None)
        _insert_test_chunk(db, "c2", "chunk two", entities=json.dumps(["Python"]))
        _insert_test_chunk(db, "c3", "chunk three", entities=None)

        result = get_chunks_needing_entities(db)
        ids = [r[0] for r in result]

        assert "c1" in ids
        assert "c3" in ids
        assert "c2" not in ids

    def test_returns_empty_when_all_have_entities(self, db_dir):
        db = db_dir[1]
        _insert_test_chunk(db, "c1", "chunk one", entities=json.dumps(["A"]))

        result = get_chunks_needing_entities(db)
        assert result == []


# ============================================================================
# build_extraction_prompt
# ============================================================================


class TestBuildExtractionPrompt:
    """Tests for the prompt builder."""

    def test_includes_chunk_ids_and_content(self):
        chunks = [("id1", "some content"), ("id2", "other content")]
        prompt = build_extraction_prompt(chunks)

        assert "[id1] some content" in prompt
        assert "[id2] other content" in prompt

    def test_includes_json_format_instruction(self):
        prompt = build_extraction_prompt([("x", "y")])
        assert "JSON" in prompt
        assert "chunk_id" in prompt
        assert "entities" in prompt


# ============================================================================
# extract_entities_batch
# ============================================================================


class TestExtractEntitiesBatch:
    """Tests for LLM entity extraction."""

    def test_uses_sonnet_model(self):
        """Backfill command uses Sonnet model specifically."""
        assert MODEL == "sonnet"

        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = _make_llm_response({"c1": ["Python"]})
        mock_result.stderr = ""

        with mock.patch("backfill.subprocess.run", return_value=mock_result) as mock_run:
            extract_entities_batch([("c1", "content")])
            call_args = mock_run.call_args[0][0]
            assert "--model" in call_args
            model_idx = call_args.index("--model")
            assert call_args[model_idx + 1] == MODEL

    def test_returns_entity_map(self):
        """Successful LLM call returns chunk_id -> entities mapping."""
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = _make_llm_response({
            "c1": ["Python", "pytest"],
            "c2": ["JavaScript"],
        })
        mock_result.stderr = ""

        with mock.patch("backfill.subprocess.run", return_value=mock_result):
            result = extract_entities_batch([("c1", "content1"), ("c2", "content2")])

        assert result == {"c1": ["Python", "pytest"], "c2": ["JavaScript"]}

    def test_returns_empty_on_nonzero_exit(self):
        """Non-zero exit code returns empty dict."""
        mock_result = mock.Mock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error"

        with mock.patch("backfill.subprocess.run", return_value=mock_result):
            result = extract_entities_batch([("c1", "content")])

        assert result == {}

    def test_returns_empty_on_invalid_json(self):
        """Invalid JSON output returns empty dict."""
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = "not valid json"
        mock_result.stderr = ""

        with mock.patch("backfill.subprocess.run", return_value=mock_result):
            result = extract_entities_batch([("c1", "content")])

        assert result == {}

    def test_returns_empty_on_timeout(self):
        """Timeout returns empty dict."""
        import subprocess as sp
        with mock.patch("backfill.subprocess.run", side_effect=sp.TimeoutExpired("cmd", 120)):
            result = extract_entities_batch([("c1", "content")])

        assert result == {}


# ============================================================================
# run_backfill
# ============================================================================


class TestRunBackfill:
    """Tests for the main backfill orchestrator."""

    def test_skips_chunks_with_entities(self, db_dir, capsys):
        """Idempotent: chunks where entities IS NOT NULL are skipped."""
        db = db_dir[1]
        _insert_test_chunk(db, "c1", "chunk one", entities=None)
        _insert_test_chunk(db, "c2", "chunk two", entities=json.dumps(["Python"]))
        _insert_test_chunk(db, "c3", "chunk three", entities=None)

        llm_response = _make_llm_response({
            "c1": ["Go", "concurrency"],
            "c3": ["Rust", "async"],
        })
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = llm_response
        mock_result.stderr = ""

        with mock.patch("backfill.subprocess.run", return_value=mock_result):
            rc = run_backfill()

        assert rc == 0
        assert query_chunk_by_id(db, "c1").entities == json.dumps(["Go", "concurrency"])
        assert query_chunk_by_id(db, "c3").entities == json.dumps(["Rust", "async"])
        assert query_chunk_by_id(db, "c2").entities == json.dumps(["Python"])

    def test_processes_chunks_without_entities(self, db_dir):
        """Chunks where entities IS NULL are sent for extraction."""
        db = db_dir[1]
        _insert_test_chunk(db, "c1", "chunk about Python", entities=None)
        _insert_test_chunk(db, "c2", "chunk about JS", entities=None)

        llm_response = _make_llm_response({
            "c1": ["Python"],
            "c2": ["JavaScript"],
        })
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = llm_response
        mock_result.stderr = ""

        with mock.patch("backfill.subprocess.run", return_value=mock_result):
            rc = run_backfill()

        assert rc == 0
        assert query_chunk_by_id(db, "c1").entities == json.dumps(["Python"])
        assert query_chunk_by_id(db, "c2").entities == json.dumps(["JavaScript"])

    def test_progress_reporting(self, db_dir, capsys):
        """Prints progress: 'Processed N/M chunks'."""
        db = db_dir[1]
        for i in range(5):
            _insert_test_chunk(db, f"c{i}", f"chunk {i}", entities=None)

        entity_map = {f"c{i}": [f"entity{i}"] for i in range(5)}
        llm_response = _make_llm_response(entity_map)
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = llm_response
        mock_result.stderr = ""

        with mock.patch("backfill.subprocess.run", return_value=mock_result):
            run_backfill()

        output = capsys.readouterr().out
        assert "5/5 chunks" in output

    def test_batch_size_respected(self, db_dir):
        """Chunks are batched (max BATCH_SIZE per LLM call)."""
        db = db_dir[1]
        chunk_count = BATCH_SIZE * 2 + 10
        for i in range(chunk_count):
            _insert_test_chunk(db, f"c{i}", f"chunk {i}", entities=None)

        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stderr = ""

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            prompt_input = kwargs.get("input", "")
            chunk_ids = []
            for line in prompt_input.split("\n"):
                if line.startswith("[c"):
                    cid = line.split("]")[0].lstrip("[")
                    chunk_ids.append(cid)
            entity_map = {cid: ["entity"] for cid in chunk_ids}
            mock_result.stdout = _make_llm_response(entity_map)
            return mock_result

        with mock.patch("backfill.subprocess.run", side_effect=side_effect):
            run_backfill(batch_size=BATCH_SIZE)

        expected_batches = (chunk_count + BATCH_SIZE - 1) // BATCH_SIZE
        assert call_count == expected_batches

    def test_idempotent_rerun(self, db_dir):
        """Running backfill twice processes zero chunks on second run."""
        db = db_dir[1]
        _insert_test_chunk(db, "c1", "chunk one", entities=None)

        llm_response = _make_llm_response({"c1": ["Python"]})
        mock_result = mock.Mock()
        mock_result.returncode = 0
        mock_result.stdout = llm_response
        mock_result.stderr = ""

        with mock.patch("backfill.subprocess.run", return_value=mock_result) as mock_run:
            run_backfill()
            mock_run.reset_mock()

            run_backfill()
            mock_run.assert_not_called()

    def test_handles_llm_failure_gracefully(self, db_dir, capsys):
        """LLM call failure logs warning, continues with next batch."""
        db = db_dir[1]
        for i in range(BATCH_SIZE + 5):
            _insert_test_chunk(db, f"c{i}", f"chunk {i}", entities=None)

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            result = mock.Mock()
            result.stderr = ""
            if call_count == 1:
                result.returncode = 1
                result.stdout = ""
                result.stderr = "API error"
            else:
                prompt_input = kwargs.get("input", "")
                chunk_ids = []
                for line in prompt_input.split("\n"):
                    if line.startswith("[c"):
                        cid = line.split("]")[0].lstrip("[")
                        chunk_ids.append(cid)
                entity_map = {cid: ["entity"] for cid in chunk_ids}
                result.returncode = 0
                result.stdout = _make_llm_response(entity_map)
            return result

        with mock.patch("backfill.subprocess.run", side_effect=side_effect):
            rc = run_backfill(batch_size=BATCH_SIZE)

        assert rc == 0
        captured = capsys.readouterr()
        assert "Warning" in captured.err or "Backfill complete" in captured.out

    def test_dry_run_mode(self, db_dir, capsys):
        """--dry-run shows what would be processed without changes."""
        db = db_dir[1]
        _insert_test_chunk(db, "c1", "chunk one", entities=None)
        _insert_test_chunk(db, "c2", "chunk two", entities=None)

        with mock.patch("backfill.subprocess.run") as mock_run:
            rc = run_backfill(dry_run=True)

        assert rc == 0
        mock_run.assert_not_called()

        output = capsys.readouterr().out
        assert "DRY RUN" in output
        assert "2 chunks" in output

        assert query_chunk_by_id(db, "c1").entities is None
        assert query_chunk_by_id(db, "c2").entities is None

    def test_no_chunks_need_processing(self, db_dir, capsys):
        """When all chunks have entities, reports nothing to do."""
        db = db_dir[1]
        _insert_test_chunk(db, "c1", "chunk one", entities=json.dumps(["A"]))

        with mock.patch("backfill.subprocess.run") as mock_run:
            rc = run_backfill()

        assert rc == 0
        mock_run.assert_not_called()

        output = capsys.readouterr().out
        assert "No chunks need entity extraction" in output
