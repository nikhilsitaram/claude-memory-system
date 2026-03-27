# B1 Completion: Integrate injection logging into load_memory.py

## Changes

### scripts/load_memory.py
- Added `import time` to top-level imports
- Changed `_load_from_db` return type from `str | None` to `tuple[str, list[dict], list[str]] | None`
- Added `tiers_metadata: list[dict]` and `alerts: list[str]` alongside `sections` at function start
- Each of the 5 tiers (Profile, Session, Project, Global, Recent) now appends metadata dict with `name`, `count`, `tokens_est`, `ids` to `tiers_metadata`
- Token estimation uses `len(content or "") // 4` per row
- Changed early `return ""` (FileNotFoundError) to `return ("", [], [])`
- Health alerts captured into `alerts` variable (renamed local import to avoid shadowing)
- Final return changed from plain string to `(sanitized_text, tiers_metadata, alerts)`
- `return None` unchanged for v2 DB signal
- `main()` now captures stdin JSON payload into `stdin_payload` and extracts `sessionId` (fallback: `session-{int(time.time())}`)
- `main()` calls `rotate_log()` after stdin consumption
- `main()` wraps `_load_from_db` with `time.monotonic()` timing
- `main()` unpacks the tuple result and calls `log_session_start()` fire-and-forget after printing
- Stdout output format (`<memory>...</memory>`) unchanged

### tests/test_load_memory.py
- Updated `TestSmartLoading`: all tests that used `result` as a string now unpack `text, tiers, alerts = result` and assert on `text`
- Updated `test_returns_empty_string_when_no_db` to `test_returns_empty_tuple_when_no_db` checking `("", [], [])`
- Updated `TestSessionContinuity`: all 5 tests unpack the tuple
- Added `TestLoadFromDbTiersMetadata` class with 7 tests:
  - `test_returns_tuple_with_three_elements`
  - `test_tier_dicts_have_required_keys`
  - `test_token_estimation_uses_char_div_4`
  - `test_returns_none_for_v2_db`
  - `test_empty_db_returns_tuple_with_zero_counts`
  - `test_tier_ids_populated`
  - `test_five_tiers_always_present`

### tests/test_integration_git_subdir.py
- Updated `_load_from_db` mock return value from `""` to `("", [], [])` to match new tuple return type

## Verification
- `python3 -m pytest tests/test_load_memory.py -v` -- 91 passed (84 existing updated + 7 new)
- `python3 -m pytest tests/ -q` -- 1095 passed, 8 skipped, 0 failures
