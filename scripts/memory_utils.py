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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    # Constants
    "MIN_PYTHON",
    "LTM_ENTRY_PATTERN",
    "SHORT_TERM_TOKENS_PER_DAY",
    "DEFAULT_SETTINGS",
    # Version check
    "check_python_version",
    # Datetime
    "to_iso_z",
    "from_iso_z",
    # Path helpers
    "get_claude_dir",
    "get_memory_dir",
    "get_daily_dir",
    "get_project_memory_dir",
    "get_projects_dir",
    "get_settings_file",
    "get_projects_index_file",
    "get_global_memory_file",
    "collect_ltm_files",
    # Settings
    "load_settings",
    "save_settings",
    # JSON I/O
    "load_json_file",
    "save_json_file",
    # Sessions index
    "load_sessions_index",
    "get_sessions_original_path",
    # Content filtering
    "filter_daily_content",
    "find_current_project",
    "get_working_days",
    # Synthesis state
    "get_synthesis_state_file",
    "load_synthesis_state",
    "save_synthesis_state",
    "update_synthesis_state",
    "prune_stale_state_entries",
    # Utilities
    "estimate_tokens",
    "project_name_to_filename",
    "extract_entry_keywords",
    "is_routed_match",
    "FileLock",
]

# Minimum Python version required
MIN_PYTHON = (3, 9)

# Lock configuration
LOCK_STALE_SECONDS = 300  # 5 minutes — locks older than this are considered stale

# =============================================================================
# Key Interfaces
# =============================================================================
# Path helpers:
#   get_memory_dir() -> Path              get_daily_dir() -> Path
#   get_project_memory_dir() -> Path      get_projects_dir() -> Path
#   get_global_memory_file() -> Path      get_settings_file() -> Path
#   get_projects_index_file() -> Path
# Settings:
#   load_settings() -> dict               save_settings(settings) -> None
# Synthesis state:
#   load_synthesis_state() -> dict        save_synthesis_state(state) -> None
#   update_synthesis_state(updates) -> None
#   prune_stale_state_entries(max_age_days?) -> int
# Content:
#   filter_daily_content(content, scope) -> str
#   find_current_project(index, pwd, include_subdirs?) -> dict | None
#   get_working_days(days_limit) -> list[str]
# Utilities:
#   estimate_tokens(text) -> int          FileLock(path, timeout?, poll?)
#   load_json_file(path, default?) -> Any  save_json_file(path, data) -> bool
# Sessions index:
#   load_sessions_index(folder) -> dict    get_sessions_original_path(data) -> str
# =============================================================================


def to_iso_z(dt: datetime) -> str:
    """Convert UTC datetime to ISO string with Z suffix."""
    return dt.isoformat().replace("+00:00", "Z")


def from_iso_z(date_str: str) -> datetime:
    """Parse ISO datetime string, handling both Z and +00:00 suffixes."""
    return datetime.fromisoformat(date_str.replace("Z", "+00:00"))


# Pattern matching LTM dated entries: - (YYYY-MM-DD) [type] description
# Note: decay.py has its own DATE_PATTERN that captures the date group for
# computation. LTM_ENTRY_PATTERN is for detection/matching only.
LTM_ENTRY_PATTERN = re.compile(r"^\s*-\s*\(\d{4}-\d{2}-\d{2}\)")


