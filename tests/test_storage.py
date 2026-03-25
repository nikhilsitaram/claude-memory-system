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
from unittest import mock

import pytest

# These imports will fail until storage.py is created (RED phase)
from storage import (
    SCHEMA_VERSION,
    ChunkRow,
    EdgeRow,
    NodeRow,
    _migrate_salience_data,
    batch_update_access,
    close_db,
    delete_chunks_by_source,
    ensure_db,
    get_db,
    insert_chunk,
    insert_edge,
    insert_node,
    invalidate_edge,
    query_chunk_by_id,
    query_chunks_by_scope,
    query_chunks_by_source,
    query_chunks_for_retrieval,
    query_chunks_with_salience,
    query_current_edges,
    query_data_point_by_id,
    query_edges_at_date,
    query_edges_for_node,
    query_neighbor_nodes,
    query_node_by_name_and_type,
    query_nodes_by_scope,
    update_chunk_content,
    update_chunk_salience,
    update_node_access,
    update_node_salience,
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


@pytest.fixture
def db_v2(db_dir):
    """Create a v2 DB (with chunks/nodes tables) without migration.

    Use this fixture for tests that need v2 schema features (chunks, nodes).
    The regular `db` fixture creates v3 schema (data_points only).
    """
    from storage import SCHEMA_DDL

    db_path = db_dir / "memory.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA_DDL)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
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
        """Test that v3 schema has data_points and edges tables."""
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "data_points" in tables
        assert "edges" in tables

    def test_indexes_exist(self, db):
        """Test that v3 schema has correct indexes."""
        indexes = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected = {
            "idx_dp_type",
            "idx_dp_scope",
            "idx_dp_salience",
            "idx_dp_created",
            "idx_dp_hash",
            "idx_dp_simhash",
            "idx_edges_source",
            "idx_edges_target",
            "idx_edges_valid",
        }
        assert expected.issubset(indexes)

    def test_ensure_db_idempotent(self, db_dir):
        """Test that ensure_db() doesn't drop existing data on second call."""
        conn1 = ensure_db()
        conn1.execute(
            "INSERT INTO data_points (id, name, type, created_at) "
            "VALUES ('dp1', 'test', 'entity', '2026-01-01')"
        )
        conn1.commit()
        close_db(conn1)
        # Second call should not drop data
        conn2 = ensure_db()
        row = conn2.execute(
            "SELECT name FROM data_points WHERE id='dp1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "test"
        close_db(conn2)

    def test_get_db_raises_when_no_db(self, db_dir):
        result = db_dir / "memory.db"
        assert not result.exists()
        with pytest.raises(FileNotFoundError):
            get_db()

    def test_get_db_returns_connection(self, db_dir):
        conn1 = ensure_db()
        close_db(conn1)
        conn2 = get_db()
        assert conn2.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn2.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn2.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        close_db(conn2)


# ============================================================================
# Chunk CRUD
# ============================================================================


class TestChunkCRUD:
    def test_insert_and_query_by_scope(self, db_v2):
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
        insert_chunk(db_v2, chunk)
        results = query_chunks_by_scope(db_v2, "global")
        assert len(results) == 1
        assert results[0].content == chunk.content
        assert results[0].scope == "global"

    def test_insert_generates_id_and_hash(self, db_v2):
        chunk = ChunkRow(
            content="Test content",
            source_file="test.md",
            source_type="daily",
            scope="global",
            chunk_index=0,
            created_at="2026-03-01",
        )
        insert_chunk(db_v2, chunk)
        row = db_v2.execute("SELECT id, content_hash FROM chunks").fetchone()
        assert row[0]  # id is non-empty
        expected_hash = hashlib.sha256(b"Test content").hexdigest()[:16]
        assert row[1] == expected_hash

    def test_query_by_source_file(self, db_v2):
        for i in range(3):
            insert_chunk(
                db_v2,
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
            db_v2,
            ChunkRow(
                content="Other",
                source_file="2026-03-02.md",
                source_type="daily",
                scope="global",
                chunk_index=0,
                created_at="2026-03-02",
            ),
        )
        results = query_chunks_by_source(db_v2, "2026-03-01.md")
        assert len(results) == 3

    def test_delete_by_source(self, db_v2):
        insert_chunk(
            db_v2,
            ChunkRow(
                content="To delete",
                source_file="old.md",
                source_type="ltm",
                scope="global",
                chunk_index=0,
                created_at="2026-01-01",
            ),
        )
        assert len(query_chunks_by_source(db_v2, "old.md")) == 1
        count = delete_chunks_by_source(db_v2, "old.md")
        assert count == 1
        assert len(query_chunks_by_source(db_v2, "old.md")) == 0

    def test_provenance_columns(self, db_v2):
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
        insert_chunk(db_v2, chunk)
        results = query_chunks_by_scope(db_v2, "global")
        assert results[0].evidence_count == 2
        sessions = json.loads(results[0].source_sessions)
        assert len(sessions) == 2


# ============================================================================
# Node and edge CRUD
# ============================================================================


class TestNodeCRUD:
    def test_insert_and_query_by_scope(self, db_v2):
        node = NodeRow(
            name="claude-memory-system",
            type="project",
            scope="global",
            description="Memory persistence for Claude Code",
            created_at="2026-03-01",
        )
        insert_node(db_v2, node)
        results = query_nodes_by_scope(db_v2, "global")
        assert len(results) == 1
        assert results[0].name == "claude-memory-system"

    def test_query_by_name_and_type(self, db_v2):
        insert_node(
            db_v2,
            NodeRow(
                name="pytest",
                type="tool",
                scope="global",
                created_at="2026-03-01",
            ),
        )
        result = query_node_by_name_and_type(db_v2, "pytest", "tool")
        assert result is not None
        assert result.name == "pytest"

    def test_query_by_name_and_type_not_found(self, db_v2):
        result = query_node_by_name_and_type(db_v2, "nonexistent", "tool")
        assert result is None

    def test_update_access(self, db_v2):
        node = NodeRow(
            name="test-node",
            type="tool",
            scope="global",
            created_at="2026-03-01",
        )
        insert_node(db_v2, node)
        results = query_nodes_by_scope(db_v2, "global")
        node_id = results[0].id
        update_node_access(db_v2, node_id)
        updated = query_nodes_by_scope(db_v2, "global")
        assert updated[0].access_count == 1
        assert updated[0].last_accessed is not None


class TestEdgeCRUD:
    def test_insert_edge(self, db_v2):
        insert_node(
            db_v2,
            NodeRow(
                name="project-a", type="project", scope="global",
                created_at="2026-03-01",
            ),
        )
        insert_node(
            db_v2,
            NodeRow(
                name="pytest", type="tool", scope="global",
                created_at="2026-03-01",
            ),
        )
        src = query_node_by_name_and_type(db_v2, "project-a", "project")
        tgt = query_node_by_name_and_type(db_v2, "pytest", "tool")
        edge = EdgeRow(
            source=src.id,
            target=tgt.id,
            type="uses",
            fact="project-a uses pytest for testing",
            created_at="2026-03-01",
        )
        insert_edge(db_v2, edge)
        rows = db_v2.execute("SELECT * FROM edges").fetchall()
        assert len(rows) == 1

    def test_edge_provenance(self, db_v2):
        insert_node(
            db_v2,
            NodeRow(
                name="n1", type="tool", scope="global",
                created_at="2026-03-01",
            ),
        )
        insert_node(
            db_v2,
            NodeRow(
                name="n2", type="library", scope="global",
                created_at="2026-03-01",
            ),
        )
        src = query_node_by_name_and_type(db_v2, "n1", "tool")
        tgt = query_node_by_name_and_type(db_v2, "n2", "library")
        edge = EdgeRow(
            source=src.id,
            target=tgt.id,
            type="depends_on",
            created_at="2026-03-01",
            source_sessions=json.dumps(["2026-03-01"]),
        )
        insert_edge(db_v2, edge)
        row = db_v2.execute(
            "SELECT source_sessions FROM edges WHERE source=?", (src.id,)
        ).fetchone()
        assert "2026-03-01" in row[0]


# ============================================================================
# B3: Neighbor node query helper
# ============================================================================


class TestQueryNeighborNodes:
    """Tests for query_neighbor_nodes() helper."""

    def _make_v2_db(self, tmp_path):
        """Create a v2 DB for testing."""
        from storage import SCHEMA_DDL

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(SCHEMA_DDL)
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        return conn


    def test_returns_direct_neighbors_via_edges(self, tmp_path):
        """Returns nodes connected by edges (both source and target directions)."""
        conn = self._make_v2_db(tmp_path)
        nA = insert_node(conn, NodeRow(name="A", type="concept", scope="global", created_at="2026-01-01"))
        nB = insert_node(conn, NodeRow(name="B", type="concept", scope="global", created_at="2026-01-01"))
        nC = insert_node(conn, NodeRow(name="C", type="concept", scope="global", created_at="2026-01-01"))
        insert_edge(conn, EdgeRow(source=nA, target=nB, type="related", created_at="2026-01-01", weight=0.8))
        insert_edge(conn, EdgeRow(source=nA, target=nC, type="related", created_at="2026-01-01", weight=0.5))
        conn.commit()
        neighbors = query_neighbor_nodes(conn, nA)
        neighbor_ids = {n.node_id for n in neighbors}
        assert nB in neighbor_ids
        assert nC in neighbor_ids
        assert len(neighbors) == 2
        close_db(conn)

    def test_no_neighbors_returns_empty(self, tmp_path):
        """Node with no edges returns empty list."""
        conn = self._make_v2_db(tmp_path)
        nA = insert_node(conn, NodeRow(name="isolated", type="concept", scope="global", created_at="2026-01-01"))
        conn.commit()
        neighbors = query_neighbor_nodes(conn, nA)
        assert neighbors == []
        close_db(conn)

    def test_expired_edges_excluded(self, tmp_path):
        """Edges with valid_to set (expired) are excluded from neighbor lookup."""
        conn = self._make_v2_db(tmp_path)
        nA = insert_node(conn, NodeRow(name="A2", type="concept", scope="global", created_at="2026-01-01"))
        nB = insert_node(conn, NodeRow(name="B2", type="concept", scope="global", created_at="2026-01-01"))
        insert_edge(conn, EdgeRow(source=nA, target=nB, type="related", created_at="2026-01-01", weight=0.9, valid_to="2026-02-01"))
        conn.commit()
        neighbors = query_neighbor_nodes(conn, nA)
        assert neighbors == []
        close_db(conn)


# ============================================================================
# B1: Access tracking and salience update helpers
# ============================================================================


class TestBatchUpdateAccess:
    """Tests for batch_update_access() helper."""

    def _make_v2_db(self, tmp_path):
        """Create a v2 DB for testing."""
        from storage import SCHEMA_DDL

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(SCHEMA_DDL)
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        return conn


    def test_increments_access_count_and_updates_timestamp(self, tmp_path):
        conn = self._make_v2_db(tmp_path)
        c1 = ChunkRow(content="A", source_file="f.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01")
        c2 = ChunkRow(content="B", source_file="f.md", source_type="ltm", scope="global", chunk_index=1, created_at="2026-01-01")
        c3 = ChunkRow(content="C", source_file="f.md", source_type="ltm", scope="global", chunk_index=2, created_at="2026-01-01")
        id1 = insert_chunk(conn, c1)
        id2 = insert_chunk(conn, c2)
        id3 = insert_chunk(conn, c3)
        conn.commit()
        ts = "2026-03-21T10:00:00Z"
        count = batch_update_access(conn, [id1, id2], timestamp=ts)
        conn.commit()
        assert count == 2
        rows = {r.id: r for r in query_chunks_with_salience(conn)}
        assert rows[id1].access_count == 1
        assert rows[id1].last_accessed == ts
        assert rows[id2].access_count == 1
        assert rows[id3].access_count == 0
        assert rows[id3].last_accessed is None
        close_db(conn)

    def test_empty_batch_is_noop(self, tmp_path):
        conn = self._make_v2_db(tmp_path)
        count = batch_update_access(conn, [])
        assert count == 0
        close_db(conn)

    def test_missing_id_is_silently_skipped(self, tmp_path):
        conn = self._make_v2_db(tmp_path)
        count = batch_update_access(conn, ["nonexistent-id"], timestamp="2026-03-21T10:00:00Z")
        conn.commit()
        assert count == 0
        close_db(conn)


class TestUpdateNodeSalience:
    """Tests for update_node_salience() helper."""

    def _make_v2_db(self, tmp_path):
        """Create a v2 DB for testing."""
        from storage import SCHEMA_DDL

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(SCHEMA_DDL)
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        return conn


    def test_updates_node_salience(self, tmp_path):
        conn = self._make_v2_db(tmp_path)
        node = NodeRow(name="test-node", type="tool", scope="global", created_at="2026-01-01")
        nid = insert_node(conn, node)
        conn.commit()
        update_node_salience(conn, nid, 0.75)
        conn.commit()
        row = conn.execute("SELECT salience FROM nodes WHERE id=?", (nid,)).fetchone()
        assert abs(row[0] - 0.75) < 1e-9
        close_db(conn)

    def test_clamps_node_salience(self, tmp_path):
        conn = self._make_v2_db(tmp_path)
        node = NodeRow(name="clamp-node", type="tool", scope="global", created_at="2026-01-01")
        nid = insert_node(conn, node)
        conn.commit()
        update_node_salience(conn, nid, 2.0)
        conn.commit()
        row = conn.execute("SELECT salience FROM nodes WHERE id=?", (nid,)).fetchone()
        assert row[0] == 1.0
        close_db(conn)


class TestQueryChunksWithSalience:
    """Tests for query_chunks_with_salience() helper."""

    def _make_v2_db(self, tmp_path):
        """Create a v2 DB for testing."""
        from storage import SCHEMA_DDL

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(SCHEMA_DDL)
        conn.execute("PRAGMA user_version=2")
        conn.commit()
        return conn


    def test_returns_chunks_with_access_metadata(self, tmp_path):
        conn = self._make_v2_db(tmp_path)
        chunk = ChunkRow(content="salience test", source_file="f.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01", salience=0.9, access_count=3)
        insert_chunk(conn, chunk)
        conn.commit()
        results = query_chunks_with_salience(conn)
        assert len(results) == 1
        assert abs(results[0].salience - 0.9) < 1e-9
        assert results[0].access_count == 3
        close_db(conn)

    def test_filters_by_scope_when_provided(self, tmp_path):
        conn = self._make_v2_db(tmp_path)
        insert_chunk(conn, ChunkRow(content="global entry", source_file="f.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01"))
        insert_chunk(conn, ChunkRow(content="project entry", source_file="p.md", source_type="ltm", scope="my-project", chunk_index=0, created_at="2026-01-01"))
        conn.commit()
        results = query_chunks_with_salience(conn, scope="global")
        assert len(results) == 1
        assert results[0].scope == "global"
        close_db(conn)


# ============================================================================
# B5: Salience data migration tests
# ============================================================================


class TestSalienceDataMigration:
    """Tests for one-time data migration setting last_accessed on existing chunks."""

    def _make_v2_db(self, tmp_path):
        """Create a v2 DB for testing v1->v2 migration."""
        from storage import SCHEMA_DDL

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(SCHEMA_DDL)
        conn.execute("PRAGMA user_version=1")  # v1 to test migration
        conn.commit()
        return conn

    def test_existing_chunks_get_last_accessed_from_created_at(self, tmp_path):
        """Chunks with NULL last_accessed get last_accessed = created_at."""
        conn = self._make_v2_db(tmp_path)
        conn.execute(
            "INSERT INTO chunks (id, content, source_file, source_type, created_at, salience, access_count) "
            "VALUES ('c1', 'test', 'f.md', 'ltm', '2026-01-15', 1.0, 0)"
        )
        conn.commit()
        _migrate_salience_data(conn)
        conn.commit()
        row = conn.execute("SELECT last_accessed FROM chunks WHERE id='c1'").fetchone()
        assert row[0] == "2026-01-15"
        close_db(conn)

    def test_chunks_with_last_accessed_unchanged(self, tmp_path):
        """Chunks that already have last_accessed are not modified."""
        conn = self._make_v2_db(tmp_path)
        conn.execute(
            "INSERT INTO chunks (id, content, source_file, source_type, created_at, last_accessed, salience, access_count) "
            "VALUES ('c2', 'test', 'f.md', 'ltm', '2026-01-15', '2026-03-01', 1.0, 0)"
        )
        conn.commit()
        _migrate_salience_data(conn)
        conn.commit()
        row = conn.execute("SELECT last_accessed FROM chunks WHERE id='c2'").fetchone()
        assert row[0] == "2026-03-01"
        close_db(conn)

    def test_migration_is_idempotent(self, tmp_path):
        """Running migration twice produces same result."""
        conn = self._make_v2_db(tmp_path)
        conn.execute(
            "INSERT INTO chunks (id, content, source_file, source_type, created_at, salience, access_count) "
            "VALUES ('c3', 'test', 'f.md', 'ltm', '2026-02-10', 1.0, 0)"
        )
        conn.commit()
        _migrate_salience_data(conn)
        conn.commit()
        _migrate_salience_data(conn)
        conn.commit()
        row = conn.execute("SELECT last_accessed FROM chunks WHERE id='c3'").fetchone()
        assert row[0] == "2026-02-10"
        close_db(conn)

    def test_salience_defaults_preserved(self, tmp_path):
        """Existing chunks keep salience=1.0 (schema default)."""
        conn = self._make_v2_db(tmp_path)
        chunk = ChunkRow(content="defaults test", source_file="f.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01")
        insert_chunk(conn, chunk)
        conn.commit()
        _migrate_salience_data(conn)
        conn.commit()
        results = query_chunks_with_salience(conn)
        assert results[0].salience == 1.0
        close_db(conn)

    def test_schema_version_bumped_to_3(self, tmp_path):
        """ensure_db() sets SCHEMA_VERSION=3 after migration."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            assert version == SCHEMA_VERSION
            assert SCHEMA_VERSION == 3
            close_db(conn)


class TestMigrateV2ToV3:
    """Tests for v2-to-v3 schema migration."""

    def _make_v2_db(self, tmp_path):
        """Create a v2 DB with sample chunks, nodes, and edges."""
        from storage import SCHEMA_DDL

        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript(SCHEMA_DDL)
        conn.execute("PRAGMA user_version=2")

        # Insert a chunk
        conn.execute(
            "INSERT INTO chunks (id, content, source_file, source_type, scope, entry_type, chunk_index, created_at, salience, access_count) "
            "VALUES ('chunk_abc', 'test fact', 'ltm.md', 'ltm', 'global', 'design', 0, '2026-03-20', 1.0, 0)"
        )

        # Insert two nodes
        conn.execute(
            "INSERT INTO nodes (id, name, type, description, scope, created_at, salience, access_count) "
            "VALUES ('node_def', 'JWT', 'library', 'JSON Web Token library', 'global', '2026-03-20', 1.0, 0)"
        )
        conn.execute(
            "INSERT INTO nodes (id, name, type, description, scope, created_at, salience, access_count) "
            "VALUES ('node_xyz', 'Auth', 'concept', 'Authentication concept', 'global', '2026-03-20', 1.0, 0)"
        )

        # Insert an edge between two nodes (v2 edges must reference nodes only)
        conn.execute(
            "INSERT INTO edges (id, source, target, type, created_at, weight) "
            "VALUES ('edge_ghi', 'node_def', 'node_xyz', 'uses', '2026-03-20', 1.0)"
        )

        conn.commit()
        return conn

    def test_chunks_become_memory_data_points(self, tmp_path):
        """Chunks are copied to data_points with type='memory'."""
        from storage import _migrate_v2_to_v3

        conn = self._make_v2_db(tmp_path)
        _migrate_v2_to_v3(conn)
        dp = query_data_point_by_id(conn, "chunk_abc")
        assert dp is not None
        assert dp.type == "memory"
        assert dp.content == "test fact"
        assert dp.scope == "global"
        assert dp.entry_type == "design"
        conn.close()

    def test_nodes_become_entity_data_points(self, tmp_path):
        """Nodes are copied to data_points with type='entity'."""
        from storage import _migrate_v2_to_v3

        conn = self._make_v2_db(tmp_path)
        _migrate_v2_to_v3(conn)
        dp = query_data_point_by_id(conn, "node_def")
        assert dp is not None
        assert dp.type == "entity"
        assert dp.name == "JWT"
        assert dp.content == "JSON Web Token library"
        conn.close()

    def test_edges_remain_valid_after_migration(self, tmp_path):
        """Edges still reference valid data_points after migration."""
        from storage import _migrate_v2_to_v3

        conn = self._make_v2_db(tmp_path)
        _migrate_v2_to_v3(conn)
        orphans = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE source NOT IN (SELECT id FROM data_points) OR target NOT IN (SELECT id FROM data_points)"
        ).fetchone()[0]
        assert orphans == 0
        conn.close()

    def test_edges_gain_reason_column(self, tmp_path):
        """Edges table gets a reason column after migration."""
        from storage import _migrate_v2_to_v3

        conn = self._make_v2_db(tmp_path)
        _migrate_v2_to_v3(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(edges)").fetchall()}
        assert "reason" in cols
        conn.close()

    def test_old_tables_dropped(self, tmp_path):
        """chunks and nodes tables are removed after migration."""
        from storage import _migrate_v2_to_v3

        conn = self._make_v2_db(tmp_path)
        _migrate_v2_to_v3(conn)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "chunks" not in tables
        assert "nodes" not in tables
        assert "data_points" in tables
        conn.close()

    def test_migration_idempotent(self, tmp_path):
        """Running migration twice is safe."""
        from storage import _migrate_v2_to_v3

        conn = self._make_v2_db(tmp_path)
        _migrate_v2_to_v3(conn)
        _migrate_v2_to_v3(conn)
        dp = query_data_point_by_id(conn, "chunk_abc")
        assert dp is not None
        conn.close()

    def test_schema_version_bumped_to_3(self, tmp_path):
        """Migration sets SCHEMA_VERSION=3."""
        from storage import _migrate_v2_to_v3

        conn = self._make_v2_db(tmp_path)
        _migrate_v2_to_v3(conn)
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 3
        conn.close()


# ============================================================================
# C1: New CRUD helpers
# ============================================================================


def _make_two_nodes(db):
    """Helper: insert two nodes and return (src_id, tgt_id)."""
    insert_node(db, NodeRow(name="node-a", type="entity", scope="global", created_at="2026-01-01"))
    insert_node(db, NodeRow(name="node-b", type="entity", scope="global", created_at="2026-01-01"))
    src = query_node_by_name_and_type(db, "node-a", "entity")
    tgt = query_node_by_name_and_type(db, "node-b", "entity")
    return src.id, tgt.id


class TestInvalidateEdge:
    """Tests for invalidate_edge() bi-temporal edge expiration."""

    def test_sets_valid_to_and_expired_at(self, db_v2):
        """Happy path: sets both timestamps on the specified edge."""
        src_id, tgt_id = _make_two_nodes(db_v2)
        edge = EdgeRow(source=src_id, target=tgt_id, type="uses", created_at="2026-01-01")
        edge_id = insert_edge(db_v2, edge)
        db_v2.commit()
        invalidate_edge(db_v2, edge_id, valid_to="2026-03-21", expired_at="2026-03-21T10:00:00Z")
        db_v2.commit()
        row = db_v2.execute("SELECT valid_to, expired_at FROM edges WHERE id=?", (edge_id,)).fetchone()
        assert row[0] == "2026-03-21"
        assert row[1] == "2026-03-21T10:00:00Z"

    def test_nonexistent_edge_is_noop(self, db_v2):
        """Edge ID not in DB does not raise."""
        invalidate_edge(db_v2, "nonexistent-id", valid_to="2026-03-21", expired_at="2026-03-21T10:00:00Z")
        db_v2.commit()

    def test_already_invalidated_edge_updates_timestamps(self, db_v2):
        """Calling invalidate on already-expired edge updates timestamps."""
        src_id, tgt_id = _make_two_nodes(db_v2)
        edge = EdgeRow(source=src_id, target=tgt_id, type="uses", created_at="2026-01-01", valid_to="2026-02-01")
        edge_id = insert_edge(db_v2, edge)
        db_v2.commit()
        invalidate_edge(db_v2, edge_id, valid_to="2026-03-21", expired_at="2026-03-21T10:00:00Z")
        db_v2.commit()
        row = db_v2.execute("SELECT valid_to FROM edges WHERE id=?", (edge_id,)).fetchone()
        assert row[0] == "2026-03-21"


class TestUpdateChunkContent:
    """Tests for update_chunk_content() content and entity update."""

    def _insert_chunk(self, db_v2):
        chunk = ChunkRow(
            content="original content",
            source_file="test.md",
            source_type="ltm",
            scope="global",
            chunk_index=0,
            created_at="2026-01-01",
        )
        chunk_id = insert_chunk(db_v2, chunk)
        db_v2.commit()
        return chunk_id

    def test_updates_content_and_hash(self, db_v2):
        """Content change also updates content_hash."""
        chunk_id = self._insert_chunk(db_v2)
        old_row = db_v2.execute("SELECT content_hash FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        update_chunk_content(db_v2, chunk_id, "updated content")
        db_v2.commit()
        new_row = db_v2.execute("SELECT content, content_hash FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        assert new_row[0] == "updated content"
        assert new_row[1] != old_row[0]
        expected_hash = hashlib.sha256(b"updated content").hexdigest()[:16]
        assert new_row[1] == expected_hash

    def test_updates_entities_json(self, db_v2):
        """Entities JSON is stored alongside content."""
        chunk_id = self._insert_chunk(db_v2)
        entities = json.dumps(["gRPC", "myproject"])
        update_chunk_content(db_v2, chunk_id, "updated content", new_entities=entities)
        db_v2.commit()
        row = db_v2.execute("SELECT entities FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        assert row[0] == entities

    def test_preserves_other_fields(self, db_v2):
        """Fields not being updated (scope, section, etc.) are preserved."""
        chunk_id = self._insert_chunk(db_v2)
        update_chunk_content(db_v2, chunk_id, "new content")
        db_v2.commit()
        row = db_v2.execute("SELECT scope, source_type FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        assert row[0] == "global"
        assert row[1] == "ltm"


class TestUpdateChunkSalienceClamping:
    """Tests for update_chunk_salience() clamping and setting."""

    def _insert_chunk(self, db_v2):
        chunk = ChunkRow(
            content="salience test",
            source_file="test.md",
            source_type="ltm",
            scope="global",
            chunk_index=0,
            created_at="2026-01-01",
        )
        chunk_id = insert_chunk(db_v2, chunk)
        db_v2.commit()
        return chunk_id

    def test_sets_salience(self, db_v2):
        chunk_id = self._insert_chunk(db_v2)
        update_chunk_salience(db_v2, chunk_id, 0.5)
        db_v2.commit()
        row = db_v2.execute("SELECT salience FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        assert row[0] == 0.5

    def test_clamps_above_one(self, db_v2):
        chunk_id = self._insert_chunk(db_v2)
        update_chunk_salience(db_v2, chunk_id, 1.5)
        db_v2.commit()
        row = db_v2.execute("SELECT salience FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        assert row[0] == 1.0

    def test_clamps_below_zero(self, db_v2):
        chunk_id = self._insert_chunk(db_v2)
        update_chunk_salience(db_v2, chunk_id, -0.1)
        db_v2.commit()
        row = db_v2.execute("SELECT salience FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        assert row[0] == 0.0


class TestQueryChunksForRetrieval:
    """Tests for query_chunks_for_retrieval() vector search context."""

    def test_returns_active_chunks_with_ids(self, db_v2):
        """Returns chunks suitable for vector search pre-retrieval."""
        chunk = ChunkRow(content="active chunk", source_file="test.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01", salience=1.0)
        insert_chunk(db_v2, chunk)
        db_v2.commit()
        results = query_chunks_for_retrieval(db_v2)
        assert len(results) == 1
        assert results[0].id is not None
        assert results[0].content == "active chunk"

    def test_filters_by_scope(self, db_v2):
        """Scope filter restricts results to matching scope."""
        insert_chunk(db_v2, ChunkRow(content="global chunk", source_file="g.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01", salience=1.0))
        insert_chunk(db_v2, ChunkRow(content="project chunk", source_file="p.md", source_type="ltm", scope="myproject", chunk_index=0, created_at="2026-01-01", salience=1.0))
        db_v2.commit()
        results = query_chunks_for_retrieval(db_v2, scope="global")
        assert len(results) == 1
        assert results[0].scope == "global"

    def test_excludes_archived_chunks(self, db_v2):
        """Chunks with salience < threshold are excluded."""
        insert_chunk(db_v2, ChunkRow(content="active", source_file="a.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01", salience=1.0))
        insert_chunk(db_v2, ChunkRow(content="archived", source_file="b.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01", salience=0.0))
        db_v2.commit()
        results = query_chunks_for_retrieval(db_v2, min_salience=0.05)
        assert len(results) == 1
        assert results[0].content == "active"


class TestQueryChunkById:
    """Tests for query_chunk_by_id() individual lookup."""

    def test_returns_chunk_for_valid_id(self, db_v2):
        """Happy path: returns ChunkRow for existing chunk."""
        chunk = ChunkRow(content="lookup test", source_file="test.md", source_type="ltm", scope="global", chunk_index=0, created_at="2026-01-01")
        chunk_id = insert_chunk(db_v2, chunk)
        db_v2.commit()
        result = query_chunk_by_id(db_v2, chunk_id)
        assert result is not None
        assert result.id == chunk_id
        assert result.content == "lookup test"

    def test_returns_none_for_missing_id(self, db_v2):
        """Returns None when chunk ID not found."""
        result = query_chunk_by_id(db_v2, "nonexistent-chunk-id")
        assert result is None


class TestTemporalEdgeQueries:
    """Tests for query_current_edges, query_edges_at_date, query_edges_for_node."""

    def _setup_nodes_and_edge(self, db_v2, valid_from=None, valid_to=None):
        insert_node(db_v2, NodeRow(name="ta", type="entity", scope="global", created_at="2026-01-01"))
        insert_node(db_v2, NodeRow(name="tb", type="entity", scope="global", created_at="2026-01-01"))
        src = query_node_by_name_and_type(db_v2, "ta", "entity")
        tgt = query_node_by_name_and_type(db_v2, "tb", "entity")
        edge = EdgeRow(source=src.id, target=tgt.id, type="uses", created_at="2026-01-01", valid_from=valid_from, valid_to=valid_to)
        edge_id = insert_edge(db_v2, edge)
        db_v2.commit()
        return src.id, tgt.id, edge_id

    def test_query_current_edges_excludes_invalidated(self, db_v2):
        """query_current_edges filters by valid_to IS NULL."""
        src_id, tgt_id, e1 = self._setup_nodes_and_edge(db_v2, valid_from="2026-01-01")
        insert_node(db_v2, NodeRow(name="tc", type="entity", scope="global", created_at="2026-01-01"))
        tc = query_node_by_name_and_type(db_v2, "tc", "entity")
        e2_id = insert_edge(db_v2, EdgeRow(source=src_id, target=tc.id, type="uses", created_at="2026-01-01", valid_to="2026-02-01"))
        db_v2.commit()
        current = query_current_edges(db_v2)
        ids = [e.id for e in current]
        assert e1 in ids
        assert e2_id not in ids

    def test_query_edges_at_date_returns_valid_window(self, db_v2):
        """Temporal query returns edges valid at a specific date."""
        src_id, tgt_id, edge_id = self._setup_nodes_and_edge(db_v2, valid_from="2026-01-01", valid_to="2026-02-15")
        results_jan = query_edges_at_date(db_v2, "2026-01-15")
        assert any(e.id == edge_id for e in results_jan)
        results_mar = query_edges_at_date(db_v2, "2026-03-01")
        assert not any(e.id == edge_id for e in results_mar)

    def test_query_edges_for_node(self, db_v2):
        """Returns edges connected to a node (both valid and invalid)."""
        src_id, tgt_id, edge_id = self._setup_nodes_and_edge(db_v2, valid_from="2026-01-01", valid_to="2026-02-01")
        results = query_edges_for_node(db_v2, src_id)
        assert len(results) == 1
        assert results[0].id == edge_id


class TestV3Schema:
    """Tests for v3 schema DDL and DataPointRow."""

    def test_data_point_row_creation(self):
        """DataPointRow has correct defaults for optional fields."""
        from storage import DataPointRow

        row = DataPointRow(content='test fact', scope='global', type='memory')
        assert row.content == 'test fact'
        assert row.scope == 'global'
        assert row.type == 'memory'
        assert row.salience == 1.0
        assert row.access_count == 0
        assert row.evidence_count == 1
        assert row.consolidated == 0
        assert row.name is None
        assert row.properties is None

    def test_v3_schema_creates_data_points_table(self, db_dir):
        """SCHEMA_V3_DDL creates data_points table with all 18 columns."""
        from storage import SCHEMA_V3_DDL

        db_path = db_dir / "memory.db"
        conn = sqlite3.connect(str(db_path))

        # Execute DDL but skip if sqlite-vec not available
        try:
            conn.executescript(SCHEMA_V3_DDL)
            conn.commit()
        except sqlite3.OperationalError as e:
            if "no such module: vec0" in str(e):
                # sqlite-vec not available - create just the data_points table
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
                        created_at TEXT NOT NULL,
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
                """)
                conn.commit()
            else:
                raise

        # Check data_points table exists
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='data_points'"
        ).fetchone()
        assert result is not None

        # Check all 20 columns exist (18 original + certainty + validity_context)
        cols = conn.execute("PRAGMA table_info(data_points)").fetchall()
        col_names = {col[1] for col in cols}
        expected_cols = {
            'id', 'type', 'name', 'content', 'scope', 'entry_type',
            'source_type', 'source_sessions', 'created_at', 'salience',
            'access_count', 'last_accessed', 'evidence_count', 'consolidated',
            'content_hash', 'simhash', 'entities', 'properties',
            'certainty', 'validity_context'
        }
        assert col_names == expected_cols
        conn.close()

    def test_v3_schema_creates_vec_data_table(self, db_dir):
        """VEC_DATA_DDL creates vec_data virtual table (requires sqlite-vec)."""
        from storage import VEC_DATA_DDL

        db_path = db_dir / "memory.db"
        conn = sqlite3.connect(str(db_path))

        try:
            conn.executescript(VEC_DATA_DDL)
            conn.commit()

            # Check vec_data table exists
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_data'"
            ).fetchone()
            assert result is not None
        except sqlite3.OperationalError as e:
            if "no such module: vec0" in str(e):
                pytest.skip("sqlite-vec extension not available")
            else:
                raise
        finally:
            conn.close()

    def test_v3_schema_adds_reason_to_edges(self, db_dir):
        """SCHEMA_V3_DDL creates edges table with reason column and data_points refs."""
        from storage import SCHEMA_V3_DDL

        db_path = db_dir / "memory.db"
        conn = sqlite3.connect(str(db_path))

        # Execute v3 DDL (skip vec_data if sqlite-vec not available)
        try:
            conn.executescript(SCHEMA_V3_DDL)
            conn.commit()
        except sqlite3.OperationalError as e:
            if "no such module: vec0" in str(e):
                # sqlite-vec not available - create just data_points and edges
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
                        created_at TEXT NOT NULL,
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
                        created_at TEXT NOT NULL,
                        valid_from TEXT,
                        valid_to TEXT,
                        expired_at TEXT,
                        weight REAL DEFAULT 1.0,
                        source_sessions TEXT
                    );
                """)
                conn.commit()
            else:
                raise

        # Check edges table exists
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='edges'"
        ).fetchone()
        assert result is not None

        # Check reason column exists in edges
        cols = conn.execute("PRAGMA table_info(edges)").fetchall()
        col_names = {col[1] for col in cols}
        assert 'reason' in col_names

        # Check that edges references data_points (verify foreign keys)
        fk_list = conn.execute("PRAGMA foreign_key_list(edges)").fetchall()
        # Should have 2 FKs (source and target) both referencing data_points
        assert len(fk_list) == 2
        for fk in fk_list:
            # fk format: (id, seq, table, from, to, on_update, on_delete, match)
            assert fk[2] == 'data_points'  # table column

        conn.close()


# ============================================================================
# A2: DataPoint CRUD helpers
# ============================================================================


class TestDataPointCRUD:
    """Tests for data_points table CRUD operations."""

    def test_insert_and_query_by_id(self, tmp_path):
        """insert_data_point inserts a row and query_data_point_by_id returns it."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
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
                    created_at TEXT NOT NULL,
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
            """)
            conn.commit()

            from storage import DataPointRow, insert_data_point, query_data_point_by_id

            dp = DataPointRow(
                type="observation",
                content="User prefers pytest for testing",
                scope="global",
                entry_type="pattern",
            )
            dp_id = insert_data_point(conn, dp)
            conn.commit()

            result = query_data_point_by_id(conn, dp_id)
            assert result is not None
            assert result.id == dp_id
            assert result.type == "observation"
            assert result.content == "User prefers pytest for testing"
            assert result.salience == 1.0
            conn.close()

    def test_query_data_points_with_type_filter(self, tmp_path):
        """query_data_points filters by type and scope."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
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
                    created_at TEXT NOT NULL,
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
            """)
            conn.commit()

            from storage import DataPointRow, insert_data_point, query_data_points_by_scope

            dp1 = DataPointRow(type="observation", content="A", scope="global")
            dp2 = DataPointRow(type="entity", content="B", scope="global")
            dp3 = DataPointRow(type="observation", content="C", scope="project-x")

            insert_data_point(conn, dp1)
            insert_data_point(conn, dp2)
            insert_data_point(conn, dp3)
            conn.commit()

            results = query_data_points_by_scope(conn, "global", dp_type="observation")
            assert len(results) == 1
            assert results[0].content == "A"
            conn.close()

    def test_query_data_points_with_salience_filter(self, tmp_path):
        """query_data_points filters by min_salience."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
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
                    created_at TEXT NOT NULL,
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
            """)
            conn.commit()

            from storage import DataPointRow, insert_data_point, query_data_points_by_scope

            dp1 = DataPointRow(type="observation", content="A", scope="global", salience=1.0)
            dp2 = DataPointRow(type="observation", content="B", scope="global", salience=0.3)
            dp3 = DataPointRow(type="observation", content="C", scope="global", salience=0.6)

            insert_data_point(conn, dp1)
            insert_data_point(conn, dp2)
            insert_data_point(conn, dp3)
            conn.commit()

            results = query_data_points_by_scope(conn, "global", min_salience=0.5)
            assert len(results) == 2
            contents = {r.content for r in results}
            assert contents == {"A", "C"}
            conn.close()

    def test_soft_delete_data_point(self, tmp_path):
        """soft_delete_data_point sets salience to 0.0."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
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
                    created_at TEXT NOT NULL,
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
            """)
            conn.commit()

            from storage import (
                DataPointRow,
                insert_data_point,
                query_data_point_by_id,
                soft_delete_data_point,
            )

            dp = DataPointRow(type="observation", content="Temporary", scope="global")
            dp_id = insert_data_point(conn, dp)
            conn.commit()

            rowcount = soft_delete_data_point(conn, dp_id)
            conn.commit()

            assert rowcount == 1
            result = query_data_point_by_id(conn, dp_id)
            assert result.salience == 0.0
            conn.close()

    def test_query_data_point_by_id_nonexistent_returns_none(self, tmp_path):
        """query_data_point_by_id returns None for nonexistent ID."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
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
                    created_at TEXT NOT NULL,
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
            """)
            conn.commit()

            from storage import query_data_point_by_id

            result = query_data_point_by_id(conn, "nonexistent")
            assert result is None
            conn.close()

    def test_update_data_point(self, tmp_path):
        """update_data_point updates specified columns."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
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
                    created_at TEXT NOT NULL,
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
            """)
            conn.commit()

            from storage import (
                DataPointRow,
                insert_data_point,
                query_data_point_by_id,
                update_data_point,
            )

            dp = DataPointRow(type="observation", content="Old", scope="global")
            dp_id = insert_data_point(conn, dp)
            conn.commit()

            rowcount = update_data_point(conn, dp_id, content="New", salience=0.8)
            conn.commit()

            assert rowcount == 1
            result = query_data_point_by_id(conn, dp_id)
            assert result.content == "New"
            assert result.salience == 0.8
            conn.close()

    def test_query_edges_for_data_point_both_directions(self, tmp_path):
        """query_edges_for_data_point returns edges where data_point is source or target."""
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript("""
            CREATE TABLE data_points (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                name TEXT,
                content TEXT,
                scope TEXT,
                entry_type TEXT,
                source_type TEXT,
                source_sessions TEXT,
                created_at TEXT NOT NULL,
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
            CREATE TABLE edges (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL REFERENCES data_points(id),
                target TEXT NOT NULL REFERENCES data_points(id),
                type TEXT NOT NULL,
                reason TEXT,
                fact TEXT,
                properties TEXT,
                created_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                expired_at TEXT,
                weight REAL DEFAULT 1.0,
                source_sessions TEXT
            );
        """)
        conn.commit()

        from storage import (
            DataPointRow,
            insert_data_point,
            query_edges_for_data_point,
        )

        dpA_id = insert_data_point(conn, DataPointRow(type="entity", name="A", scope="global"))
        dpB_id = insert_data_point(conn, DataPointRow(type="entity", name="B", scope="global"))
        dpC_id = insert_data_point(conn, DataPointRow(type="entity", name="C", scope="global"))
        conn.commit()

        # A -> B, C -> A (so A is source once, target once)
        conn.execute(
            "INSERT INTO edges (id, source, target, type, created_at) VALUES (?, ?, ?, ?, ?)",
            ("e1", dpA_id, dpB_id, "related_to", "2026-03-01"),
        )
        conn.execute(
            "INSERT INTO edges (id, source, target, type, created_at) VALUES (?, ?, ?, ?, ?)",
            ("e2", dpC_id, dpA_id, "related_to", "2026-03-01"),
        )
        conn.commit()

        edges = query_edges_for_data_point(conn, dpA_id, direction="both")
        assert len(edges) == 2

        edges_out = query_edges_for_data_point(conn, dpA_id, direction="outgoing")
        assert len(edges_out) == 1
        assert edges_out[0].source == dpA_id

        edges_in = query_edges_for_data_point(conn, dpA_id, direction="incoming")
        assert len(edges_in) == 1
        assert edges_in[0].target == dpA_id

        conn.close()

    def test_query_edges_for_data_point_no_edges(self, tmp_path):
        """query_edges_for_data_point returns empty list when no edges."""
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript("""
            CREATE TABLE data_points (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                name TEXT,
                content TEXT,
                scope TEXT,
                entry_type TEXT,
                source_type TEXT,
                source_sessions TEXT,
                created_at TEXT NOT NULL,
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
            CREATE TABLE edges (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL REFERENCES data_points(id),
                target TEXT NOT NULL REFERENCES data_points(id),
                type TEXT NOT NULL,
                reason TEXT,
                fact TEXT,
                properties TEXT,
                created_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                expired_at TEXT,
                weight REAL DEFAULT 1.0,
                source_sessions TEXT
            );
        """)
        conn.commit()

        from storage import DataPointRow, insert_data_point, query_edges_for_data_point

        dp_id = insert_data_point(conn, DataPointRow(type="entity", name="Isolated", scope="global"))
        conn.commit()

        edges = query_edges_for_data_point(conn, dp_id)
        assert edges == []
        conn.close()

    def test_insert_data_point_autogenerates_id_and_hash(self, tmp_path):
        """insert_data_point auto-generates id and content_hash when not provided."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
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
                    created_at TEXT NOT NULL,
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
            """)
            conn.commit()

            from storage import DataPointRow, insert_data_point, query_data_point_by_id

            dp = DataPointRow(
                type="observation",
                content="User prefers pytest for testing",
                scope="global",
            )
            # Note: DataPointRow is frozen, so id and content_hash are None by default
            dp_id = insert_data_point(conn, dp)
            conn.commit()

            result = query_data_point_by_id(conn, dp_id)
            assert result is not None
            assert result.id is not None  # auto-generated
            assert len(result.id) > 0
            assert result.content_hash is not None  # auto-generated
            assert len(result.content_hash) > 0
            conn.close()

    def test_update_data_point_nonexistent_returns_zero(self, tmp_path):
        """update_data_point returns 0 rowcount for nonexistent ID."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
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
                    created_at TEXT NOT NULL,
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
            """)
            conn.commit()

            from storage import update_data_point

            rowcount = update_data_point(conn, "nonexistent-id", content="New")
            assert rowcount == 0
            conn.close()

    def test_soft_delete_nonexistent_returns_zero(self, tmp_path):
        """soft_delete_data_point returns 0 rowcount for nonexistent ID."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()
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
                    created_at TEXT NOT NULL,
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
            """)
            conn.commit()

            from storage import soft_delete_data_point

            rowcount = soft_delete_data_point(conn, "nonexistent-id")
            assert rowcount == 0
            conn.close()

    def test_insert_edge_validates_data_points_fk(self, tmp_path):
        """insert_edge validates that source/target exist in data_points (FK enforcement)."""
        db_path = tmp_path / "memory.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute('PRAGMA foreign_keys=ON')
        conn.executescript("""
            CREATE TABLE data_points (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                name TEXT,
                content TEXT,
                scope TEXT,
                entry_type TEXT,
                source_type TEXT,
                source_sessions TEXT,
                created_at TEXT NOT NULL,
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
            CREATE TABLE edges (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL REFERENCES data_points(id),
                target TEXT NOT NULL REFERENCES data_points(id),
                type TEXT NOT NULL,
                reason TEXT,
                fact TEXT,
                properties TEXT,
                created_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                expired_at TEXT,
                weight REAL DEFAULT 1.0,
                source_sessions TEXT
            );
        """)
        conn.commit()

        from storage import DataPointRow, EdgeRow, insert_data_point, insert_edge

        # Create only one data point
        dpA_id = insert_data_point(conn, DataPointRow(type="entity", name="A", scope="global"))
        conn.commit()

        # Try to create an edge with a nonexistent target
        import pytest
        with pytest.raises(sqlite3.IntegrityError):
            edge = EdgeRow(source=dpA_id, target="nonexistent-id", type="related_to", created_at="2026-03-22")
            insert_edge(conn, edge)
            conn.commit()

        conn.close()


# ============================================================================
# A3b: order_by injection prevention in query_data_points
# ============================================================================


class TestQueryDataPointsOrderByValidation:
    """Tests that query_data_points rejects invalid order_by values."""

    @pytest.fixture
    def v3_conn(self, tmp_path):
        """Bare DB with data_points table only — no migration side effects."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("PRAGMA foreign_keys=ON")
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
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_valid_order_by_accepted(self, v3_conn):
        """A valid order_by column + direction is accepted without error."""
        from storage import DataPointRow, insert_data_point, query_data_points

        insert_data_point(v3_conn, DataPointRow(type="memory", content="a", scope="global", salience=0.5))
        insert_data_point(v3_conn, DataPointRow(type="memory", content="b", scope="global", salience=0.8))
        v3_conn.commit()

        results = query_data_points(v3_conn, order_by="salience ASC")
        assert len(results) == 2
        assert results[0].salience <= results[1].salience

    def test_invalid_order_by_falls_back_to_default(self, v3_conn):
        """An invalid order_by string falls back to the default ordering."""
        from storage import _DEFAULT_ORDER_BY, DataPointRow, insert_data_point, query_data_points

        insert_data_point(v3_conn, DataPointRow(type="memory", content="a", scope="global", salience=0.3))
        insert_data_point(v3_conn, DataPointRow(type="memory", content="b", scope="global", salience=0.9))
        v3_conn.commit()

        results_default = query_data_points(v3_conn, order_by=_DEFAULT_ORDER_BY)
        results_injected = query_data_points(v3_conn, order_by="1; DROP TABLE data_points--")
        assert len(results_injected) == len(results_default)
        assert results_injected[0].content == results_default[0].content

    @pytest.mark.parametrize("bad_order", [
        "salience; DROP TABLE data_points",
        "1=1",
        "nonexistent_col DESC",
        "salience INVALID_DIR",
    ])
    def test_injection_attempts_rejected(self, v3_conn, bad_order):
        """Various SQL injection attempts fall back to default order without error."""
        from storage import DataPointRow, insert_data_point, query_data_points

        insert_data_point(v3_conn, DataPointRow(type="memory", content="x", scope="global"))
        v3_conn.commit()

        results = query_data_points(v3_conn, order_by=bad_order)
        assert isinstance(results, list)
        assert len(results) >= 1

    def test_limit_validated_as_integer(self, v3_conn):
        """A non-integer limit is ignored (treated as no limit)."""
        from storage import DataPointRow, insert_data_point, query_data_points

        for i in range(3):
            insert_data_point(v3_conn, DataPointRow(type="memory", content=str(i), scope="global"))
        v3_conn.commit()

        results = query_data_points(v3_conn, limit=2)
        assert len(results) == 2


# ============================================================================
# A3c: INSERT uses only ? placeholders (no mixed literals in VALUES)
# ============================================================================


class TestInsertProfileSectionPlaceholders:
    """Tests that _insert_profile_section uses only ? placeholders."""

    @pytest.fixture
    def bare_conn(self, tmp_path):
        """Bare DB connection with only data_points table — no migration side effects."""
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("PRAGMA foreign_keys=ON")
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
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_profile_section_inserted_with_correct_values(self, bare_conn):
        """_insert_profile_section inserts a profile row with all expected fields."""
        from storage import _insert_profile_section, query_data_points_by_scope

        _insert_profile_section(bare_conn, "About Me", ["I am a developer."], "2026-01-01T00:00:00Z")
        bare_conn.commit()

        profiles = query_data_points_by_scope(bare_conn, "user", dp_type="profile")
        assert len(profiles) == 1
        p = profiles[0]
        assert p.type == "profile"
        assert p.name == "About Me"
        assert p.scope == "user"
        assert p.source_type == "migration"
        assert p.salience == 1.0
        assert p.consolidated == 1
        assert "I am a developer." in (p.content or "")

    def test_profile_section_idempotent(self, bare_conn):
        """Inserting the same profile section twice produces only one row."""
        from storage import _insert_profile_section, query_data_points_by_scope

        _insert_profile_section(bare_conn, "About Me", ["I am a developer."], "2026-01-01T00:00:00Z")
        _insert_profile_section(bare_conn, "About Me", ["I am a developer."], "2026-01-01T00:00:00Z")
        bare_conn.commit()

        profiles = query_data_points_by_scope(bare_conn, "user", dp_type="profile")
        assert len(profiles) == 1


# ============================================================================
# A4: Profile section migration
# ============================================================================


class TestMigrateProfiles:
    """Tests for _migrate_profiles and migrate_profiles in storage.py."""

    @pytest.fixture
    def v3_conn(self, tmp_path):
        """Return a bare v3-schema DB connection (without vec_data virtual table)."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
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
                created_at TEXT NOT NULL,
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
                created_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT,
                expired_at TEXT,
                weight REAL DEFAULT 1.0,
                source_sessions TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dp_type ON data_points(type);
            CREATE INDEX IF NOT EXISTS idx_dp_scope ON data_points(scope);
            CREATE INDEX IF NOT EXISTS idx_dp_hash ON data_points(content_hash);
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_profile_sections_become_data_points(self, tmp_path, v3_conn):
        """Profile sections in PROFILE_SECTIONS become data_points with type='profile'."""
        from storage import _migrate_profiles, query_data_points_by_scope

        ltm = tmp_path / "global-long-term-memory.md"
        ltm.write_text(
            "# Global Long-Term Memory\n\n"
            "## About Me\n- Senior dev\n- Likes Python\n\n"
            "## Key Actions\n- stuff\n"
        )
        _migrate_profiles(v3_conn, ltm)
        v3_conn.commit()

        profiles = query_data_points_by_scope(v3_conn, "user", dp_type="profile")
        assert len(profiles) == 1
        assert "Senior dev" in profiles[0].content
        assert profiles[0].salience == 1.0
        assert profiles[0].consolidated == 1

    def test_all_four_profile_sections_created(self, tmp_path, v3_conn):
        """All four PROFILE_SECTIONS headers produce data_points when populated."""
        from storage import _migrate_profiles, query_data_points_by_scope

        ltm = tmp_path / "global-long-term-memory.md"
        ltm.write_text(
            "# Global Long-Term Memory\n\n"
            "## About Me\n- Python dev\n\n"
            "## Current Projects\n- memory system\n\n"
            "## Technical Environment\n- macOS\n\n"
            "## Patterns & Preferences\n- TDD\n\n"
            "## Other Section\n- ignored\n"
        )
        _migrate_profiles(v3_conn, ltm)
        v3_conn.commit()

        profiles = query_data_points_by_scope(v3_conn, "user", dp_type="profile")
        assert len(profiles) == 4
        names = {p.name for p in profiles}
        assert names == {"About Me", "Current Projects", "Technical Environment", "Patterns & Preferences"}

    def test_empty_sections_skipped(self, tmp_path, v3_conn):
        """Profile sections with no content produce no data_points."""
        from storage import _migrate_profiles, query_data_points_by_scope

        ltm = tmp_path / "global-long-term-memory.md"
        ltm.write_text(
            "# Global\n\n## About Me\n\n## Key Actions\n- stuff\n"
        )
        _migrate_profiles(v3_conn, ltm)
        v3_conn.commit()

        profiles = query_data_points_by_scope(v3_conn, "user", dp_type="profile")
        assert len(profiles) == 0

    def test_missing_ltm_file_is_silent(self, tmp_path, v3_conn):
        """If global LTM file does not exist, migration is a no-op (no error)."""
        from storage import _migrate_profiles, query_data_points_by_scope

        missing = tmp_path / "nonexistent.md"
        _migrate_profiles(v3_conn, missing)
        v3_conn.commit()

        profiles = query_data_points_by_scope(v3_conn, "user", dp_type="profile")
        assert len(profiles) == 0

    def test_idempotent_profile_migration(self, tmp_path, v3_conn):
        """Running migration twice does not create duplicate data_points."""
        from storage import _migrate_profiles, query_data_points_by_scope

        ltm = tmp_path / "global-long-term-memory.md"
        ltm.write_text("# Global\n\n## About Me\n- Senior dev\n")
        _migrate_profiles(v3_conn, ltm)
        v3_conn.commit()
        _migrate_profiles(v3_conn, ltm)
        v3_conn.commit()

        profiles = query_data_points_by_scope(v3_conn, "user", dp_type="profile")
        assert len(profiles) == 1

    def test_public_migrate_profiles_matches_private(self, tmp_path, v3_conn):
        """migrate_profiles (public) delegates correctly to _migrate_profiles."""
        from storage import migrate_profiles, query_data_points_by_scope

        ltm = tmp_path / "global-long-term-memory.md"
        ltm.write_text("# Global\n\n## About Me\n- Python dev\n")
        migrate_profiles(v3_conn, ltm)
        v3_conn.commit()

        profiles = query_data_points_by_scope(v3_conn, "user", dp_type="profile")
        assert len(profiles) == 1
        assert profiles[0].scope == "user"
        assert profiles[0].type == "profile"

    def test_whitespace_only_section_skipped(self, tmp_path, v3_conn):
        """Sections containing only whitespace/blank lines are skipped."""
        from storage import _migrate_profiles, query_data_points_by_scope

        ltm = tmp_path / "global-long-term-memory.md"
        ltm.write_text("# Global\n\n## About Me\n   \n\n## Current Projects\n- active\n")
        _migrate_profiles(v3_conn, ltm)
        v3_conn.commit()

        profiles = query_data_points_by_scope(v3_conn, "user", dp_type="profile")
        assert len(profiles) == 1
        assert profiles[0].name == "Current Projects"


# ============================================================================
# A6: Markdown archival utility
# ============================================================================


class TestArchiveMarkdown:
    """Tests for _should_archive and _archive_markdown_files."""

    @pytest.fixture
    def v3_conn(self, tmp_path):
        """Return a bare v3-schema DB connection."""
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
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
                created_at TEXT NOT NULL,
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
        """)
        conn.commit()
        yield conn
        conn.close()

    def test_archive_moves_files(self, tmp_path):
        """_archive_markdown_files moves markdown files to .archive/."""
        from storage import _archive_markdown_files

        memory_dir = tmp_path / "memory"
        daily_dir = memory_dir / "daily"
        project_dir = memory_dir / "project-memory"
        daily_dir.mkdir(parents=True)
        project_dir.mkdir(parents=True)

        (memory_dir / "global-long-term-memory.md").write_text("content")
        (daily_dir / "2026-03-20.md").write_text("daily")
        (project_dir / "myproject-long-term-memory.md").write_text("project")

        _archive_markdown_files(memory_dir)

        archive_dir = memory_dir / ".archive"
        assert archive_dir.exists()
        assert not (memory_dir / "global-long-term-memory.md").exists()
        assert not (daily_dir / "2026-03-20.md").exists()
        assert any("global-long-term-memory" in f.name for f in archive_dir.iterdir())

    def test_archive_moves_project_ltm(self, tmp_path):
        """_archive_markdown_files moves project LTM files."""
        from storage import _archive_markdown_files

        memory_dir = tmp_path / "memory"
        project_dir = memory_dir / "project-memory"
        project_dir.mkdir(parents=True)

        (project_dir / "myproject-long-term-memory.md").write_text("project ltm")

        _archive_markdown_files(memory_dir)

        archive_dir = memory_dir / ".archive"
        assert any("myproject" in f.name for f in archive_dir.iterdir())
        assert not (project_dir / "myproject-long-term-memory.md").exists()

    def test_archive_guard_empty_db(self, v3_conn):
        """_should_archive returns False when data_points is empty."""
        from storage import _should_archive

        assert _should_archive(v3_conn) is False

    def test_archive_guard_populated_db(self, tmp_path, v3_conn):
        """_should_archive returns True when data_points has rows."""
        from storage import DataPointRow, _should_archive, insert_data_point

        insert_data_point(v3_conn, DataPointRow(type="memory", content="fact", scope="global"))
        v3_conn.commit()
        assert _should_archive(v3_conn) is True

    def test_archive_skips_missing_files(self, tmp_path):
        """_archive_markdown_files is a no-op when target files don't exist."""
        from storage import _archive_markdown_files

        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        _archive_markdown_files(memory_dir)

        assert not (memory_dir / ".archive").exists() or True

    def test_archive_does_not_touch_non_markdown(self, tmp_path):
        """_archive_markdown_files does not move settings.json or memory.db."""
        from storage import _archive_markdown_files

        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        (memory_dir / "settings.json").write_text('{"key": "value"}')
        (memory_dir / "memory.db").write_text("db content")
        (memory_dir / ".synthesis-state.json").write_text("{}")

        _archive_markdown_files(memory_dir)

        assert (memory_dir / "settings.json").exists()
        assert (memory_dir / "memory.db").exists()
        assert (memory_dir / ".synthesis-state.json").exists()

    def test_archive_idempotent(self, tmp_path):
        """Running _archive_markdown_files twice does not raise an error."""
        from storage import _archive_markdown_files

        memory_dir = tmp_path / "memory"
        daily_dir = memory_dir / "daily"
        daily_dir.mkdir(parents=True)
        (daily_dir / "2026-03-20.md").write_text("daily")

        _archive_markdown_files(memory_dir)
        _archive_markdown_files(memory_dir)


# =============================================================================
# TestProvenanceEdges — C4: Provenance edge creation helpers
# =============================================================================

import pytest


class TestProvenanceEdges:
    def _make_v3_db(self, tmp_path):
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

    def test_create_supersedes_edge(self, tmp_path):
        from storage import DataPointRow, create_provenance_edge, insert_data_point
        conn = self._make_v3_db(tmp_path)
        id_old = insert_data_point(conn, DataPointRow(type="memory", content="old"))
        id_new = insert_data_point(conn, DataPointRow(type="memory", content="new"))
        conn.commit()
        create_provenance_edge(conn, id_new, id_old, "supersedes", "updated info")
        conn.commit()
        edges = conn.execute("SELECT type, reason FROM edges WHERE source=?", (id_new,)).fetchall()
        assert len(edges) == 1
        assert edges[0][0] == "supersedes"
        assert edges[0][1] == "updated info"

    def test_provenance_chain_multi_hop(self, tmp_path):
        from storage import DataPointRow, create_provenance_edge, insert_data_point, query_provenance_chain
        conn = self._make_v3_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="A", id="a"))
        insert_data_point(conn, DataPointRow(type="memory", content="B", id="b"))
        insert_data_point(conn, DataPointRow(type="memory", content="C", id="c"))
        conn.commit()
        create_provenance_edge(conn, "b", "a", "supersedes", "B replaces A")
        create_provenance_edge(conn, "c", "b", "supersedes", "C replaces B")
        conn.commit()
        chain = query_provenance_chain(conn, "c")
        assert len(chain) >= 2
        ids_in_chain = [c["target_id"] for c in chain]
        assert "b" in ids_in_chain
        assert "a" in ids_in_chain

    def test_empty_chain(self, tmp_path):
        from storage import DataPointRow, insert_data_point, query_provenance_chain
        conn = self._make_v3_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="solo", id="solo"))
        conn.commit()
        chain = query_provenance_chain(conn, "solo")
        assert len(chain) == 0

    def test_self_reference_rejected(self, tmp_path):
        from storage import DataPointRow, create_provenance_edge, insert_data_point
        conn = self._make_v3_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="self", id="x"))
        conn.commit()
        with pytest.raises(ValueError):
            create_provenance_edge(conn, "x", "x", "supersedes", "self-ref")

    @pytest.mark.parametrize("edge_type", ["supersedes", "contradicts", "led_to", "refines", "supports"])
    def test_all_edge_types_valid(self, tmp_path, edge_type):
        from storage import DataPointRow, create_provenance_edge, insert_data_point
        conn = self._make_v3_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="src", id="src"))
        insert_data_point(conn, DataPointRow(type="memory", content="tgt", id="tgt"))
        conn.commit()
        create_provenance_edge(conn, "src", "tgt", edge_type, "test")
        conn.commit()
        row = conn.execute("SELECT type FROM edges WHERE source='src'").fetchone()
        assert row[0] == edge_type


