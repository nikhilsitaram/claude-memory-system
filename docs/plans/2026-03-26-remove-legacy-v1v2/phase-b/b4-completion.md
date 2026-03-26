# B4: Remove ChunkRow imports from test_health.py and update CLAUDE.md

## Status: COMPLETE

## Changes

### tests/test_health.py
- Replaced imports: `ChunkRow`, `NodeRow`, `insert_chunk`, `insert_node`, `SCHEMA_DDL` -> `DataPointRow`, `insert_data_point`, `ensure_db`
- Converted `_make_v2_db()` to `_make_db()` using `ensure_db()` (v3 schema)
- Converted all `ChunkRow(...)` test data to `DataPointRow(type="memory", ...)`
- Converted `NodeRow(...)` to `DataPointRow(type="entity", ...)`
- Replaced all `insert_chunk(db, ...)` calls with `insert_data_point(db, ...)`
- Updated schema version assertion from `== 2` to `>= 3`
- Removed redundant `_make_v3_db` method from `TestExtendedHealthReport` class (replaced with shared `_make_db` function)
- Removed redundant inline `from storage import DataPointRow, insert_data_point` in extended tests (now uses top-level import)
- Dropped fields not in DataPointRow: `source_file`, `chunk_index`, `section`

### CLAUDE.md
- Removed `globalShortTerm.workingDays | 2` from Settings Defaults table
- Removed `projectShortTerm.workingDays | 5` from Settings Defaults table
- Removed `decay.archiveRetentionDays | 365` from Settings Defaults table
- Removed short-term token limits formula line
- Updated storage.py description: removed "migration" from comment
- Removed "Archives markdown LTM entries for backward compatibility." from Decay pipeline description

### health.py
- No changes needed: already schema-aware (queries both chunks and data_points tables)

## Verification
- All 22 tests pass: `python3 -m pytest tests/test_health.py -q`
- Zero references to ChunkRow/insert_chunk/NodeRow/insert_node/SCHEMA_DDL remain in test_health.py