def collect_ltm_files() -> list[Path]:
    """Collect all LTM files (global + all project files)."""
    files: list[Path] = []
    global_f = get_global_memory_file()
    if global_f.exists():
        files.append(global_f)
    proj_dir = get_project_memory_dir()
    if proj_dir.exists():
        files.extend(proj_dir.glob("*-long-term-memory.md"))
    return files


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
        "tokenLimit": 3000,
    },
    "projectShortTerm": {
        "workingDays": 5,
        # tokenLimit calculated: workingDays × SHORT_TERM_TOKENS_PER_DAY
    },
    "projectLongTerm": {
        "tokenLimit": 3000,
    },
    "projectSettings": {
        "includeSubdirectories": False,
    },
    "synthesis": {
        "intervalHours": 2,
        "model": "haiku",
        "background": True,
    },
    "decay": {
        "ageDays": 30,
        "projectWorkingDays": 20,
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
    global_days = settings.get("globalShortTerm", {}).get(
        "workingDays", DEFAULT_SETTINGS["globalShortTerm"]["workingDays"]
    )
    project_days = settings.get("projectShortTerm", {}).get(
        "workingDays", DEFAULT_SETTINGS["projectShortTerm"]["workingDays"]
    )

    # Calculate short-term limits (setdefault ensures sub-dicts exist)
    settings.setdefault("globalShortTerm", {})["tokenLimit"] = global_days * SHORT_TERM_TOKENS_PER_DAY
    settings.setdefault("projectShortTerm", {})["tokenLimit"] = project_days * SHORT_TERM_TOKENS_PER_DAY

    # Calculate total budget as sum of 4 components
    settings["totalTokenBudget"] = (
        settings.get("globalLongTerm", {}).get("tokenLimit", DEFAULT_SETTINGS["globalLongTerm"]["tokenLimit"]) +
        settings["globalShortTerm"]["tokenLimit"] +
        settings.get("projectLongTerm", {}).get("tokenLimit", DEFAULT_SETTINGS["projectLongTerm"]["tokenLimit"]) +
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


def estimate_tokens(text: str) -> int:
    """
    Estimate token count from text.

    Uses the approximation: 1 token ≈ 4 characters (bytes).
    This is a rough estimate that works reasonably well for English text.
    """
    return len(text) // 4


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

    def _is_owner_alive(self) -> bool:
        """Check if the PID that owns the lock is still running."""
        pid_file = self.lock_path / "pid"
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Signal 0 = check existence, don't kill
            return True
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
            return False

    def _write_pid(self) -> None:
        """Write current PID into the lock directory."""
        pid_file = self.lock_path / "pid"
        try:
            pid_file.write_text(str(os.getpid()))
        except OSError:
            pass

    def _remove_lock_dir(self) -> None:
        """Remove lock directory and its contents."""
        try:
            pid_file = self.lock_path / "pid"
            if pid_file.exists():
                pid_file.unlink()
            self.lock_path.rmdir()
        except OSError:
            pass

    def acquire(self) -> bool:
        """
        Attempt to acquire the lock.

        Returns True if acquired, False if timeout.
        Checks PID liveness first, then falls back to time-based stale detection.
        """
        start_time = time.time()

        while time.time() - start_time < self.timeout:
            try:
                self.lock_path.mkdir(parents=True, exist_ok=False)
                self._write_pid()
                self._acquired = True
                return True
            except FileExistsError:
                # Lock is held — check if owner is still alive
                if not self._is_owner_alive():
                    # Owner process is dead — stale lock
                    self._remove_lock_dir()
                    continue

                # Owner is alive (or PID check inconclusive) — fall back to age check
                try:
                    lock_age = time.time() - self.lock_path.stat().st_mtime
                    if lock_age > LOCK_STALE_SECONDS:
                        self._remove_lock_dir()
                        continue
                except OSError:
                    pass

                time.sleep(self.poll_interval)

        return False

    def release(self) -> None:
        """Release the lock."""
        if self._acquired:
            self._remove_lock_dir()
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


def load_sessions_index(folder_path: Path) -> dict:
    """
    Load and parse sessions-index.json from a Claude Code project folder.

    Returns the parsed JSON dict, or empty dict if missing/invalid.
    """
    return load_json_file(folder_path / "sessions-index.json", default={})


def get_sessions_original_path(data: dict) -> str:
    """
    Extract the original project path from parsed sessions-index data.

    Tries root-level 'originalPath' first (legacy/manual format),
    falls back to entries[0].projectPath (Claude Code's actual format).

    Returns empty string if not found.
    """
    original_path = data.get("originalPath", "")
    if not original_path:
        entries = data.get("entries", [])
        if entries:
            original_path = entries[0].get("projectPath", "")
    return original_path


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
    kebab = re.sub(r"-+", "-", kebab)
    # Remove leading/trailing hyphens
    kebab = kebab.strip("-")
    return f"{kebab}-long-term-memory.md"


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
            # Skip HTML comments (template hints, not useful at load time)
            if re.match(r"^\s*<!--.*-->\s*$", line):
                continue

            # Skip entries marked as routed to LTM
            if re.match(r"^\s*-\s*\[routed\]", line):
                continue

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


# Stopwords for keyword extraction (common English words that don't help matching)
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "that", "this", "these",
    "those", "not", "no", "nor", "or", "and", "but", "if", "so", "than",
    "too", "very", "just", "about", "up", "it", "its", "use", "when",
})

