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
