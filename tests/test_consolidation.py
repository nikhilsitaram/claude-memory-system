import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


class TestFindClusters:
    """Tests for similarity-based clustering of memories."""

    def _make_db(self, tmp_path):
        from unittest.mock import patch as p

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with p("storage.get_db_path", return_value=db_path), \
             p("storage.get_memory_dir", return_value=tmp_path):
            return ensure_db()

    def test_no_clusters_when_insufficient_memories(self, tmp_path):
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="only one", scope="global", salience=0.5))
        conn.commit()
        from consolidation import find_clusters
        clusters = find_clusters(conn, similarity_threshold=0.80, max_clusters=15)
        assert len(clusters) == 0
        conn.close()

    def test_excludes_pairs_with_contradicts_edges(self, tmp_path):
        """Memories with contradicts edges are never clustered together."""
        from storage import DataPointRow, EdgeRow, insert_data_point, insert_edge
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="use Redis", scope="global", salience=0.8))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="use Redis cache", scope="global", salience=0.7))
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        insert_edge(conn, EdgeRow(source=id1, target=id2, type="contradicts", created_at=now))
        conn.commit()
        from consolidation import find_clusters
        with patch("consolidation._get_similarity_pairs", return_value=[(id1, id2, 0.95)]):
            clusters = find_clusters(conn, similarity_threshold=0.80, max_clusters=15)
        for cluster in clusters:
            assert not (id1 in cluster and id2 in cluster), "Contradicted pair should not cluster"
        conn.close()

    def test_max_cluster_size_enforced(self, tmp_path):
        """Clusters larger than max_size are split."""
        from consolidation import _split_large_cluster
        members = [f"dp-{i}" for i in range(20)]
        edges = [(members[i], members[j], 0.85) for i in range(20) for j in range(i+1, 20)]
        result = _split_large_cluster(members, edges, max_size=15)
        assert all(len(c) <= 15 for c in result)


class TestScoreClusters:
    """Tests for cluster scoring and ranking."""

    def test_larger_cluster_scores_higher(self):
        from consolidation import score_cluster
        small = {"members": ["a", "b"], "max_recency": 1.0, "avg_salience": 0.5}
        large = {"members": ["a", "b", "c", "d"], "max_recency": 1.0, "avg_salience": 0.5}
        assert score_cluster(large) > score_cluster(small)


class TestWriteMergeResult:
    """Tests for writing merged data_point and supersedes edges."""

    def _make_db(self, tmp_path):
        from unittest.mock import patch as p

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with p("storage.get_db_path", return_value=db_path), \
             p("storage.get_memory_dir", return_value=tmp_path):
            return ensure_db()

    def test_merge_creates_new_data_point(self, tmp_path):
        from consolidation import write_merge_result
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.6))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="b", scope="global", salience=0.7))
        conn.commit()

        new_id = write_merge_result(conn, "merged fact", [id1, id2], entities=["Redis"], certainty=4, scope="global")
        conn.commit()

        merged = conn.execute("SELECT content, source_type, consolidated FROM data_points WHERE id = ?", (new_id,)).fetchone()
        assert merged[0] == "merged fact"
        assert merged[1] == "consolidation"
        assert merged[2] == 1, "Merged data_point should have consolidated=1 (decay protection)"
        conn.close()

    def test_merge_creates_supersedes_edges(self, tmp_path):
        from consolidation import write_merge_result
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.6))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="b", scope="global", salience=0.7))
        conn.commit()

        new_id = write_merge_result(conn, "merged", [id1, id2], entities=[], certainty=3, scope="global")
        conn.commit()

        edges = conn.execute(
            "SELECT target FROM edges WHERE source = ? AND type = 'supersedes'", (new_id,)
        ).fetchall()
        edge_targets = {e[0] for e in edges}
        assert id1 in edge_targets
        assert id2 in edge_targets
        conn.close()

    def test_merge_soft_deletes_originals(self, tmp_path):
        from consolidation import write_merge_result
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.6))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="b", scope="global", salience=0.7))
        conn.commit()

        write_merge_result(conn, "merged", [id1, id2], entities=[], certainty=3, scope="global")
        conn.commit()

        for dp_id in [id1, id2]:
            sal = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()[0]
            assert sal == 0.0, f"Original {dp_id} should be soft-deleted"
        conn.close()

    def test_merge_inherits_max_certainty(self, tmp_path):
        """Merged data_point gets max(certainty) from members."""
        from consolidation import write_merge_result
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.6, certainty=2))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="b", scope="global", salience=0.7, certainty=4))
        conn.commit()

        new_id = write_merge_result(conn, "merged", [id1, id2], entities=[], certainty=4, scope="global")
        conn.commit()

        cert = conn.execute("SELECT certainty FROM data_points WHERE id = ?", (new_id,)).fetchone()[0]
        assert cert == 4
        conn.close()

    def test_merge_salience_boost(self, tmp_path):
        """Merged data_point gets max(salience) + 0.05, capped at 1.0."""
        from consolidation import write_merge_result
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.6))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="b", scope="global", salience=0.7))
        conn.commit()

        new_id = write_merge_result(conn, "merged", [id1, id2], entities=[], scope="global")
        conn.commit()

        sal = conn.execute("SELECT salience FROM data_points WHERE id = ?", (new_id,)).fetchone()[0]
        assert sal == 0.75  # max(0.6, 0.7) + 0.05
        conn.close()


