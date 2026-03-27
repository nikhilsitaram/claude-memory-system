#!/usr/bin/env python3
"""
Unit tests for decay.py

Run with: python -m pytest tests/test_decay.py -v
"""

import sys as _sys
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from decay import (  # noqa: I001
    ARCHIVE_SALIENCE_THRESHOLD,
    COLD_LAMBDA,
    DEFAULT_AGE_DAYS,
    HOT_LAMBDA,
    WARM_LAMBDA,
    days_since,
    decay_salience,
    main,
    pick_tier,
)

_SCRIPTS_DIR = str(__import__("pathlib").Path(__file__).parent.parent / "scripts")
if _SCRIPTS_DIR not in _sys.path:
    _sys.path.insert(0, _SCRIPTS_DIR)


# =============================================================================
# B4: Tiered Salience Decay Tests
# =============================================================================

from math import exp


class TestPickTier:
    """Tests for pick_tier() tier classification."""

    def test_hot_tier_recent_high_access(self):
        """Recent (< HOT_DAYS_THRESHOLD) + access_count > 5 = hot."""
        tier, lam = pick_tier(dt_days=2.0, access_count=10, salience=0.5)
        assert tier == "hot"
        assert lam == HOT_LAMBDA

    def test_hot_tier_recent_high_salience(self):
        """Recent + salience > 0.7 = hot."""
        tier, lam = pick_tier(dt_days=3.0, access_count=1, salience=0.8)
        assert tier == "hot"
        assert lam == HOT_LAMBDA

    def test_warm_tier_recent_low_access(self):
        """Recent but low access and salience 0.4-0.7 = warm."""
        tier, lam = pick_tier(dt_days=4.0, access_count=2, salience=0.5)
        assert tier == "warm"
        assert lam == WARM_LAMBDA

    def test_warm_tier_old_moderate_salience(self):
        """Old (>= HOT_DAYS_THRESHOLD) but salience > 0.4 = warm."""
        tier, lam = pick_tier(dt_days=10.0, access_count=3, salience=0.6)
        assert tier == "warm"
        assert lam == WARM_LAMBDA

    def test_cold_tier_old_low_salience(self):
        """Old + low salience + low access = cold."""
        tier, lam = pick_tier(dt_days=20.0, access_count=1, salience=0.2)
        assert tier == "cold"
        assert lam == COLD_LAMBDA


class TestDecaySalience:
    """Tests for decay_salience() exponential decay math."""

    def test_hot_decay_preserves_salience(self):
        """Hot tier: lambda=0.005, salience barely decreases over 5 days."""
        result = decay_salience(current_salience=0.9, dt_days=5.0, lam=HOT_LAMBDA)
        expected = 0.9 * exp(-HOT_LAMBDA * (5.0 / (0.9 + 0.1)))
        assert abs(result - expected) < 1e-9
        assert result > 0.87

    def test_cold_decay_drops_fast(self):
        """Cold tier: lambda=0.05, salience drops significantly over 20 days."""
        result = decay_salience(current_salience=0.3, dt_days=20.0, lam=COLD_LAMBDA)
        expected = 0.3 * exp(-COLD_LAMBDA * (20.0 / (0.3 + 0.1)))
        assert abs(result - expected) < 1e-9
        assert result < ARCHIVE_SALIENCE_THRESHOLD

    def test_death_spiral_for_neglected_memories(self):
        """Low-salience chunks decay faster than high-salience ones."""
        high = decay_salience(current_salience=0.8, dt_days=15.0, lam=COLD_LAMBDA)
        low = decay_salience(current_salience=0.1, dt_days=15.0, lam=COLD_LAMBDA)
        assert low < high

    def test_salience_clamped_to_0_1(self):
        """Result is always in [0.0, 1.0]."""
        result = decay_salience(current_salience=0.0, dt_days=100.0, lam=COLD_LAMBDA)
        assert result == 0.0
        result2 = decay_salience(current_salience=1.0, dt_days=0.0, lam=HOT_LAMBDA)
        assert 0.0 <= result2 <= 1.0


