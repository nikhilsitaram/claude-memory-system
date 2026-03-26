# Design: Remove Legacy v1/v2 Logic and Markdown File Dependencies

**Issue:** #98
**Date:** 2026-03-26
**Branch:** `chore/remove-legacy-v1v2`

## Problem

The codebase retains ~800+ lines of v1/v2-era code that is no longer used in the v3 SQL-first architecture. All v2 CRUD functions (insert_chunk, insert_node, query_chunks_*, query_nodes_*) have zero production callers. Migration code only runs for databases below schema v3 — all active DBs are v3. Markdown decay functions are deprecated with warnings. This dead code adds confusion for contributors, inflates test surface, and creates maintenance burden.

## Goal

Remove all v1/v2 logic so the codebase contains only v3 SQL-first architecture code. No markdown file dependencies, no v2 schema support, no migration paths.

## Success Criteria

1. `grep -rE "daily/\*\.md|long-term-memory\.md|project-memory/" scripts/` returns nothing
2. No `ChunkRow` or `NodeRow` in storage.py or its `__all__` exports
3. No `_run_synthesis_v2` in synthesis_cron.py
4. `python3 -m pytest tests/ -q` passes with zero deprecation warnings for removed functions
5. `DEFAULT_SETTINGS` has no `globalShortTerm` or `projectShortTerm` keys
6. `token_usage.py` is deleted

## Architecture

### 1. Schema Layer (storage.py) — ~400 lines removed

**Remove v2 schema DDL:**
- `SCHEMA_DDL` (creates chunks, nodes, v2-edges tables) — delete entirely
- `VEC_CHUNKS_DDL` (legacy vector table) — delete
- Rename `SCHEMA_V3_DDL` → `SCHEMA_DDL` (becomes the only schema definition)

**Remove v2 dataclasses and CRUD:**
- `ChunkRow`, `NodeRow` dataclasses
- `insert_chunk()`, `insert_node()`
- `query_chunks_by_scope()`, `query_chunks_by_source()`, `delete_chunks_by_source()`
- `query_nodes_by_scope()`, `query_node_by_name_and_type()`, `update_node_access()`
- `query_chunks_with_salience()`, `query_chunk_by_id()`
- Helper internals: `_row_to_chunk()`, `_row_to_node()`, `_CHUNK_COLUMNS`, `_NODE_COLUMNS`

**Remove all migration code:**
- `_migrate_schema()` — migration orchestrator
- `_migrate_salience_data()` — v1→v2 backfill
- `_migrate_v2_to_v3()` — v2→v3 schema migration (~150 lines)
- `_migrate_profiles()`, `PROFILE_SECTIONS` — markdown profile parsing
- `_parse_daily_entries()`, `_parse_ltm_entries()` — markdown content parsers
- `migrate_markdown_to_db()` — public markdown→DB migration entry point
- `_archive_markdown_files()` — post-migration markdown archival

**Simplify `ensure_db()`:**
- Execute only v3 DDL (the renamed `SCHEMA_DDL`)
- Call `_ensure_fts_table()` + `_backfill_fts()` directly (no migration gate)
- Keep `SCHEMA_VERSION = 3` (no schema change)
- Remove the `if current_version < SCHEMA_VERSION: _migrate_schema()` block

**Clean `__all__`:** Remove all v2 symbols.

### 2. Decay Layer (decay.py) — ~200 lines removed

**Remove:**
- Constants: `AUTO_PINNED_SECTIONS`, `DECAY_ELIGIBLE_SECTIONS`
- Section checks: `is_protected_section()`, `is_decay_eligible()`, `parse_sections()`, `parse_learnings()`
- Markdown decay: `decay_file()`, `append_to_archive()`, `purge_old_archives()`
- Deprecated orchestrator: `run()`

**Keep:** `decay_data_points()`, `cleanup_near_zero_salience()`, `main()` (simplified to v3-only path).

### 3. Settings Layer (memory_utils.py) — ~80 lines removed

