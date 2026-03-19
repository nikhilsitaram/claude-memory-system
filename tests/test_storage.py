#!/usr/bin/env python3
"""
Integration and unit tests for scripts/storage.py

Tests cover: schema creation, WAL mode, connection helpers, CRUD operations,
content hashing, and provenance tracking.

Run with: python3 -m pytest tests/test_storage.py -v
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

# These imports will fail until storage.py is created (RED phase)
from storage import (
    SCHEMA_VERSION,
    ChunkRow,
    EdgeRow,
    NodeRow,
    close_db,
    delete_chunks_by_source,
    ensure_db,
    get_db,
    insert_chunk,
    insert_edge,
    insert_node,
    query_chunks_by_scope,
    query_chunks_by_source,
    query_node_by_name_and_type,
    query_nodes_by_scope,
    update_node_access,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def db_dir(tmp_path):
    """Provide a temporary directory for the DB and patch get_db_path."""
    db_path = tmp_path / "memory.db"
    with mock.patch("storage.get_db_path", return_value=db_path):
        yield tmp_path


@pytest.fixture
def db(db_dir):
    """Create a fresh DB and return the connection."""
    conn = ensure_db()
    yield conn
    close_db(conn)


# ============================================================================
# Schema and lifecycle
# ============================================================================


class TestSchemaCreation:
    def test_ensure_db_creates_file(self, db_dir):
        conn = ensure_db()
        assert (db_dir / "memory.db").exists()
        close_db(conn)

    def test_wal_mode_enabled(self, db):
        result = db.execute("PRAGMA journal_mode").fetchone()
        assert result[0] == "wal"

    def test_busy_timeout_set(self, db):
        result = db.execute("PRAGMA busy_timeout").fetchone()
        assert result[0] == 5000

    def test_foreign_keys_enabled(self, db):
        result = db.execute("PRAGMA foreign_keys").fetchone()
        assert result[0] == 1

    def test_schema_version_stored(self, db):
        result = db.execute("PRAGMA user_version").fetchone()
        assert result[0] == SCHEMA_VERSION

    def test_tables_exist(self, db):
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "nodes" in tables
        assert "edges" in tables
        assert "chunks" in tables

    def test_indexes_exist(self, db):
        indexes = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected = {
            "idx_nodes_type",
            "idx_nodes_scope",
            "idx_nodes_simhash",
            "idx_edges_source",
            "idx_edges_target",
            "idx_edges_valid",
            "idx_chunks_source",
            "idx_chunks_scope",
            "idx_chunks_hash",
            "idx_chunks_simhash",
        }
        assert expected.issubset(indexes)

    def test_ensure_db_idempotent(self, db_dir):
        conn1 = ensure_db()
        conn1.execute(
            "INSERT INTO nodes (id, name, type, created_at) "
            "VALUES ('n1', 'test', 'tool', '2026-01-01')"
        )
        conn1.commit()
        close_db(conn1)
        # Second call should not drop data
        conn2 = ensure_db()
        row = conn2.execute(
            "SELECT name FROM nodes WHERE id='n1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "test"
        close_db(conn2)


# ============================================================================
# Chunk CRUD
# ============================================================================


class TestChunkCRUD:
    def test_insert_and_query_by_scope(self, db):
        chunk = ChunkRow(
            content="Use pytest tmp_path for isolation",
            source_file="global-long-term-memory.md",
            source_type="ltm",
            section="## Key Learnings",
            scope="global",
            entry_type="pattern",
            chunk_index=0,
            created_at="2026-03-01",
        )
        insert_chunk(db, chunk)
        results = query_chunks_by_scope(db, "global")
        assert len(results) == 1
        assert results[0].content == chunk.content
        assert results[0].scope == "global"

    def test_insert_generates_id_and_hash(self, db):
        chunk = ChunkRow(
            content="Test content",
            source_file="test.md",
            source_type="daily",
            scope="global",
            chunk_index=0,
            created_at="2026-03-01",
        )
        insert_chunk(db, chunk)
        row = db.execute("SELECT id, content_hash FROM chunks").fetchone()
        assert row[0]  # id is non-empty
        expected_hash = hashlib.sha256(b"Test content").hexdigest()[:16]
        assert row[1] == expected_hash

    def test_query_by_source_file(self, db):
        for i in range(3):
            insert_chunk(
                db,
                ChunkRow(
                    content=f"Entry {i}",
                    source_file="2026-03-01.md",
                    source_type="daily",
                    scope="global",
                    chunk_index=i,
                    created_at="2026-03-01",
                ),
            )
        insert_chunk(
            db,
            ChunkRow(
                content="Other",
                source_file="2026-03-02.md",
                source_type="daily",
                scope="global",
                chunk_index=0,
                created_at="2026-03-02",
            ),
        )
        results = query_chunks_by_source(db, "2026-03-01.md")
        assert len(results) == 3

    def test_delete_by_source(self, db):
        insert_chunk(
            db,
            ChunkRow(
                content="To delete",
                source_file="old.md",
                source_type="ltm",
                scope="global",
                chunk_index=0,
                created_at="2026-01-01",
            ),
        )
        assert len(query_chunks_by_source(db, "old.md")) == 1
        count = delete_chunks_by_source(db, "old.md")
        assert count == 1
        assert len(query_chunks_by_source(db, "old.md")) == 0

    def test_provenance_columns(self, db):
        chunk = ChunkRow(
            content="Provenance test",
            source_file="test.md",
            source_type="ltm",
            scope="global",
            chunk_index=0,
            created_at="2026-03-01",
            source_sessions=json.dumps(["2026-03-01", "2026-03-02"]),
            evidence_count=2,
        )
        insert_chunk(db, chunk)
        results = query_chunks_by_scope(db, "global")
        assert results[0].evidence_count == 2
        sessions = json.loads(results[0].source_sessions)
        assert len(sessions) == 2


# ============================================================================
# Node and edge CRUD
# ============================================================================


class TestNodeCRUD:
    def test_insert_and_query_by_scope(self, db):
        node = NodeRow(
            name="claude-memory-system",
            type="project",
            scope="global",
            description="Memory persistence for Claude Code",
            created_at="2026-03-01",
        )
        insert_node(db, node)
        results = query_nodes_by_scope(db, "global")
        assert len(results) == 1
        assert results[0].name == "claude-memory-system"

    def test_query_by_name_and_type(self, db):
        insert_node(
            db,
            NodeRow(
                name="pytest",
                type="tool",
                scope="global",
                created_at="2026-03-01",
            ),
        )
        result = query_node_by_name_and_type(db, "pytest", "tool")
        assert result is not None
        assert result.name == "pytest"

    def test_query_by_name_and_type_not_found(self, db):
        result = query_node_by_name_and_type(db, "nonexistent", "tool")
        assert result is None

    def test_update_access(self, db):
        node = NodeRow(
            name="test-node",
            type="tool",
            scope="global",
            created_at="2026-03-01",
        )
        insert_node(db, node)
        results = query_nodes_by_scope(db, "global")
        node_id = results[0].id
        update_node_access(db, node_id)
        updated = query_nodes_by_scope(db, "global")
        assert updated[0].access_count == 1
        assert updated[0].last_accessed is not None


class TestEdgeCRUD:
    def test_insert_edge(self, db):
        insert_node(
            db,
            NodeRow(
                name="project-a", type="project", scope="global",
                created_at="2026-03-01",
            ),
        )
        insert_node(
            db,
            NodeRow(
                name="pytest", type="tool", scope="global",
                created_at="2026-03-01",
            ),
        )
        src = query_node_by_name_and_type(db, "project-a", "project")
        tgt = query_node_by_name_and_type(db, "pytest", "tool")
        edge = EdgeRow(
            source=src.id,
            target=tgt.id,
            type="uses",
            fact="project-a uses pytest for testing",
            created_at="2026-03-01",
        )
        insert_edge(db, edge)
        rows = db.execute("SELECT * FROM edges").fetchall()
        assert len(rows) == 1

    def test_edge_provenance(self, db):
        insert_node(
            db,
            NodeRow(
                name="n1", type="tool", scope="global",
                created_at="2026-03-01",
            ),
        )
        insert_node(
            db,
            NodeRow(
                name="n2", type="library", scope="global",
                created_at="2026-03-01",
            ),
        )
        src = query_node_by_name_and_type(db, "n1", "tool")
        tgt = query_node_by_name_and_type(db, "n2", "library")
        edge = EdgeRow(
            source=src.id,
            target=tgt.id,
            type="depends_on",
            created_at="2026-03-01",
            source_sessions=json.dumps(["2026-03-01"]),
        )
        insert_edge(db, edge)
        row = db.execute(
            "SELECT source_sessions FROM edges WHERE source=?", (src.id,)
        ).fetchone()
        assert "2026-03-01" in row[0]
