#!/usr/bin/env python3
"""Tests for scripts/health.py."""

import sqlite3
from unittest import mock

import pytest
from health import (
    HealthReport,
    format_report,
    health_alerts,
    health_report,
)
from storage import (
    SCHEMA_DDL,
    ChunkRow,
    NodeRow,
    close_db,
    insert_chunk,
    insert_node,
)


def _make_v2_db(db_path):
    """Create a v2 DB for testing health operations."""
    conn = sqlite3.connect(str(db_path))
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript(SCHEMA_DDL)
    conn.execute("PRAGMA user_version=2")
    conn.commit()
    return conn


@pytest.fixture
def db_dir(tmp_path):
    db_path = tmp_path / "memory.db"
    with mock.patch("storage.get_db_path", return_value=db_path), \
         mock.patch("health.get_db_path", return_value=db_path):
        yield tmp_path


@pytest.fixture
def db(db_dir):
    db_path = db_dir / "memory.db"
    conn = _make_v2_db(db_path)
    yield conn
    close_db(conn)


class TestHealthReport:
    def test_empty_db(self, db):
        report = health_report(db)
        assert report.total_chunks == 0
        assert report.graph_nodes == 0
        assert report.avg_salience == 0.0

    def test_schema_version(self, db):
        report = health_report(db)
        # v2 DB for testing chunks/nodes tables
        assert report.schema_version == 2

    def test_chunk_counts(self, db):
        for i in range(5):
            insert_chunk(db, ChunkRow(
                content=f"LTM entry {i}",
                source_file="global-long-term-memory.md",
                source_type="ltm",
                scope="global",
                chunk_index=i,
                created_at="2026-03-01",
            ))
        for i in range(3):
            insert_chunk(db, ChunkRow(
                content=f"Daily entry {i}",
                source_file="2026-03-01.md",
                source_type="daily",
                scope="global",
                chunk_index=i,
                created_at="2026-03-01",
            ))
        db.commit()
        report = health_report(db)
        assert report.total_chunks == 8
        assert report.ltm_chunks == 5
        assert report.daily_chunks == 3

    def test_salience_distribution(self, db):
        # Hot
        insert_chunk(db, ChunkRow(
            content="Hot entry", source_file="t.md", source_type="ltm",
            scope="global", chunk_index=0, created_at="2026-03-01",
            salience=0.9,
        ))
        # Warm
        insert_chunk(db, ChunkRow(
            content="Warm entry", source_file="t.md", source_type="ltm",
            scope="global", chunk_index=1, created_at="2026-03-01",
            salience=0.5,
        ))
        # Cold
        insert_chunk(db, ChunkRow(
            content="Cold entry", source_file="t.md", source_type="ltm",
            scope="global", chunk_index=2, created_at="2026-03-01",
            salience=0.05,
        ))
        db.commit()
        report = health_report(db)
        assert report.hot_chunks == 1
        assert report.warm_chunks == 1
        assert report.cold_chunks == 1

    def test_node_count(self, db):
        insert_node(db, NodeRow(
            name="pytest", type="tool", scope="global",
            created_at="2026-03-01",
        ))
        db.commit()
        report = health_report(db)
        assert report.graph_nodes == 1


class TestHealthAlerts:
    def test_empty_db_alert(self):
        report = HealthReport(total_chunks=0)
        alerts = health_alerts(report)
        assert len(alerts) == 1
        assert "empty" in alerts[0].lower()

    def test_cold_ratio_alert(self):
        # 90% cold -- should trigger
        report = HealthReport(
            total_chunks=100,
            cold_chunks=90,
            warm_chunks=8,
            hot_chunks=2,
        )
        alerts = health_alerts(report)
        assert any("cold" in a.lower() for a in alerts)

    def test_no_alert_when_healthy(self):
        report = HealthReport(
            total_chunks=100,
            cold_chunks=10,
            warm_chunks=60,
            hot_chunks=30,
            avg_salience=0.6,
        )
        alerts = health_alerts(report)
        assert len(alerts) == 0


class TestFormatReport:
    def test_contains_key_fields(self):
        report = HealthReport(
            total_chunks=42, avg_salience=0.65,
            hot_chunks=10, warm_chunks=25, cold_chunks=7,
            graph_nodes=5, active_edges=8,
        )
        text = format_report(report, [])
        assert "42" in text
        assert "0.650" in text
        assert "Health Report" in text

    def test_includes_alerts(self):
        report = HealthReport()
        text = format_report(report, ["Test alert message"])
        assert "Test alert message" in text
        assert "Alerts:" in text


# =============================================================================
# A10: Extended Health Metrics Tests
# =============================================================================


class TestExtendedHealthReport:
    """Tests for new health metrics fields."""

    def _make_v3_db(self, tmp_path):
        from unittest.mock import patch
        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path), \
             patch("storage.get_memory_dir", return_value=tmp_path), \
             patch("health.get_db_path", return_value=db_path):
            conn = ensure_db()
        return conn

    def test_memories_by_scope(self, tmp_path):
        from storage import insert_data_point, DataPointRow
        conn = self._make_v3_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="g1", scope="global", salience=0.5))
        insert_data_point(conn, DataPointRow(type="memory", content="p1", scope="my-project", salience=0.5))
        conn.commit()
        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)
        assert report.memories_by_scope.get("global", 0) >= 1
        assert report.memories_by_scope.get("my-project", 0) >= 1
        conn.close()

    def test_memories_by_type(self, tmp_path):
        from storage import insert_data_point, DataPointRow
        conn = self._make_v3_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="m", scope="global", salience=0.5))
        insert_data_point(conn, DataPointRow(type="entity", name="Redis", content="Redis", scope="global", salience=0.5))
        conn.commit()
        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)
        assert report.memories_by_type.get("memory", 0) >= 1
        assert report.memories_by_type.get("entity", 0) >= 1
        conn.close()

    def test_never_accessed_pct(self, tmp_path):
        from storage import insert_data_point, DataPointRow
        conn = self._make_v3_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="never", scope="global", salience=0.5, access_count=0))
        insert_data_point(conn, DataPointRow(type="memory", content="once", scope="global", salience=0.5, access_count=1))
        conn.commit()
        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)
        assert 0.4 <= report.never_accessed_pct <= 0.6
        conn.close()

    def test_edges_per_entity(self, tmp_path):
        from datetime import datetime, timezone
        from storage import insert_data_point, insert_edge, DataPointRow, EdgeRow
        conn = self._make_v3_db(tmp_path)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        e_id = insert_data_point(conn, DataPointRow(type="entity", name="X", content="X", scope="global", salience=0.5))
        m_id = insert_data_point(conn, DataPointRow(type="memory", content="m", scope="global", salience=0.5))
        insert_edge(conn, EdgeRow(source=m_id, target=e_id, type="mentions", created_at=now))
        conn.commit()
        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)
        assert report.edges_per_entity >= 1.0
        conn.close()

    def test_cold_ratio_alert(self):
        report = HealthReport(total_chunks=10, cold_chunks=9, hot_chunks=0, warm_chunks=1)
        alerts = health_alerts(report)
        assert any("cold" in a.lower() for a in alerts)

    def test_empty_db_alert(self):
        report = HealthReport(total_chunks=0)
        alerts = health_alerts(report)
        assert any("empty" in a.lower() for a in alerts)

    def test_synthesis_error_alert(self):
        report = HealthReport(total_chunks=10, cold_chunks=0, hot_chunks=10, synthesis_errors_7d=5)
        alerts = health_alerts(report)
        assert any("synthesis errors" in a.lower() for a in alerts)
