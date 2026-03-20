#!/usr/bin/env python3
"""
Health diagnostics for Claude Code Memory System.

Queries memory.db for health metrics: chunk counts, salience distribution,
graph statistics, and potential issues.

Usage:
    python3 health.py              # Print full health report
    python3 health.py --json       # Output as JSON
    python3 health.py --alerts     # Only show alerts (non-zero exit if any)

Requirements: Python 3.9+, memory.db must exist (run install.py first)
"""

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import check_python_version, get_db_path  # noqa: E402
from storage import _get_schema_version, close_db, get_db  # noqa: E402

__all__ = [
    "COLD_RATIO_THRESHOLD",
    "HealthReport",
    "health_report",
    "health_alerts",
    "format_report",
]

# Alert thresholds
COLD_RATIO_THRESHOLD = 0.8  # Alert if 80%+ chunks are cold


@dataclass
class HealthReport:
    """Memory system health metrics."""
    total_chunks: int = 0
    avg_salience: float = 0.0
    hot_chunks: int = 0    # salience > 0.7
    warm_chunks: int = 0   # salience 0.1 - 0.7
    cold_chunks: int = 0   # salience < 0.1
    graph_nodes: int = 0
    active_edges: int = 0
    invalidated_edges: int = 0
    db_size_bytes: int = 0
    ltm_chunks: int = 0
    daily_chunks: int = 0
    schema_version: int = 0


def health_report(conn: sqlite3.Connection) -> HealthReport:
    """Query the database for health metrics.

    Returns a HealthReport dataclass. All queries are read-only.
    """
    report = HealthReport()
    report.schema_version = _get_schema_version(conn)

    # Chunk statistics
    row = conn.execute("""
        SELECT
            COUNT(*) as total,
            COALESCE(ROUND(AVG(salience), 3), 0) as avg_sal,
            SUM(CASE WHEN salience > 0.7 THEN 1 ELSE 0 END) as hot,
            SUM(CASE WHEN salience BETWEEN 0.1 AND 0.7 THEN 1 ELSE 0 END) as warm,
            SUM(CASE WHEN salience < 0.1 THEN 1 ELSE 0 END) as cold,
            SUM(CASE WHEN source_type = 'ltm' THEN 1 ELSE 0 END) as ltm,
            SUM(CASE WHEN source_type = 'daily' THEN 1 ELSE 0 END) as daily
        FROM chunks
    """).fetchone()

    report.total_chunks = row[0] or 0
    report.avg_salience = row[1] or 0.0
    report.hot_chunks = row[2] or 0
    report.warm_chunks = row[3] or 0
    report.cold_chunks = row[4] or 0
    report.ltm_chunks = row[5] or 0
    report.daily_chunks = row[6] or 0

    # Graph statistics
    report.graph_nodes = (
        conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] or 0
    )
    report.active_edges = (
        conn.execute(
            "SELECT COUNT(*) FROM edges WHERE valid_to IS NULL"
        ).fetchone()[0] or 0
    )
    report.invalidated_edges = (
        conn.execute(
            "SELECT COUNT(*) FROM edges WHERE valid_to IS NOT NULL"
        ).fetchone()[0] or 0
    )

    # DB file size
    db_path = get_db_path()
    if db_path.exists():
        report.db_size_bytes = db_path.stat().st_size

    return report


def health_alerts(report: HealthReport) -> list[str]:
    """Generate alert strings for concerning health conditions.

    Returns a list of human-readable alert strings. Empty list means healthy.
    """
    alerts = []

    if report.total_chunks == 0:
        alerts.append(
            "Memory DB is empty -- run migration to populate from existing markdown files."
        )
        return alerts  # No point checking ratios on empty DB

    cold_ratio = report.cold_chunks / report.total_chunks
    if cold_ratio >= COLD_RATIO_THRESHOLD:
        pct = int(cold_ratio * 100)
        alerts.append(
            f"{pct}% of memories are cold (salience < 0.1) -- "
            "consider running /synthesize or consolidation."
        )

    return alerts


def format_report(report: HealthReport, alerts: list[str]) -> str:
    """Format a health report as human-readable text."""
    lines = [
        "Memory System Health Report",
        "=" * 40,
        "",
        f"  Total chunks:  {report.total_chunks}",
        f"    LTM:         {report.ltm_chunks}",
        f"    Daily:       {report.daily_chunks}",
        f"  Avg salience:  {report.avg_salience:.3f}",
        f"  Hot (>0.7):    {report.hot_chunks}",
        f"  Warm (0.1-0.7):{report.warm_chunks}",
        f"  Cold (<0.1):   {report.cold_chunks}",
        "",
        f"  Graph nodes:   {report.graph_nodes}",
        f"  Active edges:  {report.active_edges}",
        f"  Invalid edges: {report.invalidated_edges}",
        "",
        f"  DB size:       {report.db_size_bytes / 1024:.1f} KB",
        f"  Schema ver:    {report.schema_version}",
    ]

    if alerts:
        lines.extend(["", "Alerts:", "-------"])
        for alert in alerts:
            lines.append(f"  - {alert}")

    return "\n".join(lines)


def main() -> int:
    """CLI entry point."""
    check_python_version()

    parser = argparse.ArgumentParser(
        description="Memory system health diagnostics"
    )
    parser.add_argument(
        "--json", action="store_true", help="Output as JSON"
    )
    parser.add_argument(
        "--alerts", action="store_true",
        help="Only show alerts (exit 1 if any)"
    )
    args = parser.parse_args()

    try:
        conn = get_db()
    except FileNotFoundError:
        if args.json:
            print(json.dumps({"error": "memory.db not found"}))
        else:
            print("Error: memory.db not found. Run install.py to create it.")
        return 1

    try:
        report = health_report(conn)
        alerts = health_alerts(report)

        if args.json:
            data = asdict(report)
            data["alerts"] = alerts
            print(json.dumps(data, indent=2))
        elif args.alerts:
            if alerts:
                for alert in alerts:
                    print(f"- {alert}")
                return 1
            else:
                print("No alerts.")
        else:
            print(format_report(report, alerts))
    finally:
        close_db(conn)

    return 0


if __name__ == "__main__":
    sys.exit(main())
