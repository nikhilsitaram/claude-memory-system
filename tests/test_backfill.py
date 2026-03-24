#!/usr/bin/env python3
"""
Unit tests for scripts/backfill.py — deprecated module.

The backfill.py module is deprecated (v2 chunks table no longer exists).
This test verifies it raises DeprecationWarning on import.
"""

import pytest


class TestBackfillDeprecated:
    """Verify backfill.py raises DeprecationWarning."""

    def test_import_raises_deprecation(self):
        """Importing backfill.py raises DeprecationWarning."""
        with pytest.raises(DeprecationWarning, match="deprecated"):
            import importlib
            import sys
            if "backfill" in sys.modules:
                del sys.modules["backfill"]
            importlib.import_module("backfill")
