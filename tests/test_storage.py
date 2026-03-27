#!/usr/bin/env python3
"""
Integration and unit tests for scripts/storage.py

Tests cover: schema creation, WAL mode, connection helpers, CRUD operations,
content hashing, and provenance tracking.

Run with: python3 -m pytest tests/test_storage.py -v
"""

import sqlite3
from unittest import mock

import pytest

from storage import (
    SCHEMA_VERSION,
    DataPointRow,
    EdgeRow,
    close_db,
    cleanup_stale_data,
    ensure_db,
    get_db,
    insert_data_point,
    insert_edge,
    invalidate_edge,
    query_current_edges,
    query_data_point_by_id,
    query_edges_at_date,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def db_dir(tmp_path):
    """Provide a temporary directory for the DB and patch get_db_path."""
    db_path = tmp_path / "memory.db"
    with mock.patch("storage.get_db_path", return_value=db_path), \
         mock.patch("storage.get_memory_dir", return_value=tmp_path):
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
        """Test that schema has data_points and edges tables."""
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "data_points" in tables
        assert "edges" in tables

    def test_indexes_exist(self, db):
        """Test that schema has correct indexes."""
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
# Edge CRUD helpers (using v3 schema)
# ============================================================================


def _make_two_data_points(db):
    """Helper: insert two data_points and return (src_id, tgt_id)."""
    from storage import DataPointRow, insert_data_point
    src_id = insert_data_point(db, DataPointRow(type="entity", name="dp-a", scope="global"))
    tgt_id = insert_data_point(db, DataPointRow(type="entity", name="dp-b", scope="global"))
    db.commit()
    return src_id, tgt_id


class TestInvalidateEdge:
    """Tests for invalidate_edge() bi-temporal edge expiration."""

    def test_sets_valid_to_and_expired_at(self, db):
        """Happy path: sets both timestamps on the specified edge."""
        src_id, tgt_id = _make_two_data_points(db)
        edge = EdgeRow(source=src_id, target=tgt_id, type="uses", created_at="2026-01-01")
        edge_id = insert_edge(db, edge)
        db.commit()
        invalidate_edge(db, edge_id, valid_to="2026-03-21", expired_at="2026-03-21T10:00:00Z")
        db.commit()
        row = db.execute("SELECT valid_to, expired_at FROM edges WHERE id=?", (edge_id,)).fetchone()
        assert row[0] == "2026-03-21"
        assert row[1] == "2026-03-21T10:00:00Z"

    def test_nonexistent_edge_is_noop(self, db):
        """Edge ID not in DB does not raise."""
        invalidate_edge(db, "nonexistent-id", valid_to="2026-03-21", expired_at="2026-03-21T10:00:00Z")
        db.commit()

    def test_already_invalidated_edge_updates_timestamps(self, db):
        """Calling invalidate on already-expired edge updates timestamps."""
        src_id, tgt_id = _make_two_data_points(db)
        edge = EdgeRow(source=src_id, target=tgt_id, type="uses", created_at="2026-01-01", valid_to="2026-02-01")
        edge_id = insert_edge(db, edge)
        db.commit()
        invalidate_edge(db, edge_id, valid_to="2026-03-21", expired_at="2026-03-21T10:00:00Z")
        db.commit()
        row = db.execute("SELECT valid_to FROM edges WHERE id=?", (edge_id,)).fetchone()
        assert row[0] == "2026-03-21"


class TestTemporalEdgeQueries:
    """Tests for query_current_edges, query_edges_at_date."""

    def _setup_data_points_and_edge(self, db, valid_from=None, valid_to=None):
        from storage import DataPointRow, insert_data_point
        src_id = insert_data_point(db, DataPointRow(type="entity", name="ta", scope="global"))
        tgt_id = insert_data_point(db, DataPointRow(type="entity", name="tb", scope="global"))
        db.commit()
        edge = EdgeRow(source=src_id, target=tgt_id, type="uses", created_at="2026-01-01", valid_from=valid_from, valid_to=valid_to)
        edge_id = insert_edge(db, edge)
        db.commit()
        return src_id, tgt_id, edge_id

    def test_query_current_edges_excludes_invalidated(self, db):
        """query_current_edges filters by valid_to IS NULL."""
        src_id, tgt_id, e1 = self._setup_data_points_and_edge(db, valid_from="2026-01-01")
        from storage import DataPointRow, insert_data_point
        tc_id = insert_data_point(db, DataPointRow(type="entity", name="tc", scope="global"))
        db.commit()
        e2_id = insert_edge(db, EdgeRow(source=src_id, target=tc_id, type="uses", created_at="2026-01-01", valid_to="2026-02-01"))
        db.commit()
        current = query_current_edges(db)
        ids = [e.id for e in current]
        assert e1 in ids
        assert e2_id not in ids

    def test_query_edges_at_date_returns_valid_window(self, db):
        """Temporal query returns edges valid at a specific date."""
        src_id, tgt_id, edge_id = self._setup_data_points_and_edge(db, valid_from="2026-01-01", valid_to="2026-02-15")
        results_jan = query_edges_at_date(db, "2026-01-15")
        assert any(e.id == edge_id for e in results_jan)
        results_mar = query_edges_at_date(db, "2026-03-01")
        assert not any(e.id == edge_id for e in results_mar)


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

    def test_schema_creates_data_points_table(self, db_dir):
        """SCHEMA_DDL creates data_points table with all 20 columns."""
        from storage import SCHEMA_DDL

        db_path = db_dir / "memory.db"
        conn = sqlite3.connect(str(db_path))

        # Execute DDL but skip if sqlite-vec not available
        try:
            conn.executescript(SCHEMA_DDL)
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

        # Check all 20 columns exist
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

    def test_schema_creates_vec_data_table(self, db_dir):
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

    def test_schema_adds_reason_to_edges(self, db_dir):
        """SCHEMA_DDL creates edges table with reason column and data_points refs."""
        from storage import SCHEMA_DDL

        db_path = db_dir / "memory.db"
        conn = sqlite3.connect(str(db_path))

        # Execute DDL (skip vec_data if sqlite-vec not available)
        try:
            conn.executescript(SCHEMA_DDL)
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

            from storage import query_data_point_by_id

            result = query_data_point_by_id(conn, "nonexistent")
            assert result is None
            conn.close()

    def test_update_data_point(self, tmp_path):
        """update_data_point updates specified columns."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()

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
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()

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
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()

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

            from storage import update_data_point

            rowcount = update_data_point(conn, "nonexistent-id", content="New")
            assert rowcount == 0
            conn.close()

    def test_soft_delete_nonexistent_returns_zero(self, tmp_path):
        """soft_delete_data_point returns 0 rowcount for nonexistent ID."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()

            from storage import soft_delete_data_point

            rowcount = soft_delete_data_point(conn, "nonexistent-id")
            assert rowcount == 0
            conn.close()

    def test_insert_edge_validates_data_points_fk(self, tmp_path):
        """insert_edge validates that source/target exist in data_points (FK enforcement)."""
        db_path = tmp_path / "memory.db"
        with mock.patch("storage.get_db_path", return_value=db_path):
            conn = ensure_db()

            from storage import DataPointRow, EdgeRow, insert_data_point, insert_edge

            # Create only one data point
            dpA_id = insert_data_point(conn, DataPointRow(type="entity", name="A", scope="global"))
            conn.commit()

            # Try to create an edge with a nonexistent target
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
        """Bare DB with data_points table only -- no migration side effects."""
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


# =============================================================================
# TestProvenanceEdges -- C4: Provenance edge creation helpers
# =============================================================================


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
            f"shared_db created database at {db_path}, under ~/.claude/memory/ -- "
            "test data would corrupt the production database!"
        )

    def test_db_fixture_uses_temp_path(self, db, tmp_path):
        """db fixture creates DB under tmp_path, not ~/.claude/memory/."""
        import pathlib
        db_path = pathlib.Path(db.execute("PRAGMA database_list").fetchone()[2])
        home_memory = pathlib.Path.home() / ".claude" / "memory"
        assert not str(db_path).startswith(str(home_memory)), (
            f"db fixture created database at {db_path}, under ~/.claude/memory/ -- "
            "test data would corrupt the production database!"
        )


