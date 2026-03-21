#!/usr/bin/env python3
"""
Vector embedding and semantic search for Claude Code Memory System.

Provides FastEmbed-based text embeddings, sqlite-vec virtual table management,
chunk indexing, and scored memory retrieval. All embedding dependencies are
optional -- the module degrades gracefully when fastembed or sqlite-vec are
not installed.

Requirements: Python 3.9+
Optional: fastembed, sqlite-vec
"""

import math
import struct
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from storage import ChunkRow, VEC_CHUNKS_DDL, _row_to_chunk, _CHUNK_COLUMNS  # noqa: E402

try:
    from fastembed import TextEmbedding
    HAS_FASTEMBED = True
except ImportError:
    HAS_FASTEMBED = False

try:
    import sqlite_vec
    HAS_SQLITE_VEC = True
except ImportError:
    HAS_SQLITE_VEC = False

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384
VEC_SIM_WEIGHT = 0.50
RECENCY_WEIGHT = 0.25
SALIENCE_WEIGHT = 0.25
RECENCY_DECAY = 0.05
VEC_BOOST_RATE = 3.0
DEFAULT_TOP_K = 10

__all__ = [
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
    "VEC_SIM_WEIGHT",
    "RECENCY_WEIGHT",
    "SALIENCE_WEIGHT",
    "RECENCY_DECAY",
    "VEC_BOOST_RATE",
    "DEFAULT_TOP_K",
    "HAS_FASTEMBED",
    "HAS_SQLITE_VEC",
    "ScoredChunk",
    "ensure_vec_table",
    "embed_text",
    "embed_batch",
    "index_chunks",
    "index_chunks_by_source",
    "delete_vec_chunks",
    "search_similar",
    "reindex_changed_files",
    "reindex_all",
    "score_memory",
]


@dataclass
class ScoredChunk:
    """A chunk with its composite retrieval score and vector similarity."""
    chunk: ChunkRow
    score: float
    vec_similarity: float


_model = None


def _get_model():
    """Return the singleton TextEmbedding model, or None if unavailable."""
    global _model
    if not HAS_FASTEMBED:
        return None
    if _model is None:
        _model = TextEmbedding(EMBEDDING_MODEL)
    return _model


def ensure_vec_table(conn) -> bool:
    """Load sqlite-vec extension and create vec_chunks virtual table.

    Returns True on success, False if sqlite-vec is not available or extension
    loading is not permitted by the SQLite build.
    """
    if not HAS_SQLITE_VEC:
        return False
    try:
        sqlite_vec.load(conn)
    except Exception:
        return False
    conn.executescript(VEC_CHUNKS_DDL)
    conn.commit()
    return True


def embed_text(text: str) -> list:
    """Embed a single text string. Returns [] if fastembed unavailable."""
    model = _get_model()
    if model is None:
        return []
    result = list(model.embed([text]))
    vec = result[0]
    if hasattr(vec, "tolist"):
        return [float(v) for v in vec.tolist()]
    return [float(v) for v in vec]


def embed_batch(texts: list) -> list:
    """Embed a list of texts. Returns [] if texts is empty or fastembed unavailable."""
    if not texts:
        return []
    model = _get_model()
    if model is None:
        return []
    results = []
    for vec in model.embed(texts):
        if hasattr(vec, "tolist"):
            results.append([float(v) for v in vec.tolist()])
        else:
            results.append([float(v) for v in vec])
    return results


def _serialize_vector(vec: list) -> bytes:
    """Pack a list of floats into a binary blob for sqlite-vec."""
    return struct.pack(f"{len(vec)}f", *vec)


def _days_since(iso_date: Optional[str], fallback: Optional[str] = None) -> float:
    """Return days elapsed since an ISO date string (UTC). Returns 0.0 if both None."""
    date_str = iso_date or fallback
    if date_str is None:
        return 0.0
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0.0, delta.total_seconds() / 86400.0)
    except (ValueError, TypeError):
        return 0.0


def score_memory(vec_distance: float, chunk: ChunkRow) -> float:
    """Compute a composite retrieval score from vector distance + recency + salience."""
    vec_sim = max(0.0, 1.0 - vec_distance)
    boosted = 1.0 - math.exp(-VEC_BOOST_RATE * vec_sim)

    last_accessed = getattr(chunk, "last_accessed", None)
    created_at = getattr(chunk, "created_at", None)
    days = _days_since(last_accessed, created_at)
    recency = math.exp(-RECENCY_DECAY * days)

    salience = chunk.salience if chunk.salience is not None else 1.0

    return VEC_SIM_WEIGHT * boosted + RECENCY_WEIGHT * recency + SALIENCE_WEIGHT * salience


