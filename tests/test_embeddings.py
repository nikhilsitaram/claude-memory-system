"""Tests for embeddings.py -- vector embedding and semantic search.

Tests cover: FastEmbed wrapper, batch embedding, vec_data population,
content hash skip, scoring function, search_similar, reindex, and
graceful degradation when optional dependencies are missing.

All tests mock the embedding model to avoid downloading models in CI.
One integration test is marked skipif for local validation with real model.

Run with: python3 -m pytest tests/test_embeddings.py -v
"""

import math
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from embeddings import (
    EMBEDDING_DIM,
    HAS_FASTEMBED,
    RECENCY_WEIGHT,
    SALIENCE_WEIGHT,
    VEC_BOOST_RATE,
    VEC_SIM_WEIGHT,
    ScoredDataPoint,
    delete_vec_data,
    embed_batch,
    embed_text,
    ensure_vec_table,
    index_data_points,
    reindex_all,
    score_memory,
    search_similar,
)
from storage import (
    DataPointRow,
    close_db,
    ensure_db,
    insert_data_point,
)


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
def db_with_vec(db):
    """DB with sqlite-vec loaded and vec_data table created.

    Skips test if sqlite-vec is not installed.
    """
    success = ensure_vec_table(db)
    if not success:
        pytest.skip("sqlite-vec not available")
    yield db


def _make_deterministic_vector(text: str) -> list[float]:
    """Generate a deterministic 384-dim vector from text for testing."""
    h = hash(text) & 0xFFFFFFFF
    return [float((h + i) % 100) / 100.0 for i in range(EMBEDDING_DIM)]


@pytest.fixture
def mock_embedder():
    """Mock FastEmbed model to return deterministic vectors.

    Returns plain Python lists (not numpy arrays) so the test suite
    works without numpy/fastembed installed.
    """
    def _mock_embed(texts):
        for text in texts:
            yield _make_deterministic_vector(text)

    mock_model = mock.MagicMock()
    mock_model.embed.side_effect = _mock_embed

    with mock.patch("embeddings._get_model", return_value=mock_model):
        yield mock_model


@pytest.fixture
def sample_chunks(db):
    """Insert 5 sample data_points and return their IDs."""
    now = datetime.now(timezone.utc)
    data_points = [
        DataPointRow(
            type="memory",
            content="Use pytest tmp_path fixture for filesystem isolation",
            source_type="ltm",
            scope="global",
            entry_type="pattern",
            created_at=(now - timedelta(days=1)).isoformat(),
        ),
        DataPointRow(
            type="memory",
            content="SQLite WAL mode enables concurrent reads from multiple tabs",
            source_type="ltm",
            scope="global",
            entry_type="gotcha",
            created_at=(now - timedelta(days=5)).isoformat(),
        ),
        DataPointRow(
            type="memory",
            content="FastEmbed produces 384-dim CPU embeddings without API keys",
            source_type="ltm",
            scope="claude-memory-system",
            entry_type="design",
            created_at=(now - timedelta(days=2)).isoformat(),
        ),
        DataPointRow(
            type="memory",
            content="Implemented vector search with sqlite-vec virtual table",
            source_type="daily",
            scope="claude-memory-system",
            entry_type="implement",
            created_at=(now - timedelta(days=0)).isoformat(),
        ),
        DataPointRow(
            type="memory",
            content="Decay runs after synthesis to archive stale entries",
            source_type="daily",
            scope="global",
            entry_type="implement",
            created_at=(now - timedelta(days=10)).isoformat(),
        ),
    ]
    ids = []
    for dp in data_points:
        dp_id = insert_data_point(db, dp)
        ids.append(dp_id)
    db.commit()
    return ids


class TestEmbedText:
    def test_returns_384_dim_vector(self, mock_embedder):
        vec = embed_text("hello world")
        assert len(vec) == EMBEDDING_DIM
        assert all(isinstance(v, float) for v in vec)

    def test_returns_empty_list_when_no_fastembed(self):
        with mock.patch("embeddings.HAS_FASTEMBED", False):
            vec = embed_text("hello world")
            assert vec == []

    def test_embed_batch_returns_list_of_vectors(self, mock_embedder):
        texts = ["hello", "world", "test"]
        vecs = embed_batch(texts)
        assert len(vecs) == 3
        assert all(len(v) == EMBEDDING_DIM for v in vecs)

    def test_embed_batch_empty_input(self, mock_embedder):
        vecs = embed_batch([])
        assert vecs == []


