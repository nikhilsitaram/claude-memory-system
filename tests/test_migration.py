#!/usr/bin/env python3
"""Tests for markdown-to-DB migration pipeline in storage.py."""

import sqlite3
from unittest import mock

import pytest
from storage import (
    SCHEMA_DDL,
    _parse_daily_entries,
    _parse_ltm_entries,
    close_db,
    migrate_markdown_to_db,
    query_chunks_by_source,
)


def _make_v2_db(db_path):
    """Create a v2 DB for testing migration operations."""
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA_DDL)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    return conn


@pytest.fixture
def db_dir(tmp_path):
    db_path = tmp_path / 'memory.db'
    with mock.patch('storage.get_db_path', return_value=db_path):
        yield tmp_path


@pytest.fixture
def db(db_dir):
    db_path = db_dir / 'memory.db'
    conn = _make_v2_db(db_path)
    yield conn
    close_db(conn)


@pytest.fixture
def memory_dir(tmp_path):
    mem = tmp_path / 'memory'
    mem.mkdir()
    (mem / 'daily').mkdir()
    (mem / 'project-memory').mkdir()
    (mem / 'global-long-term-memory.md').write_text(
        '# Long-Term Memory\n\n'
        '## Key Learnings\n'
        '- (2026-03-01) [pattern] Use pytest tmp_path for isolation\n'
        '- (2026-03-02) [gotcha] SQLite WAL needs busy_timeout\n\n'
        '## Pinned\n'
        '- Important pinned item (no date)\n',
        encoding='utf-8',
    )
    (mem / 'project-memory' / 'my-project-long-term-memory.md').write_text(
        '# My Project\n\n'
        '## Key Actions\n'
        '- (2026-03-01) [implement] Built REST API\n',
        encoding='utf-8',
    )
    (mem / 'daily' / '2026-03-01.md').write_text(
        '# 2026-03-01\n\n'
        '## Actions\n'
        '- [my-project/implement] Built REST API endpoints\n'
        '- [global/document] Updated README\n\n'
        '## Learnings\n'
        '- [routed][global/pattern] Already routed entry\n'
        '- [global/gotcha] Watch out for WAL locks\n',
        encoding='utf-8',
    )
    return mem


class TestParseLtmEntries:
    def test_parses_dated_entries(self):
        content = (
            '## Key Learnings\n'
            '- (2026-03-01) [pattern] Use tmp_path\n'
            '- (2026-03-02) [gotcha] WAL needs timeout\n'
        )
        chunks = _parse_ltm_entries(content, 'global-long-term-memory.md', 'global')
        assert len(chunks) == 2
        assert chunks[0].entry_type == 'pattern'
        assert chunks[0].created_at == '2026-03-01'
        assert chunks[0].section == '## Key Learnings'
        assert chunks[0].source_type == 'ltm'

    def test_skips_non_entry_lines(self):
        content = '# Title\nSome descriptive text\n<!-- comment -->\n'
        chunks = _parse_ltm_entries(content, 'test.md', 'global')
        assert len(chunks) == 0

    def test_skips_pinned_undated_entries(self):
        content = (
            '## Pinned\n'
            '- Important pinned item (no date)\n'
            '- Another pinned item\n\n'
            '## Key Learnings\n'
            '- (2026-03-01) [pattern] This one should be parsed\n'
        )
        chunks = _parse_ltm_entries(content, 'test.md', 'global')
        assert len(chunks) == 1
        assert chunks[0].entry_type == 'pattern'

    def test_tracks_section_headers(self):
        content = (
            '## Key Actions\n'
            '- (2026-03-01) [implement] Built API\n'
            '## Key Learnings\n'
            '- (2026-03-02) [pattern] Use fixtures\n'
        )
        chunks = _parse_ltm_entries(content, 'test.md', 'global')
        assert chunks[0].section == '## Key Actions'
        assert chunks[1].section == '## Key Learnings'


class TestParseDailyEntries:
    def test_parses_tagged_entries(self):
        content = (
            '## Actions\n'
            '- [my-project/implement] Built API\n'
            '- [global/document] Updated docs\n'
        )
        chunks = _parse_daily_entries(content, '2026-03-01.md')
        assert len(chunks) == 2
        assert chunks[0].scope == 'my-project'
        assert chunks[0].entry_type == 'implement'
        assert chunks[1].scope == 'global'

    def test_skips_routed_entries(self):
        content = (
            '## Actions\n'
            '- [routed][global/implement] Already routed\n'
            '- [global/implement] Not routed\n'
        )
        chunks = _parse_daily_entries(content, '2026-03-01.md')
        assert len(chunks) == 1
        assert 'Not routed' in chunks[0].content

    def test_extracts_date_from_filename(self):
        chunks = _parse_daily_entries(
            '## Actions\n- [global/implement] Test\n', '2026-03-15.md',
        )
        assert chunks[0].created_at == '2026-03-15'

    def test_handles_multi_scope(self):
        content = '## Actions\n- [proj1|proj2/implement] Shared work\n'
        chunks = _parse_daily_entries(content, '2026-03-01.md')
        assert chunks[0].scope == 'proj1'


class TestMigrateMarkdownToDb:
    def test_full_migration(self, db, memory_dir):
        with mock.patch('storage.get_memory_dir', return_value=memory_dir):
            stats = migrate_markdown_to_db(db)
        assert stats.ltm_files_processed == 2
        assert stats.daily_files_processed == 1
        assert stats.chunks_inserted > 0

    def test_idempotent(self, db, memory_dir):
        with mock.patch('storage.get_memory_dir', return_value=memory_dir):
            stats1 = migrate_markdown_to_db(db)
            stats2 = migrate_markdown_to_db(db)
        assert stats2.chunks_inserted == 0
        assert stats2.chunks_skipped == stats1.chunks_inserted

    def test_global_scope(self, db, memory_dir):
        with mock.patch('storage.get_memory_dir', return_value=memory_dir):
            migrate_markdown_to_db(db)
        global_chunks = query_chunks_by_source(db, 'global-long-term-memory.md')
        assert len(global_chunks) == 2
        assert all(c.scope == 'global' for c in global_chunks)

    def test_project_scope(self, db, memory_dir):
        with mock.patch('storage.get_memory_dir', return_value=memory_dir):
            migrate_markdown_to_db(db)
        project_chunks = query_chunks_by_source(db, 'my-project-long-term-memory.md')
        assert len(project_chunks) == 1
        assert project_chunks[0].scope == 'my-project'

    def test_empty_memory_dir(self, db, tmp_path):
        empty_mem = tmp_path / 'empty_memory'
        empty_mem.mkdir()
        with mock.patch('storage.get_memory_dir', return_value=empty_mem):
            stats = migrate_markdown_to_db(db)
        assert stats.ltm_files_processed == 0
        assert stats.daily_files_processed == 0
        assert stats.chunks_inserted == 0
