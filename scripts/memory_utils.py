#!/usr/bin/env python3
"""
Shared utilities for Claude Code Memory System.

Provides cross-platform path handling, settings management, token estimation,
and file locking. Used by load_memory.py, indexing.py, and other scripts.

Requirements: Python 3.9+
"""

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

# Minimum Python version required
MIN_PYTHON = (3, 9)


def check_python_version() -> None:
    """Check that Python version meets minimum requirements."""
    if sys.version_info < MIN_PYTHON:
        sys.exit(
            f"Error: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, "
            f"but running {sys.version_info.major}.{sys.version_info.minor}\n"
            f"Install a newer Python version or use pyenv/conda."
        )


def get_claude_dir() -> Path:
    """Get the Claude configuration directory (~/.claude)."""
    return Path.home() / ".claude"


def get_memory_dir() -> Path:
    """Get the memory directory (~/.claude/memory)."""
    return get_claude_dir() / "memory"


def get_daily_dir() -> Path:
    """Get the daily summaries directory."""
    return get_memory_dir() / "daily"


def get_project_memory_dir() -> Path:
    """Get the project-specific memory directory."""
    return get_memory_dir() / "project-memory"


def get_projects_dir() -> Path:
    """Get Claude Code's projects directory (source of transcripts)."""
    return get_claude_dir() / "projects"


def get_settings_file() -> Path:
    """Get the memory settings file path."""
    return get_memory_dir() / "settings.json"


def get_claude_settings_file() -> Path:
    """Get Claude Code's settings file path."""
    return get_claude_dir() / "settings.json"


def get_projects_index_file() -> Path:
    """Get the projects index file path."""
    return get_memory_dir() / "projects-index.json"


def get_global_memory_file() -> Path:
    """Get the global long-term memory file."""
    return get_memory_dir() / "global-long-term-memory.md"


def get_captured_file() -> Path:
    """Get the .captured file that tracks saved session IDs."""
    return get_memory_dir() / ".captured"


# Token limit formulas
SHORT_TERM_TOKENS_PER_DAY = 750  # With scope filtering, ~400-600 observed per day

# Default settings (tokenLimit for short-term calculated dynamically)
DEFAULT_SETTINGS = {
    "version": 3,
    "globalShortTerm": {
        "workingDays": 2,
        # tokenLimit calculated: workingDays × SHORT_TERM_TOKENS_PER_DAY
    },
    "globalLongTerm": {
        "tokenLimit": 5000,
    },
    "projectShortTerm": {
        "workingDays": 7,
        # tokenLimit calculated: workingDays × SHORT_TERM_TOKENS_PER_DAY
    },
    "projectLongTerm": {
        "tokenLimit": 5000,
    },
    "projectSettings": {
        "includeSubdirectories": False,
    },
    "synthesis": {
        "intervalHours": 2,
        "model": "sonnet",
        "background": True,
    },
    "decay": {
        "ageDays": 30,
        "archiveRetentionDays": 365,
    },
    # totalTokenBudget calculated as sum of 4 components
}


def load_settings() -> dict[str, Any]:
    """
    Load memory settings from settings.json with defaults.

    Returns settings dict with all expected keys populated.
    Short-term tokenLimits and totalTokenBudget are calculated dynamically
    from workingDays × SHORT_TERM_TOKENS_PER_DAY.
    """
    settings_file = get_settings_file()
    settings = DEFAULT_SETTINGS.copy()

    if settings_file.exists():
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                user_settings = json.load(f)
            # Deep merge user settings into defaults
            settings = _deep_merge(settings, user_settings)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not load settings from {settings_file}: {e}", file=sys.stderr)

    # Calculate dynamic token limits from workingDays
    settings = _calculate_token_limits(settings)

    return settings


def _calculate_token_limits(settings: dict[str, Any]) -> dict[str, Any]:
    """
    Calculate short-term tokenLimits and totalTokenBudget from workingDays.

    Formula: tokenLimit = workingDays × SHORT_TERM_TOKENS_PER_DAY (750)
    """
    global_days = settings.get("globalShortTerm", {}).get("workingDays", 2)
    project_days = settings.get("projectShortTerm", {}).get("workingDays", 7)

    # Calculate short-term limits
    settings["globalShortTerm"]["tokenLimit"] = global_days * SHORT_TERM_TOKENS_PER_DAY
    settings["projectShortTerm"]["tokenLimit"] = project_days * SHORT_TERM_TOKENS_PER_DAY

    # Calculate total budget as sum of 4 components
    settings["totalTokenBudget"] = (
        settings.get("globalLongTerm", {}).get("tokenLimit", 5000) +
        settings["globalShortTerm"]["tokenLimit"] +
        settings.get("projectLongTerm", {}).get("tokenLimit", 5000) +
        settings["projectShortTerm"]["tokenLimit"]
    )

    return settings


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge override dict into base dict."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def save_settings(settings: dict[str, Any]) -> None:
    """Save settings to settings.json."""
    settings_file = get_settings_file()
    settings_file.parent.mkdir(parents=True, exist_ok=True)

    with open(settings_file, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2)


