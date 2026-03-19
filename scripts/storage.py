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

from memory_utils import get_db_path, get_memory_dir  # noqa: E402

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
