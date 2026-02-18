"""Shared test configuration — path setup for all test modules."""

import sys
from pathlib import Path

# Add scripts and tests directories to path for all test modules
sys.path.insert(0, str(Path(__file__).parent))  # tests/ (for helpers)
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))  # scripts/