class TestMergeCluster:
    """Tests for LLM merge/skip with mocked subprocess."""

    def _make_db(self, tmp_path):
        from unittest.mock import patch as p

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with p("storage.get_db_path", return_value=db_path), \
             p("storage.get_memory_dir", return_value=tmp_path):
            return ensure_db()

    def test_merge_decision_returns_merge(self, tmp_path):
        """Mocked LLM returns MERGE decision."""
        from consolidation import merge_cluster
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="use Redis", scope="global", salience=0.8))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="use Redis cache", scope="global", salience=0.7))
        conn.commit()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "decision": "MERGE",
            "fact": "Use Redis for caching",
            "entities": ["Redis"],
            "reason": "Redundant memories about Redis"
        })

        with patch("subprocess.run", return_value=mock_result):
            result = merge_cluster(conn, [id1, id2], model="sonnet")

        assert result["decision"] == "MERGE"
        assert result["fact"] == "Use Redis for caching"
        conn.close()

    def test_skip_decision_returns_skip(self, tmp_path):
        """Mocked LLM returns SKIP decision."""
        from consolidation import merge_cluster
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="use Redis", scope="global", salience=0.8))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="switched to Memcached", scope="global", salience=0.7))
        conn.commit()

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "decision": "SKIP",
            "fact": "",
            "entities": [],
            "reason": "Decision reversal - keep history"
        })

        with patch("subprocess.run", return_value=mock_result):
            result = merge_cluster(conn, [id1, id2], model="sonnet")

        assert result["decision"] == "SKIP"
        conn.close()

    def test_llm_error_returns_skip(self, tmp_path):
        """LLM process error returns SKIP."""
        from consolidation import merge_cluster
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.5))
        conn.commit()

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "Error"

        with patch("subprocess.run", return_value=mock_result):
            result = merge_cluster(conn, [id1], model="sonnet")

        assert result["decision"] == "SKIP"
        conn.close()

    def test_timeout_returns_skip(self, tmp_path):
        """LLM timeout returns SKIP."""
        import subprocess as sp

        from consolidation import merge_cluster
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.5))
        conn.commit()

        with patch("subprocess.run", side_effect=sp.TimeoutExpired("claude", 120)):
            result = merge_cluster(conn, [id1], model="sonnet")

        assert result["decision"] == "SKIP"
        conn.close()


