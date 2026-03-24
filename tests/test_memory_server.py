#!/usr/bin/env python3
"""
Tests for scripts/memory_server.py

Covers: scaffold, search_memories, write_memory, delete_memory, traverse_graph.
Run with: python3 -m pytest tests/test_memory_server.py -v
"""

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import pytest
from storage import (
    DataPointRow,
    EdgeRow,
    close_db,
    ensure_db,
    insert_data_point,
    insert_edge,
    query_data_point_by_id,
)

# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def db_dir(tmp_path):
    """Provide a temporary directory for the DB and patch get_db_path."""
    db_path = tmp_path / "memory.db"
    with mock.patch("storage.get_db_path", return_value=db_path):
        yield tmp_path


@pytest.fixture
def db(db_dir):
    """Create a fresh v3 DB and return the connection."""
    conn = ensure_db()
    yield conn
    close_db(conn)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _make_dp(conn, content="test fact", scope="global", salience=0.8, dp_type="memory"):
    """Helper: insert a data_point and return its ID."""
    dp = DataPointRow(
        type=dp_type,
        content=content,
        scope=scope,
        salience=salience,
        source_type="test",
        created_at=_now(),
    )
    dp_id = insert_data_point(conn, dp)
    conn.commit()
    return dp_id


# ===========================================================================
# B1 — MCP Server Scaffold
# ===========================================================================


class TestMCPServerScaffold:
    def test_server_has_four_tools(self):
        """Server module exports TOOL_NAMES with exactly the four expected tools."""
        import memory_server
        assert set(memory_server.TOOL_NAMES) == {
            "search_memories",
            "write_memory",
            "delete_memory",
            "traverse_graph",
        }

    def test_fastembed_loads_in_background(self):
        """_warm_model_async returns a daemon Thread immediately."""
        import memory_server
        with patch("memory_server.embed_text", return_value=[0.1] * 384):
            t = memory_server._warm_model_async()
            assert isinstance(t, threading.Thread)
            assert t.daemon is True

    def test_model_ready_set_when_warmup_returns_embedding(self):
        """_model_ready is set when embed_text returns a non-empty vector."""
        import memory_server
        memory_server._model_ready.clear()
        with patch("memory_server.embed_text", return_value=[0.1] * 384):
            t = memory_server._warm_model_async()
            t.join(timeout=2)
        assert memory_server._model_ready.is_set()
        memory_server._model_ready.clear()

    def test_model_ready_not_set_when_warmup_returns_empty(self):
        """_model_ready stays unset when embed_text returns [] (FastEmbed unavailable)."""
        import memory_server
        memory_server._model_ready.clear()
        with patch("memory_server.embed_text", return_value=[]):
            t = memory_server._warm_model_async()
            t.join(timeout=2)
        assert not memory_server._model_ready.is_set()

    def test_model_ready_not_set_when_warmup_returns_none(self):
        """_model_ready stays unset when embed_text returns None."""
        import memory_server
        memory_server._model_ready.clear()
        with patch("memory_server.embed_text", return_value=None):
            t = memory_server._warm_model_async()
            t.join(timeout=2)
        assert not memory_server._model_ready.is_set()

    def test_db_connection_established(self, db_dir):
        """init_db() returns the connection from ensure_db()."""
        import memory_server
        conn = ensure_db()
        with patch("memory_server.ensure_db", return_value=conn):
            result = memory_server.init_db()
            assert result is conn
        close_db(conn)

    def test_has_mcp_flag(self):
        """HAS_MCP reflects whether the mcp SDK is importable."""
        import memory_server
        assert isinstance(memory_server.HAS_MCP, bool)

    def test_tool_implementations_callable(self):
        """All four async tool implementations exist and are callable."""
        import memory_server
        assert callable(memory_server._search_memories)
        assert callable(memory_server._write_memory)
        assert callable(memory_server._delete_memory)
        assert callable(memory_server._traverse_graph)


# ===========================================================================
# B2 — search_memories
# ===========================================================================


