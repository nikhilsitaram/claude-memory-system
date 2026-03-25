#!/usr/bin/env python3
"""
SQLite storage layer for Claude Code Memory System.

Provides DB connection helpers, schema creation, and CRUD operations
for the unified memory.db (v3: data_points + edges, replacing markdown
as the primary source of truth for structured memory).

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
-- Graph layer
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    description TEXT,
    scope TEXT,
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    salience REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    content_hash TEXT,
    simhash INTEGER,
    source_sessions TEXT,
    evidence_count INTEGER DEFAULT 1,
    consolidated INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edges (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL REFERENCES nodes(id),
    target TEXT NOT NULL REFERENCES nodes(id),
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

-- Content layer
CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_type TEXT NOT NULL,
    section TEXT,
    scope TEXT,
    entry_type TEXT,
    chunk_index INTEGER,
    created_at TEXT,
    content_hash TEXT,
    simhash INTEGER,
    salience REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    source_sessions TEXT,
    evidence_count INTEGER DEFAULT 1,
    entities TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type);
CREATE INDEX IF NOT EXISTS idx_nodes_scope ON nodes(scope);
CREATE INDEX IF NOT EXISTS idx_nodes_simhash ON nodes(simhash);
CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target);
CREATE INDEX IF NOT EXISTS idx_edges_valid ON edges(valid_to);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_file);
CREATE INDEX IF NOT EXISTS idx_chunks_scope ON chunks(scope);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(content_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_simhash ON chunks(simhash);
"""

VEC_CHUNKS_DDL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
    embedding float[384],
    +chunk_id TEXT,
    +source_type TEXT
);
"""

VEC_DATA_DDL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS vec_data USING vec0(
    embedding float[384],
    +data_point_id TEXT,
    +type TEXT
);
"""