**Remove:**
- `SHORT_TERM_TOKENS_PER_DAY` constant
- `globalShortTerm`, `projectShortTerm` sections from `DEFAULT_SETTINGS`
- `_calculate_token_limits()` function
- `get_working_days()` (scans `daily/*.md` — markdown-based)
- `_clear_working_days_cache()` and `_working_days_cache` if only used by `get_working_days()`

**Keep:**
- `get_global_working_days()`, `get_project_working_days()` (scan `.jsonl` files — transcript-based, v3)
- `totalTokenBudget` — simplify to `globalLongTerm.tokenLimit + projectLongTerm.tokenLimit`

### 4. Synthesis Layer (synthesis_cron.py) — ~42 lines removed

- Remove `_run_synthesis_v2()` function
- Remove v2 dispatch branch in `run_synthesis()` (schema version check)

### 5. Embeddings Layer (embeddings.py) — ~20 lines removed

- Remove `ScoredChunk = ScoredDataPoint` backward-compat alias
- Remove `vec_chunks` table existence warnings in `_init_vec()` and `search_similar()`

### 6. Load Memory (load_memory.py) — inline synthesis removal

- Remove `_build_embedded_files()` — reads markdown for synthesis prompt building
- Remove `write_synthesis_prompt()` — generates synthesis prompts for inline path
- Remove inline synthesis code path in `main()` (the `if pending_dates and should_synthesize and not synthesis_deferred` block)
- Keep: DB-first loading (the core v3 path), deferred synthesis triggering

### 7. Peripheral Scripts

**install.py:**
- Remove markdown template copying from `copy_templates()`
- Remove `migrate_markdown_to_db`, `_migrate_v2_to_v3` imports
- Remove migration calls from `create_database()`

**token_usage.py:**
- Delete entirely. Was an approximation of token usage across memory tiers using short-term settings that no longer exist.

**project_manager.py:**
- Remove `*-long-term-memory.md` references from `list_projects()`, `plan_move()`, `plan_merge_orphan()`, `restore_from_backup()`, `get_memory_files_for_merge()`

**devtools.py:**
- Remove markdown diagnostic tools (dedup LTM entries, mark-routed migration utility)

### 8. Templates

Delete from `templates/`:
- `daily-template.md`
- `global-long-term-memory.md`
- `project-long-term-memory.md`

### 9. Tests — ~300+ lines removed

**Remove:**
- `ChunkRow`/`NodeRow` CRUD tests in test_storage.py
- `db_v2` fixture in test_storage.py
- Deprecated decay function tests in test_decay.py (decay_file, append_to_archive, purge_old_archives, is_protected_section, is_decay_eligible, AUTO_PINNED_SECTIONS)
- `_calculate_token_limits` and `get_working_days` tests in test_memory_utils.py
- `migrate_markdown_to_db` and `_archive_markdown_files` tests in test_migration.py
- `ChunkRow` imports in test_embeddings.py, test_health.py

**Keep:** All v3 tests (data_points CRUD, decay_data_points, v3 synthesis, embeddings with vec_data).

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| SCHEMA_VERSION stays at 3 | No schema change; just removing code that creates/migrates v2 tables |
| v2 DBs become unsupported | User confirmed migration code can be removed; old DBs need fresh start |
| edges table: v3 definition only | References data_points, not nodes |
| FTS setup moves into ensure_db() | Was gated behind migration check; still needed for fresh DBs |
| Inline synthesis removed | v3 always uses deferred synthesis via synthesis_cron.py |
| token_usage.py deleted | Was an approximation; not worth refactoring for v3 |

## Non-Goals

- Refactoring v3 code or improving existing v3 behavior
- Adding new features
- Cleaning up devtools.py beyond markdown references
- Changing the database schema

## Implementation Approach

**Single phase.** The changes are tightly coupled (removing v2 types from storage.py cascades to all consumers) but highly parallelizable per-file. Each script can be cleaned independently once the storage.py interface is defined. Test cleanup follows script cleanup.

Task parallelism: storage.py must be done first (defines what symbols disappear), then all other scripts + tests can be cleaned in parallel.
