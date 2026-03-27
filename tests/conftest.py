"""Shared test configuration — path setup for all test modules."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add scripts and tests directories to path for all test modules
sys.path.insert(0, str(Path(__file__).parent))  # tests/ (for helpers)
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))  # scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))  # repo root (for install)


@pytest.fixture
def shared_db(tmp_path):
    """Shared DB fixture: patches storage.get_db_path, calls ensure_db(), yields connection."""
    from storage import ensure_db

    db_path = tmp_path / "memory.db"
    with patch("storage.get_db_path", return_value=db_path):
        conn = ensure_db()
        yield conn
    conn.close()
