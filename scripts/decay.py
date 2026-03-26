#!/usr/bin/env python3
"""
Salience-based decay for Claude Code Memory System.

Applies tiered exponential decay to data_points entries in the SQL database.

Usage:
    python decay.py              # Run decay on all data_points
    python decay.py --dry-run    # Show what would be decayed without making changes

Requirements: Python 3.9+
"""

import argparse
import sys
from datetime import date, datetime
from math import exp
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    check_python_version,
    local_today,
)

# Tiered salience decay constants
HOT_LAMBDA = 0.005
WARM_LAMBDA = 0.02
COLD_LAMBDA = 0.05
HOT_DAYS_THRESHOLD = 6
HOT_ACCESS_THRESHOLD = 5
HOT_SALIENCE_THRESHOLD = 0.7
WARM_SALIENCE_THRESHOLD = 0.4
ARCHIVE_SALIENCE_THRESHOLD = 0.05

# Default decay thresholds (used as fallbacks when settings.json missing)
DEFAULT_AGE_DAYS = 30
DEFAULT_PROJECT_WORKING_DAYS = 20


def days_since(last_accessed_iso: str | None, today: date | None = None) -> float:
    """Calculate days since last_accessed timestamp.

    Args:
        last_accessed_iso: ISO timestamp string (e.g. '2026-03-16T12:00:00Z') or None.
        today: Reference date for calculation. Defaults to local today.

    Returns:
        Float days elapsed. Returns 999.0 if last_accessed_iso is None or unparseable.
    """
    if not last_accessed_iso:
        return 999.0
    if today is None:
        today = local_today()
    try:
        dt = datetime.fromisoformat(last_accessed_iso.replace("Z", "+00:00"))
        delta = today - dt.date()
        return max(0.0, float(delta.days))
    except (ValueError, AttributeError):
        return 999.0


def pick_tier(
    dt_days: float, access_count: int, salience: float
) -> tuple[str, float]:
    """Classify a chunk into hot/warm/cold tier based on recency and salience.

    Args:
        dt_days: Days since last access.
        access_count: Number of times accessed.
        salience: Current salience score.

    Returns:
        (tier_name, lambda_rate) tuple.
    """
    if dt_days < HOT_DAYS_THRESHOLD and (
        access_count > HOT_ACCESS_THRESHOLD or salience > HOT_SALIENCE_THRESHOLD
    ):
        return "hot", HOT_LAMBDA
    if dt_days < HOT_DAYS_THRESHOLD or salience > WARM_SALIENCE_THRESHOLD:
        return "warm", WARM_LAMBDA
    return "cold", COLD_LAMBDA


def decay_salience(
    current_salience: float, dt_days: float, lam: float
) -> float:
    """Apply exponential decay with salience-dependent rate (death spiral formula).

    Formula: new = current * exp(-lambda * (dt / (current + 0.1)))
    The (current + 0.1) divisor creates intentional death spiral:
    low-salience chunks decay faster, accelerating archival.

    Args:
        current_salience: Current salience in [0.0, 1.0].
        dt_days: Days elapsed since last access.
        lam: Lambda rate for this tier.

    Returns:
        New salience value clamped to [0.0, 1.0].
    """
    if current_salience <= 0:
        return 0.0
    factor = exp(-lam * (dt_days / (current_salience + 0.1)))
    return max(0.0, min(1.0, current_salience * factor))


def decay_data_points(conn, dry_run: bool = False) -> int:
    """Apply tiered exponential decay to data_points salience.

    Queries all active memories (type='memory', salience > threshold,
    not user scope), classifies into hot/warm/cold tiers, and applies
    decay formula. Consolidated memories participate in normal decay;
    recently accessed ones survive via salience reinforcement.

    Protected from decay:
    - type != 'memory' (profile, entity, session_context)
    - scope = 'user' (permanent user preferences)

    Args:
        conn: SQLite connection with v3 schema.
        dry_run: If True, count but do not write changes.

    Returns:
        Count of data_points whose salience was reduced.
    """
    rows = conn.execute(
        "SELECT id, salience, access_count, COALESCE(last_accessed, created_at), certainty "
        "FROM data_points WHERE type = 'memory' AND salience > ? "
        "AND (scope IS NULL OR scope != 'user')",
        (ARCHIVE_SALIENCE_THRESHOLD,)
    ).fetchall()

    decayed = 0
    for dp_id, salience, access_count, last_accessed, certainty in rows:
        if certainty is not None and certainty >= 4:
            continue

        dt = days_since(last_accessed)
        _tier, lam = pick_tier(dt, access_count or 0, salience or 0)

        if certainty is not None and certainty <= 2:
            lam = lam * 2.0

        new_sal = decay_salience(salience, dt, lam)
        if new_sal != salience:
            if not dry_run:
                conn.execute(
                    "UPDATE data_points SET salience = ? WHERE id = ?",
                    (max(0.0, min(1.0, new_sal)), dp_id)
                )
            decayed += 1

    if not dry_run:
        conn.commit()
    return decayed


def cleanup_near_zero_salience(conn, threshold: float = ARCHIVE_SALIENCE_THRESHOLD, dry_run: bool = False) -> int:
    """Soft-delete data_points whose salience has decayed to near-zero.

    Finds memories with salience > 0 but <= threshold and soft-deletes them
    (sets salience=0, removes FTS entries). This prevents accumulation of
    near-zero salience data_points that clutter the DB without being useful.

    Protected from cleanup:
    - type != 'memory' (profile, entity, session_context)
    - scope = 'user' (permanent user preferences)

    Args:
        conn: SQLite connection with v3 schema.
        threshold: Salience at or below which memories are cleaned up.
        dry_run: If True, count but do not write changes.

    Returns:
        Count of data_points cleaned up (soft-deleted).
    """
    from storage import fts_delete

    rows = conn.execute(
        "SELECT id FROM data_points WHERE type = 'memory' "
        "AND salience > 0 AND salience <= ? "
        "AND (scope IS NULL OR scope != 'user')",
        (threshold,)
    ).fetchall()

    if dry_run or not rows:
        return len(rows)

    for (dp_id,) in rows:
        conn.execute(
            "UPDATE data_points SET salience = 0.0 WHERE id = ?", (dp_id,)
        )
        try:
            fts_delete(conn, dp_id)
        except Exception as e:
            print(f"Warning: FTS5 delete failed for {dp_id} during cleanup: {e}", file=sys.stderr)

    conn.commit()
    return len(rows)


def main() -> int:
    """Main entry point."""
    check_python_version()

    parser = argparse.ArgumentParser(
        description="Apply salience-based decay to memory data_points"
    )
    parser.add_argument(
        "--dry-run", "-n",
        action="store_true",
        help="Show what would be decayed without making changes"
    )
    args = parser.parse_args()

    from storage import close_db, get_db

    conn = get_db()
    try:
        decayed = decay_data_points(conn, dry_run=args.dry_run)
        cleaned = cleanup_near_zero_salience(conn, dry_run=args.dry_run)
        action = "Would decay" if args.dry_run else "Decayed"
        print(f"{action} {decayed} data_point(s), cleaned up {cleaned} near-zero salience entries")
    finally:
        close_db(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