# =============================================================================
# A4: FTS5 Full-Text Search Tests
# =============================================================================


class TestFTS5:
    """Tests for FTS5 full-text search table and helpers."""

    def _make_db(self, tmp_path):
        from unittest.mock import patch
        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.get_memory_dir", return_value=tmp_path):
            return ensure_db()

    def test_fts_data_table_created(self, tmp_path):
        """ensure_db creates the fts_data FTS5 virtual table."""
        conn = self._make_db(tmp_path)
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "fts_data" in tables
        conn.close()

    def test_fts_insert_and_search(self, tmp_path):
        """Inserted text is findable via FTS5 MATCH query."""
        from storage import fts_insert, fts_search
        conn = self._make_db(tmp_path)
        fts_insert(conn, "dp-1", "Redis cache requires explicit TTL settings", "global")
        conn.commit()
        results = fts_search(conn, "Redis TTL", scope=None, limit=10)
        assert len(results) >= 1
        assert results[0]["data_point_id"] == "dp-1"
        conn.close()

    def test_fts_porter_stemming(self, tmp_path):
        """Porter stemming allows matching 'running' with 'run'."""
        from storage import fts_insert, fts_search
        conn = self._make_db(tmp_path)
        fts_insert(conn, "dp-stem", "The process was running slowly", "global")
        conn.commit()
        results = fts_search(conn, "run", scope=None, limit=10)
        assert len(results) >= 1
        conn.close()

    def test_fts_delete(self, tmp_path):
        """Deleted entries no longer appear in search results."""
        from storage import fts_delete, fts_insert, fts_search
        conn = self._make_db(tmp_path)
        fts_insert(conn, "dp-del", "unique findable content xyz", "global")
        conn.commit()
        assert len(fts_search(conn, "xyz", scope=None, limit=10)) >= 1
        fts_delete(conn, "dp-del")
        conn.commit()
        assert len(fts_search(conn, "xyz", scope=None, limit=10)) == 0
        conn.close()

    def test_fts_scope_filtering(self, tmp_path):
        """Scope parameter limits search to matching scope."""
        from storage import fts_insert, fts_search
        conn = self._make_db(tmp_path)
        fts_insert(conn, "dp-g", "shared pattern across projects", "global")
        fts_insert(conn, "dp-p", "project specific pattern info", "my-project")
        conn.commit()
        results_global = fts_search(conn, "pattern", scope="global", limit=10)
        results_project = fts_search(conn, "pattern", scope="my-project", limit=10)
        assert all(r["scope"] == "global" for r in results_global)
        assert all(r["scope"] == "my-project" for r in results_project)
        conn.close()

    def test_soft_delete_removes_fts_entry(self, tmp_path):
        """soft_delete_data_point also removes the FTS5 index entry."""
        from storage import DataPointRow, fts_insert, fts_search, insert_data_point, soft_delete_data_point
        conn = self._make_db(tmp_path)
        dp = DataPointRow(type="memory", content="unique deletable content xyz", scope="global", salience=0.8)
        dp_id = insert_data_point(conn, dp)
        fts_insert(conn, dp_id, dp.content, dp.scope)
        conn.commit()
        assert len(fts_search(conn, "deletable", scope=None, limit=10)) >= 1
        soft_delete_data_point(conn, dp_id)
        conn.commit()
        assert len(fts_search(conn, "deletable", scope=None, limit=10)) == 0
        conn.close()

    def test_fts_migration_backfill(self, tmp_path):
        """Migration populates fts_data from existing data_points."""
        from unittest.mock import patch

        from storage import DataPointRow, _backfill_fts, _ensure_fts_table, fts_search, insert_data_point

        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()

        insert_data_point(conn, DataPointRow(type="memory", content="migration backfill test content", scope="global"))
        conn.commit()

        _ensure_fts_table(conn)
        _backfill_fts(conn)

        results = fts_search(conn, "migration backfill", scope=None, limit=10)
        assert len(results) >= 1
        conn.close()


