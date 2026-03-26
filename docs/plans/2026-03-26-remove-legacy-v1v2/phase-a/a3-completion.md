# A3: Remove short-term settings and markdown helpers from memory_utils.py

## Status: COMPLETE

## Changes Made

### scripts/memory_utils.py
- Removed `SHORT_TERM_TOKENS_PER_DAY` constant (was 750)
- Removed `globalShortTerm` and `projectShortTerm` from `DEFAULT_SETTINGS`
- Removed `archiveRetentionDays` from `DEFAULT_SETTINGS["decay"]`
- Removed `_calculate_token_limits()` function
- Removed `get_working_days()` function (markdown daily-file scanner)
- Removed `SHORT_TERM_TOKENS_PER_DAY` and `get_working_days` from `__all__`
- Removed `get_working_days` from Key Interfaces comment block
- Simplified `load_settings()`: `totalTokenBudget` now = `globalLongTerm.tokenLimit + projectLongTerm.tokenLimit`
- Updated self-test block to print LTM token limits instead of short-term days

### tests/test_memory_utils.py
- Removed imports: `SHORT_TERM_TOKENS_PER_DAY`, `_calculate_token_limits`, `get_working_days`
- Removed `TestCalculateTokenLimits` class (3 tests)
- Removed `TestGetWorkingDays` class (3 tests)
- Updated `TestLoadSettings`: tests now assert `globalShortTerm`/`projectShortTerm` are absent, `archiveRetentionDays` is absent, and `totalTokenBudget` = 6000 (3000+3000)
- Updated `test_user_overrides_merge` to test `globalLongTerm.tokenLimit` override
- Updated `test_invalid_json_returns_defaults` to check `globalLongTerm.tokenLimit`

## Kept (as specified)
- `get_global_working_days()`, `get_project_working_days()` (v3 JSONL-based)
- `_clear_working_days_cache()` (used by `rebuild_projects_index_quiet`)
- `collect_ltm_files()`, `LTM_ENTRY_PATTERN`, `is_routed_match` (deferred to B2)
- All path helpers, markdown parsing, synthesis state management

## Net diff
- 149 lines removed, 21 lines added
- 168 tests pass (6 removed, 4 new/updated)

## Downstream impact
- `scripts/decay.py` references `archiveRetentionDays` via its own `DEFAULT_ARCHIVE_RETENTION_DAYS` constant (independent)
- `scripts/token_usage.py` references `globalShortTerm`/`projectShortTerm` (will break; handled in separate task)
