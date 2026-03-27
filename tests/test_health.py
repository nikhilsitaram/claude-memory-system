#!/usr/bin/env python3
"""Tests for scripts/health.py."""

from unittest import mock

import pytest
from health import (
    HealthReport,
    format_report,
    health_alerts,
    health_report,
)
from storage import (
    DataPointRow,
    close_db,
    ensure_db,
    insert_data_point,
)


def _make_db(tmp_path):
    """Create a v3 DB for testing health operations."""
    db_path = tmp_path / "memory.db"
    with mock.patch("storage.get_db_path", return_value=db_path), \
         mock.patch("storage.get_memory_dir", return_value=tmp_path):
        conn = ensure_db()
    return conn


@pytest.fixture
def db_dir(tmp_path):
    db_path = tmp_path / "memory.db"
    with mock.patch("storage.get_db_path", return_value=db_path), \
         mock.patch("health.get_db_path", return_value=db_path):
        yield tmp_path


@pytest.fixture
def db(db_dir):
    conn = _make_db(db_dir)
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
        # v3 DB using data_points table
        assert report.schema_version >= 3

    def test_chunk_counts(self, db):
        for i in range(5):
            insert_data_point(db, DataPointRow(
                type="memory",
                content=f"LTM entry {i}",
                source_type="ltm",
                scope="global",
            ))
        for i in range(3):
            insert_data_point(db, DataPointRow(
                type="memory",
                content=f"Daily entry {i}",
                source_type="daily",
                scope="global",
            ))
        db.commit()
        report = health_report(db)
        assert report.total_chunks == 8
        assert report.ltm_chunks == 5
        assert report.daily_chunks == 3

    def test_salience_distribution(self, db):
        # Hot
        insert_data_point(db, DataPointRow(
            type="memory", content="Hot entry", source_type="ltm",
            scope="global", salience=0.9,
        ))
        # Warm
        insert_data_point(db, DataPointRow(
            type="memory", content="Warm entry", source_type="ltm",
            scope="global", salience=0.5,
        ))
        # Cold
        insert_data_point(db, DataPointRow(
            type="memory", content="Cold entry", source_type="ltm",
            scope="global", salience=0.05,
        ))
        db.commit()
        report = health_report(db)
        assert report.hot_chunks == 1
        assert report.warm_chunks == 1
        assert report.cold_chunks == 1

    def test_node_count(self, db):
        insert_data_point(db, DataPointRow(
            type="entity", name="pytest", content="pytest",
            scope="global",
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

    def test_memories_by_scope(self, tmp_path):
        conn = _make_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="g1", scope="global", salience=0.5))
        insert_data_point(conn, DataPointRow(type="memory", content="p1", scope="my-project", salience=0.5))
        conn.commit()
        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)
        assert report.memories_by_scope.get("global", 0) >= 1
        assert report.memories_by_scope.get("my-project", 0) >= 1
        conn.close()

    def test_memories_by_type(self, tmp_path):
        conn = _make_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="m", scope="global", salience=0.5))
        insert_data_point(conn, DataPointRow(type="entity", name="Redis", content="Redis", scope="global", salience=0.5))
        conn.commit()
        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)
        assert report.memories_by_type.get("memory", 0) >= 1
        assert report.memories_by_type.get("entity", 0) >= 1
        conn.close()

    def test_never_accessed_pct(self, tmp_path):
        conn = _make_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="never", scope="global", salience=0.5, access_count=0))
        insert_data_point(conn, DataPointRow(type="memory", content="once", scope="global", salience=0.5, access_count=1))
        conn.commit()
        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)
        assert 0.4 <= report.never_accessed_pct <= 0.6
        conn.close()

    def test_edges_per_entity(self, tmp_path):
        from datetime import datetime, timezone

        from storage import EdgeRow, insert_edge
        conn = _make_db(tmp_path)
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

    def test_last_consolidation_populated(self, tmp_path):
        """last_consolidation is read from metadata table."""
        conn = _make_db(tmp_path)
        insert_data_point(conn, DataPointRow(type="memory", content="m", scope="global", salience=0.5))
        conn.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO metadata (key, value) VALUES ('last_consolidation', '2026-03-20T10:00:00Z')")
        conn.commit()
        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)
        assert report.last_consolidation == "2026-03-20T10:00:00Z"
        conn.close()

    def test_newest_edge_days_populated(self, tmp_path):
        """newest_edge_days is calculated from the most recent active edge."""
        from datetime import datetime, timedelta, timezone

        from storage import EdgeRow, insert_edge
        conn = _make_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat().replace("+00:00", "Z")
        e_id = insert_data_point(conn, DataPointRow(type="entity", name="X", content="X", scope="global", salience=0.5))
        m_id = insert_data_point(conn, DataPointRow(type="memory", content="m", scope="global", salience=0.5))
        insert_edge(conn, EdgeRow(source=m_id, target=e_id, type="mentions", created_at=old_ts))
        conn.commit()
        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)
        assert report.newest_edge_days >= 9
        conn.close()

    def test_edge_staleness_alert(self):
        """Edge staleness alert fires when newest_edge_days > 7."""
        report = HealthReport(total_chunks=10, cold_chunks=0, hot_chunks=10, active_edges=5, newest_edge_days=14)
        alerts = health_alerts(report)
        assert any("no new edges" in a.lower() for a in alerts)

    def test_no_edge_staleness_alert_when_recent(self):
        """No alert when edges are recent."""
        report = HealthReport(total_chunks=10, cold_chunks=0, hot_chunks=10, active_edges=5, newest_edge_days=3)
        alerts = health_alerts(report)
        assert not any("no new edges" in a.lower() for a in alerts)

    def test_synthesis_staleness_alert(self):
        """Synthesis staleness alert fires when last_synthesis > 7 days old."""
        from datetime import datetime, timedelta, timezone
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        report = HealthReport(total_chunks=10, cold_chunks=0, hot_chunks=10, last_synthesis=old_ts)
        alerts = health_alerts(report)
        assert any("no synthesis" in a.lower() for a in alerts)


