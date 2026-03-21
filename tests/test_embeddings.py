"""Tests for embeddings.py -- vector embedding and semantic search.

Tests cover: FastEmbed wrapper, batch embedding, vec_chunks population,
content hash skip, scoring function, search_similar, reindex, and
graceful degradation when optional dependencies are missing.

All tests mock the embedding model to avoid downloading models in CI.
One integration test is marked skipif for local validation with real model.

Run with: python3 -m pytest tests/test_embeddings.py -v
"""

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from storage import (
    ChunkRow,
    VEC_CHUNKS_DDL,
    close_db,
    ensure_db,
    insert_chunk,
)

from embeddings import (
    DEFAULT_TOP_K,
    EMBEDDING_DIM,
    EMBEDDING_MODEL,
    HAS_FASTEMBED,
    HAS_SQLITE_VEC,
    RECENCY_DECAY,
    RECENCY_WEIGHT,
    SALIENCE_WEIGHT,
    ScoredChunk,
    VEC_BOOST_RATE,
    VEC_SIM_WEIGHT,
    delete_vec_chunks,
    embed_batch,
    embed_text,
    ensure_vec_table,
    index_chunks,
    index_chunks_by_source,
    reindex_all,
    reindex_changed_files,
    score_memory,
    search_similar,
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
    """DB with sqlite-vec loaded and vec_chunks table created.

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
    """Insert 5 sample chunks and return their IDs."""
    now = datetime.now(timezone.utc)
    chunks = [
        ChunkRow(
            content="Use pytest tmp_path fixture for filesystem isolation",
            source_file="global-long-term-memory.md",
            source_type="ltm",
            section="## Key Patterns",
            scope="global",
            entry_type="pattern",
            chunk_index=0,
            created_at=(now - timedelta(days=1)).isoformat(),
        ),
        ChunkRow(
            content="SQLite WAL mode enables concurrent reads from multiple tabs",
            source_file="global-long-term-memory.md",
            source_type="ltm",
            section="## Key Learnings",
            scope="global",
            entry_type="gotcha",
            chunk_index=1,
            created_at=(now - timedelta(days=5)).isoformat(),
        ),
        ChunkRow(
            content="FastEmbed produces 384-dim CPU embeddings without API keys",
            source_file="claude-memory-system-long-term-memory.md",
            source_type="ltm",
            section="## Architecture",
            scope="claude-memory-system",
            entry_type="design",
            chunk_index=0,
            created_at=(now - timedelta(days=2)).isoformat(),
        ),
        ChunkRow(
            content="Implemented vector search with sqlite-vec virtual table",
            source_file="2026-03-20.md",
            source_type="daily",
            scope="claude-memory-system",
            entry_type="implement",
            chunk_index=0,
            created_at=(now - timedelta(days=0)).isoformat(),
        ),
        ChunkRow(
            content="Decay runs after synthesis to archive stale entries",
            source_file="2026-03-19.md",
            source_type="daily",
            scope="global",
            entry_type="implement",
            chunk_index=0,
            created_at=(now - timedelta(days=10)).isoformat(),
        ),
    ]
    ids = []
    for chunk in chunks:
        chunk_id = insert_chunk(db, chunk)
        ids.append(chunk_id)
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

    def test_index_chunks_no_fastembed(self, db):
        with mock.patch("embeddings.HAS_FASTEMBED", False):
            index_chunks(db, ["id1", "id2"])

    def test_reindex_all_no_fastembed(self, db):
        with mock.patch("embeddings.HAS_FASTEMBED", False):
            reindex_all(db)

    def test_search_similar_no_sqlite_vec(self, db):
        with mock.patch("embeddings.HAS_SQLITE_VEC", False):
            results = search_similar(db, "test query")
            assert results == []


class TestIndexChunks:
    def test_index_chunks_inserts_vectors(self, db_with_vec, mock_embedder, sample_chunks):
        index_chunks(db_with_vec, sample_chunks)
        count = db_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        assert count == len(sample_chunks)

    def test_index_chunks_skips_existing_same_hash(self, db_with_vec, mock_embedder, sample_chunks):
        index_chunks(db_with_vec, sample_chunks[:2])
        index_chunks(db_with_vec, sample_chunks[:2])
        count = db_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        assert count == 2

    def test_index_chunks_by_source(self, db_with_vec, mock_embedder, sample_chunks):
        index_chunks_by_source(db_with_vec, ["global-long-term-memory.md"])
        count = db_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        assert count == 2

    def test_delete_vec_chunks(self, db_with_vec, mock_embedder, sample_chunks):
        index_chunks(db_with_vec, sample_chunks)
        delete_vec_chunks(db_with_vec, sample_chunks[:2])
        count = db_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        assert count == 3


class TestScoring:
    def test_score_memory_perfect_match(self):
        chunk = ChunkRow(
            content="test", source_file="test.md", source_type="ltm",
            created_at=datetime.now(timezone.utc).isoformat(),
            salience=1.0,
        )
        score = score_memory(0.0, chunk)
        boosted = 1 - math.exp(-VEC_BOOST_RATE * 1.0)
        expected = VEC_SIM_WEIGHT * boosted + RECENCY_WEIGHT * 1.0 + SALIENCE_WEIGHT * 1.0
        assert abs(score - expected) < 0.01

    def test_score_memory_zero_similarity(self):
        chunk = ChunkRow(
            content="test", source_file="test.md", source_type="ltm",
            created_at=datetime.now(timezone.utc).isoformat(),
            salience=1.0,
        )
        score = score_memory(1.0, chunk)
        assert score < 0.6

    def test_score_memory_recency_decay(self):
        old_date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        chunk_old = ChunkRow(
            content="test", source_file="test.md", source_type="ltm",
            created_at=old_date, salience=1.0,
        )
        new_date = datetime.now(timezone.utc).isoformat()
        chunk_new = ChunkRow(
            content="test", source_file="test.md", source_type="ltm",
            created_at=new_date, salience=1.0,
        )
        score_old = score_memory(0.2, chunk_old)
        score_new = score_memory(0.2, chunk_new)
        assert score_new > score_old

    def test_score_memory_fallback_values(self):
        chunk = ChunkRow(
            content="test", source_file="test.md", source_type="ltm",
            last_accessed=None, salience=None, created_at=None,
        )
        score = score_memory(0.3, chunk)
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0


class TestSearchSimilar:
    def test_returns_scored_chunks(self, db_with_vec, mock_embedder, sample_chunks):
        index_chunks(db_with_vec, sample_chunks)
        results = search_similar(db_with_vec, "pytest fixtures for testing")
        assert len(results) > 0
        assert all(isinstance(r, ScoredChunk) for r in results)
        assert all(r.score >= 0 for r in results)

    def test_scope_filtering(self, db_with_vec, mock_embedder, sample_chunks):
        index_chunks(db_with_vec, sample_chunks)
        results = search_similar(db_with_vec, "testing", scope="global")
        assert all(r.chunk.scope == "global" for r in results)

    def test_respects_top_k(self, db_with_vec, mock_embedder, sample_chunks):
        index_chunks(db_with_vec, sample_chunks)
        results = search_similar(db_with_vec, "testing", top_k=2)
        assert len(results) <= 2

    def test_returns_empty_when_no_vectors(self, db_with_vec, mock_embedder):
        results = search_similar(db_with_vec, "anything")
        assert results == []


class TestReindex:
    def test_reindex_all(self, db_with_vec, mock_embedder, sample_chunks):
        reindex_all(db_with_vec)
        count = db_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        assert count == len(sample_chunks)

    def test_reindex_changed_files(self, db_with_vec, mock_embedder, sample_chunks):
        reindex_changed_files(db_with_vec, ["global-long-term-memory.md"])
        count = db_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        assert count == 2

    def test_reindex_changed_files_replaces_old_vectors(self, db_with_vec, mock_embedder, sample_chunks):
        index_chunks(db_with_vec, sample_chunks)
        count_before = db_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        reindex_changed_files(db_with_vec, ["global-long-term-memory.md"])
        count_after = db_with_vec.execute("SELECT COUNT(*) FROM vec_chunks").fetchone()[0]
        assert count_after == count_before


@pytest.mark.skipif(not HAS_FASTEMBED, reason="fastembed not installed")
class TestFastEmbedIntegration:
    """Local-only integration test with real FastEmbed model."""

    def test_real_embed_text(self):
        vec = embed_text("memory system with sqlite storage")
        assert len(vec) == EMBEDDING_DIM
        assert all(isinstance(v, float) for v in vec)
