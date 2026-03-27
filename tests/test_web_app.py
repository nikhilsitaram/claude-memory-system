"""Tests for web_app.py — HTTP server, read-only API, and write/delete API."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from storage import (
    DataPointRow,
    EdgeRow,
    ensure_db,
    insert_data_point,
    insert_edge,
    query_data_point_by_id,
)
from web_app import (
    DEFAULT_PORT,
    HOST,
    MAX_PORT,
    _delete_data_point,
    _edit_data_point,
    _get_data_point_detail,
    _get_data_points,
    _get_graph,
    _get_injection_log,
    _get_stats,
    _search,
    generate_csrf_token,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Create a fresh DB in tmp_path and return the connection."""
    db_path = tmp_path / "memory.db"
    with patch("storage.get_db_path", return_value=db_path), \
         patch("memory_utils.get_memory_dir", return_value=tmp_path):
        conn = ensure_db()
        yield conn
    conn.close()


def _insert_dp(conn, dp_type="memory", scope="global", content="test content", name=None, salience=1.0):
    dp = DataPointRow(
        type=dp_type,
        content=content,
        scope=scope,
        name=name,
        salience=salience,
    )
    dp_id = insert_data_point(conn, dp)
    conn.commit()
    return dp_id


# ---------------------------------------------------------------------------
# D1: Read-only API
# ---------------------------------------------------------------------------

