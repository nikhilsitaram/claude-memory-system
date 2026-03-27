# A6: Integration test validating end-to-end token recovery

## Status: Complete (test written, awaiting A2 for pass)

## What was implemented

Created `tests/test_context_injection_cleanup.py` with 3 integration test classes:

1. **TestProfileWasteCleanup** - Inserts 5 profile waste entries (HTML comments, bare tags like "Python-3.13", "claude-code", "macOS ARM") and 1 real profile entry. Verifies `cleanup_stale_data` hard-deletes the waste and keeps real content. Asserts `stats["profiles_deleted"] == 5`.

2. **TestNearDuplicateCleanup** - Inserts 5 near-duplicate memory variants about git default branch config with varying `evidence_count` (3, 1, 1, 2, 1) and computed simhash values. Verifies only the highest-evidence entry survives. Asserts `stats["duplicates_soft_deleted"] == 4`.

3. **TestStaleProjectMemoryCleanup** - Inserts 3 stale project memories (resume backfill, completed phase references) and 1 good memory. Verifies stale entries are soft-deleted (salience=0.0) while good content is preserved. Asserts `stats["stale_soft_deleted"] >= 3`.

## Test results

- **Current state**: All 3 tests fail with `ImportError: cannot import name 'cleanup_stale_data' from 'storage'`. This is expected -- `cleanup_stale_data` will be added to `storage.py` by task A2.
- **Full suite**: 1032 passed, 8 skipped, 0 failures (existing tests unaffected).

## Dependencies

- **A2** (cleanup_stale_data implementation in storage.py) must be merged for these tests to pass.
- No other task dependencies.

## Files created

- `tests/test_context_injection_cleanup.py` -- 3 integration tests

## Deviations

None. Implementation matches task specification exactly.
