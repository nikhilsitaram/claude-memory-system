#!/usr/bin/env python3
"""
Shared utilities for Claude Code Memory System.

Provides cross-platform path handling, settings management, token estimation,
and file locking. Used by load_memory.py, indexing.py, and other scripts.

Requirements: Python 3.9+
"""

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    # Constants
    "MIN_PYTHON",
    "LTM_ENTRY_PATTERN",
    "DEFAULT_SETTINGS",
    # Version check
    "check_python_version",
    # Datetime
    "local_today",
    "utc_to_local_datestr",
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
    "get_synthesis_error_log",
    "get_db_path",
    "collect_ltm_files",
    "resolve_worktree_to_main_repo",
    "resolve_git_subdir_to_root",
    "resolve_session_path",
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
    "get_global_working_days",
    "get_project_working_days",
    "_clear_working_days_cache",
    # Synthesis state
    "get_synthesis_state_file",
    "load_synthesis_state",
    "save_synthesis_state",
    "update_synthesis_state",
    "prune_stale_state_entries",
    # Project resolution
    "resolve_project_path_to_name",
    # Markdown parsing
    "parse_markdown_sections",
    # Utilities
    "estimate_tokens",
    "project_name_to_filename",
    "extract_entry_keywords",
    "is_routed_match",
    "rebuild_projects_index_quiet",
    "FileLock",
    "sanitize_secrets",
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
# Path resolution:
#   resolve_worktree_to_main_repo(path) -> str
#   resolve_git_subdir_to_root(path) -> str
#   resolve_session_path(path) -> str
# Settings:
#   load_settings() -> dict               save_settings(settings) -> None
# Synthesis state:
#   load_synthesis_state() -> dict        save_synthesis_state(state) -> None
#   update_synthesis_state(updates) -> None
#   prune_stale_state_entries(max_age_days?) -> int
# Content:
#   filter_daily_content(content, scope) -> str
#   find_current_project(index, pwd) -> dict | None
# Utilities:
#   estimate_tokens(text) -> int          FileLock(path, timeout?, poll?)
#   load_json_file(path, default?) -> Any  save_json_file(path, data) -> bool
# Sessions index:
#   load_sessions_index(folder) -> dict    get_sessions_original_path(data) -> str
# =============================================================================


def local_today() -> date:
    """Get today's date in local timezone."""
    return datetime.now().date()


def utc_to_local_datestr(dt: datetime) -> str:
    """Convert a UTC-aware datetime to a local-timezone date string (YYYY-MM-DD)."""
    return dt.astimezone().strftime("%Y-%m-%d")


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



def get_projects_index_file() -> Path:
    """Get the projects index file path."""
    return get_memory_dir() / "projects-index.json"


def get_global_memory_file() -> Path:
    """Get the global long-term memory file."""
    return get_memory_dir() / "global-long-term-memory.md"


def get_synthesis_error_log() -> Path:
    """Get the synthesis error log file path."""
    return get_memory_dir() / ".synthesis-errors.log"


def get_db_path() -> Path:
    """Get the path to the unified memory database (memory.db)."""
    return get_memory_dir() / "memory.db"


DEFAULT_SETTINGS = {
    "version": 3,
    "globalLongTerm": {
        "tokenLimit": 3000,
    },
    "projectLongTerm": {
        "tokenLimit": 3000,
    },
    "synthesis": {
        "intervalHours": 0.5,
        "model": "sonnet",
        "background": True,
        "deferred": True,
        "minSessionMessages": 10,
        "recentWorkingDays": 7,
        "backfill": {
            "recentWorkingDays": 7,
        },
    },
    "decay": {
        "ageDays": 30,
        "projectWorkingDays": 20,
    },
    "consolidation": {
        "intervalHours": 24,
        "minMemories": 5,
        "similarityThreshold": 0.80,
        "maxClusters": 15,
        "backfillMaxClusters": 30,
        "model": "sonnet",
    },
    "recall": {
        "maxPromptLength": 500,
        "minPromptLength": 15,
        "maxInjectionsPerPrompt": 3,
        "maxTokenBudget": 500,
    },
    # totalTokenBudget calculated as globalLongTerm + projectLongTerm
}