# =============================================================================
# A1: Fix false health alert for v3 DBs with legacy tables
# =============================================================================


class TestHealthV3LegacyTableBug:
    """Regression tests: v3 DBs with leftover chunks/nodes tables should
    query data_points, not the empty legacy tables."""

    def _make_v3_db(self, tmp_path):
        from unittest.mock import patch

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with patch("storage.get_db_path", return_value=db_path),              patch("storage.get_memory_dir", return_value=tmp_path),              patch("health.get_db_path", return_value=db_path):
            conn = ensure_db()
        return conn

    def test_v3_db_with_empty_chunks_table_not_empty_alert(self, tmp_path):
        """A v3 DB with an empty legacy chunks table should report total_chunks
        from data_points and NOT trigger a 'DB empty' alert."""
        from storage import DataPointRow, insert_data_point
        conn = self._make_v3_db(tmp_path)

        conn.execute(
            "CREATE TABLE IF NOT EXISTS chunks "
            "(id TEXT, salience REAL, source_type TEXT)"
        )

        for i in range(5):
            insert_data_point(conn, DataPointRow(
                type="memory", content=f"mem {i}",
                scope="global", salience=0.5,
            ))
        conn.commit()

        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)
            alerts = health_alerts(report)

        assert report.total_chunks == 5
        assert not any("empty" in a.lower() for a in alerts)
        conn.close()

    def test_v3_db_with_legacy_nodes_table_uses_data_points(self, tmp_path):
        """A v3 DB with an empty legacy nodes table should count entity
        data_points for graph_nodes, not the empty nodes table."""
        from storage import DataPointRow, insert_data_point
        conn = self._make_v3_db(tmp_path)

        conn.execute(
            "CREATE TABLE IF NOT EXISTS nodes (id TEXT, name TEXT)"
        )

        for i in range(2):
            insert_data_point(conn, DataPointRow(
                type="entity", name=f"entity_{i}",
                content=f"entity {i}", scope="global", salience=0.5,
            ))
        conn.commit()

        with mock.patch("health.get_db_path", return_value=tmp_path / "memory.db"):
            report = health_report(conn)

        assert report.graph_nodes == 2
        conn.close()
