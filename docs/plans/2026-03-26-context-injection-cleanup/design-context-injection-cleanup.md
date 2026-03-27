# Design: Context Injection Cleanup (Issue #103)

## Problem

SessionStart injects ~4,350 tokens of memory context per session. ~1,500 tokens (~34%) are wasted on:

1. **Profile waste (~300 tokens)**: 13 of 17 profile `data_points` are empty HTML comment placeholders or bare single-word tags (e.g., `Python-3.13`, `claude-code`) migrated from the old template format. They carry zero information.

2. **Near-duplicate memories (~1,000 tokens)**: 8 near-identical "git init default branch" memories created by independent synthesis runs. Each session independently rediscovered the same fact, and `_apply_add_v3` has no dedup check — it blindly inserts.

3. **Stale project memories (~200 tokens)**: Completed work items still being served (backfill resume instructions for merged PR #100, Phase A dependencies for a merged branch, Phase A/B PR review status for completed PRs).

4. **False "DB empty" health alert**: `health.py` checks the legacy `chunks` table first (if/elif precedence). The `chunks` table exists with 0 rows from the v2-to-v3 migration, but `data_points` has 567 memories. Health alert incorrectly fires every session.

5. **Memories never decay in practice**: Global memories stuck at salience 1.0, project memories similarly inflated. Root cause: `_batch_update_data_point_access` in `load_memory.py` applies salience reinforcement (ETA=0.18) every time a memory is loaded at SessionStart. Any memory ranked high enough to be auto-loaded gets reinforced, which counteracts the existing decay (lambda=0.005 for HOT tier). The decay machinery works correctly — the problem is that passive auto-loading is treated as a usefulness signal when it is not. Only active access (MCP search, user prompt recall) indicates genuine relevance.

**Who is affected**: Every Claude Code session for this user. Wasted tokens reduce the budget available for genuinely useful context.

**Consequences of not solving**: Token waste compounds — each synthesis run can create more duplicates, stale entries accumulate, and the health alert creates noise that masks real problems.

## Goal

Recover ~1,500 wasted tokens from SessionStart injection, prevent systemic duplication in synthesis, and fix the false health alert. Every injected token should carry useful information.

## Success Criteria

1. Profile tier contains only items with actual informational content — no HTML comments, no bare single-word tags without context
2. No near-duplicate memories (SimHash Hamming distance <= 3) exist within the same scope after cleanup
3. Health check correctly reports memory count on a v3 DB with legacy tables present
4. Memories that have not been actively accessed (MCP search, user prompt recall) in 140+ days have salience below 0.5 — passive SessionStart auto-loading does not prevent decay
5. Synthesis ADD operations check for SimHash near-duplicates before inserting, converting to UPDATE when a match is found
6. Stale project memories about completed work are soft-deleted

## Architecture

### Changes by File

| # | File | Change | Lines est. |
|---|------|--------|------------|
| 1 | `scripts/health.py` | Check `user_version` first; when >=3, skip `chunks`/`nodes` branches | ~10 |
| 2 | `scripts/storage.py` | Drop legacy `chunks`/`nodes` tables in `ensure_db` when user_version >= 3 (SCHEMA_DDL already cleaned by PR #104) | ~15 |
| 3 | `scripts/synthesis.py` | SimHash dedup gate in `_apply_add_v3` | ~30 |
| 4 | `scripts/synthesis_cron.py` | Include existing scope-filtered memories in synthesis pre-retrieval prompt | ~20 |
| 5 | `scripts/load_memory.py` | Add `passive` parameter to `_batch_update_data_point_access`; skip salience reinforcement on passive loads (all scopes) | ~15 |
| 6 | `scripts/storage.py` | One-time cleanup function: delete profile waste, git-init dupes, stale memories | ~40 |
| 7 | `tests/` | Tests for all code changes | ~150 |

### SimHash Dedup Gate (synthesis.py)

In `_apply_add_v3`, before INSERT:

1. Compute SimHash of new content via `compute_simhash(fact)`
2. Query `data_points` for candidates: `type='memory' AND scope=<same> AND salience > 0 AND created_at > 90_days_ago`. Uses `idx_dp_scope` and `idx_dp_type` indexes for filtering; Hamming distance computed in Python over the candidate set.
3. For each candidate, compute Hamming distance via `hamming_distance()`; if <= 3 (near-duplicate):
   - Bump `evidence_count` on existing entry
   - Merge `source_sessions` (append new session IDs)
   - Boost salience: `min(1.0, existing.salience + 0.05)` (same pattern as `consolidation.py` line 320)
   - Return result with `status: "deduped"` instead of inserting
4. If no near-duplicate found, proceed with normal INSERT

**Why both SimHash gate AND pre-retrieval enhancement**: Belt-and-suspenders. The SimHash gate catches textual near-duplicates deterministically (same fact, slightly different wording — like the 8 git-init memories). The pre-retrieval enhancement gives the LLM context to choose UPDATE for semantic duplicates that SimHash misses (completely different wording, same concept). Neither alone covers both cases.

### Pre-retrieval Enhancement (synthesis_cron.py)

When building the synthesis prompt for a batch of sessions, include the top-10 existing memories (by salience) for the target scope. This gives the LLM context to choose UPDATE over ADD for semantic duplicates that SimHash might miss (different wording, same concept).

### Passive vs Active Reinforcement (load_memory.py)

No new decay tier needed. The existing HOT tier (lambda=0.005) already applies to all memories — the problem is that passive-load reinforcement counteracts it for any memory that ranks high enough to be auto-loaded.

Fix: add a `passive: bool = False` parameter to `_batch_update_data_point_access`:
- When `passive=True`: still update `access_count` and `last_accessed` (for tracking), but **skip salience reinforcement and neighbor boosting** for all scopes
- `_load_from_db` calls with `passive=True` — SessionStart auto-loading is not a usefulness signal; it just means the memory ranked high enough to be included
- MCP `search_memories`, user prompt recall, and other active access paths use the default `passive=False` — these represent genuine user interest and should reinforce salience

With reinforcement removed from passive loads, the existing decay tiers naturally reduce salience over time. At salience 1.0 in the HOT tier, decay drops ~0.005/day. Time to reach 0.5: ~140 days of no active access. Memories that the user actively searches or that synthesis reinforces via evidence_count bumps stay alive.

### Legacy Table Cleanup (health.py + storage.py)

**health.py**: When `user_version >= 3` (checked via `_get_schema_version`), skip the `if 'chunks' in tables` branch entirely and go straight to the `data_points` query. Similarly skip the `if 'nodes' in tables` branch for graph stats.

**storage.py**: In `ensure_db`, when `user_version >= 3`, execute `DROP TABLE IF EXISTS chunks` and `DROP TABLE IF EXISTS nodes`. The `chunks` table has 0 rows; `nodes` has data but all entity information was migrated to `data_points` (type='entity') during the v2-to-v3 migration. PR #104 already removed these definitions from `SCHEMA_DDL`, so they will not be recreated — the DROP just cleans up stale tables in existing databases.

### One-time Data Cleanup (storage.py)

New function `cleanup_stale_data(conn)` callable from CLI or migration:
- DELETE profile data_points where content matches `<!-- ` pattern or has <= 2 whitespace-separated words
- For near-duplicate clusters (SimHash Hamming distance <= 3, same scope): keep the one with highest `evidence_count` (earliest `created_at` as tiebreak), soft-delete the rest (set salience=0.0)
- Soft-delete specific stale project memories by content pattern match (patterns for: "resume backfill", "Phase A vector search", "Phase A (SimHash, PR #66)")

## Key Decisions

1. **SimHash threshold = 3**: Same as `are_near_duplicates()` default in `simhash.py`. All 8 git-init memories are paraphrases with the same key terms — SimHash catches this.

2. **Dedup converts ADD to UPDATE (not reject)**: Bumping evidence_count and source_sessions preserves provenance. Existing content is kept unless new content is significantly longer (>2x chars), in which case it replaces. Salience boost on dedup: `min(1.0, existing.salience + 0.05)`.

3. **DROP legacy tables**: Safe because `chunks` has 0 rows and `nodes` data was migrated to `data_points` type='entity'. No v2 code remains in the codebase after PR #104 (merged). The `SCHEMA_DDL` definitions and `health.py` query branches for these tables must also be removed in this PR.

4. **Passive vs active reinforcement**: The distinction solves the root cause (passive auto-load is not a usefulness signal) without changing the decay formula or adding new tiers. Being auto-loaded at SessionStart means "this memory ranked high enough" — not "the user found this useful." Only active access (MCP search, prompt recall) indicates genuine relevance and should reinforce salience.

5. **Cleanup as a callable function**: Not auto-run on every load. Can be invoked from CLI, tests, or as a one-time migration step. The SimHash dedup gate prevents recurrence.

## Non-Goals

- Refactoring the tier loading system in `load_memory.py` (works correctly, just needs cleaner data)
- Changing synthesis prompt structure beyond pre-retrieval context
- Reworking the profile storage format (just cleaning bad data from migration)
- Token budget enforcement in load_memory.py (separate concern)
- Changing the decay formula or adding new decay tiers

## Implementation Approach

**Prerequisite:** Rebase onto origin/main (which includes PR #104) before starting. The changes described assume the post-#104 state of `storage.py` and `health.py`.

Single phase — all 7 changes are independent. Tasks can be parallelized via subagents with worktree isolation.