SCHEMA_V3_DDL = """\
-- Unified data_points table (replaces chunks and nodes)
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

-- Vector search layer for data_points
CREATE VIRTUAL TABLE IF NOT EXISTS vec_data USING vec0(
    embedding float[384],
    +data_point_id TEXT,
    +type TEXT
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

__all__ = [
    # Schema & DDL
    "SCHEMA_VERSION",
    "SCHEMA_DDL",
    "VEC_CHUNKS_DDL",
    "VEC_DATA_DDL",
    "SCHEMA_V3_DDL",
    "FTS5_DDL",
    # Dataclasses
    "EdgeRow",
    "DataPointRow",
    "MigrationStats",
    "NeighborInfo",
    # DB lifecycle
    "ensure_db",
    "get_db",
    "close_db",
    # Schema & migration
    "_get_schema_version",
    "_migrate_salience_data",
    "_migrate_v2_to_v3",
    "_ensure_epistemic_columns",
    "migrate_markdown_to_db",
    "_parse_ltm_entries",
    "_parse_daily_entries",
    "PROFILE_SECTIONS",
    "_migrate_profiles",
    "migrate_profiles",
    "_should_archive",
    "_archive_markdown_files",
    # v3 data_points CRUD
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
    # v2-only (migration support — do not use for new code)
    "ChunkRow",
    "NodeRow",
    "insert_chunk",
    "query_chunks_by_scope",
    "query_chunks_by_source",
    "delete_chunks_by_source",
    "insert_node",
    "query_nodes_by_scope",
    "query_node_by_name_and_type",
    "update_node_access",
    "batch_update_access",
    "update_chunk_salience",
    "update_node_salience",
    "query_chunks_with_salience",
    "update_chunk_content",
    "query_chunks_for_retrieval",
    "query_chunk_by_id",
    "query_edges_for_node",
    "query_neighbor_nodes",
]


@dataclass
class ChunkRow:
    """Represents a row in the chunks table."""
    content: str
    source_file: str
    source_type: str
    scope: Optional[str] = None
    section: Optional[str] = None
    entry_type: Optional[str] = None
    chunk_index: int = 0
    created_at: Optional[str] = None
    content_hash: Optional[str] = None
    simhash: Optional[int] = None
    salience: float = 1.0
    access_count: int = 0
    last_accessed: Optional[str] = None
    source_sessions: Optional[str] = None
    evidence_count: int = 1
    entities: Optional[str] = None
    id: Optional[str] = None


@dataclass
class NodeRow:
    """Represents a row in the nodes table."""
    name: str
    type: str
    scope: Optional[str] = None
    description: Optional[str] = None
    access_count: int = 0
    last_accessed: Optional[str] = None
    salience: float = 1.0
    created_at: Optional[str] = None
    content_hash: Optional[str] = None
    simhash: Optional[int] = None
    source_sessions: Optional[str] = None
    evidence_count: int = 1
    consolidated: int = 0
    id: Optional[str] = None


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


@dataclass
class MigrationStats:
    """Statistics from a markdown-to-DB migration run."""
    ltm_files_processed: int = 0
    daily_files_processed: int = 0
    chunks_inserted: int = 0
    chunks_skipped: int = 0


def _get_schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute('PRAGMA user_version').fetchone()[0]


def _migrate_salience_data(conn: sqlite3.Connection) -> None:
    """One-time data migration: set last_accessed = created_at where NULL.

    Bootstraps the salience tracking system for existing chunks and nodes.
    Existing rows start with salience=1.0 (schema default) and
    access_count=0 (schema default). This backfills last_accessed from
    created_at so decay tiers start with a meaningful recency baseline.
    Idempotent: WHERE last_accessed IS NULL ensures repeated runs are safe.
    """
    conn.execute(
        "UPDATE chunks SET last_accessed = created_at "
        "WHERE last_accessed IS NULL AND created_at IS NOT NULL"
    )
    conn.execute(
        "UPDATE nodes SET last_accessed = created_at "
        "WHERE last_accessed IS NULL AND created_at IS NOT NULL"
    )


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Migrate from v2 schema (chunks+nodes) to v3 schema (unified data_points).

    Steps:
    1. Create data_points table
    2. Copy chunks → data_points with type='memory'
    3. Copy nodes → data_points with type='entity'
    4. Add reason column to edges
    5. Migrate vec_chunks → vec_data (if sqlite-vec available)
    6. Verify edge integrity
    7. Drop old chunks, nodes, vec_chunks tables
    8. Set SCHEMA_VERSION=3

    Idempotent: safe to run multiple times.
    """
    import json

    # Guard: if data_points already exists, migration already done
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "data_points" in tables:
        return

    # Step 1: Create data_points table
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

        CREATE INDEX IF NOT EXISTS idx_dp_type ON data_points(type);
        CREATE INDEX IF NOT EXISTS idx_dp_scope ON data_points(scope);
        CREATE INDEX IF NOT EXISTS idx_dp_salience ON data_points(salience);
        CREATE INDEX IF NOT EXISTS idx_dp_created ON data_points(created_at);
        CREATE INDEX IF NOT EXISTS idx_dp_hash ON data_points(content_hash);
        CREATE INDEX IF NOT EXISTS idx_dp_simhash ON data_points(simhash);
    """)

    # Step 2: Copy chunks → data_points with type='memory'
    # Build properties JSON from source_file and chunk_index
    chunks = conn.execute(
        "SELECT id, content, source_file, source_type, section, scope, entry_type, "
        "chunk_index, created_at, content_hash, simhash, salience, access_count, "
        "last_accessed, source_sessions, evidence_count, entities FROM chunks"
    ).fetchall()

    for chunk in chunks:
        (
            chunk_id, content, source_file, source_type, section, scope, entry_type,
            chunk_index, created_at, content_hash, simhash, salience, access_count,
            last_accessed, source_sessions, evidence_count, entities
        ) = chunk

        # Build properties JSON
        properties = {}
        if source_file:
            properties["source_file"] = source_file
        if chunk_index is not None:
            properties["chunk_index"] = chunk_index
        if section:
            properties["section"] = section

        properties_json = json.dumps(properties) if properties else None

        conn.execute(
            "INSERT INTO data_points "
            "(id, type, content, scope, entry_type, source_type, created_at, "
            "salience, access_count, last_accessed, content_hash, simhash, "
            "source_sessions, evidence_count, entities, properties) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_id, "memory", content, scope, entry_type, source_type, created_at,
                salience, access_count, last_accessed, content_hash, simhash,
                source_sessions, evidence_count, entities, properties_json
            )
        )

    # Step 3: Copy nodes → data_points with type='entity'
    nodes = conn.execute(
        "SELECT id, name, type, description, scope, access_count, last_accessed, "
        "salience, created_at, content_hash, simhash, source_sessions, evidence_count, "
        "consolidated FROM nodes"
    ).fetchall()

    for node in nodes:
        (
            node_id, name, node_type, description, scope, access_count, last_accessed,
            salience, created_at, content_hash, simhash, source_sessions, evidence_count,
            consolidated
        ) = node

        conn.execute(
            "INSERT INTO data_points "
            "(id, type, name, content, scope, created_at, salience, access_count, "
            "last_accessed, content_hash, simhash, source_sessions, evidence_count, "
            "consolidated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                node_id, "entity", name, description, scope, created_at, salience,
                access_count, last_accessed, content_hash, simhash, source_sessions,
                evidence_count, consolidated
            )
        )

    # Step 4: Recreate edges table with data_points references and reason column
    # Save existing edges data
    edges_data = conn.execute(
        "SELECT id, source, target, type, fact, properties, created_at, "
        "valid_from, valid_to, expired_at, weight, source_sessions FROM edges"
    ).fetchall()

    # Drop old edges table (this will also drop its indexes)
    conn.execute("DROP TABLE edges")

    # Create new edges table with data_points references and reason column
    conn.executescript("""
        CREATE TABLE edges (
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
        CREATE INDEX idx_edges_source ON edges(source);
        CREATE INDEX idx_edges_target ON edges(target);
        CREATE INDEX idx_edges_valid ON edges(valid_to);
    """)

    # Restore edges data
    for edge in edges_data:
        conn.execute(
            "INSERT INTO edges (id, source, target, type, fact, properties, created_at, "
            "valid_from, valid_to, expired_at, weight, source_sessions) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            edge
        )

    # Step 5: Verify edge integrity
    orphan_count = conn.execute(
        "SELECT COUNT(*) FROM edges WHERE "
        "source NOT IN (SELECT id FROM data_points) OR "
        "target NOT IN (SELECT id FROM data_points)"
    ).fetchone()[0]

    if orphan_count > 0:
        raise ValueError(
            f"Migration integrity error: {orphan_count} edges reference "
            "non-existent data_points"
        )

    # Step 6: Migrate vec_chunks → vec_data (if sqlite-vec available)
    if "vec_chunks" in tables:
        try:
            # Create vec_data virtual table
            conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_data USING vec0(
                    embedding float[384],
                    +data_point_id TEXT,
                    +type TEXT
                )
            """)
            # Copy data: chunk_id → data_point_id, source_type → type
            conn.execute(
                "INSERT INTO vec_data(embedding, data_point_id, type) "
                "SELECT embedding, chunk_id, source_type FROM vec_chunks"
            )
            # Drop old vec_chunks table
            conn.execute("DROP TABLE vec_chunks")
        except sqlite3.OperationalError as e:
            # sqlite-vec may not be available, skip vector migration
            if "no such module: vec0" not in str(e):
                raise

    # Step 7: Drop old tables
    conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute("DROP TABLE IF EXISTS nodes")

    # Step 8: Set schema version to 3
    conn.execute("PRAGMA user_version = 3")

    # Step 9: Migrate profile sections from global LTM (idempotent)
    _migrate_profiles(conn, get_memory_dir() / "global-long-term-memory.md")

    conn.commit()


