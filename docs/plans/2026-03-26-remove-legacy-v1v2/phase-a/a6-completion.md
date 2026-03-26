# A6: Refactor load_memory.py synthesis prompt builder and remove inline synthesis

## Status: COMPLETE

## Changes Made

### scripts/load_memory.py

**Deleted functions:**
- `_build_synthesis_instructions()` (v2 format with ===PROJECT:=== blocks)
- `_get_project_names_str()` (loaded project names from index for v2 instructions)
- `_strip_profile_sections()` and `_PROFILE_HEADERS_RE` regex (stripped profile sections from global LTM for v2 prompts)

**Refactored `_build_embedded_files`:**
- Simplified to transcript-only: removed `include_dailies`, `daily_data` parameters
- No longer reads global LTM, project LTMs, or existing daily files
- Returns `{"transcripts": {...}}` only

**Refactored `_build_preextracted_prompt`:**
- Removed `global_ltm` and `project_ltms` extraction from embedded_files
- Removed else branch that built LTM from markdown files -- now uses "(no existing memories)" when no vector_memories
- Removed existing_dailies merge block and `merge_instructions` from template

**Refactored `_build_synthesis_prompt`:**
- Now calls `_build_synthesis_instructions_v3()` instead of `_build_synthesis_instructions(project_names_str)`
- Removed `_get_project_names_str()` call

**Updated `write_synthesis_prompt` caller:**
- Changed `_build_embedded_files(single_date_files, include_dailies=..., daily_data=...)` to `_build_embedded_files(single_date_files)`

**Removed inline synthesis block from `main()`:**
- Removed entire `if pending_dates and should_synthesize and not synthesis_deferred` block (~90 lines)
- Removed v3 schema check that force-set synthesis_deferred
- Removed `settings = load_settings()` from main (no longer needed)
- Simplified stdin parsing (just consumes to prevent broken pipe, no longer tracks session_id)

**Cleaned imports:**
- Removed: `get_daily_dir`, `get_global_memory_file`, `get_project_memory_dir`
- Removed: `import re` (was only used by `_PROFILE_HEADERS_RE`)

### tests/test_load_memory.py

**Removed test classes:**
- `TestStripProfileSections` (and `SAMPLE_GLOBAL_LTM` fixture)
- `TestGetProjectNamesStr`
- `TestBuildSynthesisInstructions` (v2)
- `TestBuildEmbeddedFilesWithDailies`
- `TestPromptMergeContext`
- `TestSynthesisDeferredSetting`
- `TestSynthesisPromptRoutedMarker`
- `TestSynthesisPromptSimplified`
- `TestSynthesisPromptProjectFormat`

**Updated test classes:**
- `TestBuildPreextractedPrompt`: replaced LTM markdown assertion with "(no existing memories)" placeholder test
- `TestBuildSynthesisPromptIntegration`: updated to verify v3 MEMORY_OPS format instead of v2 PROJECT blocks
- `TestBuildEmbeddedFiles`: simplified to test transcript-only behavior, removed global_ltm/project_ltms/mock dependencies
- `TestSynthesisPromptCrud`: updated to use `_build_synthesis_instructions_v3()` instead of v2
- `TestPreRetrievalPrompt`: updated LTM fallback tests to verify "(no existing memories)" placeholder
- `TestSkipMemory`: updated to mock `check_synthesis_errors` instead of removed `load_settings` in main()

**Updated imports:**
- Removed: `_build_synthesis_instructions`, `_get_project_names_str`, `_strip_profile_sections`

## Verification

```
python3 -m pytest tests/test_load_memory.py -q
81 passed in 0.16s
```
