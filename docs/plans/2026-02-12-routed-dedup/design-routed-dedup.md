# Design: `[routed]` Dedup Markers

**Date:** 2026-02-12
**Problem:** When synthesis routes entries from daily files to LTM, both copies load into context — the LTM entry (always loaded) and the daily entry (loaded as short-term for 2-7 working days). This wastes ~3,500 bytes per session.

**Solution:** Prefix routed daily entries with `[routed]` and skip them at load time.

## Format

Daily file entries that have been routed to LTM get prefixed:

```markdown
## Learnings
- [routed][claude-memory-system/gotcha] Missing defaultdict import crashed build_projects_index()
- [claude-memory-system/pattern] JSONL fallback discovers projects (NOT routed — stays in STM)
```

## Changes

### 1. `filter_daily_content()` in `memory_utils.py`
- Skip lines matching `^\s*-\s*\[routed\]` before scope matching
- Applies to both global and project STM loading (both call this function)

### 2. Synthesis prompt in `load_memory.py` `_build_synthesis_prompt()`
- Add instruction: when writing daily summaries, prefix any entry that was routed to LTM with `[routed]`
- This happens during the Step 2 Write call (no extra Edit needed)
- Only applies to Learnings and Lessons sections (Actions/Decisions are rarely routed)

### 3. One-time migration script
- Scan all LTM files (global + project) to collect entry descriptions
- For each daily file within the STM window (7 working days):
  - For each Learnings/Lessons entry, check if a conceptual match exists in LTM
  - If match found, prefix with `[routed]`
- Matching strategy: extract key terms, check overlap threshold
- Run once, then delete script (or keep in devtools.py)

### 4. Tests
- `filter_daily_content()`: verify `[routed]` entries are skipped
- `filter_daily_content()`: verify non-routed entries still load normally
- Migration: verify correct entries get marked, others untouched

## Token Impact

- **Before:** ~33,800 bytes total memory payload
- **After:** ~30,300 bytes (save ~3,500 bytes / 10.4%)
- Savings grow with more active projects (more LTM routing = more dedup)

## Non-goals

- No changes to LTM file format
- No changes to decay logic (decay operates on LTM entries, unaffected by daily markers)
- No changes to `/recall` search (searches raw daily files, `[routed]` entries still findable)