class TestRunConsolidation:
    """Tests for the full consolidation pipeline."""

    def _make_db(self, tmp_path):
        from unittest.mock import patch as p

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with p("storage.get_db_path", return_value=db_path), \
             p("storage.get_memory_dir", return_value=tmp_path):
            return ensure_db()

    def test_dry_run_does_not_merge(self, tmp_path):
        """Dry run finds clusters but does not call merge or write."""
        from consolidation import run_consolidation
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.5))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="b", scope="global", salience=0.5))
        conn.commit()

        with patch("consolidation.find_clusters", return_value=[{"members": [id1, id2]}]):
            stats = run_consolidation(conn, settings={"consolidation": {}}, dry_run=True)

        assert stats["clusters_found"] == 1
        assert stats["clusters_merged"] == 0
        conn.close()

    def test_merge_flow(self, tmp_path):
        """Full merge flow: find clusters -> merge -> write result."""
        from consolidation import run_consolidation
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.5))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="b", scope="global", salience=0.5))
        conn.commit()

        mock_merge = MagicMock(return_value={
            "decision": "MERGE", "fact": "merged a+b", "entities": [], "reason": "redundant"
        })

        with patch("consolidation.find_clusters", return_value=[{"members": [id1, id2]}]), \
             patch("consolidation.merge_cluster", mock_merge):
            stats = run_consolidation(conn, settings={"consolidation": {}})

        assert stats["clusters_merged"] == 1
        assert stats["memories_consolidated"] == 2

        merged = conn.execute(
            "SELECT content FROM data_points WHERE source_type = 'consolidation'"
        ).fetchone()
        assert merged is not None
        conn.close()

    def test_skip_flow(self, tmp_path):
        """SKIP clusters are counted but no writes happen."""
        from consolidation import run_consolidation
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.5))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="b", scope="global", salience=0.5))
        conn.commit()

        mock_merge = MagicMock(return_value={
            "decision": "SKIP", "reason": "evolving understanding"
        })

        with patch("consolidation.find_clusters", return_value=[{"members": [id1, id2]}]), \
             patch("consolidation.merge_cluster", mock_merge):
            stats = run_consolidation(conn, settings={"consolidation": {}})

        assert stats["clusters_skipped"] == 1
        assert stats["clusters_merged"] == 0
        conn.close()

    def test_backfill_uses_higher_cluster_cap(self, tmp_path):
        """Backfill mode uses backfillMaxClusters setting."""
        from consolidation import run_consolidation
        from memory_utils import DEFAULT_SETTINGS
        conn = self._make_db(tmp_path)

        with patch("consolidation.find_clusters", return_value=[]) as mock_find:
            run_consolidation(conn, settings=DEFAULT_SETTINGS, backfill=True)

        call_kwargs = mock_find.call_args
        assert call_kwargs[1]["max_clusters"] == DEFAULT_SETTINGS["consolidation"]["backfillMaxClusters"]
        conn.close()

    def test_lock_prevents_concurrent_runs(self, tmp_path):
        """run_consolidation returns early with skipped_reason when lock is held."""
        from consolidation import run_consolidation
        conn = self._make_db(tmp_path)

        lock_dir = tmp_path / ".consolidation-lock"
        lock_dir.mkdir()
        (lock_dir / "pid").write_text(str(os.getpid()))

        with patch("memory_utils.get_memory_dir", return_value=tmp_path):
            stats = run_consolidation(conn, settings={"consolidation": {}})

        assert stats.get("skipped_reason") == "lock"
        assert stats["clusters_merged"] == 0
        (lock_dir / "pid").unlink()
        lock_dir.rmdir()
        conn.close()

    def test_empty_fact_skipped(self, tmp_path):
        """Clusters where LLM returns empty fact are skipped."""
        from consolidation import run_consolidation
        from storage import DataPointRow, insert_data_point
        conn = self._make_db(tmp_path)
        id1 = insert_data_point(conn, DataPointRow(type="memory", content="a", scope="global", salience=0.5))
        id2 = insert_data_point(conn, DataPointRow(type="memory", content="b", scope="global", salience=0.5))
        conn.commit()

        mock_merge = MagicMock(return_value={
            "decision": "MERGE", "fact": "  ", "entities": [], "reason": "dup"
        })

        with patch("consolidation.find_clusters", return_value=[{"members": [id1, id2]}]), \
             patch("consolidation.merge_cluster", mock_merge):
            stats = run_consolidation(conn, settings={"consolidation": {}})

        assert stats["clusters_skipped"] == 1
        assert stats["clusters_merged"] == 0
        conn.close()


