# Phase C Completion

**Date:** 2026-03-21
**HEAD SHA:** 79b3ec7c52aa9d6a00be817fd53b9ce3cc8d9124

## Summary

Phase C implemented the Intelligent Synthesis pipeline — a full CRUD-aware memory management system on top of the SQLite storage layer. The synthesis LLM can now issue structured ADD/UPDATE/DELETE/NOOP operations via a new `===MEMORY_OPS===` output block, which the apply pipeline executes against both the DB and markdown LTM files. DELETE operations include bi-temporal edge invalidation (valid_to/expired_at) scoped to only the deleted chunk's entity nodes. Vector pre-retrieval replaces full LTM embedding in synthesis prompts when `sqlite-vec` is available, feeding targeted existing memories (with chunk IDs for CRUD reference) instead of dumping entire LTM files. All 7 tasks completed in order with 889 tests passing and 12 skipped (fastembed not installed).

## Tasks Completed

- **C1**: Storage helpers — `invalidate_edge`, `update_chunk_content`, `update_chunk_salience`, `query_chunks_for_retrieval`, `query_chunk_by_id`, `query_current_edges`, `query_edges_at_date`, `query_edges_for_node`
- **C2**: Vector pre-retrieval — `extract_topics()` (algorithmic TF-IDF), `retrieve_existing_memories()` with ImportError fallback, `_build_preextracted_prompt()` gains `vector_memories` param
- **C3**: `===MEMORY_OPS===` parser — `MemoryOp` dataclass, `SynthesisResult.memory_ops` field, JSON parsing with malformed-JSON warning and backward compat
- **C4**: CRUD apply logic — `apply_memory_ops()`, `_apply_add/update/delete/noop()`, markdown helpers (`_append_entry_to_section`, `_update_markdown_line`, `_archive_markdown_line`), wired into `apply_results()`
- **C5**: Bi-temporal edge invalidation in `_apply_delete()` — scoped to chunk's entity nodes only, safe for chunks with no edges
- **C6**: Entity extraction guidance added to `_build_synthesis_instructions()`, entities stored in `chunks.entities` JSON column via ADD/UPDATE ops
- **C7**: MEMORY_OPS format spec in synthesis instructions — ADD/UPDATE/DELETE/NOOP action descriptions with examples, chunk ID reference rules

## Deviations

**C7 minor fix (Deviation Rule 3):** The MEMORY_OPS JSON example inside the `_build_synthesis_instructions()` f-string caused a `ValueError` because curly braces were interpreted as format specifiers. Fixed by doubling all literal braces in the JSON example block (`{{` and `}}`). No architectural change.

No other deviations.
