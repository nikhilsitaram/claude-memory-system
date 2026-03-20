---
status: In Development
---

# Storage Foundation Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use orchestrate

**Goal:** Deliver a SQLite+WAL storage layer (`memory.db`) with full schema, connection helpers, markdown-to-DB migration pipeline, provenance columns, and health diagnostics -- all non-breaking and additive alongside the existing markdown system.

**Architecture:** A single `scripts/storage.py` module owns DB lifecycle (creation, connection, schema migration, CRUD). A `scripts/health.py` module provides diagnostic queries. `install.py` calls `ensure_db()` during installation. The existing markdown read/write paths remain untouched -- the DB is a read-optimized index built from markdown content.

**Tech Stack:** Python 3.9+ stdlib `sqlite3`, WAL journal mode, `pathlib` paths, `hashlib` for content hashing, `uuid` for IDs, `pytest` with `tmp_path` for testing.

---

## Phase A -- Schema and Connection Layer
**Status:** Complete | **Rationale:** All subsequent tasks depend on the DB file existing with the correct schema and connection helpers being available. This phase establishes the foundation that Phase B (migration, health, install integration) builds upon.

### Phase A Checklist
- [x] A1: Integration test skeleton for storage module
- [x] A2: Add get_db_path to memory_utils
- [x] A3: Schema constants and DDL
- [x] A4: Connection helpers and DB lifecycle
- [x] A5: CRUD operations for chunks table
- [x] A6: CRUD operations for nodes and edges tables

### Phase A Completion Notes
<!-- Written by dispatcher after all tasks complete.
     Implementation review changes appended here by orchestrator. -->

**Completed:** 2026-03-19 | **HEAD:** 8939a69

**Summary:** All 6 tasks (A1-A6) implemented and verified. The `scripts/storage.py` module provides the complete SQLite storage foundation with schema creation, connection lifecycle, and CRUD operations for chunks, nodes, and edges.

**Test results:** 19/19 tests pass in `test_storage.py`, 148/148 pass in `test_memory_utils.py`.

**Public API (storage.py):**
- Constants: `SCHEMA_VERSION` (1), `SCHEMA_DDL`, `VEC_CHUNKS_DDL`
- Dataclasses: `ChunkRow`, `NodeRow`, `EdgeRow`
- Lifecycle: `ensure_db()` -> Connection, `get_db()` -> Connection, `close_db(conn)`
- Schema: `_get_schema_version(conn)` -> int
- Chunk CRUD: `insert_chunk(conn, chunk)` -> str, `query_chunks_by_scope(conn, scope)` -> list[ChunkRow], `query_chunks_by_source(conn, source_file)` -> list[ChunkRow], `delete_chunks_by_source(conn, source_file)` -> int
- Node CRUD: `insert_node(conn, node)` -> str, `query_nodes_by_scope(conn, scope)` -> list[NodeRow], `query_node_by_name_and_type(conn, name, type)` -> Optional[NodeRow], `update_node_access(conn, node_id)`
- Edge CRUD: `insert_edge(conn, edge)` -> str
- Internal: `_content_hash(text)` -> str, `_generate_id()` -> str

**Path helper (memory_utils.py):** `get_db_path()` -> Path (returns `get_memory_dir() / "memory.db"`)

**Deviations:**
- A4/A5/A6 committed together (single commit) because test_storage.py imports all functions at module level -- no individual task could produce a green test run in isolation.
- Fixed missing `datetime`/`timezone` and `re` imports that were in the plan spec but not in the A3 commit.

**Implementation review fixes (orchestrator):**
- Removed unused imports (`re`, `field`) from storage.py
- Added 2 tests for `get_db()`: `test_get_db_raises_when_no_db`, `test_get_db_returns_connection` (21 tests now)
- Deferred: `nodes(name, type)` uniqueness constraint — nodes are not created by B1 migration, only by future phases. Will add UNIQUE(name, type, scope) when node creation is implemented.

### Phase A Tasks

#### A1: Integration test skeleton for storage module
**Files:**
- Create: `tests/test_storage.py`

**Verification:** `python3 -m pytest tests/test_storage.py -v` -- all tests should FAIL (RED phase of double-loop TDD)

**Done when:** Test file exists with integration tests that import from `storage` and exercise the full lifecycle: create DB, insert chunks from markdown content, query by scope, verify nodes/edges, check WAL mode. All tests fail with `ImportError` or `AttributeError` since `storage.py` does not exist yet.

**Avoid:** Do not create `scripts/storage.py` yet -- this task only creates the test file. Do not use `tempfile` -- use pytest `tmp_path` fixture per CLAUDE.md conventions.

**Step 1: Create test file with imports and fixtures**

Create `tests/test_storage.py` with the following content. These tests define the public API contract that subsequent tasks implement.

