#!/usr/bin/env python3
"""
Shared utilities for Claude Code Memory System.

Provides cross-platform path handling, settings management, token estimation,
and file locking. Used by load_memory.py, indexing.py, and other scripts.

Requirements: Python 3.9+
"""

import contextlib
import copy
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
    "SHORT_TERM_TOKENS_PER_DAY",
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
    "get_pending_recall_dir",
    "get_synthesis_error_log",
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
    "get_working_days",
    # Synthesis state
    "get_synthesis_state_file",
    "load_synthesis_state",
    "save_synthesis_state",
    "update_synthesis_state",
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
# Content:
#   filter_daily_content(content, scope) -> str
#   find_current_project(index, pwd) -> dict | None
#   get_working_days(days_limit) -> list[str]
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


def get_pending_recall_dir() -> Path:
    """Get the path to the pending-recall directory."""
    return get_memory_dir() / "pending-recall"


def get_synthesis_error_log() -> Path:
    """Get the synthesis error log file path."""
    return get_memory_dir() / ".synthesis-errors.log"


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
    "synthesis": {
        "intervalHours": 0.5,
        "model": "sonnet",
        "minSessionMessages": 5,
    },
    "decay": {
        "ageDays": 30,
        "projectWorkingDays": 20,
        "archiveRetentionDays": 365,
    },
    "previousSessionRecall": {
        "enabled": True,
        "tokenLimit": 1500,
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



def _worktree_pattern_fallback(path: str) -> str:
    """Resolve a worktree path using worktree directory conventions.

    Fallback for when git is unavailable (directory deleted, git not installed).
    Checks for both /.claude/worktrees/ and /.worktrees/ patterns.
    """
    parts = Path(path).parts
    markers = ((".claude", "worktrees"), (".worktrees",))
    for marker in markers:
        mlen = len(marker)
        for i in range(len(parts) - mlen + 1):
            if parts[i : i + mlen] == marker:
                return str(Path(*parts[:i]))
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


def _normalize_projects_keys(projects: dict) -> dict:
    """Build a lowercase-keyed lookup dict from projects, merging case variants."""
    normalized: dict = {}
    for key, value in projects.items():
        lower_key = key.lower()
        if lower_key in normalized:
            # Merge workDays from case variant
            existing_days = set(normalized[lower_key].get("workDays", []))
            existing_days.update(value.get("workDays", []))
            normalized[lower_key]["workDays"] = sorted(existing_days)
            for ep in value.get("encodedPaths", []):
                if ep not in normalized[lower_key].get("encodedPaths", []):
                    normalized[lower_key].setdefault("encodedPaths", []).append(ep)
        else:
            normalized[lower_key] = copy.deepcopy(value)
    return normalized


def find_current_project(projects_index: dict, pwd: str) -> dict | None:
    """
    Find the project matching the current working directory.

    Two-tier lookup:
    1. Direct path match (case-insensitive)
    2. Basename match — if pwd's basename matches exactly one project name
       whose indexed path doesn't exist on disk (stale path from platform
       migration). Updates the index entry in-place when matched.

    Subdirectory resolution is handled upstream by resolve_session_path().

    Returns project dict with 'name', 'originalPath', 'workDays' or None.
    """
    projects = _normalize_projects_keys(projects_index.get("projects", {}))

    # Tier 1: direct path match
    result = projects.get(pwd.lower())
    if result is not None:
        return result

    # Tier 2: basename match against stale entries
    basename = Path(pwd).name.lower()
    candidates = []
    for key, data in projects.items():
        if data.get("name", "").lower() == basename:
            original = data.get("originalPath", "")
            if original and not Path(original).exists():
                candidates.append((key, data))

    if len(candidates) == 1:
        key, data = candidates[0]
        old_path = data.get("originalPath", "")
        # Update the entry to the current path
        data["originalPath"] = pwd
        data["name"] = Path(pwd).name
        # Also update the raw index so the fix persists on next write
        raw_projects = projects_index.get("projects", {})
        for raw_key, raw_data in list(raw_projects.items()):
            if raw_key.lower() == key:
                raw_projects[pwd.lower()] = raw_data
                raw_data["originalPath"] = pwd
                raw_data["name"] = Path(pwd).name
                if raw_key.lower() != pwd.lower():
                    del raw_projects[raw_key]
                break
        # Persist the corrected index to disk
        _persist_projects_index(projects_index)
        print(
            f"Auto-corrected stale project path: {old_path} -> {pwd}",
            file=sys.stderr,
        )
        return data

    return None


def _persist_projects_index(projects_index: dict) -> None:
    """Write corrected projects index back to disk."""
    output_file = get_projects_index_file()
    projects_index["lastUpdated"] = to_iso_z(datetime.now(timezone.utc))
    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(projects_index, f, indent=2)
    except IOError:
        pass


# Cache for resolve_project_path_to_name to avoid repeated file reads
_projects_index_cache: dict | None = None
_normalized_projects_cache: dict | None = None


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
    global _projects_index_cache, _normalized_projects_cache

    if not project_path and not project_hash:
        return None

    try:
        if _projects_index_cache is None:
            _projects_index_cache = load_json_file(get_projects_index_file(), {})
            _normalized_projects_cache = None  # invalidate on reload
        cache: dict = _projects_index_cache or {}
        if _normalized_projects_cache is None:
            _normalized_projects_cache = _normalize_projects_keys(cache.get("projects", {}))
        projects = _normalized_projects_cache

        # Primary: direct path lookup (case-insensitive)
        if project_path:
            data = projects.get(project_path.lower())
            if data and data.get("name"):
                return data["name"]

        # Fallback 1: match encoded folder name against encodedPaths
        if project_hash:
            for _path, data in projects.items():
                if project_hash in data.get("encodedPaths", []):
                    return data.get("name")

            # Fallback 2: prefix match for unindexed worktrees
            base = project_hash.rsplit("--worktrees-", 1)[0]
            if base != project_hash:  # only if hash contains --worktrees-
                for _path, data in projects.items():
                    for ep in data.get("encodedPaths", []):
                        ep_base = ep.rsplit("--worktrees-", 1)[0]
                        if base == ep_base:
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
