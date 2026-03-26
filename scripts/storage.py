#!/usr/bin/env python3
"""
SQLite storage layer for Claude Code Memory System.

Provides DB connection helpers, schema creation, and CRUD operations
for the unified memory.db (data_points + edges).

Requirements: Python 3.9+
"""

import hashlib
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import get_db_path, get_memory_dir  # noqa: E402

SCHEMA_VERSION = 3

# Maximum session_context entries kept per scope (oldest beyond this are pruned)
MAX_SESSION_CONTEXTS_PER_SCOPE = 3

# Whitelist for query_data_points order_by validation (Issue 1: SQL injection prevention)
_ALLOWED_ORDER_COLUMNS = {"salience", "created_at", "last_accessed", "access_count", "evidence_count"}
_ALLOWED_DIRECTIONS = {"ASC", "DESC"}
_DEFAULT_ORDER_BY = "salience DESC, created_at DESC"

SCHEMA_DDL = """\
-- Unified data_points table
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

-- Edges table for relationships between data_points
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

-- Indexes for data_points
CREATE INDEX IF NOT EXISTS idx_dp_type ON data_points(type);
CREATE INDEX IF NOT EXISTS idx_dp_scope ON data_points(scope);
CREATE INDEX IF NOT EXISTS idx_dp_salience ON data_points(salience);
CREATE INDEX IF NOT EXISTS idx_dp_created ON data_points(created_at);
CREATE INDEX IF NOT EXISTS idx_dp_hash ON data_points(content_hash);
CREATE INDEX IF NOT EXISTS idx_dp_simhash ON data_points(simhash);

-- Indexes for edges
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);

-- Key-value metadata (consolidation timestamps, etc.)
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

VEC_DATA_DDL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS vec_data USING vec0(
    embedding float[384],
    +data_point_id TEXT,
    +type TEXT
);
"""

__all__ = [
    # Schema & DDL
    "SCHEMA_VERSION",
    "SCHEMA_DDL",
    "VEC_DATA_DDL",
    "FTS5_DDL",
    # Dataclasses
    "EdgeRow",
    "DataPointRow",
    # DB lifecycle
    "ensure_db",
    "get_db",
    "close_db",
    # Schema helpers
    "_get_schema_version",
    "_ensure_epistemic_columns",
    # data_points CRUD
    "get_or_create_entity",
    "insert_data_point",
    "query_data_point_by_id",
    "query_data_points",
    "query_data_points_by_scope",
    "update_data_point",
    "soft_delete_data_point",
    "delete_data_point_soft",
    # Edges
    "insert_edge",
    "invalidate_edge",
    "query_current_edges",
    "query_edges_at_date",
    "query_edges_for_data_point",
    # Provenance
    "PROVENANCE_TYPES",
    "create_provenance_edge",
    "query_provenance_chain",
    # FTS5
    "_ensure_fts_table",
    "_backfill_fts",
    "fts_insert",
    "fts_delete",
    "fts_search",
    # Maintenance
    "prune_session_contexts",
]


@dataclass
class EdgeRow:
    """Represents a row in the edges table."""
    source: str
    target: str
    type: str
    reason: Optional[str] = None
    fact: Optional[str] = None
    properties: Optional[str] = None
    created_at: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    expired_at: Optional[str] = None
    weight: float = 1.0
    source_sessions: Optional[str] = None
    id: Optional[str] = None


@dataclass(frozen=True)
class DataPointRow:
    """Represents a row in the data_points table (v3 schema)."""
    type: str
    content: Optional[str] = None
    scope: Optional[str] = None
    name: Optional[str] = None
    entry_type: Optional[str] = None
    source_type: Optional[str] = None
    source_sessions: Optional[str] = None
    created_at: Optional[str] = None
    salience: float = 1.0
    access_count: int = 0
    last_accessed: Optional[str] = None
    evidence_count: int = 1
    consolidated: int = 0
    content_hash: Optional[str] = None
    simhash: Optional[int] = None
    entities: Optional[str] = None
    properties: Optional[str] = None
    certainty: Optional[int] = None
    validity_context: Optional[str] = None
    id: Optional[str] = None


def _get_schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute('PRAGMA user_version').fetchone()[0]