FTS5_DDL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS fts_data USING fts5(
    content,
    data_point_id UNINDEXED,
    scope UNINDEXED,
    tokenize='porter unicode61'
);
"""


def _ensure_fts_table(conn: sqlite3.Connection) -> None:
    """Create FTS5 table (backfill is handled by _migrate_schema)."""
    conn.executescript(FTS5_DDL)
    conn.commit()


def _backfill_fts(conn: sqlite3.Connection) -> None:
    """One-time backfill of existing data_points into FTS5 index.

    Called from _migrate_schema when schema version advances. Uses
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


def _migrate_schema(conn: sqlite3.Connection, current_version: int) -> None:
    if current_version < 2:
        _migrate_salience_data(conn)
    if current_version < 3:
        _migrate_v2_to_v3(conn)
        try:
            _ensure_fts_table(conn)
            _backfill_fts(conn)
        except sqlite3.OperationalError:
            pass


def ensure_db() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    conn.execute('PRAGMA foreign_keys=ON')
    current_version = _get_schema_version(conn)
    conn.executescript(SCHEMA_DDL)
    if current_version < SCHEMA_VERSION:
        _migrate_schema(conn, current_version)
    assert isinstance(SCHEMA_VERSION, int), 'SCHEMA_VERSION must be int'
    conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
    conn.commit()
    _ensure_epistemic_columns(conn)
    _ensure_metadata_table(conn)
    try:
        _ensure_fts_table(conn)
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