class TestDaysSince:
    """Tests for days_since() helper."""

    def test_returns_999_for_none(self):
        """None last_accessed treated as very old."""
        result = days_since(None)
        assert result == 999.0

    def test_returns_correct_days(self):
        """Returns correct days from ISO timestamp."""
        from datetime import date as date_cls
        ts = "2026-03-16T12:00:00Z"
        today = date_cls(2026, 3, 21)
        result = days_since(ts, today=today)
        assert result == 5.0

    def test_returns_0_for_today(self):
        """Today's timestamp returns 0.0."""
        from datetime import date as date_cls
        today = date_cls(2026, 3, 21)
        ts = "2026-03-21T00:00:00Z"
        result = days_since(ts, today=today)
        assert result == 0.0


# =============================================================================
# A2: Tiered Decay for v3 data_points
# =============================================================================


class TestDecayDataPoints:
    """Tests for decay_data_points() operating on the v3 data_points table."""

    def _make_v3_db(self, tmp_path):
        """Create a v3 DB with data_points table for testing."""
        from unittest.mock import patch as _patch

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with _patch("storage.get_db_path", return_value=db_path), \
             _patch("memory_utils.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        return conn

    def test_decays_old_memory_data_points(self, tmp_path):
        """A memory not accessed in 30+ days gets its salience reduced."""
        from decay import decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
        dp = DataPointRow(type="memory", content="old fact", scope="global", salience=0.6, last_accessed=old_ts)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        count = decay_data_points(conn)
        assert count >= 1
        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        assert row[0] < 0.6, "Salience should decrease after decay"
        conn.close()

    def test_skips_profile_type_data_points(self, tmp_path):
        """Profile data_points (type='profile') are never decayed."""
        from decay import decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat().replace("+00:00", "Z")
        dp = DataPointRow(type="profile", content="About Me", scope="user", salience=1.0, last_accessed=old_ts)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        decay_data_points(conn)
        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        assert row[0] == 1.0, "Profile should not be decayed"
        conn.close()

    def test_decays_consolidated_data_points(self, tmp_path):
        """Consolidated data_points participate in normal decay (no immunity)."""
        from decay import decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat().replace("+00:00", "Z")
        dp = DataPointRow(type="memory", content="pinned", scope="global", salience=0.8, consolidated=1, last_accessed=old_ts)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        decay_data_points(conn)
        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        assert row[0] < 0.8, "Consolidated should be decayed like any other memory"
        conn.close()

    def test_skips_user_scope_data_points(self, tmp_path):
        """User-scope memories are not decayed."""
        from decay import decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat().replace("+00:00", "Z")
        dp = DataPointRow(type="memory", content="user pref", scope="user", salience=0.7, last_accessed=old_ts)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        decay_data_points(conn)
        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        assert row[0] == 0.7, "User scope should not be decayed"
        conn.close()

    def test_dry_run_no_changes_data_points(self, tmp_path):
        """dry_run=True counts but does not modify salience."""
        from decay import decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
        dp = DataPointRow(type="memory", content="old fact", scope="global", salience=0.6, last_accessed=old_ts)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        count = decay_data_points(conn, dry_run=True)
        assert count >= 1
        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        assert row[0] == 0.6, "Dry run should not change salience"
        conn.close()

    def test_tier_classification_data_points(self, tmp_path):
        """Verify tier classification applies correct decay rates."""
        from decay import decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        now = datetime.now(timezone.utc)
        cold_ts = (now - timedelta(days=60)).isoformat().replace("+00:00", "Z")
        warm_ts = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")

        cold_dp = DataPointRow(type="memory", content="cold fact", scope="global", salience=0.3, last_accessed=cold_ts, access_count=1)
        warm_dp = DataPointRow(type="memory", content="warm fact", scope="global", salience=0.5, last_accessed=warm_ts, access_count=3)
        cold_id = insert_data_point(conn, cold_dp)
        warm_id = insert_data_point(conn, warm_dp)
        conn.commit()

        decay_data_points(conn)
        cold_row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (cold_id,)).fetchone()
        warm_row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (warm_id,)).fetchone()
        assert cold_row[0] < warm_row[0], "Cold tier should decay faster than warm"
        conn.close()