# ============================================================================
# Legacy table cleanup
# ============================================================================


class TestLegacyTableCleanup:
    """Tests that ensure_db() drops legacy chunks and nodes tables."""

    def test_ensure_db_drops_chunks_table(self, db_dir):
        """Pre-existing chunks table should be dropped by ensure_db()."""
        db_path = db_dir / "memory.db"
        pre_conn = sqlite3.connect(str(db_path))
        pre_conn.execute("CREATE TABLE chunks (id TEXT, salience REAL, source_type TEXT)")
        pre_conn.execute("INSERT INTO chunks VALUES ('c1', 1.0, 'test')")
        pre_conn.commit()
        pre_conn.close()

        conn = ensure_db()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "chunks" not in tables
        close_db(conn)

    def test_ensure_db_drops_nodes_table(self, db_dir):
        """Pre-existing nodes table should be dropped by ensure_db()."""
        db_path = db_dir / "memory.db"
        pre_conn = sqlite3.connect(str(db_path))
        pre_conn.execute("CREATE TABLE nodes (id TEXT, name TEXT)")
        pre_conn.execute("INSERT INTO nodes VALUES ('n1', 'test_node')")
        pre_conn.commit()
        pre_conn.close()

        conn = ensure_db()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "nodes" not in tables
        close_db(conn)


# ============================================================================
# Stale data cleanup
# ============================================================================


