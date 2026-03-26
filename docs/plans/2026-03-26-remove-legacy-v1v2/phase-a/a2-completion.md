# A2: Remove markdown decay functions from decay.py

## Status: COMPLETE

## Changes

### scripts/decay.py
- Updated module docstring to reflect SQL-only decay
- Removed constants: `AUTO_PINNED_SECTIONS`, `ARCHIVE_HEADER_PATTERN`, `DECAY_ELIGIBLE_SECTIONS`, `DEFAULT_ARCHIVE_RETENTION_DAYS`, `DATE_PATTERN`
- Removed functions: `parse_learning_date()`, `is_protected_section()`, `is_decay_eligible()`, `parse_sections()`, `parse_learnings()`, `should_decay_entry()`, `build_project_work_days_map()`, `decay_file()`, `append_to_archive()`, `purge_old_archives()`, `run()`
- Cleaned imports: removed `re`, `timedelta`, `get_global_memory_file`, `get_memory_dir`, `get_project_memory_dir`, `get_projects_index_file`, `load_json_file`, `project_name_to_filename`, `parse_markdown_sections`, `rebuild_projects_index_quiet`, `load_settings`
- Refactored `main()` to call `decay_data_points()` and `cleanup_near_zero_salience()` directly via `storage.get_db()`/`storage.close_db()`

### tests/test_decay.py
- Removed test classes: `TestDatePattern`, `TestArchiveHeaderPattern`, `TestParseSections`, `TestParseLearnings`, `TestShouldDecayEntry`, `TestBuildProjectWorkDaysMap`, `TestDecayFile`, `TestAppendToArchive`, `TestPurgeOldArchives`, `TestRebuildProjectsIndexQuiet`, `TestMainCallsRebuild`, `TestArchiveThreshold`
- Removed standalone parametrized tests: `test_parse_learning_date`, `test_protected_sections`, `test_non_protected`, `test_decay_eligible`, `test_not_decay_eligible`
- Cleaned imports: removed references to deleted functions/constants
- Added `TestMain` class with 3 tests: normal run, dry-run flag, and DB-close-on-error

## Verification
- `python3 -m pytest tests/test_decay.py -q` -- 35 passed
- No remaining references to removed functions in source code
