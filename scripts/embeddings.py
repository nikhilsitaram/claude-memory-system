#!/usr/bin/env python3
"""
Vector embedding and semantic search for Claude Code Memory System.

Provides FastEmbed-based text embeddings, sqlite-vec virtual table management,
data_point indexing, and scored memory retrieval. All embedding dependencies are
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

from storage import (  # noqa: E402
    _DP_COLUMNS,
    VEC_DATA_DDL,
    DataPointRow,
    _row_to_data_point,
)

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
    "ScoredDataPoint",
    "ensure_vec_table",
    "embed_text",
    "embed_batch",
    "index_data_points",
    "index_data_points_by_source",
    "delete_vec_data",
    "search_similar",
    "search_hybrid",
    "RRF_K",
    "reindex_changed_files",
    "reindex_all",
    "score_memory",
]


@dataclass
class ScoredDataPoint:
    """A data_point with its composite retrieval score and vector similarity."""
    data_point: DataPointRow
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


def _has_table(conn, table_name: str) -> bool:
    """Return True if the given table (or virtual table) exists in the DB."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE name = ?", (table_name,)
    ).fetchone()
    return row is not None


def ensure_vec_table(conn) -> bool:
    """Load sqlite-vec extension and create vec_data virtual table.

    Returns True on success, False if sqlite-vec is not available or extension
    loading is not permitted by the SQLite build.
    """
    if not HAS_SQLITE_VEC:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
    except Exception:
        return False
    finally:
        conn.enable_load_extension(False)

    if not _has_table(conn, "vec_data"):
        conn.executescript(VEC_DATA_DDL)
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


def score_memory(vec_distance: float, chunk) -> float:
    """Compute a composite retrieval score from vector distance + recency + salience.

    Accepts either a ChunkRow or DataPointRow (both expose last_accessed,
    created_at, and salience attributes with the same semantics).

    If the chunk has a ``certainty`` attribute, modulates salience:
    ``salience * (0.6 + 0.1 * certainty)``.
    """
    vec_sim = max(0.0, 1.0 - vec_distance)
    boosted = 1.0 - math.exp(-VEC_BOOST_RATE * vec_sim)

    last_accessed = getattr(chunk, "last_accessed", None)
    created_at = getattr(chunk, "created_at", None)
    days = _days_since(last_accessed, created_at)
    recency = math.exp(-RECENCY_DECAY * days)

    salience = chunk.salience if chunk.salience is not None else 1.0

    certainty = getattr(chunk, "certainty", None)
    if certainty is not None:
        salience = salience * (0.6 + 0.1 * certainty)

    return VEC_SIM_WEIGHT * boosted + RECENCY_WEIGHT * recency + SALIENCE_WEIGHT * salience


def index_data_points(conn, data_point_ids: list) -> None:
    """Embed data_points and insert into vec_data.

    Skips data_points already indexed with the same content_hash.
    """
    if not data_point_ids or not HAS_FASTEMBED:
        return

    placeholders = ",".join("?" * len(data_point_ids))
    rows = conn.execute(
        f"SELECT {_DP_COLUMNS} FROM data_points WHERE id IN ({placeholders})",
        data_point_ids,
    ).fetchall()
    data_points = [_row_to_data_point(r) for r in rows]

    existing = conn.execute(
        f"SELECT vd.data_point_id, dp.content_hash "
        f"FROM vec_data vd JOIN data_points dp ON vd.data_point_id = dp.id "
        f"WHERE vd.data_point_id IN ({placeholders})",
        data_point_ids,
    ).fetchall()
    existing_map = {row[0]: row[1] for row in existing}

    to_embed = [
        dp for dp in data_points
        if dp.id not in existing_map or existing_map.get(dp.id) != dp.content_hash
    ]

    if not to_embed:
        return

    stale_ids = [dp.id for dp in to_embed if dp.id in existing_map]
    if stale_ids:
        delete_vec_data(conn, stale_ids)

    contents = [dp.content or dp.name or "" for dp in to_embed]
    vectors = embed_batch(contents)

    for dp, vec in zip(to_embed, vectors):
        conn.execute(
            "INSERT INTO vec_data (embedding, data_point_id, type) VALUES (?, ?, ?)",
            (_serialize_vector(vec), dp.id, dp.type),
        )
    conn.commit()


def index_data_points_by_source(conn, source_types: list) -> None:
    """Index all data_points from the given source types."""
    if not source_types or not HAS_FASTEMBED:
        return
    placeholders = ",".join("?" * len(source_types))
    rows = conn.execute(
        f"SELECT id FROM data_points WHERE source_type IN ({placeholders})",
        source_types,
    ).fetchall()
    dp_ids = [r[0] for r in rows]
    if dp_ids:
        index_data_points(conn, dp_ids)


def delete_vec_data(conn, data_point_ids: list) -> None:
    """Delete vec_data rows for the given data_point IDs."""
    for dp_id in data_point_ids:
        conn.execute("DELETE FROM vec_data WHERE data_point_id = ?", (dp_id,))
    conn.commit()