FTS5_DDL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS fts_data USING fts5(
    content,
    data_point_id UNINDEXED,
    scope UNINDEXED,
    tokenize='porter unicode61'
);
"""


def _ensure_fts_table(conn: sqlite3.Connection) -> None:
    """Create FTS5 table if not exists."""
    conn.executescript(FTS5_DDL)
    conn.commit()


def _backfill_fts(conn: sqlite3.Connection) -> None:
    """One-time backfill of existing data_points into FTS5 index.

    Called from ensure_db to populate FTS index. Uses
    NOT IN subquery to avoid duplicates.
    """
    conn.execute("""
        INSERT INTO fts_data(content, data_point_id, scope)
        SELECT dp.content, dp.id, dp.scope
        FROM data_points dp
        WHERE dp.content IS NOT NULL
        AND dp.id NOT IN (SELECT data_point_id FROM fts_data)
    """)
    conn.commit()


def fts_insert(conn: sqlite3.Connection, dp_id: str, content: str, scope: str | None) -> None:
    """Insert a data_point's content into the FTS5 index."""
    if not content:
        return
    conn.execute(
        "INSERT INTO fts_data(content, data_point_id, scope) VALUES (?, ?, ?)",
        (content, dp_id, scope)
    )


def fts_delete(conn: sqlite3.Connection, dp_id: str) -> None:
    """Remove a data_point from the FTS5 index."""
    conn.execute("DELETE FROM fts_data WHERE data_point_id = ?", (dp_id,))


def fts_search(conn: sqlite3.Connection, query: str, scope: str | None, limit: int = 10) -> list[dict]:
    """Search FTS5 index with BM25 ranking.

    Returns list of dicts with 'data_point_id', 'scope', 'rank' keys,
    ordered by BM25 relevance (best first, bm25 returns negative values
    where lower/more-negative = better match).
    """
    if not query or not query.strip():
        return []

    sanitized = re.sub(r'["\(\)\^\*\-]', ' ', query)
    terms = sanitized.split()
    fts_query = " ".join(f'"{t}"' for t in terms if t.strip())
    if not fts_query:
        return []

    if scope:
        rows = conn.execute(
            "SELECT data_point_id, scope, bm25(fts_data) AS rank FROM fts_data "
            "WHERE fts_data MATCH ? AND scope = ? "
            "ORDER BY rank LIMIT ?",
            (fts_query, scope, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT data_point_id, scope, bm25(fts_data) AS rank FROM fts_data "
            "WHERE fts_data MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, limit)
        ).fetchall()

    return [{"data_point_id": r[0], "scope": r[1], "rank": r[2]} for r in rows]


METADATA_DDL = """\
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _ensure_metadata_table(conn: sqlite3.Connection) -> None:
    """Create metadata key-value table if it doesn't exist."""
    conn.executescript(METADATA_DDL)
    conn.commit()


def _ensure_epistemic_columns(conn: sqlite3.Connection) -> None:
    """Add certainty and validity_context columns if they don't exist.

    Uses PRAGMA table_info to check before ALTER TABLE (SQLite has no
    ADD COLUMN IF NOT EXISTS).
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(data_points)").fetchall()}
    if "certainty" not in existing:
        conn.execute("ALTER TABLE data_points ADD COLUMN certainty INTEGER DEFAULT NULL")
    if "validity_context" not in existing:
        conn.execute("ALTER TABLE data_points ADD COLUMN validity_context TEXT DEFAULT NULL")
    conn.commit()


def ensure_db() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA_DDL)
    try:
        conn.executescript(VEC_DATA_DDL)
    except sqlite3.OperationalError as e:
        if "no such module: vec0" not in str(e):
            raise
    assert isinstance(SCHEMA_VERSION, int), 'SCHEMA_VERSION must be int'
    conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    conn.commit()
    _ensure_epistemic_columns(conn)
    _ensure_metadata_table(conn)
    try:
        _ensure_fts_table(conn)
        _backfill_fts(conn)
    except sqlite3.OperationalError as e:
        print(f"Warning: FTS5 not available, full-text search disabled: {e}", file=sys.stderr)
    return conn


def get_db() -> sqlite3.Connection:
    db_path = get_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f'Memory database not found: {db_path}')
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA foreign_keys=ON')
    return conn


def close_db(conn: sqlite3.Connection) -> None:
    if conn:
        conn.close()


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def _generate_id() -> str:
    return uuid.uuid4().hex[:12]


def get_or_create_entity(conn: sqlite3.Connection, entity_name: str, scope: str | None) -> str:
    """Return the ID of an entity data_point, creating it if absent.

    Uses content_hash("entity:{name_lower}") for case-insensitive dedup so
    "GitHub" and "github" resolve to the same data_point. The display name
    is preserved as-is; when a more-capitalized variant is seen, the stored
    name is updated (e.g. "github" -> "GitHub").
    """
    content_hash = _content_hash(f"entity:{entity_name.lower()}")
    row = conn.execute(
        "SELECT id, name FROM data_points WHERE type='entity' AND content_hash=?",
        (content_hash,),
    ).fetchone()
    # Fallback: find entities stored with old case-sensitive hash and rehash them
    if row is None:
        row = conn.execute(
            "SELECT id, name FROM data_points WHERE type='entity' AND LOWER(name) = LOWER(?)",
            (entity_name,),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE data_points SET content_hash = ? WHERE id = ?",
                (content_hash, row[0]),
            )
    if row:
        existing_id, existing_name = row
        # Prefer the form with more uppercase letters (better display name)
        if existing_name and sum(1 for c in entity_name if c.isupper()) > sum(1 for c in existing_name if c.isupper()):
            conn.execute(
                "UPDATE data_points SET name = ?, content = ? WHERE id = ?",
                (entity_name, entity_name, existing_id),
            )
        return existing_id
    return insert_data_point(conn, DataPointRow(
        type="entity", name=entity_name, scope=scope,
        content=entity_name, content_hash=content_hash,
        source_type="system", salience=0.5,
    ))


def insert_edge(conn: sqlite3.Connection, edge: EdgeRow) -> str:
    """Insert an edge row into the edges table. Returns the edge ID.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    edge_id = edge.id or _generate_id()
    conn.execute(
        "INSERT INTO edges "
        "(id, source, target, type, reason, fact, properties, created_at, "
        "valid_from, valid_to, expired_at, weight, source_sessions) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edge_id, edge.source, edge.target, edge.type, edge.reason,
            edge.fact, edge.properties, edge.created_at, edge.valid_from,
            edge.valid_to, edge.expired_at, edge.weight,
            edge.source_sessions,
        ),
    )
    return edge_id


