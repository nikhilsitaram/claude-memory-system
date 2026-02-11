#!/usr/bin/env python3
"""
Unit tests for token_usage.py

Run with: python -m pytest tests/test_token_usage.py -v
"""

import sys
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

import pytest

# Add scripts directory to path
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from token_usage import calculate_usage


# =============================================================================
# Helpers
# =============================================================================

EXPECTED_KEYS = [
    "project_name",
    "global_long_term_tokens",
    "global_long_limit",
    "global_short_term_tokens",
    "global_short_limit",
    "global_short_days_actual",
    "project_long_term_tokens",
    "project_long_limit",
    "project_short_term_tokens",
    "project_short_limit",
    "project_short_days_actual",
    "total_tokens",
    "total_budget",
]


def _capture_usage(**overrides) -> dict[str, str]:
    """Run calculate_usage() capturing stdout, return parsed key=value dict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        daily_dir = Path(tmpdir) / "daily"
        daily_dir.mkdir()
        project_dir = Path(tmpdir) / "project-memory"
        project_dir.mkdir()
        global_file = Path(tmpdir) / "global-long-term-memory.md"

        # Write global memory if requested
        global_content = overrides.get("global_content", "")
        if global_content:
            global_file.write_text(global_content)

        # Write daily files if requested
        for date, content in overrides.get("daily_files", {}).items():
            (daily_dir / f"{date}.md").write_text(content)

        # Write project memory if requested
        project_name = overrides.get("project_name")
        if project_name:
            pfile = project_dir / f"{project_name}-long-term-memory.md"
            pfile.write_text(overrides.get("project_content", "# Project"))

        # Build project index
        project_index = {"projects": {}}
        cwd = overrides.get("cwd", "/nonexistent/path")
        if project_name:
            project_index["projects"][cwd.lower()] = {
                "name": project_name,
                "originalPath": cwd,
            }

        with mock.patch("token_usage.get_daily_dir", return_value=daily_dir), \
             mock.patch("token_usage.get_global_memory_file", return_value=global_file), \
             mock.patch("token_usage.get_project_memory_dir", return_value=project_dir), \
             mock.patch("token_usage.get_projects_index_file", return_value=Path(tmpdir) / "index.json"), \
             mock.patch("token_usage.load_json_file", return_value=project_index), \
             mock.patch("token_usage.load_settings", return_value=_default_settings()), \
             mock.patch("os.getcwd", return_value=cwd):

            captured = StringIO()
            old_stdout = sys.stdout
            sys.stdout = captured
            try:
                calculate_usage()
            finally:
                sys.stdout = old_stdout

        output = captured.getvalue()
        return dict(line.split("=", 1) for line in output.strip().split("\n") if "=" in line)


def _default_settings() -> dict:
    return {
        "globalShortTerm": {"workingDays": 2, "tokenLimit": 1500},
        "globalLongTerm": {"tokenLimit": 5000},
        "projectShortTerm": {"workingDays": 7, "tokenLimit": 5250},
        "projectLongTerm": {"tokenLimit": 5000},
        "projectSettings": {"includeSubdirectories": False},
        "totalTokenBudget": 16750,
    }


# =============================================================================
# calculate_usage Tests
# =============================================================================


class TestCalculateUsage:
    def test_output_format(self):
        """Output is key=value lines, one per metric."""
        result = _capture_usage()
        # Every line should be key=value
        assert len(result) == len(EXPECTED_KEYS)

    def test_all_metrics_present(self):
        """All expected metric keys are in the output."""
        result = _capture_usage()
        for key in EXPECTED_KEYS:
            assert key in result, f"Missing key: {key}"

    def test_handles_no_project(self):
        """When CWD doesn't match any project, project_name=none."""
        result = _capture_usage(cwd="/nonexistent/path")
        assert result["project_name"] == "none"
        assert result["project_long_term_tokens"] == "0"
        assert result["project_short_term_tokens"] == "0"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