class TestParseResponse:
    """Tests for LLM response parsing."""

    def test_valid_json(self):
        from consolidation import _parse_merge_response
        text = '{"decision": "MERGE", "fact": "test", "entities": [], "reason": "dup"}'
        result = _parse_merge_response(text)
        assert result["decision"] == "MERGE"

    def test_json_embedded_in_text(self):
        from consolidation import _parse_merge_response
        text = 'Here is my response:\n{"decision": "SKIP", "fact": "", "entities": [], "reason": "different"}\nDone.'
        result = _parse_merge_response(text)
        assert result["decision"] == "SKIP"

    def test_invalid_json_returns_skip(self):
        from consolidation import _parse_merge_response
        result = _parse_merge_response("not json at all")
        assert result["decision"] == "SKIP"


class TestBuildMergePrompt:
    """Tests for merge prompt construction."""

    def test_prompt_includes_members(self):
        from consolidation import _build_merge_prompt
        members = [
            {"id": "1", "content": "Redis is fast", "created_at": "2026-01-01T00:00:00Z", "entities": '["Redis"]'},
            {"id": "2", "content": "Redis provides caching", "created_at": "2026-01-02T00:00:00Z", "entities": '["Redis"]'},
        ]
        prompt = _build_merge_prompt(members)
        assert "Redis is fast" in prompt
        assert "Redis provides caching" in prompt
        assert "MERGE" in prompt
        assert "SKIP" in prompt


class TestConnectedComponents:
    """Tests for the union-find _connected_components algorithm."""

    def test_single_pair(self):
        from consolidation import _connected_components
        result = _connected_components([("a", "b", 0.9)], ["a", "b"])
        members = [set(comp[0]) for comp in result]
        assert members == [{"a", "b"}]

    def test_disjoint_pairs(self):
        from consolidation import _connected_components
        result = _connected_components(
            [("a", "b", 0.9), ("c", "d", 0.85)], ["a", "b", "c", "d"]
        )
        members = sorted([set(comp[0]) for comp in result], key=len)
        assert len(members) == 2
        assert {"a", "b"} in members
        assert {"c", "d"} in members

    def test_transitive_merge(self):
        from consolidation import _connected_components
        result = _connected_components(
            [("a", "b", 0.9), ("b", "c", 0.85)], ["a", "b", "c"]
        )
        members = [set(comp[0]) for comp in result]
        assert len(members) == 1
        assert members[0] == {"a", "b", "c"}

    def test_empty_input(self):
        from consolidation import _connected_components
        result = _connected_components([], [])
        assert result == []

    def test_complex_graph(self):
        """Multiple pairs forming exactly 2 components."""
        from consolidation import _connected_components
        pairs = [
            ("a", "b", 0.9),
            ("b", "c", 0.85),
            ("d", "e", 0.88),
            ("e", "f", 0.92),
        ]
        result = _connected_components(pairs, ["a", "b", "c", "d", "e", "f"])
        members = sorted([set(comp[0]) for comp in result], key=len)
        assert len(members) == 2
        assert {"a", "b", "c"} in members
        assert {"d", "e", "f"} in members