class TestCertaintyDecay:
    """Tests for certainty-aware decay behavior."""

    def _make_v3_db(self, tmp_path):
        from unittest.mock import patch as _patch

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with _patch("storage.get_db_path", return_value=db_path), \
             _patch("memory_utils.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        return conn

    def test_certainty_4_immune_to_decay(self, tmp_path):
        """Certainty 4-5 data_points are not decayed."""
        from decay import DEFAULT_AGE_DAYS, decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_AGE_DAYS * 2)).isoformat().replace("+00:00", "Z")
        dp = DataPointRow(type="memory", content="established", scope="global", salience=0.6, certainty=4, last_accessed=old_ts)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        decay_data_points(conn)
        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        assert row[0] == 0.6, "Certainty 4 should be immune to decay"
        conn.close()

    def test_certainty_5_immune_to_decay(self, tmp_path):
        """Certainty 5 data_points are also immune to decay."""
        from decay import DEFAULT_AGE_DAYS, decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_AGE_DAYS * 2)).isoformat().replace("+00:00", "Z")
        dp = DataPointRow(type="memory", content="established fact", scope="global", salience=0.7, certainty=5, last_accessed=old_ts)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        decay_data_points(conn)
        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        assert row[0] == 0.7, "Certainty 5 should be immune to decay"
        conn.close()

    def test_certainty_1_decays_faster(self, tmp_path):
        """Certainty 1-2 data_points decay at 2x rate."""
        from decay import decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_AGE_DAYS)).isoformat().replace("+00:00", "Z")
        dp_low = DataPointRow(type="memory", content="speculative", scope="global", salience=0.5, certainty=1, last_accessed=old_ts)
        dp_normal = DataPointRow(type="memory", content="normal", scope="global", salience=0.5, certainty=3, last_accessed=old_ts)
        id_low = insert_data_point(conn, dp_low)
        id_normal = insert_data_point(conn, dp_normal)
        conn.commit()

        decay_data_points(conn)
        sal_low = conn.execute("SELECT salience FROM data_points WHERE id = ?", (id_low,)).fetchone()[0]
        sal_normal = conn.execute("SELECT salience FROM data_points WHERE id = ?", (id_normal,)).fetchone()[0]
        assert sal_low < sal_normal, "Certainty 1 should decay faster than certainty 3"
        conn.close()

    def test_certainty_none_decays_normally(self, tmp_path):
        """Certainty NULL behaves like normal decay (no modifier)."""
        from decay import decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=DEFAULT_AGE_DAYS)).isoformat().replace("+00:00", "Z")
        dp_none = DataPointRow(type="memory", content="no certainty", scope="global", salience=0.5, certainty=None, last_accessed=old_ts)
        dp_normal = DataPointRow(type="memory", content="normal certainty", scope="global", salience=0.5, certainty=3, last_accessed=old_ts)
        id_none = insert_data_point(conn, dp_none)
        id_normal = insert_data_point(conn, dp_normal)
        conn.commit()

        decay_data_points(conn)
        sal_none = conn.execute("SELECT salience FROM data_points WHERE id = ?", (id_none,)).fetchone()[0]
        sal_normal = conn.execute("SELECT salience FROM data_points WHERE id = ?", (id_normal,)).fetchone()[0]
        assert sal_none == sal_normal, "Certainty NULL should behave same as certainty 3"
        conn.close()


