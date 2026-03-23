# Phase A Completion

**Date:** 2026-03-22
**HEAD SHA:** 8f7139ac949d4d22d55551cd65ab50363a2afb7e
**Test status:** 993 passed, 18 skipped, 0 failed

## Tasks Completed

- **A1** — Define v3 schema DDL and DataPointRow dataclass
- **A2** — CRUD helpers for data_points
- **A3** — v2-to-v3 migration function
- **A4** — Profile section migration
- **A5** — Update embeddings.py for vec_data
- **A6** — Markdown archival utility

## Summary

All 6 Phase A tasks were implemented in the worktree `phase3-workflow-phase-a` on branch `phase-a`.

### A1 (schema DDL + DataPointRow)
Added `SCHEMA_V3_DDL` with `data_points` (18 columns), updated `edges` DDL to reference `data_points` and include `reason TEXT`, added `VEC_DATA_DDL`. `DataPointRow` is a frozen dataclass. `SCHEMA_VERSION` set to 3.

### A2 (CRUD helpers)
Added `insert_data_point`, `query_data_points`, `query_data_points_by_scope`, `update_data_point`, `soft_delete_data_point` (alias `delete_data_point_soft`), `query_edges_for_data_point`. `insert_edge` validated with FK on v3 edges. Full test coverage in `tests/test_storage.py`.

### A3 (migration)
`_migrate_v2_to_v3` copies chunks→data_points (type='memory'), nodes→data_points (type='entity'), recreates edges table with data_points refs + reason column, migrates vec_chunks→vec_data when sqlite-vec available, drops old tables, bumps `user_version` to 3. `_migrate_schema` and `ensure_db` wire the migration automatically on v2 DBs. Tests cover round-trip, idempotency, and integrity failure.

### A4 (profile migration)
`PROFILE_SECTIONS` frozenset with 4 headers. `_migrate_profiles(conn, ltm_path)` parses global LTM markdown and creates data_points with type='profile', scope='user', salience=1.0, consolidated=1. Content hash ensures idempotency. Public `migrate_profiles` wraps the private function. Wired at step 9 of `_migrate_v2_to_v3`. 7 tests added.

### A5 (embeddings.py for vec_data)
`ScoredDataPoint` dataclass with `data_point: DataPointRow` field; `ScoredChunk = ScoredDataPoint` backward-compat alias. `index_data_points()` is the primary function; `index_chunks()` retained as deprecated v2 wrapper. `delete_vec_data()` primary; `delete_vec_chunks()` deprecated alias. `ensure_vec_table()` creates `vec_data`, warns if `vec_chunks` exists without `vec_data`. `search_similar()` queries `vec_data` and returns `ScoredDataPoint`. `reindex_changed_files()` and `reindex_all()` use `data_points` table. `score_memory()` unchanged (duck-typed, accepts both ChunkRow and DataPointRow).

### A6 (markdown archival)
`_should_archive(conn)` returns True if data_points has rows. `_archive_markdown_files(memory_dir)` moves `global-long-term-memory.md`, `daily/*.md`, and `project-memory/*-long-term-memory.md` to `.archive/` with timestamp/prefix naming. Non-markdown files untouched. 7 tests added.

## Deviations

1. **A5 `reindex_changed_files`**: The function queries `data_points WHERE properties LIKE '%"source_file": "..."'` instead of a direct `source_file` column (which doesn't exist in v3 `data_points`). Source file info is stored in the `properties` JSON column during v2→v3 migration. This is a correct approach given the v3 schema design but slightly less efficient than a dedicated column.

2. **A5 `ScoredDataPoint.data_point` field**: The field is named `data_point` (not `chunk`). The `ScoredChunk` alias maps to `ScoredDataPoint`, so callers using `scored.chunk` will get an `AttributeError`. Callers that access only `scored.score` and `scored.vec_similarity` are fully backward-compatible. Callers accessing `scored.chunk` will need to update to `scored.data_point`. This was a deliberate design choice as the done_when specified `ScoredDataPoint(data_point=dp, ...)`.

3. **A6 directory structure**: Implemented flat `.archive/` with naming prefixes (`global-long-term-memory-{ts}.md`, `daily-{name}.md`, `project-{name}.md`) rather than subdirectories. The done_when says "Preserves directory structure inside .archive/" but the prose test is written for flat structure and the prose code example also uses flat. Followed the prose code example and test.

4. **Pre-existing test failures fixed**: 4 pre-existing failures in `test_synthesis.py` were present at start. After A3/A5 work, all 4 became passing (collateral improvement). Net result is 0 failures.