class TestSearchMemories:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_sql_fallback_when_model_not_ready(self, db):
        """Falls back to salience+recency ranking when FastEmbed unavailable."""
        import memory_server
        memory_server._db_conn = db
        memory_server._model_ready.clear()

        _make_dp(db, content="first fact", scope="global", salience=0.9)
        _make_dp(db, content="second fact", scope="global", salience=0.5)

        results = self._run(memory_server._search_memories("some query", scope="global"))
        assert len(results) >= 1
        assert all("id" in r and "content" in r and "score" in r for r in results)
        scores = [r["score"] for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_sql_fallback_returns_no_crash_on_empty_db(self, db):
        """SQL fallback returns a list (possibly empty) without error."""
        import memory_server
        memory_server._db_conn = db
        memory_server._model_ready.clear()

        results = self._run(memory_server._search_memories("query_that_matches_nothing", scope="nonexistent-scope-xyz"))
        assert isinstance(results, list)

    def test_scope_filtering(self, db):
        """scope parameter limits results to matching scope."""
        import memory_server
        memory_server._db_conn = db
        memory_server._model_ready.clear()

        _make_dp(db, content="in scope A", scope="proj-a", salience=0.9)
        _make_dp(db, content="in scope B", scope="proj-b", salience=0.9)

        results = self._run(memory_server._search_memories("query", scope="proj-a"))
        assert all(r["scope"] == "proj-a" for r in results)
        assert len(results) == 1
        assert results[0]["content"] == "in scope A"

    def test_graph_boost_applied(self, db):
        """Results connected by edges get a score boost of 0.05 per connection."""
        import memory_server
        memory_server._db_conn = db
        memory_server._model_ready.clear()

        id1 = _make_dp(db, content="memory one", scope="global", salience=0.8)
        id2 = _make_dp(db, content="memory two", scope="global", salience=0.8)

        now = _now()
        insert_edge(db, EdgeRow(source=id1, target=id2, type="mentions", created_at=now))
        db.commit()

        results = self._run(memory_server._search_memories("memory", scope="global"))
        assert len(results) == 2
        id_to_score = {r["id"]: r["score"] for r in results}
        assert id_to_score[id1] > 0.8
        assert id_to_score[id2] > 0.8

    def test_graph_boost_capped_at_015(self, db):
        """Graph boost is capped at 0.15 regardless of edge count."""
        import memory_server
        memory_server._db_conn = db
        memory_server._model_ready.clear()

        base_salience = 0.5
        center_id = _make_dp(db, content="center", scope="global", salience=base_salience)

        now = _now()
        for i in range(5):
            peer_id = _make_dp(db, content=f"peer {i}", scope="global", salience=0.4)
            insert_edge(db, EdgeRow(source=center_id, target=peer_id, type="mentions", created_at=now))
        db.commit()

        results = self._run(memory_server._search_memories("center", scope="global"))
        center_result = next((r for r in results if r["id"] == center_id), None)
        assert center_result is not None
        assert center_result["score"] <= base_salience + 0.15 + 0.001

    def test_result_includes_entities_field(self, db):
        """Results include an 'entities' field (list)."""
        import memory_server
        memory_server._db_conn = db
        memory_server._model_ready.clear()

        dp = DataPointRow(
            type="memory",
            content="fact with entities",
            scope="global",
            salience=0.8,
            source_type="test",
            created_at=_now(),
            entities=json.dumps(["Alice", "Bob"]),
        )
        insert_data_point(db, dp)
        db.commit()

        results = self._run(memory_server._search_memories("fact", scope="global"))
        assert len(results) == 1
        assert isinstance(results[0]["entities"], list)
        assert "Alice" in results[0]["entities"]

    def test_vector_search_path(self, db):
        """When _model_ready is set, search_similar is called."""
        import memory_server
        from embeddings import ScoredDataPoint

        dp = DataPointRow(
            type="memory", content="searchable fact", scope="global",
            salience=0.8, source_type="test", created_at=_now(),
        )
        dp_id = insert_data_point(db, dp)
        db.commit()
        dp_with_id = query_data_point_by_id(db, dp_id)

        memory_server._db_conn = db
        memory_server._model_ready.set()
        mock_results = [ScoredDataPoint(data_point=dp_with_id, score=0.9, vec_similarity=0.9)]

        with patch("memory_server.search_similar", return_value=mock_results):
            results = self._run(memory_server._search_memories("searchable fact", scope="global"))

        assert len(results) == 1
        assert results[0]["id"] == dp_id
        memory_server._model_ready.clear()

    def test_provenance_reason_populated_from_fact_column(self, db):
        """Provenance reason is read from the edges.fact column, not a nonexistent reason column."""
        import memory_server
        memory_server._db_conn = db
        memory_server._model_ready.clear()

        old_id = _make_dp(db, content="old fact", scope="global", salience=0.7)
        new_id = _make_dp(db, content="new fact", scope="global", salience=0.9)
        now = _now()
        insert_edge(db, EdgeRow(
            source=new_id,
            target=old_id,
            type="supersedes",
            fact="updated because data changed",
            created_at=now,
            valid_from=now,
        ))
        db.commit()

        results = self._run(memory_server._search_memories("new fact", scope="global"))
        new_result = next((r for r in results if r["id"] == new_id), None)
        assert new_result is not None
        assert len(new_result["provenance"]) == 1
        prov = new_result["provenance"][0]
        assert prov["type"] == "supersedes"
        assert prov["reason"] == "updated because data changed"

    def test_provenance_reason_is_none_when_fact_is_null(self, db):
        """Provenance reason is None when the edge fact column is NULL."""
        import memory_server
        memory_server._db_conn = db
        memory_server._model_ready.clear()

        old_id = _make_dp(db, content="old item", scope="global", salience=0.7)
        new_id = _make_dp(db, content="new item", scope="global", salience=0.9)
        now = _now()
        insert_edge(db, EdgeRow(
            source=new_id,
            target=old_id,
            type="supersedes",
            created_at=now,
            valid_from=now,
        ))
        db.commit()

        results = self._run(memory_server._search_memories("new item", scope="global"))
        new_result = next((r for r in results if r["id"] == new_id), None)
        assert new_result is not None
        assert len(new_result["provenance"]) == 1
        assert new_result["provenance"][0]["reason"] is None


# ===========================================================================
# B3 — write_memory
# ===========================================================================


class TestWriteMemory:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_creates_data_point(self, db):
        """write_memory creates a data_point with the given content."""
        import memory_server
        memory_server._db_conn = db
        with patch("memory_server.embed_text", return_value=None):
            result = self._run(memory_server._write_memory("test fact", scope="proj-x"))

        assert result["status"] == "created"
        dp = query_data_point_by_id(db, result["id"])
        assert dp is not None
        assert dp.content == "test fact"
        assert dp.scope == "proj-x"

    def test_creates_entity_data_points(self, db):
        """Each entity in entities list becomes a data_point with type='entity'."""
        import memory_server
        memory_server._db_conn = db
        with patch("memory_server.embed_text", return_value=None):
            self._run(memory_server._write_memory(
                "fact about JWT and OAuth",
                scope="proj-x",
                entities=["JWT", "OAuth"],
            ))

        rows = db.execute(
            "SELECT name FROM data_points WHERE type='entity' AND scope='proj-x'"
        ).fetchall()
        entity_names = {r[0] for r in rows}
        assert "JWT" in entity_names
        assert "OAuth" in entity_names

    def test_creates_mentions_edges(self, db):
        """'mentions' edges created from memory to each entity."""
        import memory_server
        memory_server._db_conn = db
        with patch("memory_server.embed_text", return_value=None):
            result = self._run(memory_server._write_memory(
                "fact about Redis",
                scope="proj-x",
                entities=["Redis"],
            ))

        dp_id = result["id"]
        edges = db.execute(
            "SELECT type FROM edges WHERE source=? AND type='mentions'", (dp_id,)
        ).fetchall()
        assert len(edges) == 1

    def test_provenance_edge_on_supersedes(self, db):
        """When supersedes is provided, creates a supersedes edge."""
        import memory_server
        memory_server._db_conn = db

        old_id = _make_dp(db, content="old fact", scope="proj-x")
        with patch("memory_server.embed_text", return_value=None):
            result = self._run(memory_server._write_memory(
                "new fact",
                scope="proj-x",
                supersedes=old_id,
                relation_reason="updated information",
            ))

        new_id = result["id"]
        edge = db.execute(
            "SELECT type, fact FROM edges WHERE source=? AND target=?", (new_id, old_id)
        ).fetchone()
        assert edge is not None
        assert edge[0] == "supersedes"
        assert edge[1] == "updated information"

    def test_custom_relation_type(self, db):
        """relation_type overrides the default 'supersedes'."""
        import memory_server
        memory_server._db_conn = db

        old_id = _make_dp(db, content="base fact", scope="proj-x")
        with patch("memory_server.embed_text", return_value=None):
            result = self._run(memory_server._write_memory(
                "refined fact",
                scope="proj-x",
                supersedes=old_id,
                relation_type="refines",
            ))

        new_id = result["id"]
        edge = db.execute(
            "SELECT type FROM edges WHERE source=? AND target=?", (new_id, old_id)
        ).fetchone()
        assert edge[0] == "refines"

    def test_user_scope_auto_pins(self, db):
        """scope='user' sets salience=1.0 and consolidated=1."""
        import memory_server
        memory_server._db_conn = db
        with patch("memory_server.embed_text", return_value=None):
            result = self._run(memory_server._write_memory("pinned fact", scope="user"))

        dp = query_data_point_by_id(db, result["id"])
        assert dp.salience == 1.0
        assert dp.consolidated == 1

    def test_atomic_rollback_on_embedding_failure(self, db):
        """If embedding raises, no data_point is left in DB."""
        import memory_server
        memory_server._db_conn = db

        with patch("memory_server.embed_text", side_effect=RuntimeError("embed failed")):
            with patch("memory_server.insert_data_point", side_effect=RuntimeError("forced")):
                with pytest.raises(RuntimeError):
                    self._run(memory_server._write_memory("bad fact", scope="proj-x"))

        count = db.execute("SELECT COUNT(*) FROM data_points WHERE content='bad fact'").fetchone()[0]
        assert count == 0

    def test_entity_upsert_no_duplicates(self, db):
        """Writing two memories mentioning 'JWT' creates only one entity data_point."""
        import memory_server
        memory_server._db_conn = db
        with patch("memory_server.embed_text", return_value=None):
            self._run(memory_server._write_memory("first JWT fact", scope="proj-x", entities=["JWT"]))
            self._run(memory_server._write_memory("second JWT fact", scope="proj-x", entities=["JWT"]))

        count = db.execute(
            "SELECT COUNT(*) FROM data_points WHERE type='entity' AND LOWER(name)=LOWER('JWT')"
        ).fetchone()[0]
        assert count == 1

    def test_default_salience_applied(self, db):
        """Default salience of 0.7 is applied for non-user scopes."""
        import memory_server
        memory_server._db_conn = db
        with patch("memory_server.embed_text", return_value=None):
            result = self._run(memory_server._write_memory("normal fact", scope="proj-x"))

        dp = query_data_point_by_id(db, result["id"])
        assert dp.salience == 0.7


# ===========================================================================
# B3b — write_memory supersedes soft-deletes the old data point (Issue 3)
# ===========================================================================


class TestWriteMemorySupersedes:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_superseded_data_point_is_soft_deleted(self, db):
        """When supersedes is provided, the old data_point's salience is set to 0."""
        import memory_server
        memory_server._db_conn = db

        old_id = _make_dp(db, content="old fact", scope="proj-x", salience=0.9)

        old_dp = query_data_point_by_id(db, old_id)
        assert old_dp.salience == 0.9

        with patch("memory_server.embed_text", return_value=None):
            self._run(memory_server._write_memory(
                "new fact",
                scope="proj-x",
                supersedes=old_id,
            ))

        old_dp_after = query_data_point_by_id(db, old_id)
        assert old_dp_after.salience == 0.0, "Superseded data point must be soft-deleted"

    def test_supersedes_edge_still_created(self, db):
        """The supersedes edge is still created even after soft-delete."""
        import memory_server
        memory_server._db_conn = db

        old_id = _make_dp(db, content="old fact", scope="proj-x")

        with patch("memory_server.embed_text", return_value=None):
            result = self._run(memory_server._write_memory(
                "new fact",
                scope="proj-x",
                supersedes=old_id,
            ))

        new_id = result["id"]
        edge = db.execute(
            "SELECT type FROM edges WHERE source=? AND target=?", (new_id, old_id)
        ).fetchone()
        assert edge is not None
        assert edge[0] == "supersedes"


# ===========================================================================
# B1b — warmup thread exception handling (Issue 4)
# ===========================================================================


class TestWarmModelAsync:
    def test_warmup_exception_is_logged_not_silenced(self, capsys):
        """If embed_text raises during warmup, error is printed to stderr, model_ready not set."""
        import memory_server

        memory_server._model_ready.clear()

        with patch("memory_server.embed_text", side_effect=RuntimeError("model load failed")):
            t = memory_server._warm_model_async()
            t.join(timeout=2.0)

        assert not memory_server._model_ready.is_set(), "model_ready must not be set on exception"
        captured = capsys.readouterr()
        assert "model load failed" in captured.err

    def test_warmup_sets_ready_on_success(self):
        """Successful warmup sets _model_ready event."""
        import memory_server

        memory_server._model_ready.clear()

        with patch("memory_server.embed_text", return_value=[0.1, 0.2, 0.3]):
            t = memory_server._warm_model_async()
            t.join(timeout=2.0)

        assert memory_server._model_ready.is_set()


# ===========================================================================
# B4 — delete_memory
# ===========================================================================


class TestDeleteMemory:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_soft_deletes_sets_salience_zero(self, db):
        """delete_memory sets target salience to 0."""
        import memory_server
        memory_server._db_conn = db

        dp_id = _make_dp(db, content="to be deleted", scope="global", salience=0.8)
        self._run(memory_server._delete_memory(dp_id))

        dp = query_data_point_by_id(db, dp_id)
        assert dp.salience == 0.0

    def test_creates_deletion_marker(self, db):
        """Creates a marker data_point with reason as content."""
        import memory_server
        memory_server._db_conn = db

        dp_id = _make_dp(db, content="deletable memory", scope="global")
        result = self._run(memory_server._delete_memory(dp_id, reason="no longer valid"))

        marker = query_data_point_by_id(db, result["marker_id"])
        assert marker is not None
        assert marker.content == "no longer valid"
        assert marker.source_type == "deletion"

    def test_creates_supersedes_edge(self, db):
        """Marker supersedes the deleted data_point via an edge."""
        import memory_server
        memory_server._db_conn = db

        dp_id = _make_dp(db, content="gone memory", scope="global")
        result = self._run(memory_server._delete_memory(dp_id))

        marker_id = result["marker_id"]
        edge = db.execute(
            "SELECT type FROM edges WHERE source=? AND target=?", (marker_id, dp_id)
        ).fetchone()
        assert edge is not None
        assert edge[0] == "supersedes"

    def test_invalidates_related_edges(self, db):
        """Edges connected to deleted data_point get valid_to set."""
        import memory_server
        memory_server._db_conn = db

        dp_id = _make_dp(db, content="connected memory", scope="global")
        other_id = _make_dp(db, content="other memory", scope="global")
        now = _now()
        insert_edge(db, EdgeRow(source=dp_id, target=other_id, type="mentions", created_at=now))
        db.commit()

        self._run(memory_server._delete_memory(dp_id))

        edges = db.execute(
            "SELECT valid_to FROM edges WHERE (source=? OR target=?) AND type='mentions'",
            (dp_id, dp_id),
        ).fetchall()
        assert all(e[0] is not None for e in edges)

    def test_returns_deleted_content(self, db):
        """Response includes the content of the deleted memory."""
        import memory_server
        memory_server._db_conn = db

        dp_id = _make_dp(db, content="specific content", scope="global")
        result = self._run(memory_server._delete_memory(dp_id))

        assert result["status"] == "deleted"
        assert result["deleted_content"] == "specific content"

    def test_nonexistent_id_returns_error(self, db):
        """Deleting a non-existent ID returns an error dict."""
        import memory_server
        memory_server._db_conn = db

        result = self._run(memory_server._delete_memory("nonexistent-id-xyz"))
        assert "error" in result
        assert "nonexistent-id-xyz" in result["error"]

    def test_reason_preserved_in_marker(self, db):
        """The deletion reason is stored in the marker's content."""
        import memory_server
        memory_server._db_conn = db

        dp_id = _make_dp(db, content="old fact", scope="global")
        result = self._run(memory_server._delete_memory(dp_id, reason="superseded by new data"))

        marker = query_data_point_by_id(db, result["marker_id"])
        assert "superseded by new data" in marker.content

    def test_default_marker_content_from_deleted(self, db):
        """When no reason provided, marker content includes truncated original."""
        import memory_server
        memory_server._db_conn = db

        dp_id = _make_dp(db, content="the original content", scope="global")
        result = self._run(memory_server._delete_memory(dp_id))

        marker = query_data_point_by_id(db, result["marker_id"])
        assert "the original content" in marker.content


# ===========================================================================
# B5 — traverse_graph
# ===========================================================================


class TestTraverseGraph:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_single_hop(self, db):
        """Returns directly connected data_points."""
        import memory_server
        memory_server._db_conn = db

        id_a = _make_dp(db, content="node A", scope="global")
        id_b = _make_dp(db, content="node B", scope="global")
        now = _now()
        insert_edge(db, EdgeRow(source=id_a, target=id_b, type="mentions", created_at=now))
        db.commit()

        results = self._run(memory_server._traverse_graph(id_a, depth=1))
        result_ids = [r["id"] for r in results]
        assert id_b in result_ids
        assert id_a not in result_ids

    def test_multi_hop_depth_2(self, db):
        """Follows two levels of connections: A -> B -> C."""
        import memory_server
        memory_server._db_conn = db

        id_a = _make_dp(db, content="node A", scope="global")
        id_b = _make_dp(db, content="node B", scope="global")
        id_c = _make_dp(db, content="node C", scope="global")
        now = _now()
        insert_edge(db, EdgeRow(source=id_a, target=id_b, type="mentions", created_at=now))
        insert_edge(db, EdgeRow(source=id_b, target=id_c, type="mentions", created_at=now))
        db.commit()

        results = self._run(memory_server._traverse_graph(id_a, depth=2))
        result_ids = [r["id"] for r in results]
        assert id_b in result_ids
        assert id_c in result_ids

    def test_respects_depth_limit(self, db):
        """Does not traverse beyond specified depth."""
        import memory_server
        memory_server._db_conn = db

        id_a = _make_dp(db, content="node A", scope="global")
        id_b = _make_dp(db, content="node B", scope="global")
        id_c = _make_dp(db, content="node C", scope="global")
        now = _now()
        insert_edge(db, EdgeRow(source=id_a, target=id_b, type="mentions", created_at=now))
        insert_edge(db, EdgeRow(source=id_b, target=id_c, type="mentions", created_at=now))
        db.commit()

        results = self._run(memory_server._traverse_graph(id_a, depth=1))
        result_ids = [r["id"] for r in results]
        assert id_b in result_ids
        assert id_c not in result_ids

    def test_relationship_type_filter(self, db):
        """Only follows edges of specified type."""
        import memory_server
        memory_server._db_conn = db

        id_a = _make_dp(db, content="node A", scope="global")
        id_b = _make_dp(db, content="node B", scope="global")
        id_c = _make_dp(db, content="node C", scope="global")
        now = _now()
        insert_edge(db, EdgeRow(source=id_a, target=id_b, type="mentions", created_at=now))
        insert_edge(db, EdgeRow(source=id_a, target=id_c, type="supersedes", created_at=now))
        db.commit()

        results = self._run(memory_server._traverse_graph(id_a, depth=1, relationship_type="mentions"))
        result_ids = [r["id"] for r in results]
        assert id_b in result_ids
        assert id_c not in result_ids

    def test_empty_graph(self, db):
        """Returns empty list when no edges exist for the entity."""
        import memory_server
        memory_server._db_conn = db

        id_a = _make_dp(db, content="isolated node", scope="global")
        results = self._run(memory_server._traverse_graph(id_a, depth=2))
        assert results == []

    def test_provenance_chain_following(self, db):
        """Follows supersedes chains for memory provenance."""
        import memory_server
        memory_server._db_conn = db

        old_id = _make_dp(db, content="old memory", scope="global")
        new_id = _make_dp(db, content="new memory", scope="global")
        now = _now()
        insert_edge(db, EdgeRow(source=new_id, target=old_id, type="supersedes", created_at=now))
        db.commit()

        results = self._run(memory_server._traverse_graph(new_id, depth=1))
        result_ids = [r["id"] for r in results]
        assert old_id in result_ids

    def test_expired_edges_skipped(self, db):
        """Edges with valid_to set are not followed."""
        import memory_server
        memory_server._db_conn = db

        id_a = _make_dp(db, content="node A", scope="global")
        id_b = _make_dp(db, content="node B", scope="global")
        now = _now()
        insert_edge(db, EdgeRow(
            source=id_a, target=id_b, type="mentions",
            created_at=now, valid_to=now,
        ))
        db.commit()

        results = self._run(memory_server._traverse_graph(id_a, depth=1))
        result_ids = [r["id"] for r in results]
        assert id_b not in result_ids

    def test_result_structure(self, db):
        """Each result node has id, depth, relationship, content, type, name, scope."""
        import memory_server
        memory_server._db_conn = db

        id_a = _make_dp(db, content="node A", scope="proj-x")
        id_b = _make_dp(db, content="node B", scope="proj-x")
        now = _now()
        insert_edge(db, EdgeRow(source=id_a, target=id_b, type="mentions", created_at=now))
        db.commit()

        results = self._run(memory_server._traverse_graph(id_a, depth=1))
        assert len(results) == 1
        r = results[0]
        assert "id" in r
        assert "depth" in r
        assert "relationship" in r
        assert "content" in r
        assert "type" in r
        assert "scope" in r
        assert r["depth"] == 1
        assert r["relationship"] == "mentions"


# ===========================================================================
# B6 — pyproject.toml mcp dependency
# ===========================================================================


class TestPyprojectMcpDependency:
    def test_mcp_in_optional_dependencies(self):
        """pyproject.toml includes mcp in [project.optional-dependencies]."""
        repo_root = Path(__file__).parent.parent
        pyproject = repo_root / "pyproject.toml"
        content = pyproject.read_text()
        assert "mcp" in content
        assert "mcp>=1.0.0" in content or 'mcp = [' in content

    def test_mcp_version_range(self):
        """mcp dependency uses a compatible range (not a pinned version)."""
        repo_root = Path(__file__).parent.parent
        pyproject = repo_root / "pyproject.toml"
        content = pyproject.read_text()
        assert "<2.0.0" in content or "<2" in content


# =============================================================================
# A6: Hybrid Search Wiring Tests
# =============================================================================


class TestSearchMemoriesHybrid:
    """Tests for hybrid search integration in _search_memories."""

    def test_uses_search_hybrid(self):
        """_search_memories calls search_hybrid when available."""
        from unittest.mock import patch, MagicMock
        import asyncio
        import memory_server
        from memory_server import _search_memories

        mock_result = MagicMock()
        mock_result.data_point = MagicMock()
        mock_result.data_point.id = "dp-1"
        mock_result.data_point.content = "test fact"
        mock_result.data_point.scope = "global"
        mock_result.data_point.salience = 0.8
        mock_result.data_point.entities = '["Redis"]'
        mock_result.score = 0.95

        mock_conn = MagicMock()
        mock_conn.execute.return_value.fetchall.return_value = []

        with patch("memory_server.search_hybrid", return_value=[mock_result]) as mock_hybrid, \
             patch("memory_server.query_edges_for_data_point", return_value=[]), \
             patch.object(memory_server, '_db_conn', mock_conn):
            result = asyncio.get_event_loop().run_until_complete(
                _search_memories("Redis cache", scope=None, top_k=5)
            )
            mock_hybrid.assert_called_once()

    def test_falls_back_to_sql_when_hybrid_empty(self):
        """Falls back to _sql_ranked_search when search_hybrid returns nothing."""
        from unittest.mock import patch, MagicMock
        import asyncio
        from memory_server import _search_memories

        with patch("memory_server.search_hybrid", return_value=[]), \
             patch("memory_server._sql_ranked_search") as mock_sql:
            mock_sql.return_value = []
            asyncio.get_event_loop().run_until_complete(
                _search_memories("something", scope=None, top_k=5)
            )
            mock_sql.assert_called_once()


class TestCertaintyInWriteMemory:
    """Tests for certainty parameter in write_memory MCP tool."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_write_memory_accepts_certainty(self, db):
        """write_memory MCP tool accepts optional certainty parameter."""
        import memory_server
        memory_server._db_conn = db
        with patch("memory_server.embed_text", return_value=None):
            result = self._run(memory_server._write_memory(
                "certain fact", scope="proj-x", certainty=4
            ))
        dp = query_data_point_by_id(db, result["id"])
        assert dp.certainty == 4

    def test_write_memory_defaults_certainty_to_3(self, db):
        """write_memory without certainty defaults to 3."""
        import memory_server
        memory_server._db_conn = db
        with patch("memory_server.embed_text", return_value=None):
            result = self._run(memory_server._write_memory(
                "default certainty fact", scope="proj-x"
            ))
        dp = query_data_point_by_id(db, result["id"])
        assert dp.certainty == 3

    def test_write_memory_certainty_in_tool_schema(self):
        """The write_memory tool schema includes certainty property."""
        from memory_server import HAS_MCP
        if not HAS_MCP:
            pytest.skip("MCP not available")
        import asyncio
        from memory_server import server
        tools = asyncio.get_event_loop().run_until_complete(
            server.request_handlers["tools/list"](None)
        )
        write_tool = next(t for t in tools if t.name == "write_memory")
        assert "certainty" in write_tool.inputSchema["properties"]


# ===========================================================================
# Conn-is-None guard tests
# ===========================================================================


class TestConnNoneGuards:
    """All tool handlers return clear error when DB not initialized."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_search_returns_error_when_no_db(self):
        import memory_server
        memory_server._db_conn = None
        results = self._run(memory_server._search_memories("test query"))
        assert isinstance(results, list)
        assert results[0].get("error") is not None

    def test_write_returns_error_when_no_db(self):
        import memory_server
        memory_server._db_conn = None
        result = self._run(memory_server._write_memory("test fact", scope="test"))
        assert result.get("error") is not None

    def test_delete_returns_error_when_no_db(self):
        import memory_server
        memory_server._db_conn = None
        result = self._run(memory_server._delete_memory("nonexistent-id"))
        assert result.get("error") is not None

    def test_traverse_returns_error_when_no_db(self):
        import memory_server
        memory_server._db_conn = None
        result = self._run(memory_server._traverse_graph("test-entity"))
        assert result.get("error") is not None


# ===========================================================================
# Entity dedup consistency across entry points
# ===========================================================================


class TestGetOrCreateEntityUnified:
    """Verify unified entity dedup via storage.get_or_create_entity."""

    def test_same_entity_from_storage_and_server(self, db):
        """storage.get_or_create_entity and memory_server._get_or_create_entity resolve to same ID."""
        import memory_server
        from storage import get_or_create_entity

        id_from_storage = get_or_create_entity(db, "JWT", "global")
        id_from_server = memory_server._get_or_create_entity(db, "JWT", "global")
        assert id_from_storage == id_from_server

    def test_cross_scope_dedup(self, db):
        """Same entity name in different scopes resolves to one data_point (content_hash based)."""
        from storage import get_or_create_entity

        id1 = get_or_create_entity(db, "Redis", "project-a")
        id2 = get_or_create_entity(db, "Redis", "project-b")
        assert id1 == id2
