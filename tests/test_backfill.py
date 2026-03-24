#!/usr/bin/env python3
"""
Unit tests for scripts/backfill.py — deprecated module.

The backfill.py module is deprecated (v2 chunks table no longer exists).
This test verifies it raises RuntimeError on import.
"""

import pytest


class TestBackfillDeprecated:
    """Verify backfill.py raises RuntimeError."""

    def test_import_raises_deprecation(self):
        """Importing backfill.py raises RuntimeError."""
        with pytest.raises(RuntimeError, match="deprecated"):
            import importlib
            import sys
            if "backfill" in sys.modules:
                del sys.modules["backfill"]
            importlib.import_module("backfill")
