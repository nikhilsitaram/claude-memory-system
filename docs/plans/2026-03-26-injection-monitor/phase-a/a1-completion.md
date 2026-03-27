# A1: Create injection_log.py logging module with rotation

## Status: Complete

## What was done

1. **Created `scripts/injection_log.py`** -- standalone JSONL append logger with:
   - `get_log_path()` -- returns `get_memory_dir() / ".injection-log.jsonl"`
   - `_is_enabled()` -- checks `settings.injectionLog.enabled`, defaults True on missing/error
   - `log_session_start()` -- logs SessionStart hook with tiers, totals, latency, health alerts
   - `log_prompt_recall()` -- logs UserPromptSubmit hook with prompt preview (80-char truncation), candidates, injected/filtered IDs
   - `rotate_log(max_lines=500, keep_lines=200)` -- truncates when exceeded
   - `read_log(since, session_id, max_entries)` -- reads/filters JSONL entries
   - All public functions catch exceptions silently (fire-and-forget)

2. **Modified `scripts/memory_utils.py`** -- added `"injectionLog": {"enabled": True}` to `DEFAULT_SETTINGS`

3. **Created `tests/test_injection_log.py`** -- 28 tests across 7 test classes:
   - `TestGetLogPath` (2 tests)
   - `TestIsEnabled` (4 tests)
   - `TestLogSessionStart` (4 tests)
   - `TestLogPromptRecall` (4 tests)
   - `TestRotateLog` (5 tests)
   - `TestReadLog` (6 tests)
   - `TestEnabledToggle` (3 tests)

## Deviations

None. Implementation matches the task spec exactly.

## Test results

- `tests/test_injection_log.py`: 28 passed
- Full suite: 1080 passed, 8 skipped, 0 failures