def search_similar(conn, query: str, top_k: int = DEFAULT_TOP_K, scope: Optional[str] = None) -> list:
    """Search for data_points semantically similar to query.

    Returns a sorted list of ScoredDataPoint (highest score first), limited to top_k.
    Returns [] if fastembed or sqlite-vec are unavailable, or if query cannot be embedded.
    """
    if not HAS_FASTEMBED or not HAS_SQLITE_VEC:
        return []

    query_vec = embed_text(query)
    if not query_vec:
        return []

    if not _has_table(conn, "vec_data"):
        return []

    fetch_k = top_k * 3

    knn_rows = conn.execute(
        "SELECT distance, data_point_id FROM vec_data WHERE embedding MATCH ? AND k = ?",
        (_serialize_vector(query_vec), fetch_k),
    ).fetchall()

    if not knn_rows:
        return []

    dp_ids = [r[1] for r in knn_rows]
    dist_map = {r[1]: r[0] for r in knn_rows}
    placeholders = ",".join("?" * len(dp_ids))
    dp_rows = conn.execute(
        f"SELECT {_DP_COLUMNS} FROM data_points WHERE id IN ({placeholders}) AND salience > 0",
        dp_ids,
    ).fetchall()

    results = []
    for row in dp_rows:
        dp = _row_to_data_point(row)
        distance = dist_map.get(dp.id, 1.0)
        if scope is not None and dp.scope != scope:
            continue
        vec_sim = max(0.0, 1.0 - distance)
        scored = ScoredDataPoint(
            data_point=dp,
            score=score_memory(distance, dp),
            vec_similarity=vec_sim,
        )
        results.append(scored)

    results.sort(key=lambda x: x.score, reverse=True)
    return results[:top_k]


RRF_K = 60  # RRF constant (standard value from Cormack et al.)


def search_hybrid(
    conn,
    query: str,
    scope: Optional[str] = None,
    top_k: int = DEFAULT_TOP_K,
) -> list:
    """Hybrid search combining FTS5 BM25 and vector KNN via Reciprocal Rank Fusion.

    Fallback chain:
    - Both FTS5 + vector available -> RRF hybrid
    - Vector only -> vector search
    - FTS5 only -> BM25 keyword search
    - Neither -> SQL ranked fallback (salience + recency)

    Returns sorted list of ScoredDataPoint (highest score first), limited to top_k.
    """
    has_fts = _has_table(conn, "fts_data")
    has_vec = HAS_FASTEMBED and HAS_SQLITE_VEC and _has_table(conn, "vec_data")

    fts_results = []
    vec_results = []

    if has_fts:
        try:
            from storage import fts_search
            fts_hits = fts_search(conn, query, scope=scope, limit=top_k * 2)
            fts_results = [(hit["data_point_id"], rank_idx) for rank_idx, hit in enumerate(fts_hits)]
        except Exception:
            pass

    if has_vec:
        try:
            vec_results_raw = search_similar(conn, query, top_k=top_k * 2, scope=scope)
            vec_results = [(r.data_point.id, rank_idx) for rank_idx, r in enumerate(vec_results_raw)]
        except Exception:
            pass

    if fts_results and vec_results:
        scores = {}
        for dp_id, rank in fts_results:
            scores[dp_id] = scores.get(dp_id, 0.0) + 1.0 / (RRF_K + rank + 1)
        for dp_id, rank in vec_results:
            scores[dp_id] = scores.get(dp_id, 0.0) + 1.0 / (RRF_K + rank + 1)
        ranked_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)[:top_k]
        return _fetch_scored_data_points(conn, ranked_ids, scores)

    elif vec_results:
        ranked_ids = [dp_id for dp_id, _ in vec_results[:top_k]]
        scores = {dp_id: 1.0 / (RRF_K + rank + 1) for dp_id, rank in vec_results}
        return _fetch_scored_data_points(conn, ranked_ids, scores)

    elif fts_results:
        ranked_ids = [dp_id for dp_id, _ in fts_results[:top_k]]
        scores = {dp_id: 1.0 / (RRF_K + rank + 1) for dp_id, rank in fts_results}
        return _fetch_scored_data_points(conn, ranked_ids, scores)

    else:
        return _sql_ranked_fallback(conn, scope, top_k)


def _fetch_scored_data_points(conn, ranked_ids, scores):
    """Fetch data_points by ID and return as ScoredDataPoint list."""
    from storage import query_data_point_by_id
    results = []
    for dp_id in ranked_ids:
        dp = query_data_point_by_id(conn, dp_id)
        if dp:
            salience_recency = score_memory(0.5, dp)
            final_score = scores[dp_id] + salience_recency
            results.append(ScoredDataPoint(data_point=dp, score=final_score, vec_similarity=0.0))
    results.sort(key=lambda x: x.score, reverse=True)
    return results


def _sql_ranked_fallback(conn, scope, top_k):
    """Fallback search using salience + recency ordering (no semantic understanding).

    When scope is None, returns memories across all scopes (no filter).
    """
    from storage import query_data_points, query_data_points_by_scope
    if scope is None:
        dps = query_data_points(
            conn, dp_type="memory", min_salience=0.0,
            order_by="salience DESC, created_at DESC", limit=top_k
        )
    else:
        dps = query_data_points_by_scope(
            conn, scope=scope, dp_type="memory",
            order_by="salience DESC, created_at DESC", limit=top_k
        )
    return [
        ScoredDataPoint(data_point=dp, score=dp.salience, vec_similarity=0.0)
        for dp in dps
    ]


def reindex_changed_files(conn, changed_files: list) -> None:
    """Re-embed data_points from changed source files (delete old vectors then re-index)."""
    for source_file in changed_files:
        rows = conn.execute(
            "SELECT id FROM data_points WHERE properties LIKE ?",
            (f'%"source_file": "{source_file}"%',),
        ).fetchall()
        dp_ids = [r[0] for r in rows]
        if dp_ids:
            delete_vec_data(conn, dp_ids)
            index_data_points(conn, dp_ids)


def reindex_all(conn) -> None:
    """Wipe and rebuild the entire vec_data table from all data_points in the DB."""
    if not HAS_FASTEMBED:
        return
    success = ensure_vec_table(conn)
    if not success:
        return
    conn.execute("DELETE FROM vec_data")
    conn.commit()
    rows = conn.execute("SELECT id FROM data_points").fetchall()
    dp_ids = [r[0] for r in rows]
    if dp_ids:
        index_data_points(conn, dp_ids)
