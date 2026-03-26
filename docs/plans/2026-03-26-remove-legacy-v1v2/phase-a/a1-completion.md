# A1: Strip v2 schema, CRUD, and migration from storage.py

## Status: Complete

## Changes

### scripts/storage.py
- Removed `SCHEMA_DDL` (v2 with nodes/chunks/edges tables) and renamed `SCHEMA_V3_DDL` to `SCHEMA_DDL`
- Moved vec_data virtual table creation out of SCHEMA_DDL into separate try/except in `ensure_db()` (vec0 module is optional)
- Removed `VEC_CHUNKS_DDL` constant
- Removed dataclasses: `ChunkRow`, `NodeRow`, `MigrationStats`
- Removed v2 CRUD functions: `insert_chunk`, `_CHUNK_COLUMNS`, `_row_to_chunk`, `query_chunks_by_scope`, `query_chunks_by_source`, `delete_chunks_by_source`, `_NODE_COLUMNS`, `_row_to_node`, `insert_node`, `query_nodes_by_scope`, `query_node_by_name_and_type`, `update_node_access`, `query_neighbor_nodes`, `batch_update_access`, `update_chunk_content`, `update_chunk_salience`, `update_node_salience`, `query_chunks_with_salience`, `query_chunks_for_retrieval`, `query_chunk_by_id`, `query_edges_for_node`
- Removed `NeighborInfo` dataclass
- Removed migration functions: `_migrate_salience_data`, `_migrate_v2_to_v3`, `_migrate_schema`
- Removed markdown migration functions: `PROFILE_SECTIONS`, `_insert_profile_section`, `_migrate_profiles`, `migrate_profiles`, `_parse_ltm_entries`, `_parse_daily_entries`, `migrate_markdown_to_db`, `_LTM_ENTRY_RE`
- Removed archival functions: `_should_archive`, `_archive_markdown_files`
- Simplified `ensure_db()`: removed `_get_schema_version` call and migration gate, added `_backfill_fts(conn)` after `_ensure_fts_table(conn)` in try block
- Cleaned `__all__` to remove all v2/migration symbols

### tests/test_storage.py
- Removed `db_v2` fixture
- Removed v2 test classes: `TestChunkCRUD`, `TestNodeCRUD`, `TestEdgeCRUD`, `TestQueryNeighborNodes`, `TestBatchUpdateAccess`, `TestUpdateNodeSalience`, `TestQueryChunksWithSalience`, `TestSalienceDataMigration`, `TestMigrateV2ToV3`, `TestUpdateChunkContent`, `TestUpdateChunkSalienceClamping`, `TestQueryChunksForRetrieval`, `TestQueryChunkById`, `TestMigrateProfiles`, `TestArchiveMarkdown`, `TestInsertProfileSectionPlaceholders`
- Rewrote `TestInvalidateEdge` and `TestTemporalEdgeQueries` to use v3 data_points instead of v2 nodes
- Updated `TestSchemaCreation.test_indexes_exist` to not expect `idx_edges_valid`
- Updated `TestV3Schema` to reference `SCHEMA_DDL` instead of `SCHEMA_V3_DDL`
- Removed `_make_two_nodes` helper, replaced with `_make_two_data_points`
- Removed unused imports (`hashlib`, `json`, v2 symbols)

## Test Results
- `tests/test_storage.py`: 76 passed, 1 skipped (sqlite-vec)
- Full suite excluding files with known downstream breakage: 793 passed, 2 skipped, 1 expected failure in test_install.py (imports `_migrate_v2_to_v3` from storage)

## Known Downstream Breakage (out of scope for A1)
- `scripts/install.py` imports `_migrate_v2_to_v3`, `migrate_markdown_to_db`
- `tests/test_embeddings.py` imports `ChunkRow`
- `tests/test_health.py` imports `ChunkRow`
- `tests/test_migration.py` imports `_parse_daily_entries`
- `scripts/embeddings.py` references `ChunkRow` in a docstring

## Lines Removed
- storage.py: ~1913 lines -> ~870 lines (reduced by ~1043 lines)
- test_storage.py: ~2812 lines -> ~888 lines (reduced by ~1924 lines)