def insert_chunk(conn: sqlite3.Connection, chunk: ChunkRow) -> str:
    """Insert a chunk row into the chunks table.

    Auto-generates id and content_hash if not set on the ChunkRow.
    Returns the chunk ID.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    chunk_id = chunk.id or _generate_id()
    content_hash = chunk.content_hash or _content_hash(chunk.content)

    conn.execute(
        "INSERT INTO chunks "
        "(id, content, source_file, source_type, section, scope, entry_type, "
        "chunk_index, created_at, content_hash, simhash, salience, access_count, "
        "last_accessed, source_sessions, evidence_count, entities) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chunk_id, chunk.content, chunk.source_file, chunk.source_type,
            chunk.section, chunk.scope, chunk.entry_type, chunk.chunk_index,
            chunk.created_at, content_hash, chunk.simhash, chunk.salience,
            chunk.access_count, chunk.last_accessed, chunk.source_sessions,
            chunk.evidence_count, chunk.entities,
        ),
    )
    return chunk_id


_CHUNK_COLUMNS = (
    "id, content, source_file, source_type, section, scope, entry_type, "
    "chunk_index, created_at, content_hash, simhash, salience, access_count, "
    "last_accessed, source_sessions, evidence_count, entities"
)


def _row_to_chunk(row: tuple) -> ChunkRow:
    """Convert a raw SQLite row tuple to a ChunkRow dataclass.

    Column order must match _CHUNK_COLUMNS exactly.
    """
    return ChunkRow(
        id=row[0], content=row[1], source_file=row[2], source_type=row[3],
        section=row[4], scope=row[5], entry_type=row[6], chunk_index=row[7],
        created_at=row[8], content_hash=row[9], simhash=row[10],
        salience=row[11], access_count=row[12], last_accessed=row[13],
        source_sessions=row[14], evidence_count=row[15], entities=row[16],
    )


def query_chunks_by_scope(
    conn: sqlite3.Connection, scope: str
) -> list[ChunkRow]:
    """Query all chunks matching the given scope."""
    rows = conn.execute(
        f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE scope = ?", (scope,)
    ).fetchall()
    return [_row_to_chunk(r) for r in rows]


def query_chunks_by_source(
    conn: sqlite3.Connection, source_file: str
) -> list[ChunkRow]:
    """Query all chunks from a specific source file."""
    rows = conn.execute(
        f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE source_file = ?",
        (source_file,),
    ).fetchall()
    return [_row_to_chunk(r) for r in rows]


def delete_chunks_by_source(
    conn: sqlite3.Connection, source_file: str
) -> int:
    """Delete all chunks from a specific source file. Returns count deleted.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    cursor = conn.execute(
        "DELETE FROM chunks WHERE source_file = ?", (source_file,)
    )
    return cursor.rowcount


_NODE_COLUMNS = (
    "id, name, type, description, scope, access_count, last_accessed, "
    "salience, created_at, content_hash, simhash, source_sessions, "
    "evidence_count, consolidated"
)


def _row_to_node(row: tuple) -> NodeRow:
    """Convert a raw SQLite row tuple to a NodeRow dataclass.

    Column order must match _NODE_COLUMNS exactly.
    """
    return NodeRow(
        id=row[0], name=row[1], type=row[2], description=row[3],
        scope=row[4], access_count=row[5], last_accessed=row[6],
        salience=row[7], created_at=row[8], content_hash=row[9],
        simhash=row[10], source_sessions=row[11], evidence_count=row[12],
        consolidated=row[13],
    )