def invalidate_edge(
    conn: sqlite3.Connection,
    edge_id: str,
    valid_to: str,
    expired_at: str,
) -> None:
    """Set valid_to and expired_at on an edge (bi-temporal invalidation).

    Used when the LLM detects a contradiction -- the old fact is not deleted
    but marked as no longer valid, preserving historical context.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    conn.execute(
        "UPDATE edges SET valid_to = ?, expired_at = ? WHERE id = ?",
        (valid_to, expired_at, edge_id),
    )


_EDGE_COLUMNS = (
    "id, source, target, type, reason, fact, properties, created_at, "
    "valid_from, valid_to, expired_at, weight, source_sessions"
)


def _row_to_edge(row: tuple) -> EdgeRow:
    """Convert a raw SQLite row tuple to an EdgeRow dataclass."""
    return EdgeRow(
        id=row[0], source=row[1], target=row[2], type=row[3],
        reason=row[4], fact=row[5], properties=row[6], created_at=row[7],
        valid_from=row[8], valid_to=row[9], expired_at=row[10],
        weight=row[11], source_sessions=row[12],
    )


def query_current_edges(conn: sqlite3.Connection) -> list:
    """Query all currently valid edges (valid_to IS NULL)."""
    rows = conn.execute(
        f"SELECT {_EDGE_COLUMNS} FROM edges WHERE valid_to IS NULL"
    ).fetchall()
    return [_row_to_edge(r) for r in rows]


def query_edges_at_date(
    conn: sqlite3.Connection, date_str: str
) -> list:
    """Query edges that were valid at a specific date.

    Returns edges where valid_from <= date AND (valid_to IS NULL OR valid_to > date).
    """
    rows = conn.execute(
        f"SELECT {_EDGE_COLUMNS} FROM edges "
        f"WHERE (valid_from IS NULL OR valid_from <= ?) "
        f"AND (valid_to IS NULL OR valid_to > ?)",
        (date_str, date_str),
    ).fetchall()
    return [_row_to_edge(r) for r in rows]


# ============================================================================
# DataPoint CRUD
# ============================================================================

_DP_COLUMNS = (
    "id, type, name, content, scope, entry_type, source_type, source_sessions, "
    "created_at, salience, access_count, last_accessed, evidence_count, "
    "consolidated, content_hash, simhash, entities, properties, "
    "certainty, validity_context"
)


def _row_to_data_point(row: tuple) -> DataPointRow:
    """Convert a raw SQLite row tuple to a DataPointRow dataclass.

    Column order must match _DP_COLUMNS exactly.
    """
    return DataPointRow(
        id=row[0], type=row[1], name=row[2], content=row[3],
        scope=row[4], entry_type=row[5], source_type=row[6],
        source_sessions=row[7], created_at=row[8], salience=row[9],
        access_count=row[10], last_accessed=row[11], evidence_count=row[12],
        consolidated=row[13], content_hash=row[14], simhash=row[15],
        entities=row[16], properties=row[17],
        certainty=row[18] if len(row) > 18 else None,
        validity_context=row[19] if len(row) > 19 else None,
    )


def insert_data_point(conn: sqlite3.Connection, row: DataPointRow) -> str:
    """Insert a data_point row. Generates id, created_at, and content_hash.

    Returns the data_point ID.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    dp_id = row.id or _generate_id()
    created_at = row.created_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Generate content_hash from content or name
    text_for_hash = row.content or row.name or ""
    content_hash = row.content_hash or _content_hash(text_for_hash) if text_for_hash else None

    conn.execute(
        "INSERT INTO data_points "
        "(id, type, name, content, scope, entry_type, source_type, source_sessions, "
        "created_at, salience, access_count, last_accessed, evidence_count, "
        "consolidated, content_hash, simhash, entities, properties, "
        "certainty, validity_context) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            dp_id, row.type, row.name, row.content, row.scope, row.entry_type,
            row.source_type, row.source_sessions, created_at, row.salience,
            row.access_count, row.last_accessed, row.evidence_count,
            row.consolidated, content_hash, row.simhash, row.entities,
            row.properties, row.certainty, row.validity_context,
        ),
    )
    return dp_id