```python
#!/usr/bin/env python3
"""
Integration and unit tests for scripts/storage.py

Tests cover: schema creation, WAL mode, connection helpers, CRUD operations,
content hashing, and provenance tracking.

Run with: python3 -m pytest tests/test_storage.py -v
"""

import hashlib
import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

# These imports will fail until storage.py is created (RED phase)
from storage import (
    SCHEMA_VERSION,
    ChunkRow,
    EdgeRow,
    NodeRow,
    close_db,
    delete_chunks_by_source,
    ensure_db,
    get_db,
    insert_chunk,
    insert_edge,
    insert_node,
    query_chunks_by_scope,
    query_chunks_by_source,
    query_node_by_name_and_type,
    query_nodes_by_scope,
    update_node_access,
)


# ============================================================================
# Fixtures
# ============================================================================


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


# ============================================================================
# Schema and lifecycle
# ============================================================================


class TestSchemaCreation:
    def test_ensure_db_creates_file(self, db_dir):
        conn = ensure_db()
        assert (db_dir / "memory.db").exists()
        close_db(conn)

    def test_wal_mode_enabled(self, db):
        result = db.execute("PRAGMA journal_mode").fetchone()
        assert result[0] == "wal"

    def test_busy_timeout_set(self, db):
        result = db.execute("PRAGMA busy_timeout").fetchone()
        assert result[0] == 5000

    def test_foreign_keys_enabled(self, db):
        result = db.execute("PRAGMA foreign_keys").fetchone()
        assert result[0] == 1

    def test_schema_version_stored(self, db):
        result = db.execute("PRAGMA user_version").fetchone()
        assert result[0] == SCHEMA_VERSION

    def test_tables_exist(self, db):
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "nodes" in tables
        assert "edges" in tables
        assert "chunks" in tables

    def test_indexes_exist(self, db):
        indexes = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        expected = {
            "idx_nodes_type",
            "idx_nodes_scope",
            "idx_nodes_simhash",
            "idx_edges_source",
            "idx_edges_target",
            "idx_edges_valid",
            "idx_chunks_source",
            "idx_chunks_scope",
            "idx_chunks_hash",
            "idx_chunks_simhash",
        }
        assert expected.issubset(indexes)

    def test_ensure_db_idempotent(self, db_dir):
        conn1 = ensure_db()
        conn1.execute(
            "INSERT INTO nodes (id, name, type, created_at) "
            "VALUES ('n1', 'test', 'tool', '2026-01-01')"
        )
        conn1.commit()
        close_db(conn1)
        # Second call should not drop data
        conn2 = ensure_db()
        row = conn2.execute(
            "SELECT name FROM nodes WHERE id='n1'"
        ).fetchone()
        assert row is not None
        assert row[0] == "test"
        close_db(conn2)


# ============================================================================
# Chunk CRUD
# ============================================================================


class TestChunkCRUD:
    def test_insert_and_query_by_scope(self, db):
        chunk = ChunkRow(
            content="Use pytest tmp_path for isolation",
            source_file="global-long-term-memory.md",
            source_type="ltm",
            section="## Key Learnings",
            scope="global",
            entry_type="pattern",
            chunk_index=0,
            created_at="2026-03-01",
        )
        insert_chunk(db, chunk)
        results = query_chunks_by_scope(db, "global")
        assert len(results) == 1
        assert results[0].content == chunk.content
        assert results[0].scope == "global"

    def test_insert_generates_id_and_hash(self, db):
        chunk = ChunkRow(
            content="Test content",
            source_file="test.md",
            source_type="daily",
            scope="global",
            chunk_index=0,
            created_at="2026-03-01",
        )
        insert_chunk(db, chunk)
        row = db.execute("SELECT id, content_hash FROM chunks").fetchone()
        assert row[0]  # id is non-empty
        expected_hash = hashlib.sha256(b"Test content").hexdigest()[:16]
        assert row[1] == expected_hash

    def test_query_by_source_file(self, db):
        for i in range(3):
            insert_chunk(
                db,
                ChunkRow(
                    content=f"Entry {i}",
                    source_file="2026-03-01.md",
                    source_type="daily",
                    scope="global",
                    chunk_index=i,
                    created_at="2026-03-01",
                ),
            )
        insert_chunk(
            db,
            ChunkRow(
                content="Other",
                source_file="2026-03-02.md",
                source_type="daily",
                scope="global",
                chunk_index=0,
                created_at="2026-03-02",
            ),
        )
        results = query_chunks_by_source(db, "2026-03-01.md")
        assert len(results) == 3

    def test_delete_by_source(self, db):
        insert_chunk(
            db,
            ChunkRow(
                content="To delete",
                source_file="old.md",
                source_type="ltm",
                scope="global",
                chunk_index=0,
                created_at="2026-01-01",
            ),
        )
        assert len(query_chunks_by_source(db, "old.md")) == 1
        count = delete_chunks_by_source(db, "old.md")
        assert count == 1
        assert len(query_chunks_by_source(db, "old.md")) == 0

    def test_provenance_columns(self, db):
        chunk = ChunkRow(
            content="Provenance test",
            source_file="test.md",
            source_type="ltm",
            scope="global",
            chunk_index=0,
            created_at="2026-03-01",
            source_sessions=json.dumps(["2026-03-01", "2026-03-02"]),
            evidence_count=2,
        )
        insert_chunk(db, chunk)
        results = query_chunks_by_scope(db, "global")
        assert results[0].evidence_count == 2
        sessions = json.loads(results[0].source_sessions)
        assert len(sessions) == 2


# ============================================================================
# Node and edge CRUD
# ============================================================================


class TestNodeCRUD:
    def test_insert_and_query_by_scope(self, db):
        node = NodeRow(
            name="claude-memory-system",
            type="project",
            scope="global",
            description="Memory persistence for Claude Code",
            created_at="2026-03-01",
        )
        insert_node(db, node)
        results = query_nodes_by_scope(db, "global")
        assert len(results) == 1
        assert results[0].name == "claude-memory-system"

    def test_query_by_name_and_type(self, db):
        insert_node(
            db,
            NodeRow(
                name="pytest",
                type="tool",
                scope="global",
                created_at="2026-03-01",
            ),
        )
        result = query_node_by_name_and_type(db, "pytest", "tool")
        assert result is not None
        assert result.name == "pytest"

    def test_query_by_name_and_type_not_found(self, db):
        result = query_node_by_name_and_type(db, "nonexistent", "tool")
        assert result is None

    def test_update_access(self, db):
        node = NodeRow(
            name="test-node",
            type="tool",
            scope="global",
            created_at="2026-03-01",
        )
        insert_node(db, node)
        results = query_nodes_by_scope(db, "global")
        node_id = results[0].id
        update_node_access(db, node_id)
        updated = query_nodes_by_scope(db, "global")
        assert updated[0].access_count == 1
        assert updated[0].last_accessed is not None


class TestEdgeCRUD:
    def test_insert_edge(self, db):
        insert_node(
            db,
            NodeRow(
                name="project-a", type="project", scope="global",
                created_at="2026-03-01",
            ),
        )
        insert_node(
            db,
            NodeRow(
                name="pytest", type="tool", scope="global",
                created_at="2026-03-01",
            ),
        )
        src = query_node_by_name_and_type(db, "project-a", "project")
        tgt = query_node_by_name_and_type(db, "pytest", "tool")
        edge = EdgeRow(
            source=src.id,
            target=tgt.id,
            type="uses",
            fact="project-a uses pytest for testing",
            created_at="2026-03-01",
        )
        insert_edge(db, edge)
        rows = db.execute("SELECT * FROM edges").fetchall()
        assert len(rows) == 1

    def test_edge_provenance(self, db):
        insert_node(
            db,
            NodeRow(
                name="n1", type="tool", scope="global",
                created_at="2026-03-01",
            ),
        )
        insert_node(
            db,
            NodeRow(
                name="n2", type="library", scope="global",
                created_at="2026-03-01",
            ),
        )
        src = query_node_by_name_and_type(db, "n1", "tool")
        tgt = query_node_by_name_and_type(db, "n2", "library")
        edge = EdgeRow(
            source=src.id,
            target=tgt.id,
            type="depends_on",
            created_at="2026-03-01",
            source_sessions=json.dumps(["2026-03-01"]),
        )
        insert_edge(db, edge)
        row = db.execute(
            "SELECT source_sessions FROM edges WHERE source=?", (src.id,)
        ).fetchone()
        assert "2026-03-01" in row[0]
```

---

#### A2: Add get_db_path to memory_utils
**Files:**
- Modify: `scripts/memory_utils.py`
- Modify: `tests/test_memory_utils.py`

**Verification:** `python3 -m pytest tests/test_memory_utils.py -v`

