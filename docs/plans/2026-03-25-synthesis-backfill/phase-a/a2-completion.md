# A2 Completion Notes

**Summary:** Removed `consolidated != 1` filter from both `decay_data_points()` and `cleanup_near_zero_salience()` SQL queries in `scripts/decay.py`. Consolidated memories now participate in normal salience decay; only `scope='user'` remains immune. Updated docstrings to reflect the change and added 3 new tests in `TestConsolidatedDecay` class plus updated 2 existing tests that previously asserted consolidated immunity.

**Deviations:** None

**Files Changed:**
- `scripts/decay.py` — removed `AND consolidated != 1` from two SQL WHERE clauses, updated docstrings
- `tests/test_decay.py` — updated `test_skips_consolidated_data_points` -> `test_decays_consolidated_data_points`, updated `test_skips_consolidated_memories` -> `test_cleans_up_consolidated_memories`, added `TestConsolidatedDecay` class with 3 tests

**Test Results:** 94/94 tests pass, 0 failures, 0 regressions. All 5 consolidated-related tests pass (3 new + 2 updated).

**Deferred Issues:** None
