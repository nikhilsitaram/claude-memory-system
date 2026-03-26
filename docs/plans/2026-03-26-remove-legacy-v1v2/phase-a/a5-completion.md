# A5: Remove backward-compat alias and vec_chunks warnings from embeddings.py

## Status: COMPLETE

## Changes

### scripts/embeddings.py
- Removed `ScoredChunk = ScoredDataPoint` backward-compat alias
- Removed `"ScoredChunk"` from `__all__`
- Removed `vec_chunks` fallback warning block from `ensure_vec_table()` (the `if _has_table(conn, "vec_chunks")` check with `warnings.warn`)
- Removed `vec_chunks` fallback warning block from `search_similar()` (same pattern)
- Updated docstrings to remove references to v2 schema fallback behavior

### tests/test_embeddings.py
- Removed `ScoredChunk` from imports
- Removed `test_scored_chunk_alias_still_works` test from `TestScoredDataPoint`
- Removed `TestBackwardCompatAliases` class (replaced with `TestScoreMemoryWithDataPointRow` keeping the DataPointRow duck-typing test)
- Updated module docstring (vec_chunks -> vec_data)
- Fixed stale docstring in `db_with_vec` fixture (vec_chunks -> vec_data)

## Verification
- `python3 -m pytest tests/test_embeddings.py -q` -- 24 passed, 6 skipped
- No remaining references to `ScoredChunk` or `vec_chunks` in either file