class TestCleanupNearZeroSalience:
    """Tests for cleanup_near_zero_salience() which removes decayed memories."""

    def _make_v3_db(self, tmp_path):
        from unittest.mock import patch as _patch

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with _patch("storage.get_db_path", return_value=db_path), \
             _patch("memory_utils.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        return conn

    def test_cleans_up_near_zero_memories(self, tmp_path):
        """Memories at or below ARCHIVE_SALIENCE_THRESHOLD are soft-deleted."""
        from decay import ARCHIVE_SALIENCE_THRESHOLD, cleanup_near_zero_salience
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        dp_low = DataPointRow(type="memory", content="near zero", scope="global", salience=ARCHIVE_SALIENCE_THRESHOLD)
        dp_ok = DataPointRow(type="memory", content="still alive", scope="global", salience=ARCHIVE_SALIENCE_THRESHOLD + 0.1)
        id_low = insert_data_point(conn, dp_low)
        id_ok = insert_data_point(conn, dp_ok)
        conn.commit()

        cleaned = cleanup_near_zero_salience(conn)
        assert cleaned == 1
        sal_low = conn.execute("SELECT salience FROM data_points WHERE id = ?", (id_low,)).fetchone()[0]
        sal_ok = conn.execute("SELECT salience FROM data_points WHERE id = ?", (id_ok,)).fetchone()[0]
        assert sal_low == 0.0
        assert sal_ok > 0.0
        conn.close()

    def test_skips_non_memory_types(self, tmp_path):
        """Entities, profiles, session_contexts with low salience are not cleaned up."""
        from decay import ARCHIVE_SALIENCE_THRESHOLD, cleanup_near_zero_salience
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        for dp_type in ("entity", "profile", "session_context"):
            dp = DataPointRow(type=dp_type, content=f"low {dp_type}", scope="global", salience=ARCHIVE_SALIENCE_THRESHOLD)
            insert_data_point(conn, dp)
        conn.commit()

        cleaned = cleanup_near_zero_salience(conn)
        assert cleaned == 0
        conn.close()

    def test_cleans_up_consolidated_memories(self, tmp_path):
        """Consolidated memories with near-zero salience are cleaned up (no immunity)."""
        from decay import ARCHIVE_SALIENCE_THRESHOLD, cleanup_near_zero_salience
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        dp = DataPointRow(type="memory", content="pinned", scope="global", salience=ARCHIVE_SALIENCE_THRESHOLD, consolidated=1)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        cleaned = cleanup_near_zero_salience(conn)
        assert cleaned == 1
        sal = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()[0]
        assert sal == 0.0, "Consolidated near-zero memory should be soft-deleted"
        conn.close()

    def test_skips_user_scope(self, tmp_path):
        """User-scope memories are not cleaned up even at low salience."""
        from decay import ARCHIVE_SALIENCE_THRESHOLD, cleanup_near_zero_salience
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        dp = DataPointRow(type="memory", content="user pref", scope="user", salience=ARCHIVE_SALIENCE_THRESHOLD)
        insert_data_point(conn, dp)
        conn.commit()

        cleaned = cleanup_near_zero_salience(conn)
        assert cleaned == 0
        conn.close()

    def test_dry_run_no_changes(self, tmp_path):
        """Dry run counts but doesn't modify data."""
        from decay import ARCHIVE_SALIENCE_THRESHOLD, cleanup_near_zero_salience
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        dp = DataPointRow(type="memory", content="will survive", scope="global", salience=ARCHIVE_SALIENCE_THRESHOLD)
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        cleaned = cleanup_near_zero_salience(conn, dry_run=True)
        assert cleaned == 1
        sal = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()[0]
        assert sal == ARCHIVE_SALIENCE_THRESHOLD, "Dry run should not change salience"
        conn.close()

    def test_removes_fts_entries(self, tmp_path):
        """Cleaned-up memories have their FTS entries removed."""
        from decay import ARCHIVE_SALIENCE_THRESHOLD, cleanup_near_zero_salience
        from storage import DataPointRow, fts_insert, fts_search, insert_data_point

        conn = self._make_v3_db(tmp_path)
        dp = DataPointRow(type="memory", content="unique searchable xyzzy", scope="global", salience=ARCHIVE_SALIENCE_THRESHOLD)
        dp_id = insert_data_point(conn, dp)
        fts_insert(conn, dp_id, dp.content, dp.scope)
        conn.commit()

        assert len(fts_search(conn, "xyzzy", scope=None, limit=10)) >= 1
        cleanup_near_zero_salience(conn)
        assert len(fts_search(conn, "xyzzy", scope=None, limit=10)) == 0
        conn.close()

    def test_skips_already_zero_salience(self, tmp_path):
        """Data_points with salience exactly 0 are already deleted, not re-processed."""
        from decay import cleanup_near_zero_salience
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        dp = DataPointRow(type="memory", content="already dead", scope="global", salience=0.0)
        insert_data_point(conn, dp)
        conn.commit()

        cleaned = cleanup_near_zero_salience(conn)
        assert cleaned == 0
        conn.close()


class TestConsolidatedDecay:
    """Tests that consolidated memories participate in normal decay (A2)."""

    def _make_v3_db(self, tmp_path):
        from unittest.mock import patch as _patch

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with _patch("storage.get_db_path", return_value=db_path), \
             _patch("memory_utils.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        return conn

    def test_consolidated_memory_decays(self, tmp_path):
        """Consolidated memory with old last_accessed gets decayed."""
        from decay import decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat().replace("+00:00", "Z")
        dp = DataPointRow(
            type="memory", content="consolidated old fact", scope="global",
            salience=0.5, consolidated=1, last_accessed=old_ts,
        )
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        count = decay_data_points(conn)
        assert count >= 1
        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        assert row[0] < 0.5, "Consolidated memory should be decayed"
        conn.close()

    def test_consolidated_memory_with_recent_access_survives(self, tmp_path):
        """Consolidated memory with recent access retains high salience."""
        from decay import decay_data_points
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        dp = DataPointRow(
            type="memory", content="consolidated recent fact", scope="global",
            salience=0.9, consolidated=1, last_accessed=recent_ts, access_count=10,
        )
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        decay_data_points(conn)
        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()
        assert row[0] > 0.85, "Recently accessed consolidated memory should retain high salience"
        conn.close()

    def test_consolidated_near_zero_cleaned_up(self, tmp_path):
        """Consolidated memory with near-zero salience gets cleaned up."""
        from decay import ARCHIVE_SALIENCE_THRESHOLD, cleanup_near_zero_salience
        from storage import DataPointRow, insert_data_point

        conn = self._make_v3_db(tmp_path)
        dp = DataPointRow(
            type="memory", content="consolidated near zero", scope="global",
            salience=ARCHIVE_SALIENCE_THRESHOLD, consolidated=1,
        )
        dp_id = insert_data_point(conn, dp)
        conn.commit()

        cleaned = cleanup_near_zero_salience(conn)
        assert cleaned == 1
        sal = conn.execute("SELECT salience FROM data_points WHERE id = ?", (dp_id,)).fetchone()[0]
        assert sal == 0.0, "Near-zero consolidated memory should be soft-deleted"
        conn.close()


# =============================================================================
# main() entry point tests
# =============================================================================


class TestMain:
    """Tests for the refactored main() entry point."""

    def _make_v3_db(self, tmp_path):
        from unittest.mock import patch as _patch

        from storage import ensure_db
        db_path = tmp_path / "memory.db"
        with _patch("storage.get_db_path", return_value=db_path), \
             _patch("memory_utils.get_memory_dir", return_value=tmp_path):
            conn = ensure_db()
        return conn

    def test_main_calls_decay_and_cleanup(self, tmp_path):
        """main() calls decay_data_points and cleanup_near_zero_salience."""
        conn = self._make_v3_db(tmp_path)
        with mock.patch("sys.argv", ["decay.py"]), \
             mock.patch("decay.check_python_version"), \
             mock.patch("storage.get_db", return_value=conn), \
             mock.patch("storage.close_db") as mock_close, \
             mock.patch("decay.decay_data_points", return_value=3) as mock_decay, \
             mock.patch("decay.cleanup_near_zero_salience", return_value=1) as mock_cleanup:
            result = main()
        assert result == 0
        mock_decay.assert_called_once_with(conn, dry_run=False)
        mock_cleanup.assert_called_once_with(conn, dry_run=False)
        mock_close.assert_called_once_with(conn)

    def test_main_dry_run_flag(self, tmp_path):
        """main() passes dry_run=True when --dry-run is specified."""
        conn = self._make_v3_db(tmp_path)
        with mock.patch("sys.argv", ["decay.py", "--dry-run"]), \
             mock.patch("decay.check_python_version"), \
             mock.patch("storage.get_db", return_value=conn), \
             mock.patch("storage.close_db"), \
             mock.patch("decay.decay_data_points", return_value=0) as mock_decay, \
             mock.patch("decay.cleanup_near_zero_salience", return_value=0) as mock_cleanup:
            result = main()
        assert result == 0
        mock_decay.assert_called_once_with(conn, dry_run=True)
        mock_cleanup.assert_called_once_with(conn, dry_run=True)

    def test_main_closes_db_on_error(self, tmp_path):
        """main() closes the DB connection even when an error occurs."""
        conn = self._make_v3_db(tmp_path)
        with mock.patch("sys.argv", ["decay.py"]), \
             mock.patch("decay.check_python_version"), \
             mock.patch("storage.get_db", return_value=conn), \
             mock.patch("storage.close_db") as mock_close, \
             mock.patch("decay.decay_data_points", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError, match="boom"):
                main()
        mock_close.assert_called_once_with(conn)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
