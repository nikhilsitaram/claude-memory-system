# B2 Completion: Integrate injection logging into prompt_recall.py

## Changes

### scripts/prompt_recall.py
- Added `injected_log` and `filtered_log` lists before the search/filter loop
- When `is_recently_injected()` returns True, appends to `filtered_log` with `{id, content_preview, reason: "deduped"}`
- When a memory is injected, appends to `injected_log` with `{id, content_preview, scope}`
- After the try/except/finally block and elapsed time calculation, calls `log_prompt_recall()` in a try/except block with lazy import of `injection_log`
- `locals()` guards on `results`, `injected_log`, and `filtered_log` handle early exceptions
- Stdout output format unchanged -- injection logging is purely fire-and-forget

### tests/test_prompt_recall.py
- Added `TestInjectionLogging` class with helper `_make_mock_dp()`
- `test_main_calls_log_prompt_recall`: verifies log_prompt_recall is called with correct session_id, prompt_preview, candidates count, injected list, filtered list, and latency_ms
- `test_main_tracks_filtered_deduped_candidates`: pre-records an injection, verifies the deduped item appears in filtered list with reason="deduped" and the fresh item appears in injected list
- `test_main_still_works_when_injection_log_missing`: patches injection_log as None in sys.modules, verifies stdout output still contains `[memory]` block

## Verification
- `python3 -m pytest tests/test_prompt_recall.py -v` -- 23 passed (20 existing + 3 new)
- `python3 -m pytest tests/ -q` -- 2 pre-existing failures in test_load_memory.py (B1-related, not caused by B2)