def insert_node(conn: sqlite3.Connection, node: NodeRow) -> str:
    """Insert a node row into the nodes table. Returns the node ID.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    node_id = node.id or _generate_id()
    conn.execute(
        "INSERT INTO nodes "
        "(id, name, type, description, scope, access_count, last_accessed, "
        "salience, created_at, content_hash, simhash, source_sessions, "
        "evidence_count, consolidated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            node_id, node.name, node.type, node.description, node.scope,
            node.access_count, node.last_accessed, node.salience,
            node.created_at, node.content_hash, node.simhash,
            node.source_sessions, node.evidence_count, node.consolidated,
        ),
    )
    return node_id


def query_nodes_by_scope(
    conn: sqlite3.Connection, scope: str
) -> list[NodeRow]:
    """Query all nodes matching the given scope."""
    rows = conn.execute(
        f"SELECT {_NODE_COLUMNS} FROM nodes WHERE scope = ?", (scope,)
    ).fetchall()
    return [_row_to_node(r) for r in rows]


def query_node_by_name_and_type(
    conn: sqlite3.Connection, name: str, node_type: str
) -> Optional[NodeRow]:
    """Query a single node by name and type. Returns None if not found."""
    row = conn.execute(
        f"SELECT {_NODE_COLUMNS} FROM nodes WHERE name = ? AND type = ?",
        (name, node_type),
    ).fetchone()
    return _row_to_node(row) if row else None


def update_node_access(conn: sqlite3.Connection, node_id: str) -> None:
    """Increment access_count and set last_accessed to now (UTC ISO).

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn.execute(
        "UPDATE nodes SET access_count = access_count + 1, "
        "last_accessed = ? WHERE id = ?",
        (now, node_id),
    )


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


@dataclass
class NeighborInfo:
    """A neighbor node with the connecting edge weight."""
    node_id: str
    salience: float
    edge_weight: float


def query_neighbor_nodes(
    conn: sqlite3.Connection, node_id: str
) -> list:
    """Query direct graph neighbors of a node via valid edges.

    Looks up both directions (node as source or target).
    Excludes expired edges (valid_to IS NOT NULL).

    Returns:
        List of NeighborInfo with node_id, current salience, and edge weight.
    """
    rows = conn.execute(
        """
        SELECT n.id, n.salience, e.weight
        FROM edges e
        JOIN nodes n ON n.id = CASE WHEN e.source = ? THEN e.target ELSE e.source END
        WHERE (e.source = ? OR e.target = ?)
          AND e.valid_to IS NULL
        """,
        (node_id, node_id, node_id),
    ).fetchall()
    return [NeighborInfo(node_id=r[0], salience=r[1], edge_weight=r[2]) for r in rows]


def batch_update_access(
    conn: sqlite3.Connection,
    chunk_ids: list,
    timestamp: Optional[str] = None,
) -> int:
    """Batch-increment access_count and update last_accessed for given chunk IDs.

    Args:
        conn: Database connection.
        chunk_ids: List of chunk IDs to update.
        timestamp: ISO timestamp for last_accessed. Defaults to UTC now.

    Returns:
        Number of rows updated.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    if not chunk_ids:
        return 0
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    placeholders = ",".join("?" for _ in chunk_ids)
    cursor = conn.execute(
        f"UPDATE chunks SET access_count = access_count + 1, "
        f"last_accessed = ? WHERE id IN ({placeholders})",
        [timestamp] + list(chunk_ids),
    )
    return cursor.rowcount


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


def update_chunk_content(
    conn: sqlite3.Connection,
    chunk_id: str,
    new_content: str,
    new_entities: Optional[str] = None,
) -> None:
    """Update chunk content, content_hash, and optionally entities.

    Recalculates content_hash from the new content.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    new_hash = _content_hash(new_content)
    if new_entities is not None:
        conn.execute(
            "UPDATE chunks SET content = ?, content_hash = ?, entities = ? WHERE id = ?",
            (new_content, new_hash, new_entities, chunk_id),
        )
    else:
        conn.execute(
            "UPDATE chunks SET content = ?, content_hash = ? WHERE id = ?",
            (new_content, new_hash, chunk_id),
        )


