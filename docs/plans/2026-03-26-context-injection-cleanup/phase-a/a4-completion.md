# A4: Pre-retrieval enhancement for synthesis prompt -- Completion Notes

## Summary

Added scope-filtered memory injection to the synthesis prompt in `_run_synthesis_v3`. This is a "belt-and-suspenders" approach alongside the existing vector retrieval: the new injection provides the top-10 memories by salience for the target scope, ensuring the LLM sees high-salience existing memories even if vector similarity misses them.

## Changes

### scripts/synthesis_cron.py
- Added `_inject_scope_memories(conn, prompt_text, scope, limit=10)` function:
  - Queries `data_points` for `type='memory'` matching scope with `salience > 0`, ordered by salience DESC
  - Formats memories as `[id] content` lines under `## Existing Memories for scope: <scope>` header
  - Inserts section before `## Transcripts` marker if present, otherwise appends
  - Returns prompt unchanged if no memories found
- Modified `_run_synthesis_v3` signature to accept optional `scope: str = ""` parameter
- Added scope injection call after v2 section stripping, before tempfile write

### tests/test_synthesis_cron.py
- Added `TestPreRetrievalEnhancement` class with 2 tests:
  1. `test_scope_memories_injected_into_prompt` -- verifies 3 memories appear in correct format and position
  2. `test_scope_memories_limited_to_10` -- verifies only top 10 by salience are included when 15 exist

## Integration Note

The `run_synthesis` caller does not currently pass a scope (defaults to `""`), because `write_synthesis_prompt` does not output a project name. The scope injection is wired and functional -- it activates when a scope is provided. The backfill path (`run_backfill`) uses `_run_claude_backfill` directly rather than `_run_synthesis_v3`, so no change was made there.

## Test Results

- 77/77 tests pass in `test_synthesis_cron.py`
- 1034/1034 tests pass across the full suite (8 skipped, pre-existing)
- No regressions

## Deviations

None.