class TestGracefulDegradation:
    def test_embed_text_no_fastembed(self):
        with mock.patch("embeddings.HAS_FASTEMBED", False):
            assert embed_text("test") == []

    def test_embed_batch_no_fastembed(self):
        with mock.patch("embeddings.HAS_FASTEMBED", False):
            assert embed_batch(["a", "b"]) == []

    def test_ensure_vec_table_no_sqlite_vec(self, db):
        with mock.patch("embeddings.HAS_SQLITE_VEC", False):
            assert ensure_vec_table(db) is False

    def test_search_similar_no_fastembed(self, db):
        with mock.patch("embeddings.HAS_FASTEMBED", False):
            results = search_similar(db, "test query")
            assert results == []

    def test_reindex_all_no_fastembed(self, db):
        with mock.patch("embeddings.HAS_FASTEMBED", False):
            reindex_all(db)

    def test_search_similar_no_sqlite_vec(self, db):
        with mock.patch("embeddings.HAS_SQLITE_VEC", False):
            results = search_similar(db, "test query")
            assert results == []


class TestScoring:
    def test_score_memory_perfect_match(self):
        chunk = DataPointRow(
            type="memory", content="test", source_type="ltm",
            created_at=datetime.now(timezone.utc).isoformat(),
            salience=1.0,
        )
        score = score_memory(0.0, chunk)
        boosted = 1 - math.exp(-VEC_BOOST_RATE * 1.0)
        expected = VEC_SIM_WEIGHT * boosted + RECENCY_WEIGHT * 1.0 + SALIENCE_WEIGHT * 1.0
        assert abs(score - expected) < 0.01

    def test_score_memory_zero_similarity(self):
        chunk = DataPointRow(
            type="memory", content="test", source_type="ltm",
            created_at=datetime.now(timezone.utc).isoformat(),
            salience=1.0,
        )
        score = score_memory(1.0, chunk)
        assert score < 0.6

    def test_score_memory_recency_decay(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        chunk_old = DataPointRow(
            type="memory", content="test", source_type="ltm",
            created_at=old_date, salience=1.0,
        )
        new_date = datetime.now(timezone.utc).isoformat()
        chunk_new = DataPointRow(
            type="memory", content="test", source_type="ltm",
            created_at=new_date, salience=1.0,
        )
        score_old = score_memory(0.2, chunk_old)
        score_new = score_memory(0.2, chunk_new)
        assert score_new > score_old

    def test_score_memory_fallback_values(self):
        chunk = DataPointRow(
            type="memory", content="test", source_type="ltm",
            last_accessed=None, salience=None, created_at=None,
        )
        score = score_memory(0.3, chunk)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0


@pytest.mark.skipif(not HAS_FASTEMBED, reason="fastembed not installed")
class TestFastEmbedIntegration:
    """Local-only integration test with real FastEmbed model."""

    def test_real_embed_text(self):
        vec = embed_text("memory system with sqlite storage")
        assert len(vec) == EMBEDDING_DIM
        assert all(isinstance(v, float) for v in vec)


# ============================================================================
# A5: ScoredDataPoint, index_data_points, search_similar (vec_data), etc.
# ============================================================================


class TestScoredDataPoint:
    """Tests for the ScoredDataPoint dataclass."""

    def test_scored_data_point_has_correct_fields(self):
        """ScoredDataPoint wraps a DataPointRow with score and vec_similarity."""
        dp = DataPointRow(type="memory", content="test fact", scope="global")
        scored = ScoredDataPoint(data_point=dp, score=0.8, vec_similarity=0.7)
        assert scored.data_point.content == "test fact"
        assert scored.score == 0.8
        assert scored.vec_similarity == 0.7

    def test_scored_data_point_accepts_data_point_row(self):
        """ScoredDataPoint.data_point stores a DataPointRow."""
        dp = DataPointRow(type="entity", name="Python", scope="global")
        scored = ScoredDataPoint(data_point=dp, score=0.5, vec_similarity=0.4)
        assert isinstance(scored.data_point, DataPointRow)
        assert scored.data_point.type == "entity"


class TestIndexDataPoints:
    """Tests for index_data_points (v3 schema)."""

    @pytest.fixture
    def db_v3_with_vec(self, db_dir):
        """DB with v3 schema and sqlite-vec loaded; skips if vec unavailable."""
        conn = ensure_db()
        success = ensure_vec_table(conn)
        if not success:
            pytest.skip("sqlite-vec not available")
        yield conn
        close_db(conn)

    @pytest.fixture
    def sample_data_points(self, db_v3_with_vec):
        """Insert sample data_points and return their IDs."""
        conn = db_v3_with_vec
        ids = []
        for content in [
            "Use pytest tmp_path for filesystem isolation",
            "SQLite WAL mode enables concurrent reads",
            "FastEmbed produces 384-dim CPU embeddings",
        ]:
            dp_id = insert_data_point(
                conn,
                DataPointRow(type="memory", content=content, scope="global"),
            )
            ids.append(dp_id)
        conn.commit()
        return ids

    def test_index_data_points_inserts_to_vec_data(
        self, db_v3_with_vec, mock_embedder, sample_data_points
    ):
        """index_data_points writes embeddings to vec_data table."""
        index_data_points(db_v3_with_vec, sample_data_points)
        count = db_v3_with_vec.execute("SELECT COUNT(*) FROM vec_data").fetchone()[0]
        assert count == len(sample_data_points)

    def test_index_data_points_skips_already_indexed(
        self, db_v3_with_vec, mock_embedder, sample_data_points
    ):
        """Calling index_data_points twice does not create duplicates."""
        index_data_points(db_v3_with_vec, sample_data_points[:2])
        index_data_points(db_v3_with_vec, sample_data_points[:2])
        count = db_v3_with_vec.execute("SELECT COUNT(*) FROM vec_data").fetchone()[0]
        assert count == 2

    def test_delete_vec_data_removes_rows(
        self, db_v3_with_vec, mock_embedder, sample_data_points
    ):
        """delete_vec_data removes specified IDs from vec_data."""
        index_data_points(db_v3_with_vec, sample_data_points)
        delete_vec_data(db_v3_with_vec, sample_data_points[:1])
        count = db_v3_with_vec.execute("SELECT COUNT(*) FROM vec_data").fetchone()[0]
        assert count == len(sample_data_points) - 1

    def test_search_similar_queries_vec_data(
        self, db_v3_with_vec, mock_embedder, sample_data_points
    ):
        """search_similar returns ScoredDataPoint list from vec_data."""
        index_data_points(db_v3_with_vec, sample_data_points)
        results = search_similar(db_v3_with_vec, "pytest testing patterns")
        assert len(results) > 0
        assert all(isinstance(r, ScoredDataPoint) for r in results)
        assert all(isinstance(r.data_point, DataPointRow) for r in results)

    def test_ensure_vec_table_creates_vec_data(self, db_v3_with_vec):
        """ensure_vec_table creates vec_data table."""
        tables = {
            row[0]
            for row in db_v3_with_vec.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'shadow')"
            ).fetchall()
        }
        assert "vec_data" in tables


