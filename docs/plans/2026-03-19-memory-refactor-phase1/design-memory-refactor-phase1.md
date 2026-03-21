# Design: Memory System Refactor — Phase 1 (Foundation)

**Date:** 2026-03-19
**Parent issue:** #64

## Goal

Replace file-based linear scanning with a unified SQLite + sqlite-vec storage layer that supports semantic vector search, graph relationships, and structured metadata — all in a single `memory.db` file.

Phase 1 lays the foundation: storage schema, text processing pipeline, and vector search. No LLM-dependent features — everything here is algorithmic or uses local CPU embeddings.

## Non-Goals

- LLM-driven dedup (#52), contradiction detection (#54), salience decay (#50) — Phase 2
- Smart context loading (#58), session continuity (#61), lighter synthesis (#62) — Phase 3
- Triplet embeddings (#51), consolidation (#56), rule extraction (#57) — Phase 4
- Changes to the existing markdown-based read/write paths (those continue working unchanged)

## Architecture Approach

### Single Database, Three Layers

```
memory.db (SQLite + WAL mode)
├── Graph Layer:    nodes, edges (entity relationships)
├── Vector Layer:   vec_chunks (sqlite-vec virtual table, float[384])
└── Content Layer:  chunks (text content, metadata, scores)
```

All three layers live in one file, one transaction model, one WAL lock. This means:
- Graph nodes can JOIN with vector similarity in a single SQL query
- Chunk metadata (salience, access count) updates atomically with content
- No sync issues between separate stores

### Coexistence with Markdown

Phase 1 does NOT replace the markdown files. It indexes them:
- Markdown files remain the source of truth for the existing pipelines
- The DB is a read-optimized index built from markdown content
- Existing `load_memory.py`, `synthesis.py`, `decay.py` continue working unchanged
- Phase 3 (Workflow) will migrate the read path to use the DB

This means Phase 1 is **non-breaking** — the DB is additive infrastructure.

### Embedding Model

FastEmbed with `sentence-transformers/all-MiniLM-L6-v2`:
- 384 dimensions, CPU-only, no API key
- ~50ms per embedding on modern hardware
- Installed via `pip install fastembed`

## Worktree Breakdown

### Worktree 1: `storage-foundation` (#49 + #55 schema + #63 diagnostics)

**What it delivers:**
- `scripts/storage.py` — DB connection helpers, schema creation, WAL setup, migration utilities
- Full schema: `nodes`, `edges`, `chunks`, `vec_chunks` tables with all columns including provenance (#55: `source_sessions`, `evidence_count`) and SimHash (#53: `simhash` column)
- Migration pipeline: scan existing markdown files → populate DB tables
- `scripts/health.py` — diagnostic queries against the DB (chunk counts, node/edge stats, salience distribution)
- Integration into `install.py` for initial DB creation

**Key decisions:**
- Schema includes forward-looking columns (provenance, simhash, salience) even though Phase 1 doesn't populate all of them. Avoids ALTER TABLE later.
- `vec_chunks` DDL is defined as a constant (`VEC_CHUNKS_DDL`) but NOT created at install time -- the sqlite-vec extension is a Worktree 3 dependency. Worktree 3 (vector-search) will create and populate it.
- WAL mode + `busy_timeout=5000` for concurrent reads from multiple Claude Code tabs.
- Connection pooling not needed — each script opens/closes its own connection.

**Schema** (from #49, with #55 additions):

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;

CREATE TABLE nodes (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,          -- project, tool, library, convention, person, file
    description TEXT,
    scope TEXT,                  -- project name or 'global'
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    salience REAL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    content_hash TEXT,
    simhash INTEGER,
    source_sessions TEXT,        -- JSON array of session dates (#55)
    evidence_count INTEGER DEFAULT 1,
    consolidated INTEGER DEFAULT 0
);

CREATE TABLE edges (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL REFERENCES nodes(id),
    target TEXT NOT NULL REFERENCES nodes(id),
    type TEXT NOT NULL,          -- uses, prefers, depends_on, related_to, supersedes
    fact TEXT,
    properties TEXT,             -- JSON blob
    created_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    expired_at TEXT,
    weight REAL DEFAULT 1.0,
    source_sessions TEXT
);

CREATE TABLE chunks (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    source_file TEXT NOT NULL,
    source_type TEXT NOT NULL,   -- 'ltm', 'daily', 'triplet'
    section TEXT,                -- '## Key Actions', etc.
    scope TEXT,                  -- project name or 'global'
    entry_type TEXT,             -- implement, design, gotcha, etc.
    chunk_index INTEGER,
    created_at TEXT,
    content_hash TEXT,
    simhash INTEGER,
    salience REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    source_sessions TEXT,
    evidence_count INTEGER DEFAULT 1,
    entities TEXT                -- JSON: extracted entities (#48, populated later)
);

CREATE VIRTUAL TABLE vec_chunks USING vec0(
    embedding float[384],
    +chunk_id TEXT,
    +source_type TEXT
);

-- Indexes
CREATE INDEX idx_nodes_type ON nodes(type);
CREATE INDEX idx_nodes_scope ON nodes(scope);
CREATE INDEX idx_nodes_simhash ON nodes(simhash);
CREATE INDEX idx_edges_source ON edges(source);
CREATE INDEX idx_edges_target ON edges(target);
CREATE INDEX idx_edges_valid ON edges(valid_to);
CREATE INDEX idx_chunks_source ON chunks(source_file);
CREATE INDEX idx_chunks_scope ON chunks(scope);
CREATE INDEX idx_chunks_hash ON chunks(content_hash);
CREATE INDEX idx_chunks_simhash ON chunks(simhash);
```

### Worktree 2: `text-processing` (#47 + #53)

**What it delivers:**
- `scripts/chunking.py` — chunk LTM files by paragraph with overlap, chunk daily files by entry
- `scripts/simhash.py` — SimHash fingerprinting (~20 lines) + Hamming distance comparison
- Each chunk is a dataclass with: `content`, `source_file`, `section`, `chunk_index`, `created_at`, `content_hash`, `simhash`, `scope`, `entry_type`, `source_type`

**Key decisions:**
- LTM chunking: paragraph-level with 10-20% overlap, respecting `## Section` boundaries (uses existing `parse_markdown_sections()`)
- Daily chunking: each `- [scope/type] Description` line is one chunk (natural boundary)
- SimHash: 64-bit, Hamming threshold 3 for near-duplicate detection
- Token counting: use `estimate_tokens()` from memory_utils (char/4 heuristic) — no tiktoken dependency
- Chunks are returned as dataclass instances, not written to DB (that's storage-foundation's job via an integration function)

### Worktree 3: `vector-search` (#46)

**What it delivers:**
- `scripts/embeddings.py` — FastEmbed wrapper, batch embedding, vec_chunks population
- Multi-signal retrieval scoring function
- Re-embedding pipeline: after synthesis writes files, re-embed changed chunks
- Integration into `run_post_processing()` in synthesis.py

**Key decisions:**
- Rebuild-on-write, not rebuild-on-read — keeps SessionStart fast
- Batch embedding: process all chunks of a changed file in one FastEmbed call
- Content hash comparison: skip re-embedding unchanged chunks
- Scoring formula (from #46):
  ```python
  def score_memory(vec_distance, chunk):
      vec_sim = 1.0 - vec_distance
      boosted = 1 - exp(-3.0 * vec_sim)  # saturating exponential
      recency = exp(-0.05 * days_since(chunk.last_accessed))
      return 0.50 * boosted + 0.25 * recency + 0.25 * chunk.salience
  ```
- Fallback: if `memory.db` doesn't exist, all vector operations are no-ops (existing file-based system continues working)

**Dependency:** Requires storage-foundation (#49) and text-processing (#47) to be merged first.

## Implementation Approach

**Phasing rationale:** Storage and text processing have zero dependency on each other — develop in parallel. Vector search needs both, so it runs after they merge.

```
Week 1:  [storage-foundation]  ||  [text-processing]    (parallel)
                    \                    /
Week 2:              [vector-search]                     (sequential)
```

Each worktree gets its own branch, PR, and test suite. No worktree depends on uncommitted work from another.

## Testing Strategy

- `tests/test_storage.py` — schema creation, migration from markdown, CRUD operations, WAL concurrency
- `tests/test_chunking.py` — paragraph chunking with overlap, daily entry chunking, section boundary respect
- `tests/test_simhash.py` — fingerprint generation, Hamming distance, near-duplicate detection
- `tests/test_embeddings.py` — FastEmbed integration, batch embedding, content hash skip, scoring function
- `tests/test_health.py` — diagnostic queries, alert thresholds

All tests use `tmp_path` fixture for filesystem isolation. No hardcoded values — derive from constants.

## Future Phases (Outline)

Separate `/design` sessions will produce detailed plans for each. This section captures scope and dependencies for sequencing.

### Phase 2: Intelligence

| Worktree | Issues | Scope | Depends On |
|----------|--------|-------|------------|
| `llm-dedup` | #52 + #54 | Two-stage Mem0-style fact extraction → ADD/UPDATE/DELETE/NOOP decisions. Bi-temporal edges (`valid_from`/`valid_to`) for contradiction detection. Replaces `is_routed_match()` keyword overlap. Runs via `claude -p` during deferred synthesis. | Phase 1 (vector search for retrieving similar memories) |
| `salience-decay` | #50 | Replace binary age-based `decay.py` with tiered salience (hot/warm/cold λ rates). Access tracking in `load_memory.py` (increment `access_count`, update `last_accessed`). Reinforcement on access (+0.18 diminishing). Associative reinforcement via graph neighbors. Archive threshold: salience < 0.05. | Phase 1 (DB for metadata storage) |
| `entity-extraction` | #48 | Regex extractors (EMAIL, URL, DATE, IP, MENTION, etc.) run during chunk creation. Populate `entities` JSON column on chunks table. Enable filtered retrieval ("memories with GitHub URLs"). | Phase 1 (chunking pipeline, DB schema) |

**Key Phase 2 integration point:** The dedup engine (#52) fundamentally changes synthesis — facts are extracted and individually evaluated against existing memories via vector search + LLM, rather than the current approach of embedding all LTM in the synthesis prompt for keyword dedup.

### Phase 3: Workflow

| Worktree | Issues | Scope | Depends On |
|----------|--------|-------|------------|
| `smart-loading` | #58 + #61 + #62 | Replace firehose `load_memory.py` with semantic retrieval (top-K by composite score). Session continuity via `session_context` graph nodes written at SessionEnd. Lighter synthesis drops LTM-in-prompt (dedup handled by vector search + LLM engine). | Phase 1 (vectors) + Phase 2 (salience, dedup engine) |
| `semantic-recall` | #59 + #60 | Upgrade `/recall` from grep to sqlite-vec query (text + triplet results, ranked by composite score). Deprecate `/remember` with migration path (Pinned → graph nodes with `salience=1.0`). | Phase 1 (vectors) + Phase 2 (salience for auto-promotion) |

**Key Phase 3 integration point:** This is where the existing file-based read path (`load_memory.py`) gets replaced with DB-driven retrieval. Phase 1 and 2 are additive (DB alongside markdown); Phase 3 makes the DB the primary read path.

### Phase 4: Enrichment

| Worktree | Issues | Scope | Depends On |
|----------|--------|-------|------------|
| `triplet-embeddings` | #51 | Embed graph edges as natural language triplets ("project X uses library Y") in vec_chunks with `source_type='triplet'`. Enables "how does X relate to Y?" semantic queries. | Phase 1 (graph + vectors) |
| `consolidation` | #56 | Weekly systemd timer clusters similar memories (embedding similarity > 0.8), LLM summarizes each cluster, marks originals as consolidated with salience boost. Memory defragmentation. | Phase 1 (vectors) + Phase 2 (salience) |
| `rule-extraction` | #57 | Optional synthesis step: LLM extracts implicit coding rules from transcripts, writes to `.proposed-rules.md`, user reviews at next SessionStart. New `/rules` skill. | Phase 2 (synthesis pipeline) |

### Standalone (anytime)

| Worktree | Issues | Scope |
|----------|--------|-------|
| `project-merge-fix` | #45 | `rewrite_daily_tags()` + `execute_memory_merge()` for project rename flow. Bug fix in current system, orthogonal to refactor. |

### Phase Dependency Chain

```
Phase 1 (Foundation)     — DB + chunks + vectors (non-breaking, additive)
    ↓
Phase 2 (Intelligence)   — dedup + salience + entities (enhances write path)
    ↓
Phase 3 (Workflow)        — smart loading + recall (replaces read path)
    ↓
Phase 4 (Enrichment)     — triplets + consolidation + rules (optional value-adds)
```
