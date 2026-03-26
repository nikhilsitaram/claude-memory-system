# B2: Remove LTM-operating commands from devtools.py

## Status: COMPLETE

## Changes

### scripts/devtools.py
- Removed `cmd_mark_routed()` function (one-time migration command)
- Removed `cmd_validate_ltm()` function (LTM file validator)
- Removed subparser registrations for `mark-routed` and `validate-ltm`
- Removed unused `re` import (only used by removed functions)
- Updated module docstring (removed "mark-routed dedup migration" reference)

### scripts/memory_utils.py
Removed orphaned helpers (only called by the deleted devtools commands):
- `LTM_ENTRY_PATTERN` constant + `__all__` entry
- `collect_ltm_files()` function + `__all__` entry
- `extract_entry_keywords()` function + `__all__` entry
- `is_routed_match()` function + `__all__` entry
- `_STOPWORDS` private constant (only used by `extract_entry_keywords`)
- `_ENTRY_PREFIX_PATTERN` private constant (only used by `extract_entry_keywords`)

### tests/test_devtools.py
- Removed all test classes (`TestKeywordDedupRealWorldCases`, `TestKeywordDedupEdgeCases`, `TestMarkRoutedKeywordDedup`, `TestValidateLtm`) -- all tested removed functions

### tests/test_memory_utils.py
- Removed `TestRoutedMatching` class (tested `is_routed_match` and `extract_entry_keywords`)
- Removed `TestLtmEntryPattern` class (tested `LTM_ENTRY_PATTERN`)
- Removed `TestCollectLtmFiles` class (tested `collect_ltm_files`)
- Removed `TestExtractEntryKeywordsMultiScope` class (tested `extract_entry_keywords`)
- Removed `extract_entry_keywords` and `is_routed_match` imports

## Verification
- Grep confirmed no remaining callers of removed symbols in scripts/ or tests/
- `python3 -m pytest tests/test_devtools.py tests/test_memory_utils.py -q` -- 154 passed
