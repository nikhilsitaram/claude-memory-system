# Phase C Completion Notes

## Summary

Phase C implements the memory consolidation pipeline -- a batch process that finds clusters of redundant memories via vector similarity, uses headless LLM to decide merge vs. skip, and writes merged results with full provenance.

## Tasks Completed

### C1: Consolidation Pipeline Core
- **Created** `scripts/consolidation.py` with: `find_clusters`, `score_cluster`, `merge_cluster` (headless `claude -p`), `write_merge_result`, `run_consolidation`
- KNN-based connected components clustering (not HDBSCAN)
- Edge-aware exclusion: contradicts/supersedes pairs never cluster
- Max cluster size enforcement via weakest-edge removal
- Merged data_points get `consolidated=1` (decay protection), `source_type='consolidation'`, salience boost (max+0.05)
- SKIP handling: graceful passthrough when LLM refuses to merge
- FTS5 sync on new merged data_points
- **Tests**: 21 tests covering clustering, scoring, edge exclusion, merge writes, supersedes edges, soft-delete, SKIP flow, backfill mode, response parsing, prompt construction

### C2: Synthesis Cron Integration
- **Added** `metadata` table to storage.py (key-value store for timestamps)
- **Added** to `synthesis_cron.py`: `_should_consolidate()`, `_is_backfill()`, `_update_consolidation_timestamp()`, `_run_consolidation_post_step()`
- Consolidation runs as post-step after successful synthesis (v3 only)
- Daily gate: checks `last_consolidation` timestamp + `minMemories` threshold
- First-run detection triggers backfill mode (higher cluster cap)
- NULL-safe SQL for source_type filtering
- **Tests**: 6 gate tests + 3 metadata table tests

### C3: Settings and Install
- **Added** `consolidation` settings to `DEFAULT_SETTINGS`: intervalHours=24, minMemories=5, similarityThreshold=0.80, maxClusters=15, backfillMaxClusters=30, model=sonnet
- **Added** `recall` settings to `DEFAULT_SETTINGS`: maxPromptLength=500, minPromptLength=15, maxInjectionsPerPrompt=3, maxTokenBudget=500
- **Added** `consolidation.py` to `link_scripts()` in install.py
- **Tests**: 4 tests for settings presence and load_settings merge behavior

### C4: /consolidate Skill
- **Created** `skills/consolidate/SKILL.md` with frontmatter and usage instructions
- **Added** `consolidate` to `link_skills()` in install.py
- **Tests**: 1 test for skill registration

## Test Results

- Baseline: 1066 passed
- Final: 1101 passed, 8 skipped, 0 failed
- New tests: 35 (21 consolidation + 6 gate + 3 metadata + 4 settings + 1 skill)

## Files Changed

### New Files
- `scripts/consolidation.py` -- consolidation pipeline core
- `skills/consolidate/SKILL.md` -- manual trigger skill
- `tests/test_consolidation.py` -- consolidation tests

### Modified Files
- `install.py` -- link_scripts + link_skills additions
- `scripts/memory_utils.py` -- consolidation + recall DEFAULT_SETTINGS
- `scripts/storage.py` -- metadata table DDL + _ensure_metadata_table()
- `scripts/synthesis_cron.py` -- consolidation gate + post-step integration
- `tests/test_install.py` -- consolidation/skill install tests
- `tests/test_memory_utils.py` -- settings tests
- `tests/test_storage.py` -- metadata table tests
- `tests/test_synthesis_cron.py` -- consolidation gate tests

## Handoff to Phase D

1. `consolidation.py` is fully functional but requires vector embeddings (fastembed + sqlite-vec) for real clustering. Without them, `_get_similarity_pairs` returns empty list.
2. `metadata` table is available for any key-value storage needs (e.g., benchmark run timestamps).
3. `recall` settings in DEFAULT_SETTINGS are now wired -- Phase B's prompt_recall.py can read them via `load_settings()["recall"]`.
4. The `consolidated=1` flag on merged data_points protects them from decay.