class TestScoreMemoryWithDataPointRow:
    """Tests that score_memory works with DataPointRow."""

    def test_score_memory_with_data_point_row(self):
        """score_memory accepts DataPointRow."""
        dp = DataPointRow(
            type="memory",
            content="test",
            scope="global",
            created_at=datetime.now(timezone.utc).isoformat(),
            salience=1.0,
        )
        score = score_memory(0.0, dp)
        assert isinstance(score, float)
        assert score > 0


# =============================================================================
# A5: Hybrid Search (RRF) Tests
# =============================================================================


class TestHybridSearch:
    """Tests for RRF hybrid search combining FTS5 + vector KNN."""

    def _make_db_with_fts(self, tmp_path):
        from unittest.mock import patch

        from storage import DataPointRow, ensure_db, fts_insert, insert_data_point
        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path), \
             patch("memory_utils.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        dp1 = DataPointRow(type="memory", content="Redis cache requires explicit TTL", scope="global", salience=0.8)
        dp2 = DataPointRow(type="memory", content="SQLite WAL mode for concurrency", scope="global", salience=0.7)
        dp3 = DataPointRow(type="memory", content="Always use pytest fixtures", scope="global", salience=0.6)
        id1 = insert_data_point(conn, dp1)
        id2 = insert_data_point(conn, dp2)
        id3 = insert_data_point(conn, dp3)
        fts_insert(conn, id1, dp1.content, dp1.scope)
        fts_insert(conn, id2, dp2.content, dp2.scope)
        fts_insert(conn, id3, dp3.content, dp3.scope)
        conn.commit()
        return conn, [id1, id2, id3]

    def test_fts_only_fallback(self, tmp_path):
        """When vector search is unavailable, hybrid falls back to FTS5 BM25."""
        from unittest.mock import patch

        from embeddings import search_hybrid
        conn, ids = self._make_db_with_fts(tmp_path)
        with patch("embeddings.HAS_FASTEMBED", False), \
             patch("embeddings.HAS_SQLITE_VEC", False):
            results = search_hybrid(conn, "Redis cache", scope=None, top_k=5)
        assert len(results) >= 1
        assert results[0].data_point.id == ids[0]
        conn.close()

    def test_sql_fallback_when_no_fts_no_vector(self, tmp_path):
        """When both FTS5 and vector are unavailable, falls back to SQL ranked."""
        from unittest.mock import patch

        from embeddings import search_hybrid
        conn, ids = self._make_db_with_fts(tmp_path)
        with patch("embeddings.HAS_FASTEMBED", False), \
             patch("embeddings.HAS_SQLITE_VEC", False), \
             patch("embeddings._has_table", return_value=False):
            results = search_hybrid(conn, "cache", scope=None, top_k=5)
        assert len(results) >= 1
        conn.close()

    def test_results_ordered_by_score(self, tmp_path):
        """Results are sorted by composite score, highest first."""
        from unittest.mock import patch

        from embeddings import search_hybrid
        conn, ids = self._make_db_with_fts(tmp_path)
        with patch("embeddings.HAS_FASTEMBED", False):
            results = search_hybrid(conn, "Redis", scope=None, top_k=5)
        if len(results) >= 2:
            assert results[0].score >= results[1].score
        conn.close()

    def test_scope_filtering(self, tmp_path):
        """Scope parameter limits results to matching scope."""
        from unittest.mock import patch

        from embeddings import search_hybrid
        from storage import DataPointRow, fts_insert, insert_data_point
        conn, ids = self._make_db_with_fts(tmp_path)
        dp = DataPointRow(type="memory", content="Redis project-specific config", scope="my-proj", salience=0.9)
        dp_id = insert_data_point(conn, dp)
        fts_insert(conn, dp_id, dp.content, dp.scope)
        conn.commit()
        with patch("embeddings.HAS_FASTEMBED", False):
            results = search_hybrid(conn, "Redis", scope="my-proj", top_k=5)
        assert all(r.data_point.scope == "my-proj" for r in results)
        conn.close()

    def test_top_k_limits_results(self, tmp_path):
        """top_k parameter limits the number of results returned."""
        from unittest.mock import patch

        from embeddings import search_hybrid
        conn, ids = self._make_db_with_fts(tmp_path)
        with patch("embeddings.HAS_FASTEMBED", False):
            results = search_hybrid(conn, "cache SQLite pytest", scope=None, top_k=1)
        assert len(results) <= 1
        conn.close()


class TestCertaintyScoring:
    """Tests for certainty modulation in score_memory."""

    def test_certainty_modulates_score(self):
        from unittest.mock import MagicMock

        from embeddings import score_memory

        high_cert_dp = MagicMock(salience=0.5, last_accessed=None, created_at=None, certainty=5)
        low_cert_dp = MagicMock(salience=0.5, last_accessed=None, created_at=None, certainty=1)
        no_cert_dp = MagicMock(salience=0.5, last_accessed=None, created_at=None, certainty=None)

        high_score = score_memory(0.5, high_cert_dp)
        low_score = score_memory(0.5, low_cert_dp)
        neutral_score = score_memory(0.5, no_cert_dp)

        assert high_score > low_score, "Higher certainty should produce higher score"
        assert neutral_score > 0, "None certainty should not crash and produce a positive score"

    def test_certainty_none_is_neutral(self):
        """certainty=None should not modify salience (backward compatible)."""
        from unittest.mock import MagicMock

        from embeddings import score_memory

        dp_none = MagicMock(salience=0.8, last_accessed=None, created_at=None, certainty=None)
        dp_no_attr = MagicMock(salience=0.8, last_accessed=None, created_at=None, spec=[])
        del dp_no_attr.certainty

        score_none = score_memory(0.5, dp_none)
        score_no_attr = score_memory(0.5, dp_no_attr)

        assert score_none == score_no_attr, "None and missing certainty should produce same score"
