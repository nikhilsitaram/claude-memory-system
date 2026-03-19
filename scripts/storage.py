#!/usr/bin/env python3
"""
SQLite storage layer for Claude Code Memory System.

Provides DB connection helpers, schema creation, and CRUD operations
for the unified memory.db (graph nodes, edges, content chunks).

The DB coexists with markdown files -- it is a read-optimized index,
not a replacement for the markdown source of truth.

Requirements: Python 3.9+
"""

import hashlib
import re
import sqlite3
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import get_db_path  # noqa: E402

# Schema version -- increment when schema changes require migration
SCHEMA_VERSION = 1

# Full DDL for initial schema creation
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

# Optional: vec_chunks virtual table (requires sqlite-vec extension)
VEC_CHUNKS_DDL = """\
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
    embedding float[384],
    +chunk_id TEXT,
    +source_type TEXT
);
"""

__all__ = [
    "SCHEMA_VERSION",
    "SCHEMA_DDL",
    "VEC_CHUNKS_DDL",
    "ChunkRow",
    "NodeRow",
    "EdgeRow",
    "ensure_db",
    "get_db",
    "close_db",
    "_get_schema_version",
    "insert_chunk",
    "query_chunks_by_scope",
    "query_chunks_by_source",
    "delete_chunks_by_source",
    "insert_node",
    "query_nodes_by_scope",
    "query_node_by_name_and_type",
    "update_node_access",
    "insert_edge",
]


@dataclass
class ChunkRow:
    """Represents a row in the chunks table."""
    content: str
    source_file: str
    source_type: str  # 'ltm', 'daily', 'triplet'
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
    source_sessions: Optional[str] = None  # JSON array
    evidence_count: int = 1
    entities: Optional[str] = None  # JSON
    id: Optional[str] = None  # Auto-generated if None


@dataclass
class NodeRow:
    """Represents a row in the nodes table."""
    name: str
    type: str  # project, tool, library, convention, person, file
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
    id: Optional[str] = None  # Auto-generated if None


@dataclass
class EdgeRow:
    """Represents a row in the edges table."""
    source: str  # node ID
    target: str  # node ID
    type: str  # uses, prefers, depends_on, related_to, supersedes
    fact: Optional[str] = None
    properties: Optional[str] = None  # JSON
    created_at: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    expired_at: Optional[str] = None
    weight: float = 1.0
    source_sessions: Optional[str] = None
    id: Optional[str] = None  # Auto-generated if None


def _get_schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute('PRAGMA user_version').fetchone()[0]


def _migrate_schema(conn: sqlite3.Connection, current_version: int) -> None:
    pass  # v1 is the initial schema -- no migrations needed yet


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


# ============================================================================
# Chunk CRUD (A5)
# ============================================================================

def insert_chunk(conn: sqlite3.Connection, chunk: ChunkRow) -> str:
    """Insert a chunk row into the chunks table.

    Auto-generates id and content_hash if not set on the ChunkRow.
    Returns the chunk ID.
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


# Column list is the single source of truth for SELECT order.
# _row_to_chunk maps by position -- keep these two in sync.
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
    """Delete all chunks from a specific source file. Returns count deleted."""
    cursor = conn.execute(
        "DELETE FROM chunks WHERE source_file = ?", (source_file,)
    )
    return cursor.rowcount


# ============================================================================
# Node and Edge CRUD (A6)
# ============================================================================

# Column list is the single source of truth for SELECT order.
# _row_to_node maps by position -- keep these two in sync.
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
    """Insert a node row into the nodes table. Returns the node ID."""
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
    """Increment access_count and set last_accessed to now (UTC ISO)."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn.execute(
        "UPDATE nodes SET access_count = access_count + 1, "
        "last_accessed = ? WHERE id = ?",
        (now, node_id),
    )


def insert_edge(conn: sqlite3.Connection, edge: EdgeRow) -> str:
    """Insert an edge row into the edges table. Returns the edge ID."""
    edge_id = edge.id or _generate_id()
    conn.execute(
        "INSERT INTO edges "
        "(id, source, target, type, fact, properties, created_at, "
        "valid_from, valid_to, expired_at, weight, source_sessions) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            edge_id, edge.source, edge.target, edge.type, edge.fact,
            edge.properties, edge.created_at, edge.valid_from,
            edge.valid_to, edge.expired_at, edge.weight,
            edge.source_sessions,
        ),
    )
    return edge_id
