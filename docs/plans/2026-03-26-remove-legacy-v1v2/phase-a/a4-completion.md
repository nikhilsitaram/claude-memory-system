# A4: Remove v2 synthesis dispatch from synthesis_cron.py

## Status: COMPLETE

## Changes

### scripts/synthesis_cron.py
- Deleted `_get_schema_version()` helper (was at ~line 277)
- Deleted `_run_synthesis_v2()` function (was at ~line 282, ~40 lines)
- Simplified `run_synthesis()`: removed schema version detection and v2 fallback dispatch; now always uses `_run_synthesis_v3()` as the sole synthesis path
- The function now unconditionally calls `get_db()` and passes the connection to `_run_synthesis_v3()`, `_run_decay_v3()`, and `_run_consolidation_post_step()`
- DB connection is properly closed in a `finally` block

### tests/test_synthesis_cron.py
- Removed 4 tests from `TestSynthesisCronV3`:
  - `test_get_schema_version_returns_3`
  - `test_get_schema_version_returns_0_for_fresh_db`
  - `test_v3_path_invoked_for_schema_3`
  - `test_v2_path_invoked_for_old_schema`
- Updated `TestRunSynthesis` tests to mock `_run_synthesis_v3` instead of `subprocess.run` (since `run_synthesis` no longer calls subprocess directly)
- Added new tests: `test_runs_decay_after_synthesis`, `test_runs_consolidation_on_success`, `test_skips_consolidation_on_failure`, `test_closes_db_on_success`
- Renamed `test_calls_claude_p_with_prompt` to `test_calls_v3_synthesis` to reflect new behavior

## Verification
```
python3 -m pytest tests/test_synthesis_cron.py -q
75 passed in 1.02s
```
