#!/usr/bin/env python3
"""
Injection log — JSONL append logger for memory injection diagnostics.

Logs SessionStart (load_memory) and UserPromptSubmit (prompt_recall) hook
invocations to a single rotating JSONL file. Fire-and-forget: all public
functions catch exceptions silently so they never break callers.

Requirements: Python 3.9+
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path setup — allow running from scripts/ or project root
# ---------------------------------------------------------------------------
_script_dir = Path(__file__).resolve().parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

from memory_utils import FileLock, get_memory_dir, load_settings  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LOG_FILENAME = ".injection-log.jsonl"
MAX_LINES = 500
KEEP_LINES = 200
CONTENT_PREVIEW_LENGTH = 80


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def get_log_path() -> Path:
    """Return the path to the injection log JSONL file."""
    return get_memory_dir() / LOG_FILENAME


def _is_enabled() -> bool:
    """Check whether injection logging is enabled in settings.

    Defaults to True when the setting is absent or on any error.
    """
    try:
        settings = load_settings()
        return settings.get("injectionLog", {}).get("enabled", True)
    except Exception:
        return True


def _append_entry(entry: dict) -> None:
    """Append a single JSON line to the log file.

    Creates parent directories if needed. Raises on error (callers catch).
    """
    log_path = get_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, default=str) + "\n")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def log_session_start(
    session_id: str,
    project_scope: str,
    tiers: list,
    latency_ms: float,
    health_alerts: list | None = None,
) -> None:
    """Log a SessionStart hook invocation.

    Args:
        session_id: Claude session identifier.
        project_scope: Project path or scope string.
        tiers: List of dicts with name, count, tokens_est, ids.
        latency_ms: Hook execution time in milliseconds.
        health_alerts: Optional list of health alert strings.
    """
    try:
        if not _is_enabled():
            return

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "hook": "SessionStart",
            "project_scope": project_scope,
            "tiers": tiers,
            "total_items": sum(t.get("count", 0) for t in tiers),
            "total_tokens_est": sum(t.get("tokens_est", 0) for t in tiers),
            "latency_ms": round(latency_ms, 1),
            "health_alerts": health_alerts or [],
        }
        _append_entry(entry)
    except Exception:
        pass


def log_prompt_recall(
    session_id: str,
    prompt_preview: str,
    candidates: int,
    injected: list,
    filtered: list,
    latency_ms: float,
) -> None:
    """Log a UserPromptSubmit hook invocation (prompt recall).

    Args:
        session_id: Claude session identifier.
        prompt_preview: Truncated prompt text for diagnostics.
        candidates: Number of candidate memories considered.
        injected: List of data_point IDs that were injected.
        filtered: List of data_point IDs that were filtered out.
        latency_ms: Hook execution time in milliseconds.
    """
    try:
        if not _is_enabled():
            return

        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "session_id": session_id,
            "hook": "UserPromptSubmit",
            "prompt_preview": prompt_preview[:CONTENT_PREVIEW_LENGTH],
            "candidates": candidates,
            "injected": injected,
            "filtered": filtered,
            "latency_ms": round(latency_ms, 1),
        }
        _append_entry(entry)
    except Exception:
        pass


def rotate_log(
    max_lines: int = MAX_LINES,
    keep_lines: int = KEEP_LINES,
) -> None:
    """Rotate the log file if it exceeds max_lines.

    Keeps the most recent keep_lines lines, discarding older entries.
    """
    try:
        log_path = get_log_path()
        if not log_path.exists():
            return

        lock = FileLock(log_path.parent / ".injection-log.lock", timeout=2)
        if not lock.acquire():
            return
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines(keepends=True)
            if len(lines) <= max_lines:
                return
            log_path.write_text("".join(lines[-keep_lines:]), encoding="utf-8")
        finally:
            lock.release()
    except Exception:
        pass


def read_log(
    since: datetime | None = None,
    session_id: str | None = None,
    max_entries: int = 500,
) -> list[dict]:
    """Read and filter recent injection log entries.

    Args:
        since: Only include entries after this timestamp.
            Defaults to 1 hour ago.
        session_id: Optional filter for a specific session.
        max_entries: Maximum number of entries to return.

    Returns:
        List of parsed log entry dicts, up to max_entries.
    """
    try:
        log_path = get_log_path()
        if not log_path.exists():
            return []

        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=1)

        results: list[dict] = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            ts_str = entry.get("ts", "")
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue

            if ts <= since:
                continue

            if session_id is not None and entry.get("session_id") != session_id:
                continue

            results.append(entry)

        return results[-max_entries:]
    except Exception:
        return []