class TestEnrichClusterMetadata:
    """Tests for _enrich_cluster_metadata populating avg_salience and max_recency."""

    def test_avg_salience_computed(self, shared_db):
        from consolidation import _enrich_cluster_metadata
        from storage import DataPointRow, insert_data_point

        id1 = insert_data_point(shared_db, DataPointRow(type="memory", content="a", scope="global", salience=0.4))
        id2 = insert_data_point(shared_db, DataPointRow(type="memory", content="b", scope="global", salience=0.8))
        shared_db.commit()

        cluster = {"members": [id1, id2], "similarities": []}
        _enrich_cluster_metadata(shared_db, cluster)

        assert cluster["avg_salience"] == pytest.approx(0.6)

    def test_max_recency_recent(self, shared_db):
        """A data_point created now should have max_recency close to 1.0."""
        from consolidation import _enrich_cluster_metadata
        from storage import DataPointRow, insert_data_point

        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        id1 = insert_data_point(shared_db, DataPointRow(
            type="memory", content="recent", scope="global", salience=0.5, created_at=now,
        ))
        shared_db.commit()

        cluster = {"members": [id1], "similarities": []}
        _enrich_cluster_metadata(shared_db, cluster)

        assert cluster["max_recency"] > 0.99

    def test_max_recency_old(self, shared_db):
        """A data_point created 180 days ago should have max_recency ~0.507."""
        from consolidation import _enrich_cluster_metadata
        from storage import DataPointRow, insert_data_point

        old_ts = (datetime.now(timezone.utc) - timedelta(days=180)).isoformat().replace("+00:00", "Z")
        id1 = insert_data_point(shared_db, DataPointRow(
            type="memory", content="old", scope="global", salience=0.5, created_at=old_ts,
        ))
        shared_db.commit()

        cluster = {"members": [id1], "similarities": []}
        _enrich_cluster_metadata(shared_db, cluster)

        expected = max(0.0, 1.0 - 180 / 365.0)
        assert cluster["max_recency"] == pytest.approx(expected, abs=0.02)

    def test_picks_most_recent_timestamp(self, shared_db):
        """max_recency should reflect the most recent member, not the oldest."""
        from consolidation import _enrich_cluster_metadata
        from storage import DataPointRow, insert_data_point

        old_ts = (datetime.now(timezone.utc) - timedelta(days=300)).isoformat().replace("+00:00", "Z")
        new_ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        id_old = insert_data_point(shared_db, DataPointRow(
            type="memory", content="old", scope="global", salience=0.5, created_at=old_ts,
        ))
        id_new = insert_data_point(shared_db, DataPointRow(
            type="memory", content="new", scope="global", salience=0.5, created_at=new_ts,
        ))
        shared_db.commit()

        cluster = {"members": [id_old, id_new], "similarities": []}
        _enrich_cluster_metadata(shared_db, cluster)

        assert cluster["max_recency"] > 0.99


class TestGetExcludedPairs:
    """Tests for _get_excluded_pairs returning supersedes/contradicts pairs."""

    def test_returns_contradicts_pairs(self, shared_db):
        from consolidation import _get_excluded_pairs
        from storage import DataPointRow, EdgeRow, insert_data_point, insert_edge

        id1 = insert_data_point(shared_db, DataPointRow(type="memory", content="a", scope="global", salience=0.5))
        id2 = insert_data_point(shared_db, DataPointRow(type="memory", content="b", scope="global", salience=0.5))
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        insert_edge(shared_db, EdgeRow(source=id1, target=id2, type="contradicts", created_at=now))
        shared_db.commit()

        excluded = _get_excluded_pairs(shared_db, [id1, id2])
        assert (id1, id2) in excluded

    def test_returns_supersedes_pairs(self, shared_db):
        from consolidation import _get_excluded_pairs
        from storage import DataPointRow, EdgeRow, insert_data_point, insert_edge

        id1 = insert_data_point(shared_db, DataPointRow(type="memory", content="a", scope="global", salience=0.5))
        id2 = insert_data_point(shared_db, DataPointRow(type="memory", content="b", scope="global", salience=0.5))
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        insert_edge(shared_db, EdgeRow(source=id1, target=id2, type="supersedes", created_at=now))
        shared_db.commit()

        excluded = _get_excluded_pairs(shared_db, [id1, id2])
        assert (id1, id2) in excluded

    def test_expired_edges_excluded(self, shared_db):
        """Edges with valid_to set should NOT appear in excluded pairs."""
        from consolidation import _get_excluded_pairs
        from storage import DataPointRow, EdgeRow, insert_data_point, insert_edge

        id1 = insert_data_point(shared_db, DataPointRow(type="memory", content="a", scope="global", salience=0.5))
        id2 = insert_data_point(shared_db, DataPointRow(type="memory", content="b", scope="global", salience=0.5))
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        insert_edge(shared_db, EdgeRow(source=id1, target=id2, type="contradicts", created_at=now, valid_to=past))
        shared_db.commit()

        excluded = _get_excluded_pairs(shared_db, [id1, id2])
        assert (id1, id2) not in excluded

    def test_empty_active_ids(self, shared_db):
        from consolidation import _get_excluded_pairs
        excluded = _get_excluded_pairs(shared_db, [])
        assert excluded == set()