def get_setting(path: str, default: Any = None) -> Any:
    """
    Get a nested setting by dot-notation path.

    Example: get_setting("projectShortTerm.workingDays", 7)
    """
    settings = load_settings()
    parts = path.split(".")
    value = settings

    for part in parts:
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return default

    return value


def estimate_tokens(text: str) -> int:
    """
    Estimate token count from text.

    Uses the approximation: 1 token ≈ 4 characters (bytes).
    This is a rough estimate that works reasonably well for English text.
    """
    return len(text) // 4


def estimate_file_tokens(filepath: Path) -> int:
    """Estimate tokens in a file."""
    if not filepath.exists():
        return 0
    try:
        content = filepath.read_text(encoding="utf-8")
        return estimate_tokens(content)
    except (IOError, UnicodeDecodeError):
        # Fall back to byte count
        try:
            return filepath.stat().st_size // 4
        except OSError:
            return 0


class FileLock:
    """
    Cross-platform file locking using directory creation.

    This works on all platforms (Windows, macOS, Linux) because
    mkdir is atomic and will fail if the directory already exists.

    Usage:
        with FileLock("~/.claude/memory/.mylock"):
            # critical section
    """

    def __init__(self, lock_path: str | Path, timeout: float = 10.0, poll_interval: float = 0.1):
        """
        Initialize file lock.

        Args:
            lock_path: Path to the lock directory (will be created as marker)
            timeout: Maximum time to wait for lock (seconds)
            poll_interval: Time between lock attempts (seconds)
        """
        self.lock_path = Path(lock_path).expanduser()
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._acquired = False

    def acquire(self) -> bool:
        """
        Attempt to acquire the lock.

        Returns True if acquired, False if timeout.
        """
        start_time = time.time()

        while time.time() - start_time < self.timeout:
            try:
                self.lock_path.mkdir(parents=True, exist_ok=False)
                self._acquired = True
                return True
            except FileExistsError:
                # Lock is held by another process
                # Check if it's stale (older than 5 minutes)
                try:
                    lock_age = time.time() - self.lock_path.stat().st_mtime
                    if lock_age > 300:  # 5 minutes
                        # Stale lock, remove it
                        self.lock_path.rmdir()
                        continue
                except OSError:
                    pass

                time.sleep(self.poll_interval)

        return False

    def release(self) -> None:
        """Release the lock."""
        if self._acquired:
            try:
                self.lock_path.rmdir()
            except OSError:
                pass
            self._acquired = False

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise TimeoutError(f"Could not acquire lock: {self.lock_path}")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()


def load_json_file(filepath: Path, default: Any = None) -> Any:
    """Load JSON from file with error handling."""
    if not filepath.exists():
        return default
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Warning: Could not load {filepath}: {e}", file=sys.stderr)
        return default


def save_json_file(filepath: Path, data: Any, indent: int = 2) -> bool:
    """Save data to JSON file with error handling."""
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
        return True
    except IOError as e:
        print(f"Error: Could not save {filepath}: {e}", file=sys.stderr)
        return False


def project_name_to_filename(project_name: str) -> str:
    """
    Convert project name to kebab-case filename.

    Example: "My Project" -> "my-project-long-term-memory.md"
    """
    # Convert to lowercase and replace spaces with hyphens
    kebab = project_name.lower().replace(" ", "-")
    # Remove any characters that aren't alphanumeric or hyphens
    kebab = "".join(c for c in kebab if c.isalnum() or c == "-")
    # Remove consecutive hyphens
    while "--" in kebab:
        kebab = kebab.replace("--", "-")
    # Remove leading/trailing hyphens
    kebab = kebab.strip("-")
    return f"{kebab}-long-term-memory.md"


def get_captured_sessions() -> set[str]:
    """Get set of already-captured session IDs."""
    captured_file = get_captured_file()
    if not captured_file.exists():
        return set()
    try:
        content = captured_file.read_text(encoding="utf-8")
        return set(line.strip() for line in content.splitlines() if line.strip())
    except IOError:
        return set()


def add_captured_session(session_id: str, captured_set: Optional[set[str]] = None) -> None:
    """
    Add a session ID to the captured list.

    Args:
        session_id: The session ID to add
        captured_set: Optional pre-loaded set to avoid re-reading file
    """
    captured_file = get_captured_file()
    captured_file.parent.mkdir(parents=True, exist_ok=True)

    # Check if already captured (use provided set or load from file)
    if captured_set is not None:
        if session_id in captured_set:
            return
    else:
        captured = get_captured_sessions()
        if session_id in captured:
            return

    # Append to file
    with open(captured_file, "a", encoding="utf-8") as f:
        f.write(f"{session_id}\n")