**Done when:** `get_db_path()` is exported from `memory_utils.py` and returns `get_memory_dir() / "memory.db"`. The `__all__` list includes `get_db_path`. Test verifies the path and the export. This establishes the canonical path helper before `storage.py` is created, so `storage.py` can import it from the start.

**Avoid:** Do not add any storage/DB logic to memory_utils -- only the path helper. The storage module will own all DB operations.

**Step 1: Add get_db_path function to memory_utils.py**

In `scripts/memory_utils.py`, add the following function after `get_synthesis_error_log()` (around line 209):

```python
def get_db_path() -> Path:
    """Get the path to the unified memory database (memory.db)."""
    return get_memory_dir() / "memory.db"
```

**Step 2: Add to __all__**

In the `__all__` list in `scripts/memory_utils.py`, add `"get_db_path"` after `"get_synthesis_error_log"` (around line 46):

```python
    "get_synthesis_error_log",
    "get_db_path",
    "collect_ltm_files",
```

**Step 3: Add tests**

Add to `tests/test_memory_utils.py`:

```python
class TestGetDbPath:
    def test_returns_memory_db_path(self, tmp_path):
        with mock.patch("memory_utils.get_memory_dir", return_value=tmp_path):
            from memory_utils import get_db_path
            result = get_db_path()
            assert result == tmp_path / "memory.db"

    def test_in_all_exports(self):
        from memory_utils import __all__
        assert "get_db_path" in __all__
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/test_memory_utils.py::TestGetDbPath -v
```

---

#### A3: Schema constants and DDL
**Files:**
- Create: `scripts/storage.py`

**Verification:** `python3 -c "from storage import SCHEMA_VERSION, ChunkRow, NodeRow, EdgeRow; print('OK')"` -- imports succeed. `python3 -m pytest tests/test_storage.py::TestSchemaCreation -v` -- schema tests still RED (no functions yet, but constants importable)