# Regex to strip tag prefixes: [routed], [scope/type], (YYYY-MM-DD)
_ENTRY_PREFIX_PATTERN = re.compile(
    r"^\s*-\s*(?:\[routed\])?\s*(?:\[[^\]]+\])?\s*(?:\(\d{4}-\d{2}-\d{2}\))?\s*(?:\[[^\]]+\])?\s*"
)


def extract_entry_keywords(entry: str) -> set[str]:
    """
    Extract meaningful keywords from a memory entry line.

    Strips tag prefixes ([scope/type], [routed], (date)), stopwords,
    and short tokens. Returns lowercase keyword set.
    """
    # Remove tag/date prefixes
    text = _ENTRY_PREFIX_PATTERN.sub("", entry)
    # Tokenize: split on non-alphanumeric, lowercase
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    # Filter stopwords and short tokens
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 2}


def is_routed_match(stm_entry: str, ltm_entry: str, threshold: float = 0.5) -> bool:
    """
    Check if a short-term memory entry matches a long-term memory entry.

    Uses keyword overlap: if >= threshold of the smaller set's keywords
    appear in the larger set, it's a match.

    Args:
        stm_entry: Daily file entry line (e.g., "- [scope/type] Description")
        ltm_entry: LTM entry line (e.g., "- (2026-02-12) [type] Description")
        threshold: Minimum overlap ratio (0.0-1.0) to consider a match

    Returns:
        True if entries are conceptual duplicates
    """
    stm_kw = extract_entry_keywords(stm_entry)
    ltm_kw = extract_entry_keywords(ltm_entry)

    if not stm_kw or not ltm_kw:
        return False

    overlap = len(stm_kw & ltm_kw)
    smaller = min(len(stm_kw), len(ltm_kw))

    return overlap / smaller >= threshold


def get_synthesis_state_file() -> Path:
    """Get the .synthesis-state.json file path."""
    return get_memory_dir() / ".synthesis-state.json"


def load_synthesis_state() -> dict:
    """Load synthesis state (high water marks per session)."""
    state_file = get_synthesis_state_file()
    data = load_json_file(state_file, default={"sessions": {}})
    if "sessions" not in data:
        data["sessions"] = {}
    return data


def save_synthesis_state(state: dict) -> None:
    """Save synthesis state atomically (write to tmp, rename).

    Not using save_json_file because we need atomic rename (write tmp + rename).
    """
    state_file = get_synthesis_state_file()
    tmp_file = state_file.with_suffix(".tmp")
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp_file.rename(state_file)


def update_synthesis_state(session_updates: dict[str, dict]) -> None:
    """Update synthesis state with new offsets for given sessions.

    Args:
        session_updates: Dict mapping session_id -> {"offset": int, "lines": int}
    """
    state = load_synthesis_state()
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for sid, info in session_updates.items():
        state["sessions"][sid] = {
            "offset": info["offset"],
            "lines": info["lines"],
            "last_synthesized": now_iso,
        }
    save_synthesis_state(state)


def prune_stale_state_entries(max_age_days: int = 7) -> int:
    """Remove state entries for sessions older than max_age_days or missing from disk.

    Scans .synthesis-state.json and removes entries where the session's .jsonl
    file has mtime older than max_age_days or no longer exists on disk.

    Returns number of entries pruned.
    """
    state = load_synthesis_state()
    sessions = state.get("sessions", {})
    if not sessions:
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_days * 86400)
    projects_dir = get_projects_dir()
    to_remove = []

    for sid in sessions:
        # Find the session file across project dirs
        found = False
        if projects_dir.exists():
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                session_file = proj_dir / f"{sid}.jsonl"
                if session_file.exists():
                    found = True
                    if session_file.stat().st_mtime < cutoff:
                        to_remove.append(sid)
                    break
        if not found:
            to_remove.append(sid)

    for sid in to_remove:
        sessions.pop(sid, None)

    if to_remove:
        save_synthesis_state(state)

    return len(to_remove)


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