def index_chunks(conn, chunk_ids: list) -> None:
    """Embed chunks and insert into vec_chunks. Skips chunks already indexed with same hash."""
    if not chunk_ids or not HAS_FASTEMBED:
        return

    placeholders = ",".join("?" * len(chunk_ids))
    rows = conn.execute(
        f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE id IN ({placeholders})",
        chunk_ids,
    ).fetchall()
    chunks = [_row_to_chunk(r) for r in rows]

    existing = conn.execute(
        f"SELECT vc.chunk_id, c.content_hash FROM vec_chunks vc JOIN chunks c ON vc.chunk_id = c.id WHERE vc.chunk_id IN ({placeholders})",
        chunk_ids,
    ).fetchall()
    existing_map = {row[0]: row[1] for row in existing}

    to_embed = [c for c in chunks if c.id not in existing_map or existing_map.get(c.id) != c.content_hash]

    if not to_embed:
        return

    stale_ids = [c.id for c in to_embed if c.id in existing_map]
    if stale_ids:
        delete_vec_chunks(conn, stale_ids)

    contents = [c.content for c in to_embed]
    vectors = embed_batch(contents)

    for chunk, vec in zip(to_embed, vectors):
        conn.execute(
            "INSERT INTO vec_chunks (embedding, chunk_id, source_type) VALUES (?, ?, ?)",
            (_serialize_vector(vec), chunk.id, chunk.source_type),
        )
    conn.commit()


def index_chunks_by_source(conn, source_files: list) -> None:
    """Index all chunks from the given source files."""
    if not source_files or not HAS_FASTEMBED:
        return
    placeholders = ",".join("?" * len(source_files))
    rows = conn.execute(
        f"SELECT id FROM chunks WHERE source_file IN ({placeholders})",
        source_files,
    ).fetchall()
    chunk_ids = [r[0] for r in rows]
    if chunk_ids:
        index_chunks(conn, chunk_ids)


def delete_vec_chunks(conn, chunk_ids: list) -> None:
    """Delete vec_chunks rows for the given chunk IDs."""
    for chunk_id in chunk_ids:
        conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (chunk_id,))
    conn.commit()


def search_similar(conn, query: str, top_k: int = DEFAULT_TOP_K, scope: Optional[str] = None) -> list:
    """Search for chunks semantically similar to query.

    Returns a sorted list of ScoredChunk (highest score first), limited to top_k.
    Returns [] if fastembed or sqlite-vec are unavailable, or if query cannot be embedded.
    """
    if not HAS_FASTEMBED or not HAS_SQLITE_VEC:
        return []

    query_vec = embed_text(query)
    if not query_vec:
        return []

    fetch_k = top_k * 3
    qualified = ", ".join(f"c.{col.strip()}" for col in _CHUNK_COLUMNS.split(","))

    rows = conn.execute(
        f"SELECT vc.distance, {qualified} FROM vec_chunks vc JOIN chunks c ON vc.chunk_id = c.id WHERE vc.embedding MATCH ? ORDER BY vc.distance LIMIT ?",
        (_serialize_vector(query_vec), fetch_k),
    ).fetchall()

    results = []
    for row in rows:
        distance = row[0]
        chunk = _row_to_chunk(row[1:])
        if scope is not None and chunk.scope != scope:
            continue
        vec_sim = max(0.0, 1.0 - distance)
        scored = ScoredChunk(chunk=chunk, score=score_memory(distance, chunk), vec_similarity=vec_sim)
        results.append(scored)

    results.sort(key=lambda x: x.score, reverse=True)
    return results[:top_k]


def reindex_changed_files(conn, changed_files: list) -> None:
    """Re-embed chunks from changed files (delete old vectors then re-index)."""
    for source_file in changed_files:
        rows = conn.execute(
            "SELECT id FROM chunks WHERE source_file = ?", (source_file,)
        ).fetchall()
        chunk_ids = [r[0] for r in rows]
        if chunk_ids:
            delete_vec_chunks(conn, chunk_ids)
            index_chunks_by_source(conn, [source_file])


def reindex_all(conn) -> None:
    """Wipe and rebuild the entire vec_chunks table from all chunks in the DB."""
    if not HAS_FASTEMBED:
        return
    success = ensure_vec_table(conn)
    if not success:
        return
    conn.execute("DELETE FROM vec_chunks")
    conn.commit()
    rows = conn.execute("SELECT id FROM chunks").fetchall()
    chunk_ids = [r[0] for r in rows]
    if chunk_ids:
        index_chunks(conn, chunk_ids)
