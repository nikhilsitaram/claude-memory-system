# Design: Vector Search (#46)

**Date:** 2026-03-21
**Parent:** design-memory-refactor-phase1.md (Worktree 3)
**Issue:** #46

## Problem

The memory system has no semantic retrieval — searching relies on exact keyword matching. Phase 1's storage layer and text processing pipeline are complete (storage-foundation, text-processing merged), but without embeddings the DB is just a structured copy of the markdown files. Vector search is the capability that makes the DB valuable: enabling "find memories similar to X" queries that power Phase 2 (LLM dedup via vector retrieval) and Phase 3 (smart loading replacing the firehose `load_memory.py`).

## Goal

Deliver a CPU-only embedding + vector search module (`scripts/embeddings.py`) that indexes all DB chunks as 384-dim vectors and provides multi-signal ranked retrieval (vector similarity + recency + salience).

## Success Criteria

1. `search_similar(conn, "how does the chunking pipeline work")` returns relevant chunks ranked by composite score
2. After synthesis writes new daily/LTM files, `reindex_changed_files()` embeds only changed chunks (content hash skip)
3. If fastembed or sqlite-vec is not installed, all vector operations are silent no-ops — existing markdown system unaffected
4. `run_post_processing()` in synthesis.py calls re-indexing automatically after decay

## Architecture

**Single new module:** `scripts/embeddings.py` — owns all embedding and vector search logic.

**Three responsibilities:**
1. **Embed** — Convert text chunks to 384-dim float vectors via FastEmbed
2. **Index** — Store vectors in `vec_chunks` virtual table (sqlite-vec)
3. **Retrieve** — Multi-signal ranked search (vector similarity + recency + salience)

### Dependencies

- `fastembed` — CPU embedding (`pip install fastembed`)
- `sqlite-vec` — Vector virtual table extension (`pip install sqlite-vec`)
- Both are optional: missing = no-ops

### Integration Point

`run_post_processing()` in `synthesis.py` gains a new step after `run_decay()`:
```python
try:
    _reindex_after_synthesis()
except Exception:
    pass  # Non-critical
```

`_reindex_after_synthesis()` calls `reindex_all(conn)` — simplest approach for Phase 1 since the memory corpus is small (hundreds of chunks) and synthesis may modify files unpredictably. Future phases can optimize to incremental reindex if needed.

## Key Decisions

1. **FastEmbed with `sentence-transformers/all-MiniLM-L6-v2`** — 384 dims, CPU-only, ~50ms/embedding, no API key needed.

2. **sqlite-vec for vector storage** — Loaded via `import sqlite_vec; sqlite_vec.load(conn)` (the `sqlite-vec` Python package provides `load()` which handles extension loading internally). `VEC_CHUNKS_DDL` constant already exists in `storage.py`; this module creates the virtual table on first use. If `import sqlite_vec` fails, `ensure_vec_table()` returns `False`.

3. **Graceful degradation** — If fastembed or sqlite-vec aren't installed:
   - `embed_batch()` returns empty list
   - `search_similar()` returns empty list
   - `reindex_changed_files()` does nothing
   - No exceptions propagated

4. **Content hash skip** — Before embedding, check if the chunk's `id` already exists in `vec_chunks` AND the corresponding row in `chunks` has the same `content_hash`. If both hold, skip re-embedding. (Note: `vec_chunks` has no `content_hash` column — the check JOINs to the `chunks` table.)

5. **Rebuild-on-write, not rebuild-on-read** — Embedding happens after synthesis writes files, not during SessionStart. Keeps startup fast.

6. **Scoring formula:**
   ```python
   def score_memory(vec_distance, chunk):
       vec_sim = 1.0 - vec_distance
       boosted = 1 - exp(-3.0 * vec_sim)    # saturating exponential
       recency = exp(-0.05 * days_since(chunk.last_accessed))
       return 0.50 * boosted + 0.25 * recency + 0.25 * chunk.salience
   ```
   **Phase 1 fallbacks:** Until Phase 2 populates `last_accessed` and `salience`, fallback values are used: `last_accessed` defaults to `created_at` (or current time if both NULL), and `salience` defaults to `1.0`. Phase 1 scoring is effectively `0.50 * vec_boosted + 0.25 * recency_from_creation + 0.25 * 1.0`.

## Public API

| Function | Signature | Purpose |
|----------|-----------|---------|
| `ensure_vec_table` | `(conn) -> bool` | Create `vec_chunks` if sqlite-vec available; returns success |
| `embed_text` | `(text) -> list[float]` | Single text → 384-dim vector |
| `embed_batch` | `(texts) -> list[list[float]]` | Batch embedding |
| `index_chunks` | `(conn, chunk_ids: list[str])` | Query `chunks` table by IDs, embed content, insert into vec_chunks. Accepts chunk IDs (not dataclass instances) — queries the DB internally, avoiding Chunk/ChunkRow type boundary issues. |
| `index_chunks_by_source` | `(conn, source_files: list[str])` | Query `chunks` by `source_file`, embed + index. Convenience wrapper for reindex flows. |
| `delete_vec_chunks` | `(conn, chunk_ids: list[str])` | Remove vectors for deleted chunks |
| `search_similar` | `(conn, query, top_k=10, scope=None) -> list[ScoredChunk]` | Embed query → vec search → score → rank. Scope filtering: fetches `top_k * 3` candidates from `vec_chunks`, JOINs to `chunks` for metadata, filters by scope if specified, returns top `top_k` after scoring. |
| `reindex_changed_files` | `(conn, changed_files: list[str])` | Delete old vectors for files → call `index_chunks_by_source` |
| `reindex_all` | `(conn)` | Full re-index of all chunks in DB |

**Constants:**
- `EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"`
- `EMBEDDING_DIM = 384`
- `VEC_SIM_WEIGHT = 0.50`, `RECENCY_WEIGHT = 0.25`, `SALIENCE_WEIGHT = 0.25`
- `RECENCY_DECAY = 0.05`, `VEC_BOOST_RATE = 3.0`
- `DEFAULT_TOP_K = 10`

**Dataclass:**
- `ScoredChunk(chunk: ChunkRow, score: float, vec_similarity: float)`

## Non-Goals

- Changes to `load_memory.py` read path (Phase 3)
- LLM-based dedup (Phase 2)
- Access tracking / salience updates on read (Phase 2)
- Triplet embeddings (Phase 4)

## Implementation Approach

Single phase — no internal dependency layers. All functions depend on the same two externals (fastembed + sqlite-vec), so there's no benefit to splitting.

## Testing Strategy

- Mock FastEmbed in unit tests to avoid model download in CI
- One `@pytest.mark.skipif(not HAS_FASTEMBED)` integration test for local validation
- Test classes: `TestEmbedText`, `TestIndexChunks`, `TestSearchSimilar`, `TestScoring`, `TestGracefulDegradation`, `TestReindex`
- All tests use `tmp_path` + `mock.patch` for isolation, constants imported from source