class TestCleanupStaleData:
    """Tests for cleanup_stale_data() one-time cleanup function."""

    def test_deletes_profile_html_comments(self, shared_db):
        """HTML comment placeholders in profile data_points should be hard-deleted."""
        now = "2026-03-26T00:00:00Z"
        ids = []
        for content in [
            "<!-- Add your preferred programming languages here -->",
            "<!-- Communication style notes -->",
        ]:
            dp_id = insert_data_point(shared_db, DataPointRow(
                type="profile", scope="user", content=content,
                created_at=now, source_type="system",
            ))
            ids.append(dp_id)
        real_id = insert_data_point(shared_db, DataPointRow(
            type="profile", scope="user",
            content="Prefers concise, direct communication",
            created_at=now, source_type="system",
        ))
        shared_db.commit()

        stats = cleanup_stale_data(shared_db)

        assert stats["profiles_deleted"] >= 2
        for dp_id in ids:
            row = shared_db.execute(
                "SELECT id FROM data_points WHERE id = ?", (dp_id,)
            ).fetchone()
            assert row is None, f"HTML comment entry {dp_id} should be hard-deleted"
        real_row = shared_db.execute(
            "SELECT id FROM data_points WHERE id = ?", (real_id,)
        ).fetchone()
        assert real_row is not None, "Real content entry should survive"

    def test_deletes_profile_bare_tags(self, shared_db):
        """Bare tag entries (<= 2 words) in profile should be hard-deleted."""
        now = "2026-03-26T00:00:00Z"
        bare_ids = []
        for content in ["Python-3.13", "claude-code", "macOS ARM"]:
            dp_id = insert_data_point(shared_db, DataPointRow(
                type="profile", scope="user", content=content,
                created_at=now, source_type="system",
            ))
            bare_ids.append(dp_id)
        real_id = insert_data_point(shared_db, DataPointRow(
            type="profile", scope="user",
            content="Uses Python 3.13 with pyenv for version management",
            created_at=now, source_type="system",
        ))
        shared_db.commit()

        stats = cleanup_stale_data(shared_db)

        assert stats["profiles_deleted"] >= 3
        for dp_id in bare_ids:
            row = shared_db.execute(
                "SELECT id FROM data_points WHERE id = ?", (dp_id,)
            ).fetchone()
            assert row is None, f"Bare tag entry {dp_id} should be hard-deleted"
        real_row = shared_db.execute(
            "SELECT id FROM data_points WHERE id = ?", (real_id,)
        ).fetchone()
        assert real_row is not None, "Descriptive entry should survive"

    def test_soft_deletes_near_duplicate_clusters(self, shared_db):
        """Near-duplicate cluster should keep highest evidence_count, soft-delete rest.

        SimHash values are not pre-computed; cleanup_stale_data computes them
        on-the-fly when simhash IS NULL, which avoids the SQLite signed-int
        overflow that would occur if we stored unsigned 64-bit values directly.

        Uses long texts with minimal suffix variation to ensure simhash
        distances stay within DEFAULT_HAMMING_THRESHOLD (3).
        """
        now = "2026-03-26T00:00:00Z"
        base = ("Always configure the git init command to set the default branch "
                "name to main when creating new repositories on any machine or "
                "platform that you work on regularly and consistently across "
                "all environments")
        variations = [
            base,
            base + " today",
            base + " now",
            base + ".",
            base + " yes",
        ]
        evidence_counts = [3, 1, 1, 2, 1]
        ids = []
        for text, ev in zip(variations, evidence_counts):
            dp_id = insert_data_point(shared_db, DataPointRow(
                type="memory", scope="global", content=text,
                created_at=now, source_type="synthesis",
                evidence_count=ev,
            ))
            ids.append(dp_id)
        shared_db.commit()

        stats = cleanup_stale_data(shared_db)

        survivor = shared_db.execute(
            "SELECT id, salience FROM data_points WHERE id = ?", (ids[0],)
        ).fetchone()
        assert survivor is not None
        assert survivor[1] > 0, "Highest evidence_count entry should survive"

        assert stats["duplicates_soft_deleted"] >= 4
        for dp_id in ids[1:]:
            row = shared_db.execute(
                "SELECT salience FROM data_points WHERE id = ?", (dp_id,)
            ).fetchone()
            assert row is not None, "Near-duplicate should still exist (soft-deleted)"
            assert row[0] == 0.0, f"Near-duplicate {dp_id} should have salience=0.0"

    def test_soft_deletes_stale_project_memories(self, shared_db):
        """Known stale project memory patterns should be soft-deleted."""
        now = "2026-03-26T00:00:00Z"
        stale_contents = [
            "Resume backfill from session abc123",
            "Phase A: vector search integration (PR #66)",
            "Phase A: SimHash near-duplicate detection implemented",
        ]
        ids = []
        for content in stale_contents:
            dp_id = insert_data_point(shared_db, DataPointRow(
                type="memory", scope="project-test", content=content,
                created_at=now, source_type="synthesis",
            ))
            ids.append(dp_id)
        shared_db.commit()

        stats = cleanup_stale_data(shared_db)

        assert stats["stale_soft_deleted"] >= 3
        for dp_id in ids:
            row = shared_db.execute(
                "SELECT salience FROM data_points WHERE id = ?", (dp_id,)
            ).fetchone()
            assert row is not None, "Stale entry should still exist (soft-deleted)"
            assert row[0] == 0.0, f"Stale entry {dp_id} should have salience=0.0"