def update_chunk_salience(
    conn: sqlite3.Connection, chunk_id: str, new_salience: float
) -> None:
    """Update salience for a specific chunk, clamped to [0.0, 1.0].

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    clamped = max(0.0, min(1.0, new_salience))
    conn.execute(
        "UPDATE chunks SET salience = ? WHERE id = ?", (clamped, chunk_id)
    )


def update_node_salience(
    conn: sqlite3.Connection, node_id: str, new_salience: float
) -> None:
    """Update salience for a specific node, clamped to [0.0, 1.0].

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    clamped = max(0.0, min(1.0, new_salience))
    conn.execute(
        "UPDATE nodes SET salience = ? WHERE id = ?", (clamped, node_id)
    )


def query_chunks_with_salience(
    conn: sqlite3.Connection, scope: Optional[str] = None
) -> list:
    """Query chunks including access metadata (access_count, last_accessed, salience).

    Args:
        conn: Database connection.
        scope: Optional scope filter. If None, returns all chunks.

    Returns:
        List of ChunkRow instances with all fields populated.
    """
    if scope:
        rows = conn.execute(
            f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE scope = ?", (scope,)
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_CHUNK_COLUMNS} FROM chunks"
        ).fetchall()
    return [_row_to_chunk(r) for r in rows]


def query_chunks_for_retrieval(
    conn: sqlite3.Connection,
    scope: Optional[str] = None,
    min_salience: float = 0.05,
) -> list:
    """Query active chunks suitable for vector search pre-retrieval.

    Excludes chunks below min_salience (effectively archived).
    Returns chunks with all fields including id (needed for CRUD references).
    """
    if scope:
        rows = conn.execute(
            f"SELECT {_CHUNK_COLUMNS} FROM chunks "
            f"WHERE scope = ? AND salience >= ?",
            (scope, min_salience),
        ).fetchall()
    else:
        rows = conn.execute(
            f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE salience >= ?",
            (min_salience,),
        ).fetchall()
    return [_row_to_chunk(r) for r in rows]


def query_chunk_by_id(
    conn: sqlite3.Connection, chunk_id: str
) -> Optional[ChunkRow]:
    """Query a single chunk by ID. Returns None if not found."""
    row = conn.execute(
        f"SELECT {_CHUNK_COLUMNS} FROM chunks WHERE id = ?", (chunk_id,)
    ).fetchone()
    return _row_to_chunk(row) if row else None


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


def query_edges_for_node(
    conn: sqlite3.Connection, node_id: str
) -> list:
    """Query all edges (valid and invalid) connected to a node."""
    rows = conn.execute(
        f"SELECT {_EDGE_COLUMNS} FROM edges "
        f"WHERE source = ? OR target = ?",
        (node_id, node_id),
    ).fetchall()
    return [_row_to_edge(r) for r in rows]