def query_data_point_by_id(
    conn: sqlite3.Connection, dp_id: str
) -> Optional[DataPointRow]:
    """Query a single data_point by ID. Returns None if not found."""
    row = conn.execute(
        f"SELECT {_DP_COLUMNS} FROM data_points WHERE id = ?", (dp_id,)
    ).fetchone()
    return _row_to_data_point(row) if row else None


def query_data_points(
    conn: sqlite3.Connection,
    *,
    dp_type: Optional[str] = None,
    scope: Optional[str] = None,
    min_salience: Optional[float] = None,
    limit: Optional[int] = None,
    order_by: str = "salience DESC, created_at DESC",
) -> list[DataPointRow]:
    """Query data_points with optional filters.

    Args:
        conn: Database connection.
        dp_type: Filter by type (e.g., 'observation', 'entity').
        scope: Filter by scope (e.g., 'global', 'project-x').
        min_salience: Return only rows with salience >= this value.
        limit: Maximum number of rows to return.
        order_by: ORDER BY clause (default: 'salience DESC, created_at DESC').

    Returns:
        List of DataPointRow instances ordered by the given clause.
    """
    conditions = []
    params = []

    if dp_type:
        conditions.append("type = ?")
        params.append(dp_type)
    if scope:
        conditions.append("scope = ?")
        params.append(scope)
    if min_salience is not None:
        conditions.append("salience >= ?")
        params.append(min_salience)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    # Validate order_by to prevent SQL injection: each term must be "column [DIR]"
    validated_order_by = _DEFAULT_ORDER_BY
    if order_by:
        parts = [t.strip() for t in order_by.split(",")]
        valid_parts = []
        for part in parts:
            tokens = part.split()
            if len(tokens) == 1 and tokens[0] in _ALLOWED_ORDER_COLUMNS:
                valid_parts.append(tokens[0])
            elif (len(tokens) == 2 and tokens[0] in _ALLOWED_ORDER_COLUMNS
                  and tokens[1].upper() in _ALLOWED_DIRECTIONS):
                valid_parts.append(f"{tokens[0]} {tokens[1].upper()}")
        validated_order_by = ", ".join(valid_parts) if valid_parts else _DEFAULT_ORDER_BY
    order_clause = f"ORDER BY {validated_order_by}"

    # Validate limit to prevent SQL injection: must be a non-negative integer
    if limit is not None:
        if not isinstance(limit, int) or limit < 0:
            limit = None
    limit_clause = f"LIMIT {int(limit)}" if limit is not None else ""

    query = f"SELECT {_DP_COLUMNS} FROM data_points {where_clause} {order_clause} {limit_clause}"
    rows = conn.execute(query, params).fetchall()
    return [_row_to_data_point(r) for r in rows]