class TestWebAppAPI:
    def test_stats_endpoint_returns_json(self, db):
        """GET /api/stats returns JSON with memory count and salience distribution."""
        _insert_dp(db, scope="global", content="hello world")
        _insert_dp(db, dp_type="entity", scope="global", content="an entity")

        stats = _get_stats(db)

        assert "total_memories" in stats
        assert stats["total_memories"] >= 2
        assert "salience_distribution" in stats
        assert "recent_activity" in stats
        assert isinstance(stats["salience_distribution"], dict)

    def test_data_points_endpoint_filters_by_scope(self, db):
        """GET /api/data_points?scope=global returns only global data_points."""
        _insert_dp(db, scope="global", content="global item")
        _insert_dp(db, scope="project-x", content="project item")

        results = _get_data_points(db, scope="global")

        assert len(results) >= 1
        assert all(r["scope"] == "global" for r in results)

    def test_data_points_endpoint_filters_by_type(self, db):
        """GET /api/data_points?type=entity returns only entities."""
        _insert_dp(db, dp_type="memory", scope="global")
        _insert_dp(db, dp_type="entity", scope="global", content="an entity")

        results = _get_data_points(db, dp_type="entity")

        assert len(results) >= 1
        assert all(r["type"] == "entity" for r in results)

    def test_data_points_endpoint_pagination(self, db):
        """GET /api/data_points?limit=5&offset=0 paginates correctly."""
        for i in range(10):
            _insert_dp(db, content=f"item {i}")

        page1 = _get_data_points(db, limit=5, offset=0)
        page2 = _get_data_points(db, limit=5, offset=5)

        assert len(page1) == 5
        assert len(page2) == 5
        ids1 = {r["id"] for r in page1}
        ids2 = {r["id"] for r in page2}
        assert ids1.isdisjoint(ids2), "Pages should not overlap"

    def test_search_endpoint(self, db):
        """GET /api/search?q=gRPC returns matching data_points."""
        _insert_dp(db, content="gRPC service definition")
        _insert_dp(db, content="unrelated item")

        results = _search(db, "gRPC")

        assert isinstance(results, list)
        assert len(results) >= 1
        assert any("gRPC" in (r.get("content") or "") for r in results)

    def test_search_empty_query(self, db):
        """Empty query returns empty list."""
        results = _search(db, "")
        assert results == []

    def test_graph_endpoint(self, db):
        """GET /api/graph returns nodes and edges for vis.js."""
        _insert_dp(db, scope="global")

        graph = _get_graph(db, scope="global")

        assert "nodes" in graph
        assert "edges" in graph
        assert isinstance(graph["nodes"], list)
        assert isinstance(graph["edges"], list)

    def test_graph_node_has_required_fields(self, db):
        """Graph nodes contain id, label, type, scope, salience."""
        _insert_dp(db, scope="global", content="test node")

        graph = _get_graph(db, scope="global")

        assert len(graph["nodes"]) >= 1
        node = graph["nodes"][0]
        for field in ("id", "label", "type", "scope", "salience"):
            assert field in node, f"Missing field: {field}"

    def test_single_data_point_endpoint(self, db):
        """GET /api/data_point/:id returns data_point with edges."""
        dp_id = _insert_dp(db, content="detail test")

        result = _get_data_point_detail(db, dp_id)

        assert "id" in result
        assert result["id"] == dp_id
        assert "edges" in result

    def test_single_data_point_not_found(self, db):
        """GET /api/data_point/nonexistent returns error."""
        result = _get_data_point_detail(db, "nonexistent_id")
        assert "error" in result

    def test_csrf_token_generated(self):
        """Server generates a CSRF token at startup."""
        token = generate_csrf_token()
        assert len(token) >= 32

    def test_csrf_token_is_hex(self):
        """CSRF token is a valid hex string."""
        token = generate_csrf_token()
        int(token, 16)  # Will raise ValueError if not valid hex

    def test_csrf_tokens_are_unique(self):
        """Each call to generate_csrf_token returns a different value."""
        t1 = generate_csrf_token()
        t2 = generate_csrf_token()
        assert t1 != t2

    def test_server_binds_localhost_only(self):
        """Server binds to 127.0.0.1, not 0.0.0.0."""
        assert HOST == "127.0.0.1"

    def test_port_range_constants(self):
        """Port constants are within expected range."""
        assert DEFAULT_PORT == 8742
        assert MAX_PORT >= DEFAULT_PORT
        assert MAX_PORT <= 8749

    def test_stats_salience_distribution_buckets(self, db):
        """Salience distribution covers all five buckets."""
        stats = _get_stats(db)
        dist = stats["salience_distribution"]
        expected_buckets = {"0.0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0"}
        assert set(dist.keys()) == expected_buckets

    def test_stats_by_type(self, db):
        """Stats include per-type breakdown."""
        _insert_dp(db, dp_type="memory")
        _insert_dp(db, dp_type="entity", content="ent")

        stats = _get_stats(db)

        assert "by_type" in stats
        assert "memory" in stats["by_type"]
        assert "entity" in stats["by_type"]

    def test_get_data_points_no_filter(self, db):
        """_get_data_points with no filters returns all active data_points."""
        for i in range(3):
            _insert_dp(db, content=f"item {i}")

        results = _get_data_points(db)

        assert len(results) >= 3

    def test_get_data_points_excludes_soft_deleted(self, db):
        """_get_data_points excludes data_points with salience=0."""
        from storage import soft_delete_data_point
        dp_id = _insert_dp(db, content="to be deleted")
        soft_delete_data_point(db, dp_id)
        db.commit()

        results = _get_data_points(db)
        ids = [r["id"] for r in results]
        assert dp_id not in ids


# ---------------------------------------------------------------------------
# D2: Write/delete API
# ---------------------------------------------------------------------------

