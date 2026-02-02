#!/usr/bin/env python3
"""
SessionEnd/PreCompact hook - saves transcript to memory system.

This script reads session info from stdin (JSON with transcript_path and session_id),
copies the transcript to the dated memory directory, and records the session ID
to prevent duplicate captures.

Input (via stdin JSON):
{
    "transcript_path": "~/.claude/projects/.../session_id.jsonl",
    "session_id": "abc123-def456-..."
}

Requirements: Python 3.9+
"""

import json
import sys
from datetime import datetime
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    check_python_version,
    get_transcripts_dir,
    get_captured_sessions,
    add_captured_session,
)


def expand_path(path_str: str) -> Path:
    """Expand ~ and resolve path."""
    return Path(path_str).expanduser().resolve()


def main() -> None:
    """Main entry point - read stdin, copy transcript, update captured list."""
    check_python_version()

    # Read input from stdin
    try:
        input_data = sys.stdin.read()
        if not input_data.strip():
            # No input provided
            sys.exit(0)

        data = json.loads(input_data)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON input: {e}", file=sys.stderr)
        sys.exit(1)

    # Extract fields
    transcript_path_str = data.get("transcript_path", "")
    session_id = data.get("session_id", "")

    if not transcript_path_str or not session_id:
        # Missing required fields, but exit cleanly
        sys.exit(0)

    # Expand path (handle ~ and resolve)
    transcript_path = expand_path(transcript_path_str)

    # Check if transcript file exists
    if not transcript_path.exists():
        # File doesn't exist yet, nothing to save
        sys.exit(0)

    # Determine destination
    today = datetime.now().strftime("%Y-%m-%d")
    dest_dir = get_transcripts_dir() / today
    dest_file = dest_dir / f"{session_id}.jsonl"

    # Create destination directory
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Copy transcript (always overwrite to get latest version)
    try:
        content = transcript_path.read_bytes()
        dest_file.write_bytes(content)
    except IOError as e:
        print(f"Error copying transcript: {e}", file=sys.stderr)
        sys.exit(1)

    # Record session ID if not already captured
    captured = get_captured_sessions()
    if session_id not in captured:
        add_captured_session(session_id)


if __name__ == "__main__":
    main()
