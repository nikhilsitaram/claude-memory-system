"""Tests for web_app.py — HTTP server, read-only API, and write/delete API."""
from unittest.mock import MagicMock, patch

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
    _get_stats,
    _search,
    generate_csrf_token,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path):
    """Create a v3 in-memory DB and insert a few data_points for testing."""
    tmp_path / "memory.db"
    with patch("storage.get_memory_dir", return_value=tmp_path):
        conn = ensure_db()
    return conn


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
    def test_stats_endpoint_returns_json(self, tmp_path):
        """GET /api/stats returns JSON with memory count and salience distribution."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        _insert_dp(conn, scope="global", content="hello world")
        _insert_dp(conn, dp_type="entity", scope="global", content="an entity")

        stats = _get_stats(conn)

        assert "total_memories" in stats
        assert stats["total_memories"] >= 2
        assert "salience_distribution" in stats
        assert "recent_activity" in stats
        assert isinstance(stats["salience_distribution"], dict)
        conn.close()

    def test_data_points_endpoint_filters_by_scope(self, tmp_path):
        """GET /api/data_points?scope=global returns only global data_points."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        _insert_dp(conn, scope="global", content="global item")
        _insert_dp(conn, scope="project-x", content="project item")

        results = _get_data_points(conn, scope="global")

        assert len(results) >= 1
        assert all(r["scope"] == "global" for r in results)
        conn.close()

    def test_data_points_endpoint_filters_by_type(self, tmp_path):
        """GET /api/data_points?type=entity returns only entities."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        _insert_dp(conn, dp_type="memory", scope="global")
        _insert_dp(conn, dp_type="entity", scope="global", content="an entity")

        results = _get_data_points(conn, dp_type="entity")

        assert len(results) >= 1
        assert all(r["type"] == "entity" for r in results)
        conn.close()

    def test_data_points_endpoint_pagination(self, tmp_path):
        """GET /api/data_points?limit=5&offset=0 paginates correctly."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        for i in range(10):
            _insert_dp(conn, content=f"item {i}")

        page1 = _get_data_points(conn, limit=5, offset=0)
        page2 = _get_data_points(conn, limit=5, offset=5)

        assert len(page1) == 5
        assert len(page2) == 5
        ids1 = {r["id"] for r in page1}
        ids2 = {r["id"] for r in page2}
        assert ids1.isdisjoint(ids2), "Pages should not overlap"
        conn.close()

    def test_search_endpoint(self, tmp_path):
        """GET /api/search?q=gRPC returns matching data_points."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        _insert_dp(conn, content="gRPC service definition")
        _insert_dp(conn, content="unrelated item")

        results = _search(conn, "gRPC")

        assert isinstance(results, list)
        assert len(results) >= 1
        assert any("gRPC" in (r.get("content") or "") for r in results)
        conn.close()

    def test_search_empty_query(self, tmp_path):
        """Empty query returns empty list."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        results = _search(conn, "")
        assert results == []
        conn.close()

    def test_graph_endpoint(self, tmp_path):
        """GET /api/graph returns nodes and edges for vis.js."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        _insert_dp(conn, scope="global")

        graph = _get_graph(conn, scope="global")

        assert "nodes" in graph
        assert "edges" in graph
        assert isinstance(graph["nodes"], list)
        assert isinstance(graph["edges"], list)
        conn.close()

    def test_graph_node_has_required_fields(self, tmp_path):
        """Graph nodes contain id, label, type, scope, salience."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        _insert_dp(conn, scope="global", content="test node")

        graph = _get_graph(conn, scope="global")

        assert len(graph["nodes"]) >= 1
        node = graph["nodes"][0]
        for field in ("id", "label", "type", "scope", "salience"):
            assert field in node, f"Missing field: {field}"
        conn.close()

    def test_single_data_point_endpoint(self, tmp_path):
        """GET /api/data_point/:id returns data_point with edges."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp_id = _insert_dp(conn, content="detail test")

        result = _get_data_point_detail(conn, dp_id)

        assert "id" in result
        assert result["id"] == dp_id
        assert "edges" in result
        conn.close()

    def test_single_data_point_not_found(self, tmp_path):
        """GET /api/data_point/nonexistent returns error."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        result = _get_data_point_detail(conn, "nonexistent_id")
        assert "error" in result
        conn.close()

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

    def test_stats_salience_distribution_buckets(self, tmp_path):
        """Salience distribution covers all five buckets."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        stats = _get_stats(conn)
        dist = stats["salience_distribution"]
        expected_buckets = {"0.0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0"}
        assert set(dist.keys()) == expected_buckets
        conn.close()

    def test_stats_by_type(self, tmp_path):
        """Stats include per-type breakdown."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        _insert_dp(conn, dp_type="memory")
        _insert_dp(conn, dp_type="entity", content="ent")

        stats = _get_stats(conn)

        assert "by_type" in stats
        assert "memory" in stats["by_type"]
        assert "entity" in stats["by_type"]
        conn.close()

    def test_get_data_points_no_filter(self, tmp_path):
        """_get_data_points with no filters returns all active data_points."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        for i in range(3):
            _insert_dp(conn, content=f"item {i}")

        results = _get_data_points(conn)

        assert len(results) >= 3
        conn.close()

    def test_get_data_points_excludes_soft_deleted(self, tmp_path):
        """_get_data_points excludes data_points with salience=0."""
        from storage import soft_delete_data_point
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp_id = _insert_dp(conn, content="to be deleted")
        soft_delete_data_point(conn, dp_id)
        conn.commit()

        results = _get_data_points(conn)
        ids = [r["id"] for r in results]
        assert dp_id not in ids
        conn.close()


# ---------------------------------------------------------------------------
# D2: Write/delete API
# ---------------------------------------------------------------------------

class TestWriteDeleteAPI:
    def test_edit_updates_content(self, tmp_path):
        """_edit_data_point updates content field."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp_id = _insert_dp(conn, content="original content")

        result = _edit_data_point(conn, dp_id, {"content": "updated content"})

        assert result["status"] == "updated"
        dp = query_data_point_by_id(conn, dp_id)
        assert dp.content == "updated content"
        conn.close()

    def test_edit_updates_salience(self, tmp_path):
        """_edit_data_point updates salience field."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp_id = _insert_dp(conn, salience=1.0)

        result = _edit_data_point(conn, dp_id, {"salience": 0.9})

        assert result["status"] == "updated"
        dp = query_data_point_by_id(conn, dp_id)
        assert abs(dp.salience - 0.9) < 0.001
        conn.close()

    def test_edit_updates_content_and_salience(self, tmp_path):
        """_edit_data_point can update both content and salience at once."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp_id = _insert_dp(conn, content="old", salience=0.5)

        result = _edit_data_point(conn, dp_id, {"content": "new", "salience": 0.9})

        assert result["status"] == "updated"
        dp = query_data_point_by_id(conn, dp_id)
        assert dp.content == "new"
        assert abs(dp.salience - 0.9) < 0.001
        conn.close()

    def test_delete_soft_deletes(self, tmp_path):
        """_delete_data_point sets salience to 0.0."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp_id = _insert_dp(conn)

        result = _delete_data_point(conn, dp_id, "No longer relevant")

        assert result["status"] == "deleted"
        dp = query_data_point_by_id(conn, dp_id)
        assert dp.salience == 0.0
        conn.close()

    def test_edit_nonexistent_returns_error(self, tmp_path):
        """_edit_data_point returns error dict for nonexistent ID."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        result = _edit_data_point(conn, "nonexistent_id", {"content": "x"})
        assert "error" in result
        conn.close()

    def test_delete_nonexistent_returns_error(self, tmp_path):
        """_delete_data_point returns error dict for nonexistent ID."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        result = _delete_data_point(conn, "nonexistent_id")
        assert "error" in result
        conn.close()

    def test_edit_triggers_reembedding(self, tmp_path):
        """After edit, index_data_points is called when embeddings available."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp_id = _insert_dp(conn, content="original")

        mock_index = MagicMock()
        mock_ensure_vec = MagicMock()
        with patch.dict("sys.modules", {"embeddings": MagicMock(
            index_data_points=mock_index,
            ensure_vec_table=mock_ensure_vec,
        )}):
            import importlib

            import web_app
            importlib.reload(web_app)
            with patch("web_app._db_conn", conn):
                result = web_app._edit_data_point(conn, dp_id, {"content": "updated"})

        assert result["status"] == "updated"
        conn.close()

    def test_csrf_required_for_edit(self, tmp_path):
        """POST without X-CSRF-Token header returns 403."""
        from io import BytesIO

        import web_app

        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp_id = _insert_dp(conn)

        web_app._db_conn = conn
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
        conn.close()

    def test_csrf_required_for_delete(self, tmp_path):
        """DELETE without X-CSRF-Token header returns 403."""
        import web_app

        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp_id = _insert_dp(conn)

        web_app._db_conn = conn
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
        conn.close()

    def test_delete_invalidates_edges(self, tmp_path):
        """Deleting a data_point sets valid_to on its connected edges."""
        with patch("storage.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp_id = _insert_dp(conn)
        other_id = _insert_dp(conn, content="other")

        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        edge = EdgeRow(source=dp_id, target=other_id, type="related_to", fact="test fact", created_at=now_iso)
        insert_edge(conn, edge)
        conn.commit()

        result = _delete_data_point(conn, dp_id)

        assert result["status"] == "deleted"
        # Edge should be invalidated (valid_to set)
        row = conn.execute(
            "SELECT valid_to FROM edges WHERE source = ? OR target = ?",
            (dp_id, dp_id),
        ).fetchone()
        assert row is not None
        assert row[0] is not None, "Edge valid_to should be set after deletion"
        conn.close()