**Done when:** `SCHEMA_VERSION`, `SCHEMA_DDL`, `__all__`, and all dataclass definitions (`ChunkRow`, `NodeRow`, `EdgeRow`) are importable from `storage`. The DDL string matches the design doc schema exactly (including all provenance columns from #55 and simhash from #53). Tests still fail because `ensure_db` etc. are not yet implemented.

**Avoid:** Do not implement any functions yet -- only constants, `__all__`, and dataclasses. Do not add `vec_chunks` virtual table to the DDL -- sqlite-vec is a Worktree 3 dependency; we define the SQL constant but gate creation behind a `has_sqlite_vec()` check (added when Worktree 3 lands). Do not use `TypedDict` -- use `@dataclass` for consistency with existing `synthesis.py` patterns.

**Step 1: Create storage.py with schema version and DDL string**

Create `scripts/storage.py`:

```python
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
```

**Step 2: Add `__all__` exports**

Append to `scripts/storage.py` after the DDL constants (before the dataclasses). This list will be extended in A4-A6 as functions are added.

```python
__all__ = [
    "SCHEMA_VERSION",
    "SCHEMA_DDL",
    "VEC_CHUNKS_DDL",
    "ChunkRow",
    "NodeRow",
    "EdgeRow",
]
```

**Step 3: Add dataclass definitions**

Append to `scripts/storage.py` after the `__all__` list:

```python
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
```

---

#### A4: Connection helpers and DB lifecycle
**Files:**
- Modify: `scripts/storage.py`

**Verification:** `python3 -m pytest tests/test_storage.py::TestSchemaCreation -v` -- all schema tests GREEN

**Done when:** `ensure_db()`, `get_db()`, `close_db()`, `_get_schema_version()`, and `_migrate_schema()` are implemented. `ensure_db()` creates the DB file with WAL mode, `busy_timeout=5000`, `foreign_keys=ON`, sets `user_version` to `SCHEMA_VERSION`, and executes `SCHEMA_DDL`. If the DB already exists with an older version, `_migrate_schema()` is called (no-op for v1). Calling `ensure_db()` twice does not drop existing data (idempotent via `CREATE IF NOT EXISTS`). The `TestSchemaCreation` class passes. `__all__` is extended with `"ensure_db"`, `"get_db"`, `"close_db"`, `"_get_schema_version"`.

**Avoid:** Do not attempt to create `vec_chunks` -- that requires the sqlite-vec extension which is a Worktree 3 dependency. The `VEC_CHUNKS_DDL` constant exists for documentation but is not executed. Do not use connection pooling -- each caller opens/closes its own connection per the design doc.

**Step 1: Implement ensure_db and schema migration stub**

Add to `scripts/storage.py`:

```python
def _get_schema_version(conn: sqlite3.Connection) -> int:
    """Read the current schema version from the database."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _migrate_schema(conn: sqlite3.Connection, current_version: int) -> None:
    """Run incremental schema migrations from current_version to SCHEMA_VERSION.

    Currently a no-op for v1. Future schema changes add elif branches:
        if current_version < 2:
            conn.executescript("ALTER TABLE chunks ADD COLUMN ...")
    """
    pass  # v1 is the initial schema -- no migrations needed yet


def ensure_db() -> sqlite3.Connection:
    """Create or open the memory database with WAL mode and full schema.

    Idempotent -- safe to call multiple times. Uses CREATE IF NOT EXISTS
    so existing data is never dropped. If the DB exists with an older
    schema version, runs incremental migrations.

    Returns an open connection. Caller must call close_db() when done.
    """
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")

    current_version = _get_schema_version(conn)
    conn.executescript(SCHEMA_DDL)

    if current_version < SCHEMA_VERSION:
        _migrate_schema(conn, current_version)

    # PRAGMA doesn't support parameterized queries; guard the constant type
    assert isinstance(SCHEMA_VERSION, int), "SCHEMA_VERSION must be int"
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.commit()
    return conn
```

**Step 2: Implement get_db and close_db**

```python
def get_db() -> sqlite3.Connection:
    """Open a read/write connection to an existing memory database.

    Unlike ensure_db(), this does not create the schema -- it assumes
    the DB already exists. Sets WAL pragmas for the connection.

    Raises FileNotFoundError if the DB does not exist.
    """
    db_path = get_db_path()
    if not db_path.exists():
        raise FileNotFoundError(f"Memory database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def close_db(conn: sqlite3.Connection) -> None:
    """Close a database connection safely."""
    if conn:
        conn.close()
```

**Step 3: Add content_hash and ID generation helpers (used by CRUD in A5)**

```python
def _content_hash(text: str) -> str:
    """Generate a truncated SHA-256 hash of text content.

    Returns first 16 hex characters (64 bits) -- sufficient for
    change detection without excessive storage.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _generate_id() -> str:
    """Generate a unique ID for a row."""
    return uuid.uuid4().hex[:12]
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/test_storage.py::TestSchemaCreation -v
```

All 8 tests in `TestSchemaCreation` should pass. Other test classes still fail (CRUD not implemented).

---

#### A5: CRUD operations for chunks table
**Files:**
- Modify: `scripts/storage.py`

**Verification:** `python3 -m pytest tests/test_storage.py::TestChunkCRUD -v` -- all chunk tests GREEN

**Done when:** `insert_chunk()`, `query_chunks_by_scope()`, `query_chunks_by_source()`, and `delete_chunks_by_source()` are implemented. `insert_chunk` auto-generates `id` and `content_hash` when not provided. All `TestChunkCRUD` tests pass. `__all__` is extended with `"insert_chunk"`, `"query_chunks_by_scope"`, `"query_chunks_by_source"`, `"delete_chunks_by_source"`.

**Avoid:** Do not auto-commit inside CRUD functions -- let callers control transactions. Use `conn.commit()` only after batch operations in higher-level functions. The test fixture's `ensure_db()` already commits the schema, and individual test methods can rely on SQLite's implicit transaction for single-connection reads of uncommitted writes (autocommit behavior in Python sqlite3 for DML within the same connection).

**Step 1: Implement insert_chunk**

```python
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
```

**Step 2: Implement query functions**

```python
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
```

**Step 3: Implement delete function**

```python
def delete_chunks_by_source(
    conn: sqlite3.Connection, source_file: str
) -> int:
    """Delete all chunks from a specific source file. Returns count deleted."""
    cursor = conn.execute(
        "DELETE FROM chunks WHERE source_file = ?", (source_file,)
    )
    return cursor.rowcount
```

**Step 4: Run tests**

```bash
python3 -m pytest tests/test_storage.py::TestChunkCRUD -v
```

All 5 tests in `TestChunkCRUD` should pass.

---

#### A6: CRUD operations for nodes and edges tables
**Files:**
- Modify: `scripts/storage.py`

**Verification:** `python3 -m pytest tests/test_storage.py::TestNodeCRUD tests/test_storage.py::TestEdgeCRUD -v` -- all node/edge tests GREEN

**Done when:** `insert_node()`, `query_nodes_by_scope()`, `query_node_by_name_and_type()`, `update_node_access()`, and `insert_edge()` are implemented. All `TestNodeCRUD` and `TestEdgeCRUD` tests pass. Full test suite (`python3 -m pytest tests/test_storage.py -v`) is GREEN. `__all__` is extended with `"insert_node"`, `"query_nodes_by_scope"`, `"query_node_by_name_and_type"`, `"update_node_access"`, `"insert_edge"`.

**Avoid:** Do not add complex graph traversal queries yet -- keep to simple single-table CRUD. Graph traversal is a Phase 2 concern. Do not add cascade deletes on edges when nodes are deleted -- that logic belongs to higher-level migration/cleanup functions.

**Step 1: Implement node CRUD**

```python
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
```

**Step 2: Implement edge CRUD**

```python
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
```

**Step 3: Run full Phase A test suite**

```bash
python3 -m pytest tests/test_storage.py -v
```

All tests in `TestSchemaCreation`, `TestChunkCRUD`, `TestNodeCRUD`, and `TestEdgeCRUD` should be GREEN.

---

## Phase B -- Migration, Health, and Install Integration
**Status:** Not Started | **Rationale:** With the storage layer in place from Phase A, this phase populates the DB from existing markdown files, adds diagnostic queries, and wires everything into the installer. These tasks consume the Phase A API and can only run after it is complete.

### Phase B Checklist
- [ ] B1: Markdown-to-DB migration pipeline
- [ ] B2: Health diagnostics script
- [ ] B3: Install.py integration

### Phase B Completion Notes
<!-- Written by dispatcher after all tasks complete.
     Implementation review changes appended here by orchestrator. -->

### Phase B Tasks

#### B1: Markdown-to-DB migration pipeline
> **Handoff from A6:** Confirmed -- storage.py public API matches planned design exactly. Key imports for B1: `ensure_db`, `close_db`, `insert_chunk`, `ChunkRow`, `_content_hash`, `query_chunks_by_source` from `storage`; `get_db_path`, `get_memory_dir` from `memory_utils`. No signature changes from planned design. The `re` module is already imported in storage.py.

**Files:**
- Modify: `scripts/storage.py`
- Create: `tests/test_migration.py`

**Verification:** `python3 -m pytest tests/test_migration.py -v`

**Done when:** `migrate_markdown_to_db()` scans all LTM files (global + project) and daily files, parses them into chunks using entry-level boundaries, and inserts them into the DB. Content hashing ensures re-running migration skips unchanged content. Running migration twice on the same files produces no duplicates. The function returns a `MigrationStats` dataclass with counts. `__all__` is extended with `"MigrationStats"`, `"migrate_markdown_to_db"`.

**Avoid:** Do not modify any existing markdown files during migration -- the DB is additive. Do not chunk at sub-paragraph level -- each `- (date) [type] description` LTM entry is one chunk, and each `- [scope/type] description` daily entry is one chunk. Paragraph chunking with overlap is Worktree 2's responsibility (`chunking.py`); this migration uses the natural entry boundaries. Do not import from `chunking.py` since it does not exist yet.

**Step 1: Define migration stats dataclass**

Add to `scripts/storage.py` after the existing dataclass definitions:

```python
@dataclass
class MigrationStats:
    """Statistics from a markdown-to-DB migration run."""
    ltm_files_processed: int = 0
    daily_files_processed: int = 0
    chunks_inserted: int = 0
    chunks_skipped: int = 0  # Already in DB (same content_hash)
```

**Step 2: Implement LTM file parser**

Add to `scripts/storage.py` (the `import re` is already at the top from A3):

```python
# Patterns for parsing LTM entries
_LTM_ENTRY_RE = re.compile(
    r"^\s*-\s*\((\d{4}-\d{2}-\d{2})\)\s*\[([^\]]+)\]\s*(.+)"
)


def _parse_ltm_entries(
    content: str, source_file: str, scope: str
) -> list[ChunkRow]:
    """Parse an LTM markdown file into ChunkRow objects.

    Each '- (date) [type] description' line becomes one chunk.
    Section headers (## Key Actions, etc.) are tracked for the section field.
    Lines without the dated entry pattern are skipped.
    """
    chunks = []
    current_section = None
    chunk_index = 0

    for line in content.split("\n"):
        if line.startswith("## "):
            current_section = line.strip()
            continue

        match = _LTM_ENTRY_RE.match(line)
        if match:
            entry_date, entry_type, description = (
                match.group(1),
                match.group(2),
                match.group(3).strip(),
            )
            full_content = line.strip()
            chunks.append(
                ChunkRow(
                    content=full_content,
                    source_file=source_file,
                    source_type="ltm",
                    section=current_section,
                    scope=scope,
                    entry_type=entry_type,
                    chunk_index=chunk_index,
                    created_at=entry_date,
                )
            )
            chunk_index += 1

    return chunks
```

**Step 3: Implement daily file parser**

```python
def _parse_daily_entries(
    content: str, source_file: str
) -> list[ChunkRow]:
    """Parse a daily markdown file into ChunkRow objects.

    Each '- [scope/type] description' line becomes one chunk.
    Routed entries (prefixed with [routed]) are skipped.
    """
    chunks = []
    current_section = None
    chunk_index = 0
    # Extract date from filename (YYYY-MM-DD.md)
    date_str = Path(source_file).stem if source_file else None

    for line in content.split("\n"):
        if line.startswith("## "):
            current_section = line.strip()
            continue

        stripped = line.strip()
        if not stripped.startswith("- "):
            continue

        # Skip routed entries
        if "[routed]" in stripped:
            continue

        # Try to parse scope/type tag
        tag_match = re.match(
            r"^\s*-\s*\[([^\]/]+)(?:/([^\]]+))?\]\s*(.+)", stripped
        )
        if tag_match:
            scope_part = tag_match.group(1).strip().lower()
            entry_type = tag_match.group(2).strip() if tag_match.group(2) else None

            # Multi-scope entries (scope1|scope2) -- use first scope
            if "|" in scope_part:
                scope_part = scope_part.split("|")[0].strip()

            chunks.append(
                ChunkRow(
                    content=stripped,
                    source_file=source_file,
                    source_type="daily",
                    section=current_section,
                    scope=scope_part,
                    entry_type=entry_type,
                    chunk_index=chunk_index,
                    created_at=date_str,
                )
            )
            chunk_index += 1

    return chunks
```

**Step 4: Implement the migration function**

```python
def migrate_markdown_to_db(conn: sqlite3.Connection) -> MigrationStats:
    """Migrate existing markdown memory files into the database.

    Scans:
    - Global LTM: ~/.claude/memory/global-long-term-memory.md
    - Project LTMs: ~/.claude/memory/project-memory/*-long-term-memory.md
    - Daily files: ~/.claude/memory/daily/*.md

    Uses content_hash to skip chunks that already exist in the DB.
    Idempotent -- safe to run multiple times.

    Returns MigrationStats with counts of processed files and chunks.
    """
    stats = MigrationStats()

    # Collect existing content hashes for dedup
    existing_hashes = {
        row[0]
        for row in conn.execute(
            "SELECT content_hash FROM chunks WHERE content_hash IS NOT NULL"
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

    # Global LTM
    global_file = get_memory_dir() / "global-long-term-memory.md"
    if global_file.exists():
        content = global_file.read_text(encoding="utf-8")
        chunks = _parse_ltm_entries(content, global_file.name, "global")
        _insert_chunks(chunks)
        stats.ltm_files_processed += 1

    # Project LTMs
    project_dir = get_memory_dir() / "project-memory"
    if project_dir.exists():
        for ltm_file in sorted(project_dir.glob("*-long-term-memory.md")):
            content = ltm_file.read_text(encoding="utf-8")
            # Derive scope from filename: "my-project-long-term-memory.md" -> "my-project"
            scope = ltm_file.stem.replace("-long-term-memory", "")
            chunks = _parse_ltm_entries(content, ltm_file.name, scope)
            _insert_chunks(chunks)
            stats.ltm_files_processed += 1

    # Daily files
    daily_dir = get_memory_dir() / "daily"
    if daily_dir.exists():
        for daily_file in sorted(daily_dir.glob("*.md")):
            content = daily_file.read_text(encoding="utf-8")
            chunks = _parse_daily_entries(content, daily_file.name)
            _insert_chunks(chunks)
            stats.daily_files_processed += 1

    conn.commit()
    return stats
```

**Step 5: Create test file**

Create `tests/test_migration.py`:

```python
#!/usr/bin/env python3
"""Tests for markdown-to-DB migration pipeline in storage.py."""

import json
from pathlib import Path
from unittest import mock

import pytest

from storage import (
    ChunkRow,
    MigrationStats,
    _parse_daily_entries,
    _parse_ltm_entries,
    close_db,
    ensure_db,
    migrate_markdown_to_db,
    query_chunks_by_scope,
    query_chunks_by_source,
)


@pytest.fixture
def db_dir(tmp_path):
    db_path = tmp_path / "memory.db"
    with mock.patch("storage.get_db_path", return_value=db_path):
        yield tmp_path


@pytest.fixture
def db(db_dir):
    conn = ensure_db()
    yield conn
    close_db(conn)


@pytest.fixture
def memory_dir(tmp_path):
    """Create a mock memory directory with sample markdown files."""
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "daily").mkdir()
    (mem / "project-memory").mkdir()

    # Global LTM
    (mem / "global-long-term-memory.md").write_text(
        "# Long-Term Memory\n\n"
        "## Key Learnings\n"
        "- (2026-03-01) [pattern] Use pytest tmp_path for isolation\n"
        "- (2026-03-02) [gotcha] SQLite WAL needs busy_timeout\n\n"
        "## Pinned\n"
        "- Important pinned item (no date)\n",
        encoding="utf-8",
    )

    # Project LTM
    (mem / "project-memory" / "my-project-long-term-memory.md").write_text(
        "# My Project\n\n"
        "## Key Actions\n"
        "- (2026-03-01) [implement] Built REST API\n",
        encoding="utf-8",
    )

    # Daily file
    (mem / "daily" / "2026-03-01.md").write_text(
        "# 2026-03-01\n\n"
        "## Actions\n"
        "- [my-project/implement] Built REST API endpoints\n"
        "- [global/document] Updated README\n\n"
        "## Learnings\n"
        "- [routed][global/pattern] Already routed entry\n"
        "- [global/gotcha] Watch out for WAL locks\n",
        encoding="utf-8",
    )

    return mem


class TestParseLtmEntries:
    def test_parses_dated_entries(self):
        content = (
            "## Key Learnings\n"
            "- (2026-03-01) [pattern] Use tmp_path\n"
            "- (2026-03-02) [gotcha] WAL needs timeout\n"
        )
        chunks = _parse_ltm_entries(content, "global-long-term-memory.md", "global")
        assert len(chunks) == 2
        assert chunks[0].entry_type == "pattern"
        assert chunks[0].created_at == "2026-03-01"
        assert chunks[0].section == "## Key Learnings"
        assert chunks[0].source_type == "ltm"

    def test_skips_non_entry_lines(self):
        content = (
            "# Title\n"
            "Some descriptive text\n"
            "<!-- comment -->\n"
        )
        chunks = _parse_ltm_entries(content, "test.md", "global")
        assert len(chunks) == 0

    def test_skips_pinned_undated_entries(self):
        """Pinned entries lack dates and are intentionally excluded from migration.

        These are human-curated items that don't follow the dated entry pattern.
        They remain in markdown only -- the DB indexes dated entries.
        """
        content = (
            "## Pinned\n"
            "- Important pinned item (no date)\n"
            "- Another pinned item\n\n"
            "## Key Learnings\n"
            "- (2026-03-01) [pattern] This one should be parsed\n"
        )
        chunks = _parse_ltm_entries(content, "test.md", "global")
        assert len(chunks) == 1
        assert chunks[0].entry_type == "pattern"

    def test_tracks_section_headers(self):
        content = (
            "## Key Actions\n"
            "- (2026-03-01) [implement] Built API\n"
            "## Key Learnings\n"
            "- (2026-03-02) [pattern] Use fixtures\n"
        )
        chunks = _parse_ltm_entries(content, "test.md", "global")
        assert chunks[0].section == "## Key Actions"
        assert chunks[1].section == "## Key Learnings"


class TestParseDailyEntries:
    def test_parses_tagged_entries(self):
        content = (
            "## Actions\n"
            "- [my-project/implement] Built API\n"
            "- [global/document] Updated docs\n"
        )
        chunks = _parse_daily_entries(content, "2026-03-01.md")
        assert len(chunks) == 2
        assert chunks[0].scope == "my-project"
        assert chunks[0].entry_type == "implement"
        assert chunks[1].scope == "global"

    def test_skips_routed_entries(self):
        content = (
            "## Actions\n"
            "- [routed][global/implement] Already routed\n"
            "- [global/implement] Not routed\n"
        )
        chunks = _parse_daily_entries(content, "2026-03-01.md")
        assert len(chunks) == 1
        assert "Not routed" in chunks[0].content

    def test_extracts_date_from_filename(self):
        chunks = _parse_daily_entries(
            "## Actions\n- [global/implement] Test\n",
            "2026-03-15.md",
        )
        assert chunks[0].created_at == "2026-03-15"

    def test_handles_multi_scope(self):
        content = "## Actions\n- [proj1|proj2/implement] Shared work\n"
        chunks = _parse_daily_entries(content, "2026-03-01.md")
        # Uses first scope
        assert chunks[0].scope == "proj1"


class TestMigrateMarkdownToDb:
    def test_full_migration(self, db, memory_dir):
        with mock.patch("storage.get_memory_dir", return_value=memory_dir):
            stats = migrate_markdown_to_db(db)
        assert stats.ltm_files_processed == 2  # global + 1 project
        assert stats.daily_files_processed == 1
        assert stats.chunks_inserted > 0

    def test_idempotent(self, db, memory_dir):
        with mock.patch("storage.get_memory_dir", return_value=memory_dir):
            stats1 = migrate_markdown_to_db(db)
            stats2 = migrate_markdown_to_db(db)
        assert stats2.chunks_inserted == 0
        assert stats2.chunks_skipped == stats1.chunks_inserted

    def test_global_scope(self, db, memory_dir):
        with mock.patch("storage.get_memory_dir", return_value=memory_dir):
            migrate_markdown_to_db(db)
        global_chunks = query_chunks_by_source(
            db, "global-long-term-memory.md"
        )
        assert len(global_chunks) == 2
        assert all(c.scope == "global" for c in global_chunks)

    def test_project_scope(self, db, memory_dir):
        with mock.patch("storage.get_memory_dir", return_value=memory_dir):
            migrate_markdown_to_db(db)
        project_chunks = query_chunks_by_source(
            db, "my-project-long-term-memory.md"
        )
        assert len(project_chunks) == 1
        assert project_chunks[0].scope == "my-project"

    def test_empty_memory_dir(self, db, tmp_path):
        empty_mem = tmp_path / "empty_memory"
        empty_mem.mkdir()
        with mock.patch("storage.get_memory_dir", return_value=empty_mem):
            stats = migrate_markdown_to_db(db)
        assert stats.ltm_files_processed == 0
        assert stats.daily_files_processed == 0
        assert stats.chunks_inserted == 0
```

**Step 6: Run tests**

```bash
python3 -m pytest tests/test_migration.py -v
```

---

#### B2: Health diagnostics script
> **Handoff from A6:** Confirmed -- storage.py exports match planned design. Key imports for B2: `get_db`, `close_db`, `_get_schema_version` from `storage`; `get_db_path` from `memory_utils`. For test fixtures: `ensure_db`, `insert_chunk`, `insert_node`, `ChunkRow`, `NodeRow`, `SCHEMA_VERSION` from `storage`. No signature changes.

**Files:**
- Create: `scripts/health.py`
- Create: `tests/test_health.py`

**Verification:** `python3 -m pytest tests/test_health.py -v`

**Done when:** `scripts/health.py` provides `health_report()` returning a `HealthReport` dataclass with: total_chunks, avg_salience, hot/warm/cold chunk counts, graph_nodes, active_edges, invalidated_edges, db_size_bytes. Also provides `health_alerts()` returning a list of alert strings for concerning conditions. CLI mode (`python3 health.py`) prints a formatted report. All tests pass.

**Avoid:** Do not import from `fastembed` or any vector dependencies -- health.py only queries the relational tables. Do not query `vec_chunks` since it may not exist (no sqlite-vec). Do not add SessionStart hook integration in this task -- that is future work per #63 design.

**Step 1: Create health.py**

Create `scripts/health.py`:

```python
#!/usr/bin/env python3
"""
Health diagnostics for Claude Code Memory System.

Queries memory.db for health metrics: chunk counts, salience distribution,
graph statistics, and potential issues.

Usage:
    python3 health.py              # Print full health report
    python3 health.py --json       # Output as JSON
    python3 health.py --alerts     # Only show alerts (non-zero exit if any)

Requirements: Python 3.9+, memory.db must exist (run install.py first)
"""

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import check_python_version, get_db_path  # noqa: E402
from storage import _get_schema_version, close_db, get_db  # noqa: E402

# Alert thresholds
COLD_RATIO_THRESHOLD = 0.8  # Alert if 80%+ chunks are cold
STALE_GRAPH_DAYS = 7  # Alert if no new edges in N days


@dataclass
class HealthReport:
    """Memory system health metrics."""
    total_chunks: int = 0
    avg_salience: float = 0.0
    hot_chunks: int = 0    # salience > 0.7
    warm_chunks: int = 0   # salience 0.1 - 0.7
    cold_chunks: int = 0   # salience < 0.1
    graph_nodes: int = 0
    active_edges: int = 0
    invalidated_edges: int = 0
    db_size_bytes: int = 0
    ltm_chunks: int = 0
    daily_chunks: int = 0
    schema_version: int = 0


def health_report(conn: sqlite3.Connection) -> HealthReport:
    """Query the database for health metrics.

    Returns a HealthReport dataclass. All queries are read-only.
    """
    report = HealthReport()
    report.schema_version = _get_schema_version(conn)

    # Chunk statistics
    row = conn.execute("""
        SELECT
            COUNT(*) as total,
            COALESCE(ROUND(AVG(salience), 3), 0) as avg_sal,
            SUM(CASE WHEN salience > 0.7 THEN 1 ELSE 0 END) as hot,
            SUM(CASE WHEN salience BETWEEN 0.1 AND 0.7 THEN 1 ELSE 0 END) as warm,
            SUM(CASE WHEN salience < 0.1 THEN 1 ELSE 0 END) as cold,
            SUM(CASE WHEN source_type = 'ltm' THEN 1 ELSE 0 END) as ltm,
            SUM(CASE WHEN source_type = 'daily' THEN 1 ELSE 0 END) as daily
        FROM chunks
    """).fetchone()

    report.total_chunks = row[0] or 0
    report.avg_salience = row[1] or 0.0
    report.hot_chunks = row[2] or 0
    report.warm_chunks = row[3] or 0
    report.cold_chunks = row[4] or 0
    report.ltm_chunks = row[5] or 0
    report.daily_chunks = row[6] or 0

    # Graph statistics
    report.graph_nodes = (
        conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] or 0
    )
    report.active_edges = (
        conn.execute(
            "SELECT COUNT(*) FROM edges WHERE valid_to IS NULL"
        ).fetchone()[0] or 0
    )
    report.invalidated_edges = (
        conn.execute(
            "SELECT COUNT(*) FROM edges WHERE valid_to IS NOT NULL"
        ).fetchone()[0] or 0
    )

    # DB file size
    db_path = get_db_path()
    if db_path.exists():
        report.db_size_bytes = db_path.stat().st_size

    return report


def health_alerts(report: HealthReport) -> list[str]:
    """Generate alert strings for concerning health conditions.

    Returns a list of human-readable alert strings. Empty list means healthy.
    """
    alerts = []

    if report.total_chunks == 0:
        alerts.append(
            "Memory DB is empty -- run migration to populate from existing markdown files."
        )
        return alerts  # No point checking ratios on empty DB

    cold_ratio = report.cold_chunks / report.total_chunks
    if cold_ratio >= COLD_RATIO_THRESHOLD:
        pct = int(cold_ratio * 100)
        alerts.append(
            f"{pct}% of memories are cold (salience < 0.1) -- "
            "consider running /synthesize or consolidation."
        )

    return alerts


def format_report(report: HealthReport, alerts: list[str]) -> str:
    """Format a health report as human-readable text."""
    lines = [
        "Memory System Health Report",
        "=" * 40,
        "",
        f"  Total chunks:  {report.total_chunks}",
        f"    LTM:         {report.ltm_chunks}",
        f"    Daily:       {report.daily_chunks}",
        f"  Avg salience:  {report.avg_salience:.3f}",
        f"  Hot (>0.7):    {report.hot_chunks}",
        f"  Warm (0.1-0.7):{report.warm_chunks}",
        f"  Cold (<0.1):   {report.cold_chunks}",
        "",
        f"  Graph nodes:   {report.graph_nodes}",
        f"  Active edges:  {report.active_edges}",
        f"  Invalid edges: {report.invalidated_edges}",
        "",
        f"  DB size:       {report.db_size_bytes / 1024:.1f} KB",
        f"  Schema ver:    {report.schema_version}",
    ]

    if alerts:
        lines.extend(["", "Alerts:", "-------"])
        for alert in alerts:
            lines.append(f"  - {alert}")

    return "\n".join(lines)


def main() -> int:
    """CLI entry point."""
    check_python_version()

    parser = argparse.ArgumentParser(
        description="Memory system health diagnostics"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output as JSON"
    )
    parser.add_argument(
        "--alerts", action="store_true",
        help="Only show alerts (exit 1 if any)"
    )
    args = parser.parse_args()

    try:
        conn = get_db()
    except FileNotFoundError:
        if args.json:
            print(json.dumps({"error": "memory.db not found"}))
        else:
            print("Error: memory.db not found. Run install.py to create it.")
        return 1

    try:
        report = health_report(conn)
        alerts = health_alerts(report)

        if args.json:
            data = asdict(report)
            data["alerts"] = alerts
            print(json.dumps(data, indent=2))
        elif args.alerts:
            if alerts:
                for alert in alerts:
                    print(f"- {alert}")
                return 1
            else:
                print("No alerts.")
        else:
            print(format_report(report, alerts))
    finally:
        close_db(conn)

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

**Step 2: Create test file**

Create `tests/test_health.py`:

```python
#!/usr/bin/env python3
"""Tests for scripts/health.py."""

import json
from unittest import mock

import pytest

from health import (
    COLD_RATIO_THRESHOLD,
    HealthReport,
    format_report,
    health_alerts,
    health_report,
)
from storage import (
    SCHEMA_VERSION,
    ChunkRow,
    NodeRow,
    close_db,
    ensure_db,
    insert_chunk,
    insert_node,
)


@pytest.fixture
def db_dir(tmp_path):
    db_path = tmp_path / "memory.db"
    with mock.patch("storage.get_db_path", return_value=db_path), \
         mock.patch("health.get_db_path", return_value=db_path):
        yield tmp_path


@pytest.fixture
def db(db_dir):
    conn = ensure_db()
    yield conn
    close_db(conn)


class TestHealthReport:
    def test_empty_db(self, db):
        report = health_report(db)
        assert report.total_chunks == 0
        assert report.graph_nodes == 0
        assert report.avg_salience == 0.0

    def test_schema_version(self, db):
        report = health_report(db)
        assert report.schema_version == SCHEMA_VERSION

    def test_chunk_counts(self, db):
        for i in range(5):
            insert_chunk(db, ChunkRow(
                content=f"LTM entry {i}",
                source_file="global-long-term-memory.md",
                source_type="ltm",
                scope="global",
                chunk_index=i,
                created_at="2026-03-01",
            ))
        for i in range(3):
            insert_chunk(db, ChunkRow(
                content=f"Daily entry {i}",
                source_file="2026-03-01.md",
                source_type="daily",
                scope="global",
                chunk_index=i,
                created_at="2026-03-01",
            ))
        db.commit()
        report = health_report(db)
        assert report.total_chunks == 8
        assert report.ltm_chunks == 5
        assert report.daily_chunks == 3

    def test_salience_distribution(self, db):
        # Hot
        insert_chunk(db, ChunkRow(
            content="Hot entry", source_file="t.md", source_type="ltm",
            scope="global", chunk_index=0, created_at="2026-03-01",
            salience=0.9,
        ))
        # Warm
        insert_chunk(db, ChunkRow(
            content="Warm entry", source_file="t.md", source_type="ltm",
            scope="global", chunk_index=1, created_at="2026-03-01",
            salience=0.5,
        ))
        # Cold
        insert_chunk(db, ChunkRow(
            content="Cold entry", source_file="t.md", source_type="ltm",
            scope="global", chunk_index=2, created_at="2026-03-01",
            salience=0.05,
        ))
        db.commit()
        report = health_report(db)
        assert report.hot_chunks == 1
        assert report.warm_chunks == 1
        assert report.cold_chunks == 1

    def test_node_count(self, db):
        insert_node(db, NodeRow(
            name="pytest", type="tool", scope="global",
            created_at="2026-03-01",
        ))
        db.commit()
        report = health_report(db)
        assert report.graph_nodes == 1


class TestHealthAlerts:
    def test_empty_db_alert(self):
        report = HealthReport(total_chunks=0)
        alerts = health_alerts(report)
        assert len(alerts) == 1
        assert "empty" in alerts[0].lower()

    def test_cold_ratio_alert(self):
        # 90% cold -- should trigger
        report = HealthReport(
            total_chunks=100,
            cold_chunks=90,
            warm_chunks=8,
            hot_chunks=2,
        )
        alerts = health_alerts(report)
        assert any("cold" in a.lower() for a in alerts)

    def test_no_alert_when_healthy(self):
        report = HealthReport(
            total_chunks=100,
            cold_chunks=10,
            warm_chunks=60,
            hot_chunks=30,
            avg_salience=0.6,
        )
        alerts = health_alerts(report)
        assert len(alerts) == 0


class TestFormatReport:
    def test_contains_key_fields(self):
        report = HealthReport(
            total_chunks=42, avg_salience=0.65,
            hot_chunks=10, warm_chunks=25, cold_chunks=7,
            graph_nodes=5, active_edges=8,
        )
        text = format_report(report, [])
        assert "42" in text
        assert "0.650" in text
        assert "Health Report" in text

    def test_includes_alerts(self):
        report = HealthReport()
        text = format_report(report, ["Test alert message"])
        assert "Test alert message" in text
        assert "Alerts:" in text
```

**Step 3: Run tests**

```bash
python3 -m pytest tests/test_health.py -v
```

---

#### B3: Install.py integration
> **Handoff from A6:** Confirmed -- storage.py exports match planned design. Key imports for B3: `ensure_db`, `close_db`, `migrate_markdown_to_db` from `storage`. Note: `MigrationStats` and `migrate_markdown_to_db` will be added to storage.py by B1 before B3 runs. No signature changes from planned design.

**Files:**
- Modify: `install.py`
- Modify: `tests/test_install.py`

**Verification:** `python3 -m pytest tests/test_install.py -v`

**Done when:** `install.py` has a new `create_database()` function that calls `ensure_db()` and `migrate_markdown_to_db()`, printing migration stats. It is called from `main()` after `copy_templates()` and before the legacy scripts cleanup. The `link_scripts()` function includes `storage.py` and `health.py` in the symlink list. Tests verify the new function is called and that the scripts are in the link list.

**Avoid:** Do not make `create_database()` a hard failure -- if it raises, print a warning and continue (the markdown system still works without the DB). Do not move install.py imports to top-level since the `storage` module is in the scripts directory and requires path setup.

**Step 1: Add storage.py and health.py to link_scripts**

In `install.py`, find the `scripts_to_link` list inside `link_scripts()` (currently at line 139) and add two entries after `"synthesis_cron.py"` and before `"token_usage.py"`:

```python
        "storage.py",  # SQLite storage layer (DB lifecycle, CRUD, migration)
        "health.py",  # Memory health diagnostics
```

**Step 2: Add create_database function**

Add after the `copy_templates()` function definition (around line 244) in `install.py`:

```python
def create_database(script_dir: Path) -> None:
    """Create or update memory.db and run markdown migration.

    Non-fatal -- prints warning on failure since the markdown system
    continues to work without the DB.
    """
    try:
        # Import storage module from scripts directory
        scripts_path = script_dir / "scripts"
        if str(scripts_path) not in sys.path:
            sys.path.insert(0, str(scripts_path))

        from storage import close_db, ensure_db, migrate_markdown_to_db

        conn = ensure_db()
        try:
            stats = migrate_markdown_to_db(conn)
            print(f"Memory database: {stats.ltm_files_processed} LTM files, "
                  f"{stats.daily_files_processed} daily files, "
                  f"{stats.chunks_inserted} chunks indexed "
                  f"({stats.chunks_skipped} unchanged)")
        finally:
            close_db(conn)
    except Exception as e:
        print(f"Warning: Could not create memory database: {e}")
        print("  The markdown-based system continues to work without it.")
```

**Step 3: Wire into main()**

In `install.py` `main()`, add the call after `copy_templates(script_dir)` (line 541) and before the `# Clean up legacy scripts` comment (line 543):

```python
    # Create/update memory database
    create_database(script_dir)
```

**Step 4: Add tests**

Add to `tests/test_install.py`:

```python
# ---------------------------------------------------------------------------
# create_database
# ---------------------------------------------------------------------------


class TestCreateDatabase:
    def test_link_scripts_includes_storage(self, tmp_path):
        """Verify storage.py symlink is created by link_scripts."""
        repo_dir = tmp_path / "repo"
        scripts_src = repo_dir / "scripts"
        scripts_src.mkdir(parents=True)
        (scripts_src / "storage.py").write_text("pass")
        with mock.patch("memory_utils.Path.home", return_value=tmp_path):
            install.create_directories()
            install.link_scripts(repo_dir)
        assert (tmp_path / ".claude" / "scripts" / "storage.py").exists()

    def test_link_scripts_includes_health(self, tmp_path):
        """Verify health.py symlink is created by link_scripts."""
        repo_dir = tmp_path / "repo"
        scripts_src = repo_dir / "scripts"
        scripts_src.mkdir(parents=True)
        (scripts_src / "health.py").write_text("pass")
        with mock.patch("memory_utils.Path.home", return_value=tmp_path):
            install.create_directories()
            install.link_scripts(repo_dir)
        assert (tmp_path / ".claude" / "scripts" / "health.py").exists()

    def test_create_database_handles_import_error(self, capsys):
        """Verify graceful degradation if storage module missing."""
        with mock.patch.dict(sys.modules, {"storage": None}):
            # Should not raise
            install.create_database(Path("/nonexistent"))
        output = capsys.readouterr().out
        assert "Warning" in output or "Could not" in output
```

**Step 5: Run full test suite to verify everything is GREEN**

```bash
python3 -m pytest tests/test_storage.py tests/test_migration.py tests/test_health.py tests/test_memory_utils.py tests/test_install.py -v
```

All tests should be GREEN. The storage foundation is complete.