# ============================================================================
# DataPoint CRUD (v3 schema)
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

    Note: This function works with the v3 schema where edges reference data_points.
          For v2 schema (edges reference nodes), use query_edges_for_node instead.
    """
    if direction == "outgoing":
        condition = "source = ?"
    elif direction == "incoming":
        condition = "target = ?"
    else:  # "both"
        condition = "source = ? OR target = ?"

    # V3 edges schema matches _EDGE_COLUMNS exactly; reuse it.
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


_LTM_ENTRY_RE = re.compile(
    r'^\s*-\s*\((\d{4}-\d{2}-\d{2})\)\s*\[([^\]]+)\]\s*(.+)'
)

# Profile section names that are migrated to data_points with type='profile'
PROFILE_SECTIONS = frozenset({
    "About Me",
    "Current Projects",
    "Technical Environment",
    "Patterns & Preferences",
})


def _insert_profile_section(
    conn: sqlite3.Connection, header: str, lines: list, created_at: str
) -> None:
    """Insert a single profile section as a data_point.

    Skips insertion if a profile data_point with the same content_hash already
    exists (idempotency).
    """
    content = "\n".join(lines).strip()
    if not content:
        return

    h = _content_hash(content)
    existing = conn.execute(
        "SELECT id FROM data_points WHERE content_hash = ? AND type = 'profile'",
        (h,),
    ).fetchone()
    if existing:
        return

    conn.execute(
        "INSERT INTO data_points "
        "(id, type, name, content, scope, source_type, created_at, "
        "salience, consolidated, content_hash) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (_generate_id(), "profile", header, content, "user", "migration", created_at, 1.0, 1, h),
    )


def _migrate_profiles(conn: sqlite3.Connection, ltm_path: Path) -> None:
    """Parse global LTM markdown and create profile data_points.

    Only processes sections whose headers match PROFILE_SECTIONS.
    Sections with no content are skipped.
    Idempotent: uses content_hash to avoid duplicate inserts.

    Args:
        conn: Database connection.
        ltm_path: Path to global-long-term-memory.md.
    """
    if not ltm_path.exists():
        return

    created_at = datetime.fromtimestamp(
        ltm_path.stat().st_mtime, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")

    content = ltm_path.read_text(encoding="utf-8")
    current_header: Optional[str] = None
    current_lines: list = []

    for line in content.split("\n"):
        if line.startswith("## "):
            if current_header in PROFILE_SECTIONS and current_lines:
                _insert_profile_section(conn, current_header, current_lines, created_at)
            current_header = line[3:].strip()
            current_lines = []
        elif current_header is not None:
            current_lines.append(line)

    # Handle final section
    if current_header in PROFILE_SECTIONS and current_lines:
        _insert_profile_section(conn, current_header, current_lines, created_at)


def migrate_profiles(conn: sqlite3.Connection, ltm_path: Path) -> None:
    """Public API: migrate profile sections from global LTM to data_points.

    Parses ## About Me, ## Current Projects, ## Technical Environment, and
    ## Patterns & Preferences sections from the global LTM markdown file,
    creating one data_point per section with type='profile', scope='user',
    salience=1.0, consolidated=1.

    Idempotent: safe to run multiple times.

    Note: Does not call conn.commit(). Caller must commit explicitly.
    """
    _migrate_profiles(conn, ltm_path)


def _parse_ltm_entries(content: str, source_file: str, scope: str) -> list[ChunkRow]:
    """Parse an LTM markdown file into ChunkRow objects.
    Each '- (date) [type] description' line becomes one chunk.
    Section headers (## Key Actions, etc.) are tracked for the section field.
    Lines without the dated entry pattern are skipped.
    """
    chunks = []
    current_section = None
    chunk_index = 0
    for line in content.split('\n'):
        if line.startswith('## '):
            current_section = line.strip()
            continue
        match = _LTM_ENTRY_RE.match(line)
        if match:
            entry_date = match.group(1)
            entry_type = match.group(2)
            full_content = line.strip()
            chunks.append(ChunkRow(
                content=full_content, source_file=source_file,
                source_type='ltm', section=current_section,
                scope=scope, entry_type=entry_type,
                chunk_index=chunk_index, created_at=entry_date,
            ))
            chunk_index += 1
    return chunks


def _parse_daily_entries(content: str, source_file: str) -> list[ChunkRow]:
    """Parse a daily markdown file into ChunkRow objects.
    Each '- [scope/type] description' line becomes one chunk.
    Routed entries (prefixed with [routed]) are skipped.
    """
    chunks = []
    current_section = None
    chunk_index = 0
    date_str = Path(source_file).stem if source_file else None
    for line in content.split('\n'):
        if line.startswith('## '):
            current_section = line.strip()
            continue
        stripped = line.strip()
        if not stripped.startswith('- '):
            continue
        if '[routed]' in stripped:
            continue
        tag_match = re.match(r'^\s*-\s*\[([^\]/]+)(?:/([^\]]+))?\]\s*(.+)', stripped)
        if tag_match:
            scope_part = tag_match.group(1).strip().lower()
            entry_type = tag_match.group(2).strip() if tag_match.group(2) else None
            if '|' in scope_part:
                scope_part = scope_part.split('|')[0].strip()
            chunks.append(ChunkRow(
                content=stripped, source_file=source_file,
                source_type='daily', section=current_section,
                scope=scope_part, entry_type=entry_type,
                chunk_index=chunk_index, created_at=date_str,
            ))
            chunk_index += 1
    return chunks


def migrate_markdown_to_db(conn: sqlite3.Connection) -> MigrationStats:
    """Migrate existing markdown memory files into the database.
    Scans global LTM, project LTMs, and daily files.
    Uses content_hash to skip chunks that already exist in the DB.
    Idempotent -- safe to run multiple times.
    Schema-aware: queries chunks for v2, data_points for v3+.
    """
    stats = MigrationStats()

    # Check which table to query for existing hashes
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    if 'chunks' in tables:
        # v2 schema
        existing_hashes = {
            row[0] for row in conn.execute(
                'SELECT content_hash FROM chunks WHERE content_hash IS NOT NULL'
            ).fetchall()
        }
    elif 'data_points' in tables:
        # v3+ schema
        existing_hashes = {
            row[0] for row in conn.execute(
                'SELECT content_hash FROM data_points WHERE type = \'memory\' AND content_hash IS NOT NULL'
            ).fetchall()
        }
    else:
        # Empty DB
        existing_hashes = set()

    schema_version = _get_schema_version(conn)

    def _insert_chunks(chunks: list[ChunkRow]) -> None:
        for chunk in chunks:
            h = _content_hash(chunk.content)
            if h in existing_hashes:
                stats.chunks_skipped += 1
                continue
            existing_hashes.add(h)
            if schema_version >= 3:
                # v3: write directly to data_points
                insert_data_point(conn, DataPointRow(
                    type="memory", content=chunk.content, scope=chunk.scope,
                    entry_type=chunk.entry_type, source_type=chunk.source_type,
                    salience=chunk.salience, content_hash=h,
                    created_at=chunk.created_at,
                ))
            else:
                # v2: write to chunks table
                chunk.content_hash = h
                insert_chunk(conn, chunk)
            stats.chunks_inserted += 1

    global_file = get_memory_dir() / 'global-long-term-memory.md'
    if global_file.exists():
        content = global_file.read_text(encoding='utf-8')
        chunks = _parse_ltm_entries(content, global_file.name, 'global')
        _insert_chunks(chunks)
        stats.ltm_files_processed += 1

    project_dir = get_memory_dir() / 'project-memory'
    if project_dir.exists():
        for ltm_file in sorted(project_dir.glob('*-long-term-memory.md')):
            content = ltm_file.read_text(encoding='utf-8')
            scope = ltm_file.stem.replace('-long-term-memory', '')
            chunks = _parse_ltm_entries(content, ltm_file.name, scope)
            _insert_chunks(chunks)
            stats.ltm_files_processed += 1

    daily_dir = get_memory_dir() / 'daily'
    if daily_dir.exists():
        for daily_file in sorted(daily_dir.glob('*.md')):
            content = daily_file.read_text(encoding='utf-8')
            chunks = _parse_daily_entries(content, daily_file.name)
            _insert_chunks(chunks)
            stats.daily_files_processed += 1

    # v2 only: re-run salience data migration for newly inserted chunks.
    # On v3, data_points are inserted with correct fields directly.
    if schema_version < 3:
        _migrate_salience_data(conn)

    conn.commit()
    return stats


# ============================================================================
# A6: Markdown archival utility
# ============================================================================


def _should_archive(conn: sqlite3.Connection) -> bool:
    """Return True if the data_points table has at least one row.

    Used as a migration safety guard: archival should only proceed if the v3
    migration successfully populated data_points.
    """
    count = conn.execute("SELECT COUNT(*) FROM data_points").fetchone()[0]
    return count > 0


def _archive_markdown_files(memory_dir: Path) -> None:
    """Move markdown memory files to .archive/ subdirectory.

    Moves:
    - daily/*.md
    - global-long-term-memory.md
    - project-memory/*-long-term-memory.md

    Preserves filenames with a timestamp prefix inside .archive/.
    Skips files that don't exist. Non-markdown state files (settings.json,
    memory.db, .synthesis-state.json) are never touched.
    """
    archive_dir = memory_dir / ".archive"
    archive_dir.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")

    global_ltm = memory_dir / "global-long-term-memory.md"
    if global_ltm.exists():
        global_ltm.rename(archive_dir / f"global-long-term-memory-{timestamp}.md")

    daily_dir = memory_dir / "daily"
    if daily_dir.exists():
        for f in daily_dir.glob("*.md"):
            f.rename(archive_dir / f"daily-{f.name}")

    project_dir = memory_dir / "project-memory"
    if project_dir.exists():
        for f in project_dir.glob("*-long-term-memory.md"):
            f.rename(archive_dir / f"project-{f.name}")
