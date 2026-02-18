# Ticket: [routed] dedup filtering implemented but inconsistently applied

**Date:** 2026-02-18
**Priority:** Medium
**Component:** synthesis, load_memory.py
**Related:** docs/plans/2026-02-12-routed-dedup-design.md, docs/plans/2026-02-12-routed-dedup-implementation.md

## Problem

The `[routed]` dedup system (designed 2026-02-12) is partially implemented:

- `filter_daily_content()` correctly skips `[routed]` entries at load time (Task 1 done)
- Keyword matching helpers (`extract_entry_keywords`, `is_routed_match`) exist (Task 3 helpers done)
- Synthesis prompt includes `[routed]` marking instructions (Task 2 done)

But synthesis is **not consistently marking entries** as `[routed]`. Measured on 2026-02-18 for the `investing` project (5 short-term working days):

| Day | Routed | Unrouted | Total |
|-----|--------|----------|-------|
| 2026-02-14 | 0 | 0 | 0 (no investing entries) |
| 2026-02-15 | 3 | 39 | 42 |
| 2026-02-16 | 0 | 34 | 34 |
| 2026-02-17 | 3 | 24 | 27 |
| 2026-02-18 | 8 | 13 | 21 |

Only 14 of 124 investing entries are marked `[routed]` (11%). Many entries exist in both the daily file AND the project long-term memory file, loading duplicate content into every session.

## Impact

- ~3,145 duplicate tokens loaded per session (estimated)
- Project short-term at 5,544 tokens (106% of 5,250 limit), would drop to ~2,400 with proper dedup
- 3 of 4 memory components over their individual limits (global LTM 105%, project LTM 101%, project STM 106%)
- Total memory at 96% of budget, growing

## Root Cause

The synthesis subagent receives the `[routed]` instruction but doesn't reliably follow it. Likely reasons:

1. **Instruction buried in long prompt** — the `[routed]` marking instruction competes with many other synthesis instructions for attention
2. **No enforcement** — marking is purely instruction-based with no programmatic verification
3. **One-time migration never run** — the `devtools.py mark-routed` command exists but was never executed against the daily files, so the backlog of unmarked entries persists

## Proposed Fix

### Option A: Run migration + reinforce prompt (low effort)

1. Run `python3 scripts/devtools.py mark-routed` to retroactively mark existing duplicates
2. Move the `[routed]` instruction higher in the synthesis prompt (before routing instructions, not after)
3. Add a post-synthesis verification step: after synthesis writes daily files, scan for entries that match LTM and mark any the subagent missed

### Option B: Programmatic dedup at load time (medium effort, more robust)

Instead of relying on the synthesis subagent to mark entries, filter duplicates programmatically when loading short-term memory:

1. At load time, collect all LTM entry keywords
2. For each STM entry, run `is_routed_match()` against LTM entries
3. Skip matches (same as `[routed]` filtering, but computed live)
4. Keep `[routed]` markers as an optimization hint (skip the match check for pre-marked entries)

Trade-off: adds ~50-100ms to session startup but eliminates dependence on subagent compliance.

### Option C: Hybrid (recommended)

1. Run the one-time migration now (Option A step 1)
2. Add programmatic dedup as a fallback at load time (Option B)
3. Keep synthesis `[routed]` marking as a fast path (pre-marked entries skip the match check)

This gives immediate relief (migration), long-term robustness (programmatic fallback), and performance (pre-marked fast path).

## Acceptance Criteria

- [ ] `python3 scripts/token_usage.py` shows project STM under 5,250 token limit
- [ ] `echo '{"session_id": "test"}' | python3 scripts/load_memory.py 2>/dev/null | grep -c "\[routed\]"` returns 0 (no routed entries in output, excluding synthesis prompt text)
- [ ] Entries that exist in both LTM and daily files are not loaded twice
- [ ] Full test suite passes
