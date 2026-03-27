#!/usr/bin/env python3
"""Integration tests for context injection cleanup (Issue #103).

Validates end-to-end that profile waste, near-duplicate memories,
and stale project memories are cleaned up and excluded from
SessionStart injection.
"""

from datetime import datetime, timezone
from unittest import mock

from storage import (
    DataPointRow,
    cleanup_stale_data,
    ensure_db,
    insert_data_point,
    query_data_point_by_id,
)


def _make_db(tmp_path):
    """Create a v3 DB with patched paths."""
    db_path = tmp_path / "memory.db"
    with mock.patch("storage.get_db_path", return_value=db_path), \
         mock.patch("memory_utils.get_memory_dir", return_value=tmp_path):
        conn = ensure_db()
    return conn


class TestProfileWasteCleanup:
    """Verify profile items with HTML comments or bare tags are removed."""

    def test_cleanup_removes_waste_keeps_real_content(self, tmp_path):
        conn = _make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        waste_ids = []
        for content in [
            "<!-- Add your preferred programming languages here -->",
            "<!-- Communication style notes -->",
            "Python-3.13",
            "claude-code",
            "macOS ARM",
        ]:
            dp_id = insert_data_point(conn, DataPointRow(
                type="profile", content=content, scope="user",
                salience=1.0, created_at=now,
            ))
            waste_ids.append(dp_id)

        real_id = insert_data_point(conn, DataPointRow(
            type="profile",
            content="Prefers concise, direct communication without emojis",
            scope="user", salience=1.0, created_at=now,
        ))
        conn.commit()

        stats = cleanup_stale_data(conn)

        for dp_id in waste_ids:
            assert query_data_point_by_id(conn, dp_id) is None, \
                f"Waste entry {dp_id} should be deleted"

        real = query_data_point_by_id(conn, real_id)
        assert real is not None
        assert real.salience > 0

        assert stats["profiles_deleted"] == 5
        conn.close()


class TestNearDuplicateCleanup:
    """Verify near-duplicate clusters are reduced to 1 survivor."""

    def test_keeps_highest_evidence_count(self, tmp_path):
        conn = _make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        base = ("Always configure the git init command to set the default branch "
                "name to main when creating new repositories on any machine or "
                "platform that you work on regularly and consistently across "
                "all environments")
        variants = [base, base + " today", base + " now", base + ".", base + " yes"]
        ids = []
        evidence_counts = [3, 1, 1, 2, 1]
        for content, ev in zip(variants, evidence_counts):
            dp_id = insert_data_point(conn, DataPointRow(
                type="memory", content=content, scope="global",
                salience=0.7, evidence_count=ev, created_at=now,
            ))
            ids.append(dp_id)
        conn.commit()

        stats = cleanup_stale_data(conn)

        survivors = []
        for dp_id in ids:
            dp = query_data_point_by_id(conn, dp_id)
            if dp and dp.salience > 0:
                survivors.append(dp_id)

        assert len(survivors) == 1
        assert survivors[0] == ids[0]  # evidence_count=3
        assert stats["duplicates_soft_deleted"] == 4
        conn.close()


class TestStaleProjectMemoryCleanup:
    """Verify stale project memories about completed work are soft-deleted."""

    def test_stale_patterns_soft_deleted(self, tmp_path):
        conn = _make_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        stale_contents = [
            "Resume backfill from session abc123",
            "Phase A: vector search integration is complete (PR #66 merged)",
            "Phase A: SimHash near-duplicate detection implemented",
        ]
        stale_ids = []
        for content in stale_contents:
            dp_id = insert_data_point(conn, DataPointRow(
                type="memory", content=content, scope="claude-memory-system",
                salience=0.7, created_at=now,
            ))
            stale_ids.append(dp_id)

        good_id = insert_data_point(conn, DataPointRow(
            type="memory",
            content="SQL-first memory system uses data_points table",
            scope="claude-memory-system", salience=0.7, created_at=now,
        ))
        conn.commit()

        stats = cleanup_stale_data(conn)

        for dp_id in stale_ids:
            dp = query_data_point_by_id(conn, dp_id)
            assert dp.salience == 0.0, f"Stale entry {dp_id} should be soft-deleted"

        good = query_data_point_by_id(conn, good_id)
        assert good.salience > 0

        assert stats["stale_soft_deleted"] >= 3
        conn.close()
