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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import get_db_path, get_memory_dir  # noqa: E402

SCHEMA_VERSION = 1

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
    "MigrationStats",
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
    "batch_update_access",
    "update_chunk_salience",
    "update_node_salience",
    "query_chunks_with_salience",
    "migrate_markdown_to_db",
    "_parse_ltm_entries",
    "_parse_daily_entries",
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
    fact: Optional[str] = None
    properties: Optional[str] = None
    created_at: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    expired_at: Optional[str] = None
    weight: float = 1.0
    source_sessions: Optional[str] = None
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


def _migrate_schema(conn: sqlite3.Connection, current_version: int) -> None:
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


_LTM_ENTRY_RE = re.compile(
    r'^\s*-\s*\((\d{4}-\d{2}-\d{2})\)\s*\[([^\]]+)\]\s*(.+)'
)


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
    """
    stats = MigrationStats()
    existing_hashes = {
        row[0] for row in conn.execute(
            'SELECT content_hash FROM chunks WHERE content_hash IS NOT NULL'
        ).fetchall()
    }

    def _insert_chunks(chunks: list[ChunkRow]) -> None:
        for chunk in chunks:
            h = _content_hash(chunk.content)
            if h in existing_hashes:
                stats.chunks_skipped += 1
                continue
            chunk.content_hash = h
            insert_chunk(conn, chunk)
            existing_hashes.add(h)
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

    conn.commit()
    return stats