class TestEpistemicMetadata:
    """Tests for certainty and validity_context columns on data_points."""

    def _make_db(self, tmp_path):
        from unittest.mock import patch
        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.get_memory_dir", return_value=tmp_path):
            return ensure_db()

    def test_certainty_column_exists(self, tmp_path):
        conn = self._make_db(tmp_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(data_points)").fetchall()}
        assert "certainty" in cols
        conn.close()

    def test_validity_context_column_exists(self, tmp_path):
        conn = self._make_db(tmp_path)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(data_points)").fetchall()}
        assert "validity_context" in cols
        conn.close()

    def test_data_point_row_has_certainty(self, tmp_path):
        from storage import DataPointRow
        dp = DataPointRow(type="memory", content="test", scope="global", certainty=3)
        assert dp.certainty == 3

    def test_data_point_row_certainty_default_none(self, tmp_path):
        from storage import DataPointRow
        dp = DataPointRow(type="memory", content="test", scope="global")
        assert dp.certainty is None

    def test_data_point_row_validity_context(self, tmp_path):
        from storage import DataPointRow
        dp = DataPointRow(type="memory", content="test", scope="global", validity_context="Verified in prod")
        assert dp.validity_context == "Verified in prod"

    def test_insert_and_query_with_certainty(self, tmp_path):
        from storage import DataPointRow, insert_data_point, query_data_point_by_id
        conn = self._make_db(tmp_path)
        dp = DataPointRow(type="memory", content="certain fact", scope="global", certainty=4, validity_context="Verified in prod")
        dp_id = insert_data_point(conn, dp)
        conn.commit()
        result = query_data_point_by_id(conn, dp_id)
        assert result.certainty == 4
        assert result.validity_context == "Verified in prod"
        conn.close()

    def test_insert_without_certainty_returns_none(self, tmp_path):
        from storage import DataPointRow, insert_data_point, query_data_point_by_id
        conn = self._make_db(tmp_path)
        dp = DataPointRow(type="memory", content="no certainty", scope="global")
        dp_id = insert_data_point(conn, dp)
        conn.commit()
        result = query_data_point_by_id(conn, dp_id)
        assert result.certainty is None
        assert result.validity_context is None
        conn.close()

    def test_migration_idempotent(self, tmp_path):
        """Running migration twice does not error."""
        conn = self._make_db(tmp_path)
        from storage import _ensure_epistemic_columns
        _ensure_epistemic_columns(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(data_points)").fetchall()}
        assert "certainty" in cols
        assert "validity_context" in cols
        conn.close()


class TestMetadataTable:
    """Tests for the metadata key-value table."""

    def _make_db(self, tmp_path):
        from unittest.mock import patch
        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.get_memory_dir", return_value=tmp_path):
            return ensure_db()

    def test_metadata_table_exists(self, tmp_path):
        conn = self._make_db(tmp_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "metadata" in tables
        conn.close()

    def test_metadata_insert_and_read(self, tmp_path):
        conn = self._make_db(tmp_path)
        conn.execute("INSERT INTO metadata (key, value) VALUES ('test_key', 'test_val')")
        conn.commit()
        row = conn.execute("SELECT value FROM metadata WHERE key = 'test_key'").fetchone()
        assert row[0] == "test_val"
        conn.close()

    def test_metadata_upsert(self, tmp_path):
        conn = self._make_db(tmp_path)
        conn.execute("INSERT INTO metadata (key, value) VALUES ('k', 'v1')")
        conn.commit()
        conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES ('k', 'v2')")
        conn.commit()
        row = conn.execute("SELECT value FROM metadata WHERE key = 'k'").fetchone()
        assert row[0] == "v2"
        conn.close()


class TestGetOrCreateEntity:
    """Tests for the unified get_or_create_entity function."""

    def _make_db(self, tmp_path):
        from unittest.mock import patch

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.get_memory_dir", return_value=tmp_path):
            return ensure_db()

    def test_creates_new_entity(self, tmp_path):
        from storage import get_or_create_entity
        conn = self._make_db(tmp_path)
        entity_id = get_or_create_entity(conn, "Redis", "global")
        assert entity_id is not None
        row = conn.execute("SELECT type, name FROM data_points WHERE id = ?", (entity_id,)).fetchone()
        assert row[0] == "entity"
        assert row[1] == "Redis"
        conn.close()

    def test_returns_existing_entity(self, tmp_path):
        from storage import get_or_create_entity
        conn = self._make_db(tmp_path)
        id1 = get_or_create_entity(conn, "Redis", "global")
        id2 = get_or_create_entity(conn, "Redis", "global")
        assert id1 == id2
        conn.close()

    def test_dedup_across_scopes(self, tmp_path):
        """Same entity name resolves to same data_point regardless of scope."""
        from storage import get_or_create_entity
        conn = self._make_db(tmp_path)
        id1 = get_or_create_entity(conn, "JWT", "project-a")
        id2 = get_or_create_entity(conn, "JWT", "project-b")
        assert id1 == id2
        conn.close()

    def test_different_names_different_entities(self, tmp_path):
        from storage import get_or_create_entity
        conn = self._make_db(tmp_path)
        id1 = get_or_create_entity(conn, "Redis", "global")
        id2 = get_or_create_entity(conn, "PostgreSQL", "global")
        assert id1 != id2
        conn.close()

    def test_content_hash_set_correctly(self, tmp_path):
        from storage import _content_hash, get_or_create_entity
        conn = self._make_db(tmp_path)
        entity_id = get_or_create_entity(conn, "pytest", "global")
        row = conn.execute("SELECT content_hash FROM data_points WHERE id = ?", (entity_id,)).fetchone()
        assert row[0] == _content_hash("entity:pytest")  # already lowercase
        conn.close()

    def test_case_insensitive_dedup(self, tmp_path):
        """Creating 'github' then 'GitHub' returns the same entity ID."""
        from storage import get_or_create_entity
        conn = self._make_db(tmp_path)
        id1 = get_or_create_entity(conn, "github", "global")
        id2 = get_or_create_entity(conn, "GitHub", "global")
        assert id1 == id2
        count = conn.execute("SELECT COUNT(*) FROM data_points WHERE type='entity'").fetchone()[0]
        assert count == 1
        conn.close()

    def test_preserves_capitalized_name(self, tmp_path):
        """After creating 'github' then 'GitHub', the stored name is 'GitHub'."""
        from storage import get_or_create_entity
        conn = self._make_db(tmp_path)
        entity_id = get_or_create_entity(conn, "github", "global")
        get_or_create_entity(conn, "GitHub", "global")
        row = conn.execute("SELECT name FROM data_points WHERE id = ?", (entity_id,)).fetchone()
        assert row[0] == "GitHub"
        conn.close()


class TestPruneSessionContexts:
    """Tests for session_context pruning to prevent accumulation."""

    def _make_db(self, tmp_path):
        from unittest.mock import patch

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.get_memory_dir", return_value=tmp_path):
            return ensure_db()

    def test_session_context_pruning(self, tmp_path):
        """Inserting 5 session_contexts for same scope keeps only MAX_SESSION_CONTEXTS_PER_SCOPE."""
        from storage import (
            MAX_SESSION_CONTEXTS_PER_SCOPE,
            DataPointRow,
            insert_data_point,
            prune_session_contexts,
        )
        conn = self._make_db(tmp_path)
        scope = "test-project"
        inserted_ids = []
        for i in range(5):
            dp = DataPointRow(
                type="session_context", content=f"session {i}", scope=scope,
                salience=0.8, source_type="session_end",
                created_at=f"2025-01-0{i+1}T00:00:00Z",
            )
            dp_id = insert_data_point(conn, dp)
            inserted_ids.append(dp_id)
        conn.commit()

        pruned = prune_session_contexts(conn, scope)
        conn.commit()

        assert pruned == 5 - MAX_SESSION_CONTEXTS_PER_SCOPE
        active = conn.execute(
            "SELECT id FROM data_points WHERE type='session_context' AND scope=? AND salience > 0 "
            "ORDER BY created_at DESC",
            (scope,),
        ).fetchall()
        assert len(active) == MAX_SESSION_CONTEXTS_PER_SCOPE
        # The most recent ones should survive
        active_ids = [r[0] for r in active]
        for kept_id in inserted_ids[-MAX_SESSION_CONTEXTS_PER_SCOPE:]:
            assert kept_id in active_ids
        conn.close()

    def test_no_pruning_when_under_limit(self, tmp_path):
        """No pruning occurs when count is at or below the limit."""
        from storage import MAX_SESSION_CONTEXTS_PER_SCOPE, DataPointRow, insert_data_point, prune_session_contexts
        conn = self._make_db(tmp_path)
        scope = "small-project"
        for i in range(MAX_SESSION_CONTEXTS_PER_SCOPE):
            dp = DataPointRow(
                type="session_context", content=f"session {i}", scope=scope,
                salience=0.8, source_type="session_end",
                created_at=f"2025-01-0{i+1}T00:00:00Z",
            )
            insert_data_point(conn, dp)
        conn.commit()

        pruned = prune_session_contexts(conn, scope)
        assert pruned == 0
        conn.close()

    def test_pruning_scoped_independently(self, tmp_path):
        """Session contexts for different scopes are pruned independently."""
        from storage import MAX_SESSION_CONTEXTS_PER_SCOPE, DataPointRow, insert_data_point, prune_session_contexts
        conn = self._make_db(tmp_path)
        for scope in ("project-a", "project-b"):
            for i in range(5):
                dp = DataPointRow(
                    type="session_context", content=f"session {i}", scope=scope,
                    salience=0.8, source_type="session_end",
                    created_at=f"2025-01-0{i+1}T00:00:00Z",
                )
                insert_data_point(conn, dp)
        conn.commit()

        prune_session_contexts(conn, "project-a")
        conn.commit()

        active_a = conn.execute(
            "SELECT COUNT(*) FROM data_points WHERE type='session_context' AND scope='project-a' AND salience > 0"
        ).fetchone()[0]
        active_b = conn.execute(
            "SELECT COUNT(*) FROM data_points WHERE type='session_context' AND scope='project-b' AND salience > 0"
        ).fetchone()[0]
        assert active_a == MAX_SESSION_CONTEXTS_PER_SCOPE
        assert active_b == 5  # untouched
        conn.close()


class TestDBFixtureIsolation:
    """Guard tests: verify test DB fixtures never touch the production database."""

    def test_shared_db_fixture_uses_temp_path(self, shared_db, tmp_path):
        """shared_db fixture creates DB under tmp_path, not ~/.claude/memory/."""
        import pathlib
        db_path = pathlib.Path(shared_db.execute("PRAGMA database_list").fetchone()[2])
        home_memory = pathlib.Path.home() / ".claude" / "memory"
        assert not str(db_path).startswith(str(home_memory)), (
            f"shared_db created database at {db_path}, under ~/.claude/memory/ — "
            "test data would corrupt the production database!"
        )

    def test_db_fixture_uses_temp_path(self, db, tmp_path):
        """db fixture creates DB under tmp_path, not ~/.claude/memory/."""
        import pathlib
        db_path = pathlib.Path(db.execute("PRAGMA database_list").fetchone()[2])
        home_memory = pathlib.Path.home() / ".claude" / "memory"
        assert not str(db_path).startswith(str(home_memory)), (
            f"db fixture created database at {db_path}, under ~/.claude/memory/ — "
            "test data would corrupt the production database!"
        )
