# B3: Delete token_usage.py, markdown templates, test_migration.py, test_token_usage.py

**Status:** Complete

## Files Deleted
- `scripts/token_usage.py`
- `templates/daily-template.md`
- `templates/global-long-term-memory.md`
- `templates/project-long-term-memory.md`
- `tests/test_migration.py`
- `tests/test_token_usage.py`

## Reference Cleanup
Stale references to deleted files were found and removed in:
- `scripts/install.py` — removed `token_usage.py` from `scripts_to_link`, removed markdown template copy loop and `global-long-term-memory.md` default copy (dead code since templates deleted)
- `scripts/uninstall.py` — removed `token_usage.py` from cleanup paths list and manual removal instructions
- `scripts/devtools.py` — removed `token_usage.py` from verify-install script list, removed `tokens` mode from `memory-status` command (argparse choices and execution block)
- `tests/test_install.py` — rewrote `TestCopyTemplates` tests to only check settings.json and web templates (no longer tests markdown template copying)

## Verification
- `grep -rn "token_usage" scripts/ tests/` — no matches
- `grep -rn "daily-template" scripts/` — no matches
- Test suite: 1173 passed, 2 skipped (0 failures)
