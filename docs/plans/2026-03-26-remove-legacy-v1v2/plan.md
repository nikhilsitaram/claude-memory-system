---
status: In Development
---

# Remove all v1/v2 legacy code so the codebase contains only v3 SQL-first architecture code. Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use orchestrate

**Goal:** Remove all v1/v2 legacy code so the codebase contains only v3 SQL-first architecture code.
**Architecture:** Phase A strips v2 schema, CRUD, migration, and markdown logic from core scripts (storage.py, decay.py, memory_utils.py, synthesis_cron.py, embeddings.py, load_memory.py). Phase B cleans peripheral scripts (install.py, devtools.py, project_manager.py), deletes token_usage.py and markdown templates, and removes all v2 test code.
**Tech Stack:** Python 3.9+, SQLite, pytest

---

## Phase A — Core script cleanup
**Status:** Not Started | **Rationale:** storage.py defines the symbols all other scripts import. It must be cleaned first so downstream consumers compile. The remaining core scripts (decay.py, memory_utils.py, synthesis_cron.py, embeddings.py, load_memory.py) can then be cleaned in parallel since they do not import from each other's removed symbols.

- [ ] A1: Strip v2 schema, CRUD, and migration from storage.py — *storage.py has no ChunkRow, NodeRow, SCHEMA_DDL (v2), VEC_CHUNKS_DDL, insert_chunk, insert_node, query_chunks_*, query_nodes_*, _migrate_*, migrate_markdown_to_db, _archive_markdown_files, PROFILE_SECTIONS, MigrationStats. SCHEMA_V3_DDL is renamed to SCHEMA_DDL. ensure_db() only executes v3 DDL. __all__ has no v2 symbols. All tests pass.*
- [ ] A2: Remove markdown decay functions from decay.py — *decay.py has no AUTO_PINNED_SECTIONS, DECAY_ELIGIBLE_SECTIONS, is_protected_section, is_decay_eligible, parse_sections, parse_learnings, should_decay_entry, build_project_work_days_map, parse_learning_date, decay_file, append_to_archive, purge_old_archives, or run(). main() calls decay_data_points and cleanup_near_zero_salience directly. All tests pass.*
- [ ] A3: Remove short-term settings and markdown helpers from memory_utils.py — *memory_utils.py has no SHORT_TERM_TOKENS_PER_DAY, globalShortTerm, projectShortTerm, or archiveRetentionDays in DEFAULT_SETTINGS. _calculate_token_limits() is removed. get_working_days() is removed. totalTokenBudget is calculated as globalLongTerm.tokenLimit + projectLongTerm.tokenLimit. __all__ has no removed symbols. All tests pass.*
- [ ] A4: Remove v2 synthesis dispatch from synthesis_cron.py — *synthesis_cron.py has no _run_synthesis_v2 function, no _get_schema_version helper, no version check in run_synthesis(). The v3 path (_run_synthesis_v3) is the only synthesis path. All tests pass.*
- [ ] A5: Remove backward-compat alias and vec_chunks warnings from embeddings.py — *embeddings.py has no ScoredChunk alias, no vec_chunks fallback warnings in ensure_vec_table() or search_similar(). __all__ has no ScoredChunk. All tests pass.*
- [ ] A6: Refactor load_memory.py synthesis prompt builder and remove inline synthesis — *load_memory.py has no _build_synthesis_instructions (v2), _get_project_names_str, _strip_profile_sections. _build_embedded_files reads only transcripts (no global_ltm, project_ltms, existing_dailies). _build_preextracted_prompt has no LTM markdown fallback or existing_dailies merge. _build_synthesis_prompt calls _build_synthesis_instructions_v3(). Inline synthesis block in main() (lines 955-1027) is removed. All tests pass.*

## Phase B — Peripheral cleanup, template deletion, and test sweep
**Status:** Not Started | **Rationale:** Peripheral scripts (install.py, devtools.py, project_manager.py) import from storage.py and memory_utils.py. They must be cleaned after Phase A removes those symbols. Template deletion and the test_migration/test_token_usage removal are independent but grouped here to keep Phase A focused on core logic.

- [ ] B1: Clean install.py: remove markdown template copying and migration calls — *install.py copy_templates() no longer copies daily-template.md, global-long-term-memory.md, or project-long-term-memory.md. create_database() uses ensure_db() instead of v2 DDL + migrate_markdown_to_db + _migrate_v2_to_v3. No imports of migrate_markdown_to_db or _migrate_v2_to_v3. All tests pass.*
- [ ] B2: Remove LTM-operating commands from devtools.py and orphaned LTM helpers from memory_utils.py — *devtools.py has no cmd_mark_routed, cmd_validate_ltm functions. The main() parser no longer registers mark-routed or validate-ltm subcommands. No imports of collect_ltm_files. memory_utils.py has no collect_ltm_files, LTM_ENTRY_PATTERN, or is_routed_match. All tests pass.*
- [ ] B3: Delete token_usage.py, markdown templates, test_migration.py, and test_token_usage.py — *scripts/token_usage.py, templates/daily-template.md, templates/global-long-term-memory.md, templates/project-long-term-memory.md, tests/test_migration.py, and tests/test_token_usage.py are all deleted. Full test suite passes.*
- [ ] B4: Remove ChunkRow imports from test_health.py and update CLAUDE.md — *test_health.py no longer imports ChunkRow from storage; uses insert_data_point/DataPointRow instead. CLAUDE.md settings table no longer lists globalShortTerm, projectShortTerm, or decay.archiveRetentionDays. Short-term token limits note is removed. All tests pass.*
