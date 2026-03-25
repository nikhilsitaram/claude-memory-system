# A1 Completion Notes

**Summary:** Added `get_global_working_days(n)` and `get_project_working_days(project_scope, n)` to `memory_utils.py`, both deriving active dates from `.jsonl` file mtimes in `~/.claude/projects/`. Both use a module-level `_working_days_cache` dict (keyed by function+args) with `_clear_working_days_cache()` for test isolation. Added `synthesis.recentWorkingDays=7` and `synthesis.backfill.recentWorkingDays=7` to `DEFAULT_SETTINGS`.

**Deviations:**
- Added `@pytest.mark.working_day` marker to test classes and registered it in `pyproject.toml` so the plan's verification command (`-k 'working_day'`) matches the new tests. Without this, pytest's `-k` substring matching would not find `WorkingDays` (CamelCase) from `working_day` (snake_case). — Rule 1 (Auto-fix bug) — verification command would not select any tests otherwise.
- Used a separate `_projects_index_for_working_days` cache instead of reusing `_projects_index_cache` to avoid coupling between the two subsystems and ensure `_clear_working_days_cache()` only clears working-day-related state. — Rule 2 (Auto-add critical) — test isolation requires independent cache control.

**Files Changed:**
- `scripts/memory_utils.py` — Added working-day functions, cache, and synthesis settings
- `tests/test_memory_utils.py` — Added 11 tests across 3 test classes
- `pyproject.toml` — Registered `working_day` pytest marker

**Test Results:** 11/11 new tests pass. Full suite: 1218 passed, 8 skipped (all pre-existing).

**Deferred Issues:** None.