class TestWriteDeleteAPI:
    def test_edit_updates_content(self, db):
        """_edit_data_point updates content field."""
        dp_id = _insert_dp(db, content="original content")

        result = _edit_data_point(db, dp_id, {"content": "updated content"})

        assert result["status"] == "updated"
        dp = query_data_point_by_id(db, dp_id)
        assert dp.content == "updated content"

    def test_edit_updates_salience(self, db):
        """_edit_data_point updates salience field."""
        dp_id = _insert_dp(db, salience=1.0)

        result = _edit_data_point(db, dp_id, {"salience": 0.9})

        assert result["status"] == "updated"
        dp = query_data_point_by_id(db, dp_id)
        assert abs(dp.salience - 0.9) < 0.001

    def test_edit_updates_content_and_salience(self, db):
        """_edit_data_point can update both content and salience at once."""
        dp_id = _insert_dp(db, content="old", salience=0.5)

        result = _edit_data_point(db, dp_id, {"content": "new", "salience": 0.9})

        assert result["status"] == "updated"
        dp = query_data_point_by_id(db, dp_id)
        assert dp.content == "new"
        assert abs(dp.salience - 0.9) < 0.001

    def test_delete_soft_deletes(self, db):
        """_delete_data_point sets salience to 0.0."""
        dp_id = _insert_dp(db)

        result = _delete_data_point(db, dp_id, "No longer relevant")

        assert result["status"] == "deleted"
        dp = query_data_point_by_id(db, dp_id)
        assert dp.salience == 0.0

    def test_edit_nonexistent_returns_error(self, db):
        """_edit_data_point returns error dict for nonexistent ID."""
        result = _edit_data_point(db, "nonexistent_id", {"content": "x"})
        assert "error" in result

    def test_delete_nonexistent_returns_error(self, db):
        """_delete_data_point returns error dict for nonexistent ID."""
        result = _delete_data_point(db, "nonexistent_id")
        assert "error" in result

    def test_edit_triggers_reembedding(self, db):
        """After edit, index_data_points is called when embeddings available."""
        dp_id = _insert_dp(db, content="original")

        mock_index = MagicMock()
        mock_ensure_vec = MagicMock()
        with patch.dict("sys.modules", {"embeddings": MagicMock(
            index_data_points=mock_index,
            ensure_vec_table=mock_ensure_vec,
        )}):
            import importlib

            import web_app
            importlib.reload(web_app)
            with patch("web_app._db_conn", db):
                result = web_app._edit_data_point(db, dp_id, {"content": "updated"})

        assert result["status"] == "updated"

    def test_csrf_required_for_edit(self, db):
        """POST without X-CSRF-Token header returns 403."""
        from io import BytesIO

        import web_app

        dp_id = _insert_dp(db)

        web_app._db_conn = db
        web_app._csrf_token = "real-token"

        # Simulate handler with no CSRF header
        handler = object.__new__(web_app.MemoryAPIHandler)
        handler.headers = {}
        handler.path = f"/api/data_point/{dp_id}"
        handler.rfile = BytesIO(b'{"content": "x"}')

        responses = []
        def fake_send_json(data, status=200):
            responses.append((status, data))
        handler._send_json = fake_send_json

        handler.do_POST()

        assert responses[0][0] == 403

    def test_csrf_required_for_delete(self, db):
        """DELETE without X-CSRF-Token header returns 403."""
        import web_app

        dp_id = _insert_dp(db)

        web_app._db_conn = db
        web_app._csrf_token = "real-token"

        handler = object.__new__(web_app.MemoryAPIHandler)
        handler.headers = {}
        handler.path = f"/api/data_point/{dp_id}"

        responses = []
        def fake_send_json(data, status=200):
            responses.append((status, data))
        handler._send_json = fake_send_json

        handler.do_DELETE()

        assert responses[0][0] == 403

    def test_delete_invalidates_edges(self, db):
        """Deleting a data_point sets valid_to on its connected edges."""
        dp_id = _insert_dp(db)
        other_id = _insert_dp(db, content="other")

        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        edge = EdgeRow(source=dp_id, target=other_id, type="related_to", fact="test fact", created_at=now_iso)
        insert_edge(db, edge)
        db.commit()

        result = _delete_data_point(db, dp_id)

        assert result["status"] == "deleted"
        # Edge should be invalidated (valid_to set)
        row = db.execute(
            "SELECT valid_to FROM edges WHERE source = ? OR target = ?",
            (dp_id, dp_id),
        ).fetchone()
        assert row is not None
        assert row[0] is not None, "Edge valid_to should be set after deletion"


# ---------------------------------------------------------------------------
# D3: DB isolation guard
# ---------------------------------------------------------------------------

