#!/usr/bin/env python3
"""
Deferred synthesis runner for systemd timer / launchd agent / SessionEnd hook.

Extracts transcripts, builds synthesis prompt, and invokes
headless ``claude -p`` to run synthesis outside active sessions.

Invoked by:
- systemd timer (Linux): claude-memory-synthesis.timer
- launchd agent (macOS): com.claude.memory-synthesis
- SessionEnd hook: fires on every session exit

Usage:
    python3 synthesis_cron.py           # Normal run (checks schedule)
    python3 synthesis_cron.py --force   # Skip schedule check
"""

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from load_memory import (
    get_last_synthesis_file,
    should_synthesize,
    write_synthesis_prompt,
)
from memory_utils import (
    get_synthesis_error_log,
    get_synthesis_log_file,
    get_synthesis_stats_file,
    load_settings,
)

SYNTHESIS_ERROR_LOG = get_synthesis_error_log()

LOG_ROTATION_BYTES = 500 * 1024  # 500 KB


def rotate_log_if_needed() -> None:
    """Rotate synthesis.log to synthesis.log.1 if it exceeds the size threshold.

    macOS only -- the log path comes from launchd StandardOutPath. On other
    platforms get_synthesis_log_file() returns None and this is a no-op.
    Single-backup rotation: synthesis.log -> synthesis.log.1 (overwrites).

    Note: launchd binds stdout to the log file's inode at agent launch. After
    rotation, the current process still writes to the renamed synthesis.log.1.
    The new synthesis.log is created on the next launchd invocation.
    """
    log_path = get_synthesis_log_file()
    if log_path is None or not log_path.exists():
        return
    try:
        if log_path.stat().st_size > LOG_ROTATION_BYTES:
            rotated = log_path.with_name(log_path.name + ".1")
            log_path.rename(rotated)
    except OSError:
        pass


def _log(message: str, file=None) -> None:
    """Print a timestamped log message to stdout (default) or a given file.

    launchd captures stdout to synthesis.log via StandardOutPath.
    Format: [YYYY-MM-DDTHH:MM:SSZ] message
    """
    if file is None:
        file = sys.stdout
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{timestamp}] {message}", file=file)


def _parse_token_usage(stdout: str) -> tuple[int, int, bool]:
    """Parse token usage from claude --output-format json response.

    Expected shape: {"usage": {"input_tokens": N, "output_tokens": M}, ...}

    Returns:
        (input_tokens, output_tokens, parse_failed); on any parse failure
        returns (0, 0, True).
    """
    try:
        data = json.loads(stdout)
        usage = data["usage"]
        return (int(usage["input_tokens"]), int(usage["output_tokens"]), False)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return (0, 0, True)


