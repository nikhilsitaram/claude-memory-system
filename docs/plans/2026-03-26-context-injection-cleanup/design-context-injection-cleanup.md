# Design: Context Injection Cleanup (Issue #103)

## Problem

SessionStart injects ~4,350 tokens of memory context per session. ~1,500 tokens (~34%) are wasted on:

1. **Profile waste (~300 tokens)**: 13 of 17 profile `data_points` are empty HTML comment placeholders or bare single-word tags (e.g., `Python-3.13`, `claude-code`) migrated from the old template format. They carry zero information.

2. **Near-duplicate memories (~1,000 tokens)**: 8 near-identical "git init default branch" memories created by independent synthesis runs. Each session independently rediscovered the same fact, and `_apply_add_v3` has no dedup check — it blindly inserts.

3. **Stale project memories (~200 tokens)**: Completed work items still being served (backfill resume instructions for merged PR #100, Phase A dependencies for a merged branch, Phase A/B PR review status for completed PRs).

4. **False "DB empty" health alert**: `health.py` checks the legacy `chunks` table first (if/elif precedence). The `chunks` table exists with 0 rows from the v2→v3 migration, but `data_points` has 567 memories. Health alert incorrectly fires every session.

5. **Global memories never decay**: 32 global memories pinned at salience 1.0. Useful when first discovered but never accessed again — permanently occupying tier-4 slots with no rotation.

**Who is affected**: Every Claude Code session for this user. Wasted tokens reduce the budget available for genuinely useful context.

**Consequences of not solving**: Token waste compounds — each synthesis run can create more duplicates, stale entries accumulate, and the health alert creates noise that masks real problems.

## Goal

Recover ~1,500 wasted tokens from SessionStart injection, prevent systemic duplication in synthesis, and fix the false health alert. Every injected token should carry useful information.

## Success Criteria

1. Profile tier contains only items with actual informational content — no HTML comments, no bare single-word tags without context
2. No near-duplicate memories (SimHash Hamming distance ≤ 3) exist within the same scope after cleanup
3. Health check correctly reports memory count on a v3 DB with legacy tables present
4. Global memories decay with λ=0.005 when not accessed (~0.5 after ~140 days of zero access)
5. Synthesis ADD operations check for SimHash near-duplicates before inserting, converting to UPDATE when a match is found
6. Stale project memories about completed work are soft-deleted
7. All existing tests pass; new tests cover the dedup gate, global decay, and health check fix

## Architecture

### Changes by File

| # | File | Change | Lines est. |
|---|------|--------|------------|
| 1 | `scripts/health.py` | Check `user_version` first; when ≥3, skip `chunks` branch | ~10 |
| 2 | `scripts/storage.py` | Drop legacy `chunks`/`nodes` tables in v3 migration path | ~15 |
| 3 | `scripts/synthesis.py` | SimHash dedup gate in `_apply_add_v3` | ~30 |
| 4 | `scripts/synthesis_cron.py` | Include existing scope-filtered memories in synthesis pre-retrieval prompt | ~20 |
| 5 | `scripts/decay.py` | Add GLACIAL tier (λ=0.005) for `scope='global'` memories | ~15 |
| 6 | `scripts/storage.py` | One-time cleanup function: delete profile waste, git-init dupes, stale memories | ~40 |
| 7 | `tests/` | Tests for all code changes | ~150 |

### SimHash Dedup Gate (synthesis.py)

In `_apply_add_v3`, before INSERT:

1. Compute SimHash of new content via `compute_simhash(fact)`
2. Query `data_points` for candidates in the same scope with `type='memory'` and `salience > 0`
3. For each candidate, compute Hamming distance; if ≤ 3 (near-duplicate):
   - Bump `evidence_count` on existing entry
   - Merge `source_sessions`
   - Boost salience by reinforcement
   - Return result with `status: "deduped"` instead of inserting
4. If no near-duplicate found, proceed with normal INSERT

**SimHash query approach**: Query candidates with same scope using the `idx_dp_simhash` index. SQLite doesn't support Hamming distance natively, so fetch candidates and compute in Python. Limit to recent entries (last 90 days) to bound the scan.

### Pre-retrieval Enhancement (synthesis_cron.py)

When building the synthesis prompt for a batch of sessions, include the top-10 existing memories (by salience) for the target scope. This gives the LLM context to choose UPDATE over ADD for semantic duplicates that SimHash might miss (different wording, same concept).

### Global Decay (decay.py)

Add a new decay tier:
- **GLACIAL**: `scope='global'` → λ=0.005
- Applied before the existing HOT/WARM/COLD classification
- Global memories still get access-based reinforcement, so frequently-used ones stay high

### Legacy Table Cleanup (storage.py)

On DB open when `user_version >= 3`:
- `DROP TABLE IF EXISTS chunks`
- `DROP TABLE IF EXISTS nodes`

These tables are empty (0 rows) since the v2→v3 migration. PR #104 already removed all code that reads/writes them. Dropping them prevents health.py confusion and reduces DB file size.

### One-time Data Cleanup (storage.py)

New function `cleanup_stale_data()` callable from CLI or migration:
- DELETE profile data_points where content matches HTML comment pattern or is ≤ 2 words
- For git-init duplicates: keep the one with highest evidence_count (or earliest created_at as tiebreak), soft-delete the rest
- Soft-delete specific stale project memories by content pattern match

## Key Decisions

1. **SimHash threshold = 3**: Same as `are_near_duplicates()` default. All 8 git-init memories are paraphrases with the same key terms — SimHash catches this.

2. **Dedup converts ADD→UPDATE (not reject)**: Bumping evidence_count and source_sessions preserves provenance. The existing content is kept unless the new content is significantly longer (>2x chars), in which case it replaces.

3. **DROP legacy tables**: Safe because chunks=0 rows, nodes still has data but is unused by any v3 code (entities are in data_points). No migration path back to v2 — users on v2 must upgrade.

4. **λ=0.005 for global**: ~140 days to reach 0.5 with zero access. Slower than project memories but not permanent. Access reinforcement keeps useful entries alive.

5. **Cleanup as a callable function**: Not auto-run on every load. Can be invoked from CLI, tests, or as a one-time migration step.

## Non-Goals

- Refactoring the tier loading system in `load_memory.py` (works correctly, just needs cleaner data)
- Changing synthesis prompt structure beyond pre-retrieval context
- Reworking the profile storage format (just cleaning bad data from migration)
- Token budget enforcement in load_memory.py (separate concern)

## Implementation Approach

Single phase — all 7 changes are independent. Tasks can be parallelized via subagents with worktree isolation.