def query_data_points_by_scope(
    conn: sqlite3.Connection,
    scope: str,
    *,
    dp_type: Optional[str] = None,
    min_salience: Optional[float] = None,
    limit: Optional[int] = None,
    order_by: str = "salience DESC, created_at DESC",
) -> list[DataPointRow]:
    """Query data_points for a given scope with optional filters.

    Args:
        conn: Database connection.
        scope: Scope to filter by (e.g., 'global', 'project-x').
        dp_type: Optional type filter.
        min_salience: Optional minimum salience threshold.
        limit: Optional maximum number of rows.
        order_by: ORDER BY clause (default: 'salience DESC, created_at DESC').

    Returns:
        List of DataPointRow instances ordered by the given clause.
    """
    return query_data_points(
        conn, scope=scope, dp_type=dp_type, min_salience=min_salience,
        limit=limit, order_by=order_by
    )


def update_data_point(conn: sqlite3.Connection, dp_id: str, **kwargs) -> int:
    """Update specified columns of a data_point row.

    Args:
        conn: Database connection.
        dp_id: ID of the data_point to update.
        **kwargs: Column-value pairs to update (e.g., content="new", salience=0.8).

    Returns:
        Number of rows affected (0 if not found, 1 if updated).

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    if not kwargs:
        return 0

    # Valid columns for update (exclude id and created_at)
    valid_cols = {
        "type", "name", "content", "scope", "entry_type", "source_type",
        "source_sessions", "salience", "access_count", "last_accessed",
        "evidence_count", "consolidated", "content_hash", "simhash",
        "entities", "properties", "certainty", "validity_context",
    }

    updates = []
    params = []
    for col, val in kwargs.items():
        if col in valid_cols:
            updates.append(f"{col} = ?")
            params.append(val)

    if not updates:
        return 0

    params.append(dp_id)
    query = f"UPDATE data_points SET {', '.join(updates)} WHERE id = ?"
    cursor = conn.execute(query, params)
    return cursor.rowcount


def soft_delete_data_point(conn: sqlite3.Connection, dp_id: str) -> int:
    """Soft-delete a data_point by setting salience to 0.0.

    Args:
        conn: Database connection.
        dp_id: ID of the data_point to soft-delete.

    Returns:
        Number of rows affected (0 if not found, 1 if deleted).

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    cursor = conn.execute(
        "UPDATE data_points SET salience = 0.0 WHERE id = ?", (dp_id,)
    )
    try:
        fts_delete(conn, dp_id)
    except Exception as e:
        import sys as _sys
        print(f"Warning: FTS5 delete failed for {dp_id}: {e}", file=_sys.stderr)
    return cursor.rowcount


# Alias for compatibility
delete_data_point_soft = soft_delete_data_point