def _append_stats(
    prompt_label: str,
    model: str,
    duration_s: float,
    tokens: tuple[int, int],
    status: str,
    error: str | None = None,
    token_parse_failed: bool = False,
) -> None:
    """Append one JSON record to the synthesis stats file.

    Args:
        prompt_label: Date label identifying this synthesis run.
        model: Model name (e.g. "sonnet").
        duration_s: Wall-clock duration in seconds.
        tokens: (input_tokens, output_tokens) tuple.
        status: "ok" or "error".
        error: Error description (only when status="error").
        token_parse_failed: True if --output-format json parsing failed.
    """
    stats_file = get_synthesis_stats_file()
    stats_file.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prompt": prompt_label,
        "model": model,
        "duration_s": round(duration_s, 1),
        "input_tokens": tokens[0],
        "output_tokens": tokens[1],
        "status": status,
    }
    if error is not None:
        record["error"] = error
    if token_parse_failed:
        record["token_parse_failed"] = True
    with open(stats_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def should_run_deferred_synthesis() -> bool:
    """Check if deferred synthesis should run now.

    Returns False if should_synthesize() says it's not time yet (recently ran).
    Returns True if synthesis is due.
    """
    settings = load_settings()
    return should_synthesize(settings)


def build_claude_command(model: str) -> list[str]:
    """Build the claude -p command for headless synthesis.

    The prompt is piped via stdin (not as a positional arg) because
    --allowedTools is variadic and would consume a trailing prompt string.

    Args:
        model: Model name (e.g. "sonnet", "haiku")

    Returns:
        Command list suitable for subprocess.run(cmd, stdin=...).
    """
    return [
        "claude",
        "-p",
        "--no-session-persistence",
        "--model", model,
        "--output-format", "json",
        "--permission-mode", "bypassPermissions",
        "--allowedTools", "Write,Bash,Read",
    ]


def _log_error(message: str) -> None:
    """Append a timestamped error to the synthesis error log.

    SessionStart reads this log and surfaces errors to the user.
    """
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(SYNTHESIS_ERROR_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {message}\n")


def _clear_eager_timestamp() -> None:
    """Remove the eager .last-synthesis timestamp on failure.

    The eager write prevents concurrent runs, but if synthesis crashes
    the stale timestamp blocks retries until the next interval window.
    Clearing it lets the next timer invocation retry immediately.
    """
    ts_file = get_last_synthesis_file()
    if ts_file.exists():
        ts_file.unlink()


def run_synthesis(force: bool = False) -> int:
    """Run the full deferred synthesis pipeline."""
    rotate_log_if_needed()

    if not force and not should_run_deferred_synthesis():
        _log("Synthesis not due (recently ran)")
        return 0

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        write_synthesis_prompt()

    output = captured.getvalue()

    if "No pending" in output:
        _log("No pending transcripts")
        return 0

    model = "sonnet"
    prompt_files = []
    for line in output.strip().splitlines():
        if line.startswith("model="):
            model = line.split("=", 1)[1]
        elif line.startswith("prompt_file="):
            path = line.split("=", 1)[1]
            if Path(path).exists():
                prompt_files.append(path)

    if not prompt_files:
        msg = "No prompt files generated"
        _log(f"Error: {msg}", file=sys.stderr)
        _log_error(msg)
        return 1

    get_last_synthesis_file().write_text(
        datetime.now(timezone.utc).isoformat(),
        encoding="utf-8",
    )

    cmd_base = build_claude_command(model)
    env = os.environ.copy()
    env["CLAUDECODE"] = ""

    failed = False
    for prompt_file in prompt_files:
        date_label = Path(prompt_file).stem
        _log(f"Running synthesis for {date_label} with model={model}")
        t0 = time.monotonic()
        try:
            with open(prompt_file, encoding="utf-8") as f:
                result = subprocess.run(
                    cmd_base,
                    stdin=f,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            duration = time.monotonic() - t0
            msg = f"Synthesis failed for {date_label}: {exc}"
            _log(f"Error: {msg}", file=sys.stderr)
            _log_error(msg)
            try:
                _append_stats(date_label, model, duration, (0, 0), "error", error=str(exc))
            except OSError:
                pass
            failed = True
            continue

        duration = time.monotonic() - t0
        if result.returncode != 0:
            msg = f"claude -p exited {result.returncode} for {date_label}"
            if result.stderr:
                msg += f": {result.stderr[:200]}"
            _log(f"Error: {msg}", file=sys.stderr)
            _log_error(msg)
            try:
                _append_stats(date_label, model, duration, (0, 0), "error", error=msg)
            except OSError:
                pass
            failed = True
            continue

        in_tok, out_tok, parse_failed = _parse_token_usage(result.stdout)
        try:
            _append_stats(date_label, model, duration, (in_tok, out_tok), "ok",
                          token_parse_failed=parse_failed)
        except OSError:
            pass
        if parse_failed:
            _log(f"Synthesis complete for {date_label} ({duration:.1f}s, in=? out=?)")
        else:
            _log(f"Synthesis complete for {date_label} ({duration:.1f}s, in={in_tok}, out={out_tok})")

    if failed:
        return 1

    _log("All synthesis runs complete")
    return 0


def main() -> int:
    """CLI entry point for deferred synthesis."""
    parser = argparse.ArgumentParser(description="Deferred synthesis runner")
    parser.add_argument("--force", action="store_true", help="Skip schedule check")
    args = parser.parse_args()
    return run_synthesis(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
