# A1: Fix false health alert for v3 DBs with legacy tables

## Status: Complete

## Summary

Fixed a bug where `health_report()` in `scripts/health.py` queried empty legacy `chunks`/`nodes` tables instead of the `data_points` table on v3+ databases. This caused false "DB empty" alerts when a v3 DB retained empty legacy tables from migration.

## Changes

### scripts/health.py
- **Chunk/memory statistics block (line 87)**: Reordered `if/elif` chain to check `report.schema_version >= 3 and 'data_points' in tables` first, before falling back to `'chunks' in tables` for v2 DBs. Added a third `elif 'data_points' in tables` fallback for edge cases.
- **Graph statistics block (line 139)**: Same reordering -- checks `schema_version >= 3` first for `data_points` entity query, then falls back to `nodes` table for v2, with a `data_points` fallback.

### tests/test_health.py
- Added `TestHealthV3LegacyTableBug` class with 2 regression tests:
  - `test_v3_db_with_empty_chunks_table_not_empty_alert`: v3 DB with empty `chunks` table and 5 data_points memories reports `total_chunks == 5` and no "empty" alert.
  - `test_v3_db_with_legacy_nodes_table_uses_data_points`: v3 DB with empty `nodes` table and 2 entity data_points reports `graph_nodes == 2`.

## Verification

- Both new tests confirmed to FAIL before the fix (red phase)
- All 24 health tests pass after the fix (green phase)
- Full test suite: 1034 passed, 8 skipped, 0 failures

## Deviations

None. Implementation followed the task specification exactly.