def load_settings() -> dict[str, Any]:
    """
    Load memory settings from settings.json with defaults.

    Returns settings dict with all expected keys populated.
    totalTokenBudget is calculated as globalLongTerm + projectLongTerm.
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

    # Calculate total budget from long-term components
    settings["totalTokenBudget"] = (
        settings.get("globalLongTerm", {}).get("tokenLimit", DEFAULT_SETTINGS["globalLongTerm"]["tokenLimit"]) +
        settings.get("projectLongTerm", {}).get("tokenLimit", DEFAULT_SETTINGS["projectLongTerm"]["tokenLimit"])
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


def rebuild_projects_index_quiet() -> None:
    """Rebuild projects-index.json, suppressing output. Best-effort."""
    try:
        from indexing import build_projects_index

        with contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            build_projects_index()
    except Exception:
        pass
    _clear_projects_index_cache()
    _clear_working_days_cache()


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


# =============================================================================
# Working Days from JSONL Session Files
# =============================================================================

_working_days_cache: dict[str, list[str]] = {}
_projects_index_for_working_days: dict | None = None


def _clear_working_days_cache() -> None:
    """Clear the working days cache (for testing and between sessions)."""
    global _projects_index_for_working_days
    _working_days_cache.clear()
    _projects_index_for_working_days = None


def get_global_working_days(n: int) -> list[str]:
    """Get the most recent N working days across all projects.

    Scans .jsonl file mtimes in ``~/.claude/projects/`` to determine which
    calendar dates had session activity.  Results are cached in the
    module-level ``_working_days_cache`` dict.

    Args:
        n: Maximum number of dates to return.

    Returns:
        List of date strings (``YYYY-MM-DD``), sorted newest-first.
    """
    cache_key = f"global:{n}"
    if cache_key in _working_days_cache:
        return _working_days_cache[cache_key]

    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        _working_days_cache[cache_key] = []
        return []

    dates: set[str] = set()
    for project_folder in projects_dir.iterdir():
        if not project_folder.is_dir():
            continue
        for jsonl_file in project_folder.glob("*.jsonl"):
            mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=timezone.utc)
            dates.add(utc_to_local_datestr(mtime))

    result = sorted(dates, reverse=True)[:n]
    _working_days_cache[cache_key] = result
    return result


def _load_projects_index_for_working_days() -> dict:
    """Load projects-index.json for working day lookups (cached separately)."""
    global _projects_index_for_working_days
    if _projects_index_for_working_days is not None:
        return _projects_index_for_working_days
    index_file = get_projects_index_file()
    raw = load_json_file(index_file, default={})
    _projects_index_for_working_days = raw.get("projects", {})
    return _projects_index_for_working_days


def get_project_working_days(project_scope: str, n: int) -> list[str]:
    """Get the most recent N working days for a specific project.

    Looks up project folders via ``projects-index.json`` (matching on
    project name), including worktree variants.  Falls back to suffix
    matching on folder names if the index has no match.

    Args:
        project_scope: Project name (as stored in projects-index).
        n: Maximum number of dates to return.

    Returns:
        List of date strings (``YYYY-MM-DD``), sorted newest-first.
    """
    cache_key = f"project:{project_scope}:{n}"
    if cache_key in _working_days_cache:
        return _working_days_cache[cache_key]

    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        _working_days_cache[cache_key] = []
        return []

    # Look up matching folders from projects-index.json
    index = _load_projects_index_for_working_days()
    matching_folders: list[Path] = []
    for _proj_path, info in index.items():
        if info.get("name") == project_scope:
            for encoded_path in info.get("encodedPaths", []):
                folder_path = projects_dir / encoded_path
                if folder_path.exists():
                    matching_folders.append(folder_path)

    # Fallback: suffix match on folder name
    if not matching_folders:
        for project_folder in projects_dir.iterdir():
            if project_folder.is_dir() and project_folder.name.endswith(f"-{project_scope}"):
                matching_folders.append(project_folder)

    dates: set[str] = set()
    for folder in matching_folders:
        for jsonl_file in folder.glob("*.jsonl"):
            mtime = datetime.fromtimestamp(jsonl_file.stat().st_mtime, tz=timezone.utc)
            dates.add(utc_to_local_datestr(mtime))

    result = sorted(dates, reverse=True)[:n]
    _working_days_cache[cache_key] = result
    return result


# Regex to extract scope(s) from tagged entries: [scope/type] or [scope1|scope2/type]
TAG_PATTERN = re.compile(r"^\s*-\s*\[([^\]/]+(?:\|[^\]/]+)*)(?:/[^\]]+)?\]")

# Pre-compiled patterns for filter_daily_content hot path (runs every SessionStart)
_COMMENT_LINE_RE = re.compile(r"^\s*<!--.*-->\s*$")
_ROUTED_PREFIX_RE = re.compile(r"^\s*-\s*\[routed\]")
_DATE_ONLY_RE = re.compile(r"^#\s+\d{4}-\d{2}-\d{2}\s*$")


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
            if _COMMENT_LINE_RE.match(line):
                continue

            # Skip entries marked as routed to LTM
            if _ROUTED_PREFIX_RE.match(line):
                continue

            # Check if this is a tagged entry
            match = TAG_PATTERN.match(line)
            if match:
                entry_scopes = [s.lower() for s in match.group(1).split("|")]
                # Include if any scope matches (case-insensitive)
                if scope.lower() in entry_scopes:
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
    if filtered.strip() and not _DATE_ONLY_RE.match(filtered.strip()):
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


def _worktree_pattern_fallback(path: str) -> str:
    """Resolve a worktree path using directory convention.

    Fallback for when git is unavailable (directory deleted, git not installed).
    Checks for both /.claude/worktrees/ (Claude Code convention) and
    /.worktrees/ (legacy convention), returning everything before the marker.
    """
    for marker in ("/.claude/worktrees/", "/.worktrees/"):
        idx = path.find(marker)
        if idx != -1:
            return path[:idx]
    return path


def resolve_worktree_to_main_repo(path: str) -> str:
    """Resolve a git worktree path to its main repository root.

    Uses git rev-parse to detect if the given path is inside a worktree.
    If so, returns the main repository root. Otherwise returns the
    original path unchanged. Falls back to /.worktrees/ path pattern
    when git is unavailable (e.g. deleted worktree directories).
    """
    try:
        toplevel_result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if toplevel_result.returncode != 0:
            return _worktree_pattern_fallback(path)
        toplevel = toplevel_result.stdout.strip()
        if not toplevel:
            return _worktree_pattern_fallback(path)

        common_result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
        if common_result.returncode != 0:
            return _worktree_pattern_fallback(path)
        common_dir = common_result.stdout.strip()
        if not common_dir:
            return _worktree_pattern_fallback(path)

        # common_dir is the main repo's .git/ directory
        # Its parent is the main repo root
        main_repo_root = str(Path(common_dir).parent)

        if main_repo_root != toplevel:
            # We're in a worktree — return the main repo root
            return main_repo_root

        # Git says not a worktree, but path may use /.worktrees/ convention
        # (e.g. subdirectory within repo named .worktrees/)
        return _worktree_pattern_fallback(path)
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return _worktree_pattern_fallback(path)


def resolve_git_subdir_to_root(path: str) -> str:
    """Resolve a git subdirectory to its repository root.

    If path is inside a git repo but is not the root:
      - If the relative path is gitignored -> return path unchanged (separate project)
      - If not gitignored -> return git root (collapse to parent project)

    If path IS the git root, or not in a git repo, returns unchanged.
    Falls back to returning path unchanged on any error.
    """
    try:
        toplevel_result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if toplevel_result.returncode != 0:
            return path
        toplevel = toplevel_result.stdout.strip()
        if not toplevel:
            return path

        # Normalize both paths for comparison
        norm_path = os.path.normpath(path)
        norm_toplevel = os.path.normpath(toplevel)

        if norm_path == norm_toplevel:
            return norm_toplevel  # Already at git root

        # Compute relative path from git root
        rel_path = os.path.relpath(norm_path, norm_toplevel)

        # Check if relative path is gitignored
        ignore_result = subprocess.run(
            ["git", "-C", norm_toplevel, "check-ignore", "-q", rel_path],
            capture_output=True, text=True, timeout=5,
        )

        if ignore_result.returncode == 0:
            # Path IS gitignored — keep as separate project
            return norm_path
        elif ignore_result.returncode == 1:
            # Path is NOT gitignored — collapse to git root
            return norm_toplevel
        else:
            # Unexpected error from check-ignore
            return path
    except (FileNotFoundError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired, OSError):
        return path


def resolve_session_path(path: str) -> str:
    """Full resolution chain: worktree -> git-subdir -> result.

    Applies both resolution steps in order:
    1. resolve_worktree_to_main_repo — handles git worktrees
    2. resolve_git_subdir_to_root — handles non-root subdirs of git repos
    """
    path = resolve_worktree_to_main_repo(path)
    path = resolve_git_subdir_to_root(path)
    return path


def find_current_project(projects_index: dict, pwd: str) -> dict | None:
    """
    Find the project matching the current working directory.

    Uses exact match only. Subdirectory resolution is handled upstream
    by resolve_session_path() before this function is called.

    Returns project dict with 'name', 'originalPath', 'workDays' or None.
    """
    projects = projects_index.get("projects", {})
    pwd_lower = pwd.lower()
    return projects.get(pwd_lower)


# Cache for resolve_project_path_to_name to avoid repeated file reads
_projects_index_cache: dict | None = None


def resolve_project_path_to_name(
    project_path: str | None,
    project_hash: str | None = None,
) -> str | None:
    """Resolve a project path or encoded hash to a project name.

    Loads projects-index.json (cached) and resolves using three strategies:
    1. Direct path lookup via project_path
    2. Encoded folder name match via project_hash against encodedPaths
    3. Worktree prefix match when hash contains ``--worktrees-``

    Args:
        project_path: Original filesystem path (e.g., "/home/user/myproject")
        project_hash: Encoded folder name (e.g., "-home-user-myproject")

    Returns:
        Project name string, or None if not found.
    """
    global _projects_index_cache

    if not project_path and not project_hash:
        return None

    try:
        if _projects_index_cache is None:
            _projects_index_cache = load_json_file(get_projects_index_file(), {})
        projects = _projects_index_cache.get("projects", {})

        # Primary: direct path lookup (resolve worktree paths first)
        if project_path:
            resolved = _worktree_pattern_fallback(project_path)
            for try_path in dict.fromkeys([resolved, project_path]):
                data = projects.get(try_path) or projects.get(try_path.lower())
                if data and data.get("name"):
                    return data["name"]

        # Fallback 1: match encoded folder name against encodedPaths
        # For worktree hashes, try prefix match first to resolve to parent
        # project before falling back to exact match on the worktree's own entry.
        if project_hash:
            is_worktree_hash = ("--claude-worktrees-" in project_hash
                                or "--worktrees-" in project_hash)

            if is_worktree_hash:
                # Prefix match: resolve worktree hash to parent project
                for separator in ("--claude-worktrees-", "--worktrees-"):
                    base = project_hash.rsplit(separator, 1)[0]
                    if base != project_hash:
                        for _path, data in projects.items():
                            for ep in data.get("encodedPaths", []):
                                ep_base = ep.rsplit(separator, 1)[0]
                                if base == ep_base:
                                    return data.get("name")
                        break  # matched separator but no parent found

            # Exact match against encodedPaths
            for _path, data in projects.items():
                if project_hash in data.get("encodedPaths", []):
                    return data.get("name")
    except Exception:
        pass
    return None


def _clear_projects_index_cache() -> None:
    """Clear the projects index cache (for testing)."""
    global _projects_index_cache
    _projects_index_cache = None


# =============================================================================
# Markdown Parsing
# =============================================================================


def parse_markdown_sections(content: str) -> list[tuple[str, list[str]]]:
    """Parse markdown content into sections split by ``## `` headers.

    Returns a list of ``(header, content_lines)`` tuples. Content before
    the first ``## `` header is returned with an empty-string header.

    Args:
        content: Raw markdown text.

    Returns:
        List of (header, content_lines) where header is the full ``## ...``
        line (stripped) or ``""`` for the preamble, and content_lines is a
        list of individual lines (without trailing newline).
    """
    sections: list[tuple[str, list[str]]] = []
    current_header = ""
    current_lines: list[str] = []

    for line in content.split("\n"):
        if line.startswith("## "):
            if current_header or current_lines:
                sections.append((current_header, current_lines))
            current_header = line.strip()
            current_lines = []
        else:
            current_lines.append(line)

    if current_header or current_lines:
        sections.append((current_header, current_lines))

    return sections


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
    print(f"  Global LTM token limit:  {settings['globalLongTerm']['tokenLimit']}")
    print(f"  Project LTM token limit: {settings['projectLongTerm']['tokenLimit']}")
    print(f"  Token budget:            {settings['totalTokenBudget']}")


# =============================================================================
# Secret sanitization
# =============================================================================

import re as _re

_SECRET_PATTERNS = [
    (_re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws_key]"),
    (_re.compile(r"sk_live_[a-zA-Z0-9]{20,}"), "[REDACTED:api_key]"),
    (_re.compile(r"sk_test_[a-zA-Z0-9]{20,}"), "[REDACTED:api_key]"),
    (_re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "[REDACTED:api_key]"),
    (_re.compile(r"(postgres|mysql|mongodb)://[^\s]+"), "[REDACTED:connection_string]"),
    (_re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"), "[REDACTED:jwt]"),
    (_re.compile(r"-----BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY-----"), "[REDACTED:private_key]"),
    (_re.compile(r"(token|password|secret|apikey)\s*[=:]\s*['\"][^\s'\"]{8,}['\"]", _re.IGNORECASE), "[REDACTED:secret_assignment]"),
]


def sanitize_secrets(text: str) -> str:
    """Detect and redact common secret patterns from text.

    Replaces matches with [REDACTED:<type>] markers.

    Args:
        text: Input text that may contain secrets.

    Returns:
        Text with secrets replaced by redaction markers.
    """
    if not text:
        return text
    result = text
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
