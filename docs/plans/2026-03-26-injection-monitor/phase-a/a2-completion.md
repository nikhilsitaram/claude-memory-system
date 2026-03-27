# A2: Register injection_log.py in install.py - Completion Notes

## Summary
Added `injection_log.py` to the `scripts_to_link` list in `link_scripts()` so it gets symlinked to `~/.claude/scripts/` during installation.

## Changes Made

### scripts/install.py
- Added `"injection_log.py"` to `scripts_to_link` list after `"web_app.py"`, with comment describing its purpose as injection monitor logging for SessionStart/PromptRecall hooks.

### tests/test_install.py
- Added `TestInjectionLogRegistration` class with one test (`test_injection_log_in_scripts_to_link`) that uses `inspect.getsource` to verify the script name appears in the `link_scripts()` function body.

## TDD Process
1. Wrote failing test -- confirmed it failed (injection_log.py not found in source)
2. Added injection_log.py to scripts_to_link list
3. Confirmed test passes
4. Ran full test suite -- all 62 tests pass, no regressions

## Deviations
None.