def prune_session_contexts(
    conn: sqlite3.Connection,
    scope: str,
    max_keep: int = MAX_SESSION_CONTEXTS_PER_SCOPE,
) -> int:
    """Delete older session_context entries for a scope, keeping the newest *max_keep*.

    Soft-deletes (salience=0) and removes FTS entries for pruned data_points.
    Also invalidates edges connected to pruned data_points.

    Args:
        conn: Database connection.
        scope: Project scope to prune session_contexts for.
        max_keep: Maximum number of session_context entries to retain per scope.

    Returns:
        Number of session_context entries pruned.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    rows = conn.execute(
        "SELECT id FROM data_points "
        "WHERE type='session_context' AND scope=? AND salience > 0 "
        "ORDER BY created_at DESC",
        (scope,),
    ).fetchall()

    if len(rows) <= max_keep:
        return 0

    to_prune = [r[0] for r in rows[max_keep:]]
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    pruned = 0
    for dp_id in to_prune:
        conn.execute(
            "UPDATE data_points SET salience = 0.0 WHERE id = ?", (dp_id,)
        )
        try:
            fts_delete(conn, dp_id)
        except Exception as e:
            print(f"Warning: FTS5 delete failed for {dp_id} during session context pruning: {e}", file=sys.stderr)
        # Invalidate connected edges (set both valid_to and expired_at for consistency with invalidate_edge)
        conn.execute(
            "UPDATE edges SET valid_to = ?, expired_at = ? WHERE (source = ? OR target = ?) AND valid_to IS NULL",
            (now_iso, now_iso, dp_id, dp_id),
        )
        pruned += 1
    return pruned


def query_edges_for_data_point(
    conn: sqlite3.Connection, data_point_id: str, direction: str = "both"
) -> list[EdgeRow]:
    """Query edges connected to a data_point.

    Args:
        conn: Database connection.
        data_point_id: ID of the data_point.
        direction: 'both' (default), 'outgoing' (source), or 'incoming' (target).

    Returns:
        List of EdgeRow instances.

    Note: Edges reference data_points in the current schema.
    """
    if direction == "outgoing":
        condition = "source = ?"
    elif direction == "incoming":
        condition = "target = ?"
    else:  # "both"
        condition = "source = ? OR target = ?"

    # Edges schema matches _EDGE_COLUMNS exactly; reuse it.
    if direction == "both":
        rows = conn.execute(
            f"SELECT {_EDGE_COLUMNS} FROM edges WHERE {condition}",
            (data_point_id, data_point_id),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_EDGE_COLUMNS} FROM edges WHERE {condition}",
            (data_point_id,),
        ).fetchall()

    return [_row_to_edge(r) for r in rows]


PROVENANCE_TYPES = frozenset({"supersedes", "contradicts", "led_to", "refines", "supports"})


def create_provenance_edge(
    conn: sqlite3.Connection,
    source_id: str,
    target_id: str,
    edge_type: str,
    reason: Optional[str] = None,
) -> str:
    """Create a provenance edge between two data_points.

    Args:
        conn: Database connection.
        source_id: ID of the newer/current data_point (the one doing the relating).
        target_id: ID of the older/referenced data_point.
        edge_type: One of PROVENANCE_TYPES (supersedes, contradicts, led_to, refines, supports).
        reason: Optional human-readable explanation for the relationship.

    Returns:
        The new edge ID.

    Raises:
        ValueError: If source_id == target_id (self-reference) or edge_type is invalid.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    if source_id == target_id:
        raise ValueError("Cannot create self-referencing provenance edge")
    if edge_type not in PROVENANCE_TYPES:
        raise ValueError(f"Invalid provenance type: {edge_type!r}. Must be one of {sorted(PROVENANCE_TYPES)}")
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return insert_edge(conn, EdgeRow(
        source=source_id, target=target_id, type=edge_type,
        reason=reason, created_at=now, valid_from=now,
    ))


def query_provenance_chain(
    conn: sqlite3.Connection,
    data_point_id: str,
) -> list:
    """Follow provenance edges from a data_point using a recursive CTE.

    Traverses outgoing supersedes/contradicts/refines edges up to 10 hops deep.

    Args:
        conn: Database connection.
        data_point_id: Starting data_point ID.

    Returns:
        List of dicts: {source_id, target_id, type, reason, depth}.
        Empty list if no provenance edges exist.
    """
    rows = conn.execute("""
        WITH RECURSIVE chain(source_id, target_id, edge_type, reason, depth) AS (
            SELECT source, target, type, reason, 1
            FROM edges
            WHERE source = ? AND type IN ('supersedes', 'contradicts', 'refines')
              AND valid_to IS NULL
            UNION ALL
            SELECT e.source, e.target, e.type, e.reason, c.depth + 1
            FROM chain c
            JOIN edges e ON e.source = c.target_id
            WHERE e.type IN ('supersedes', 'contradicts', 'refines')
              AND e.valid_to IS NULL
              AND c.depth < 10
        )
        SELECT source_id, target_id, edge_type, reason, depth
        FROM chain ORDER BY depth
    """, (data_point_id,)).fetchall()
    return [{"source_id": r[0], "target_id": r[1], "type": r[2],
             "reason": r[3], "depth": r[4]} for r in rows]

