---
status: Not Yet Started
---

# CPU-only embedding + vector search module for semantic memory retrieval Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use orchestrate

**Goal:** CPU-only embedding + vector search module for semantic memory retrieval
**Architecture:** Single new module `scripts/embeddings.py` owns all embedding and vector search logic. FastEmbed (sentence-transformers/all-MiniLM-L6-v2) produces 384-dim vectors stored in sqlite-vec virtual table (`vec_chunks`). Multi-signal ranked retrieval combines vector similarity (saturating exponential boost), recency, and salience. Graceful degradation: if fastembed or sqlite-vec are missing, all operations are silent no-ops. Integration point: `run_post_processing()` in synthesis.py calls `reindex_all()` after decay.
**Tech Stack:** Python 3.9+, fastembed (optional), sqlite-vec (optional), sqlite3 stdlib, dataclasses, math, pytest with tmp_path and unittest.mock

---

## Phase A — Embeddings and Vector Search
**Status:** Not Started | **Rationale:** Single phase because all functions depend on the same two externals (fastembed + sqlite-vec) with no internal dependency layers. The design doc explicitly states no benefit to splitting.

- [ ] A1: Integration test skeleton for embeddings module — *Test file exists with test classes TestEmbedText, TestIndexChunks, TestSearchSimilar, TestScoring, TestGracefulDegradation, TestReindex. All tests fail with ImportError since embeddings.py does not exist yet.*
- [ ] A2: Core module with constants, dataclass, embed functions, and vec table setup — *Module imports cleanly. Constants EMBEDDING_MODEL, EMBEDDING_DIM, VEC_SIM_WEIGHT, RECENCY_WEIGHT, SALIENCE_WEIGHT, RECENCY_DECAY, VEC_BOOST_RATE, DEFAULT_TOP_K are defined. ScoredChunk dataclass exists. HAS_FASTEMBED and HAS_SQLITE_VEC flags detect availability. ensure_vec_table(conn) creates vec_chunks virtual table or returns False. embed_text() and embed_batch() return vectors or empty lists on graceful degradation. TestEmbedText and TestGracefulDegradation tests pass.*
- [ ] A3: Index and delete operations for vec_chunks — *index_chunks(conn, chunk_ids) queries chunks table, embeds content, inserts into vec_chunks with content_hash skip logic. index_chunks_by_source(conn, source_files) wraps index_chunks for source-file-based workflows. delete_vec_chunks(conn, chunk_ids) removes vectors. All TestIndexChunks tests pass.*
- [ ] A4: Scoring function and search_similar — *score_memory(vec_distance, chunk) implements the saturating exponential formula with Phase 1 fallbacks (last_accessed defaults to created_at, salience defaults to 1.0). search_similar(conn, query, top_k, scope) embeds query, fetches top_k*3 candidates from vec_chunks, JOINs to chunks for metadata, applies scope filter, scores and ranks, returns top_k ScoredChunk list. All TestScoring and TestSearchSimilar tests pass.*
- [ ] A5: Reindex functions and synthesis.py integration — *reindex_changed_files(conn, changed_files) deletes old vectors then calls index_chunks_by_source. reindex_all(conn) re-indexes all chunks in DB. synthesis.py run_post_processing() calls _reindex_after_synthesis() after run_decay(), wrapped in try/except. All TestReindex tests pass.*
- [ ] A6: Install integration and full test suite GREEN — *embeddings.py added to link_scripts() in install.py. Full test suite passes: all TestEmbedText, TestIndexChunks, TestSearchSimilar, TestScoring, TestGracefulDegradation, TestReindex tests GREEN. test_install.py verifies embeddings.py is in the scripts list.*
