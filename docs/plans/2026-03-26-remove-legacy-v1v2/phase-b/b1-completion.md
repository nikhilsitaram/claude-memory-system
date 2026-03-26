# B1: Clean install.py - Completion Notes

## Status: Complete

## Changes Made

### scripts/install.py

1. **copy_templates()**: Removed markdown template list (`global-long-term-memory.md`, `project-long-term-memory.md`, `daily-template.md`) and the copy loop. Removed the block that copies `global-long-term-memory.md` to memory root. Function now only copies web frontend templates and `settings.json`.

2. **create_database()**: Replaced manual DB creation (SCHEMA_DDL, v2 seed, migrate_markdown_to_db, _migrate_v2_to_v3) with a single `ensure_db()` call from storage.py. Removed imports of `migrate_markdown_to_db`, `_migrate_v2_to_v3`, `SCHEMA_DDL`, `sqlite3`, and `get_db_path`.

### tests/test_install.py

1. **TestCopyTemplates**: Renamed `test_copies_templates_and_defaults` to `test_copies_settings_and_web_templates`. Now verifies settings.json and web templates are copied, and asserts markdown templates are NOT copied. Renamed `test_does_not_overwrite_existing_files` to `test_does_not_overwrite_existing_settings` (only settings.json is relevant now).

2. **TestCreateDatabase**: Replaced `test_create_database_creates_db_and_migrates` with `test_create_database_calls_ensure_db` which mocks `ensure_db()` and `close_db()` to verify the new delegation pattern.

## Test Results

```
61 passed in 0.08s
```

All tests pass, including the updated copy_templates and create_database tests.