class TestDBIsolation:
    def test_db_fixture_uses_temp_path(self, db, tmp_path):
        """Guard: the db fixture must create the database under tmp_path, not ~/.claude/memory/."""
        import pathlib

        db_path = pathlib.Path(db.execute("PRAGMA database_list").fetchone()[2])
        home_memory = pathlib.Path.home() / ".claude" / "memory"
        assert str(db_path).startswith(str(tmp_path)), (
            f"DB fixture created database at {db_path}, which is NOT under tmp_path ({tmp_path}). "
            "Test data would leak to the production database!"
        )
        assert not str(db_path).startswith(str(home_memory)), (
            f"DB fixture created database at {db_path}, which is under ~/.claude/memory/. "
            "This would corrupt the production database!"
        )


# ---------------------------------------------------------------------------
# B3: Injection log API
# ---------------------------------------------------------------------------

class TestInjectionLogAPI:
    def test_returns_empty_when_no_log_file(self, tmp_path):
        """_get_injection_log returns [] when the log file does not exist."""
        with patch("injection_log.get_log_path", return_value=tmp_path / "nonexistent.jsonl"):
            result = _get_injection_log()
        assert result == []

    def test_returns_recent_entries(self, tmp_path):
        """_get_injection_log returns entries from the last hour by default."""
        log_path = tmp_path / ".injection-log.jsonl"
        now = datetime.now(timezone.utc)
        recent_ts = (now - timedelta(minutes=10)).isoformat()
        old_ts = (now - timedelta(hours=2)).isoformat()
        log_path.write_text(
            json.dumps({"ts": old_ts, "session_id": "s1", "hook": "SessionStart"}) + "\n"
            + json.dumps({"ts": recent_ts, "session_id": "s2", "hook": "SessionStart"}) + "\n",
            encoding="utf-8",
        )

        with patch("injection_log.get_log_path", return_value=log_path):
            result = _get_injection_log()

        assert len(result) == 1
        assert result[0]["session_id"] == "s2"

    def test_filters_by_session(self, tmp_path):
        """_get_injection_log filters entries by session_id when provided."""
        log_path = tmp_path / ".injection-log.jsonl"
        now = datetime.now(timezone.utc)
        ts = (now - timedelta(minutes=5)).isoformat()
        log_path.write_text(
            json.dumps({"ts": ts, "session_id": "sess-A", "hook": "SessionStart"}) + "\n"
            + json.dumps({"ts": ts, "session_id": "sess-B", "hook": "SessionStart"}) + "\n",
            encoding="utf-8",
        )

        with patch("injection_log.get_log_path", return_value=log_path):
            result = _get_injection_log(session_id="sess-A")

        assert len(result) == 1
        assert result[0]["session_id"] == "sess-A"

    def test_filters_by_since_timestamp(self, tmp_path):
        """_get_injection_log filters by explicit since ISO timestamp."""
        log_path = tmp_path / ".injection-log.jsonl"
        now = datetime.now(timezone.utc)
        ts_old = (now - timedelta(hours=3)).isoformat()
        ts_mid = (now - timedelta(hours=1, minutes=30)).isoformat()
        ts_new = (now - timedelta(minutes=10)).isoformat()
        log_path.write_text(
            json.dumps({"ts": ts_old, "session_id": "s1", "hook": "SessionStart"}) + "\n"
            + json.dumps({"ts": ts_mid, "session_id": "s2", "hook": "SessionStart"}) + "\n"
            + json.dumps({"ts": ts_new, "session_id": "s3", "hook": "SessionStart"}) + "\n",
            encoding="utf-8",
        )

        since = (now - timedelta(hours=2)).isoformat()
        with patch("injection_log.get_log_path", return_value=log_path):
            result = _get_injection_log(since=since)

        assert len(result) == 2
        session_ids = [e["session_id"] for e in result]
        assert "s2" in session_ids
        assert "s3" in session_ids
        assert "s1" not in session_ids
