#!/usr/bin/env python3
"""
Deferred synthesis runner for systemd timer / SessionEnd hook.

Extracts transcripts, builds synthesis prompt, and invokes
headless ``claude -p`` to run synthesis outside active sessions.

Usage:
    python3 synthesis_cron.py           # Normal run (checks schedule)
    python3 synthesis_cron.py --force   # Skip schedule check
"""

import argparse
import io
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from load_memory import (
    get_last_synthesis_file,
    should_synthesize,
    write_synthesis_prompt,
)
from memory_utils import load_settings


def should_run_deferred_synthesis() -> bool:
    """Check if deferred synthesis should run now.

    Returns False if:
    - synthesis.deferred is not True
    - should_synthesize() says it's not time yet (recently ran)

    Returns True if deferred mode is enabled and synthesis is due.
    """
    settings = load_settings()
    if not settings.get("synthesis", {}).get("deferred", False):
        return False
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
        "--permission-mode", "bypassPermissions",
        "--allowedTools", "Write,Bash,Read",
    ]


def run_synthesis(force: bool = False) -> int:
    """Run the full deferred synthesis pipeline.

    Args:
        force: If True, skip the schedule check.

    Returns:
        0 on success or nothing to do, 1 on failure.
    """
    if not force and not should_run_deferred_synthesis():
        print("Synthesis not due (deferred=false or recently ran)")
        return 0

    # Capture write_synthesis_prompt output to parse model + prompt_file
    old_stdout = sys.stdout
    sys.stdout = captured = io.StringIO()
    try:
        write_synthesis_prompt()
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()

    if "No pending" in output:
        print("No pending transcripts")
        return 0

    # Parse model=<model> and prompt_file=<path> from output
    model = "sonnet"
    prompt_file = None
    for line in output.strip().splitlines():
        if line.startswith("model="):
            model = line.split("=", 1)[1]
        elif line.startswith("prompt_file="):
            prompt_file = line.split("=", 1)[1]

    if not prompt_file or not Path(prompt_file).exists():
        print("Error: No prompt file generated", file=sys.stderr)
        return 1

    # Write eager timestamp to prevent concurrent runs
    get_last_synthesis_file().write_text(
        datetime.now(timezone.utc).isoformat(),
        encoding="utf-8",
    )

    # Build and run claude -p (prompt piped via stdin)
    cmd = build_claude_command(model)
    env = os.environ.copy()
    env["CLAUDECODE"] = ""  # Unset nesting guard

    print(f"Running synthesis with model={model}")
    try:
        with open(prompt_file, encoding="utf-8") as f:
            result = subprocess.run(
                cmd,
                stdin=f,
                env=env,
                capture_output=True,
                text=True,
                timeout=300,  # 5 minute timeout
            )
    except subprocess.TimeoutExpired:
        print("Error: Synthesis timed out after 300s", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(f"Error: claude -p exited {result.returncode}", file=sys.stderr)
        if result.stderr:
            print(result.stderr[:500], file=sys.stderr)
        return 1

    print("Synthesis complete")
    return 0


def main() -> int:
    """CLI entry point for deferred synthesis."""
    parser = argparse.ArgumentParser(description="Deferred synthesis runner")
    parser.add_argument("--force", action="store_true", help="Skip schedule check")
    args = parser.parse_args()
    return run_synthesis(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