def remove_captured_session(session_id: str) -> bool:
    """
    Remove a session ID from .captured. Returns True if found and removed.

    Args:
        session_id: The session ID to remove
    """
    captured_file = get_captured_file()
    if not captured_file.exists():
        return False

    try:
        lines = captured_file.read_text(encoding="utf-8").splitlines()
        new_lines = [line for line in lines if line.strip() != session_id]

        if len(new_lines) == len(lines):
            return False  # Not found

        captured_file.write_text("\n".join(new_lines) + "\n" if new_lines else "", encoding="utf-8")
        return True
    except IOError:
        return False


def get_working_days(days_limit: int) -> list[str]:
    """
    Get the most recent N working days (days with daily files).

    This scans existing files rather than iterating calendar dates,
    so days without activity don't count against the limit.
    """
    daily_dir = get_daily_dir()
    if not daily_dir.exists():
        return []

    # Find all daily files and sort by date descending
    daily_files = sorted(
        daily_dir.glob("*.md"),
        key=lambda p: p.stem,
        reverse=True
    )

    # Return the most recent N dates
    return [f.stem for f in daily_files[:days_limit]]


# Regex to extract scope from tagged entries: [scope/type] or [scope]
TAG_PATTERN = re.compile(r"^\s*-\s*\[([^\]/]+)(?:/[^\]]+)?\]")


def filter_daily_content(content: str, scope: str) -> str:
    """
    Filter daily file content to include only entries matching the given scope.

    Args:
        content: Raw markdown content from a daily file
        scope: Either "global" or a project name to filter by

    Returns:
        Filtered content with only matching entries, preserving section structure.
        Returns empty string if no entries match.
    """
    lines = content.split("\n")
    result_lines = []
    current_section = None
    section_lines = []
    section_has_content = False

    def flush_section():
        """Add current section to result if it has content."""
        nonlocal section_lines, section_has_content
        if current_section and section_has_content:
            result_lines.extend(section_lines)
        section_lines = []
        section_has_content = False

    for line in lines:
        # Check for date header (# YYYY-MM-DD)
        if line.startswith("# "):
            flush_section()
            result_lines.append(line)
            current_section = None
            continue

        # Check for section header (## Section)
        if line.startswith("## "):
            flush_section()
            current_section = line
            section_lines = [line]
            continue

        # If we're in a section, process the line
        if current_section:
            # Check if this is a tagged entry
            match = TAG_PATTERN.match(line)
            if match:
                entry_scope = match.group(1).lower()
                # Include if scope matches (case-insensitive)
                if entry_scope == scope.lower():
                    section_lines.append(line)
                    section_has_content = True
            elif line.strip() == "":
                # Keep blank lines within sections that have content
                section_lines.append(line)
            elif not line.strip().startswith("-"):
                # Non-list paragraph text within section - include for global scope only
                if scope.lower() == "global":
                    section_lines.append(line)
                    section_has_content = True
            # Skip untagged list items (treat as needing explicit tag)

    # Flush final section
    flush_section()

    # Clean up: remove trailing empty lines and ensure proper spacing
    while result_lines and result_lines[-1].strip() == "":
        result_lines.pop()

    filtered = "\n".join(result_lines)

    # Only return content if we have more than just the date header
    if filtered.strip() and not re.match(r"^#\s+\d{4}-\d{2}-\d{2}\s*$", filtered.strip()):
        return filtered
    return ""


def find_current_project(projects_index: dict, pwd: str, include_subdirs: bool) -> dict | None:
    """
    Find the project matching the current working directory.

    Returns project dict with 'name', 'originalPath', 'workDays' or None.
    """
    projects = projects_index.get("projects", {})
    pwd_lower = pwd.lower()

    if include_subdirs:
        # Match if PWD starts with any known project path (longest match wins)
        best_match = None
        best_length = 0

        for path_key, project in projects.items():
            if pwd_lower.startswith(path_key) or pwd_lower == path_key:
                if len(path_key) > best_length:
                    best_match = project
                    best_length = len(path_key)

        return best_match
    else:
        # Exact match only
        return projects.get(pwd_lower)


if __name__ == "__main__":
    # Basic self-test
    check_python_version()

    print("Memory Utils Self-Test")
    print("=" * 40)
    print(f"Claude dir:     {get_claude_dir()}")
    print(f"Memory dir:     {get_memory_dir()}")
    print(f"Settings file:  {get_settings_file()}")
    print(f"Global memory:  {get_global_memory_file()}")
    print()

    settings = load_settings()
    print("Settings:")
    print(f"  Global short-term days:  {settings['globalShortTerm']['workingDays']}")
    print(f"  Project short-term days: {settings['projectShortTerm']['workingDays']}")
    print(f"  Token budget:            {settings['totalTokenBudget']}")
    print()

    working_days = get_working_days(7)
    print(f"Recent working days ({len(working_days)}):")
    for day in working_days[:5]:
        print(f"  - {day}")
    if len(working_days) > 5:
        print(f"  ... and {len(working_days) - 5} more")
