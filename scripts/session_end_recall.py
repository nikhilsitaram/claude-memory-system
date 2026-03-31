#!/usr/bin/env python3
"""
SessionEnd hook - writes pending-recall file for instant context in next session.

Reads {session_id, transcript_path, cwd} from stdin JSON (delivered by Claude Code).
Writes ~/.claude/memory/pending-recall/{session_id}.md with:
- YAML-like frontmatter (session_id, project, timestamp, cwd)
- First user prompt as blockquote
- Assistant messages in chronological order (newest-first token budgeting,
  oldest-first output)

No LLM involved. Targets <5s execution time.

Requirements: Python 3.9+
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    find_current_project,
    get_pending_recall_dir,
    get_projects_index_file,
    load_json_file,
    load_settings,
    resolve_session_path,
)
from transcript_ops import extract_first_user_prompt, parse_jsonl_file

MAX_MESSAGE_LINES = 50


def _truncate_message(text: str) -> str:
    """Truncate a message exceeding MAX_MESSAGE_LINES with head/tail split."""
    lines = text.split("\n")
    if len(lines) <= MAX_MESSAGE_LINES:
        return text
    head = MAX_MESSAGE_LINES // 3
    tail = MAX_MESSAGE_LINES - head
    truncated = len(lines) - head - tail
    return "\n".join(
        lines[:head]
        + [f"\n... [{truncated} lines truncated] ...\n"]
        + lines[-tail:]
    )


def write_recall_file(
    session_id: str,
    transcript_path: Path,
    cwd: str,
    recall_dir: Path | None = None,
    token_limit: int | None = None,
) -> None:
    """Write a pending-recall file from session transcript.

    Args:
        session_id: The Claude Code session ID
        transcript_path: Path to the session's JSONL transcript
        cwd: Working directory from the ended session
        recall_dir: Override for pending-recall directory (for testing)
        token_limit: Override for token budget (for testing)
    """
    if recall_dir is None:
        recall_dir = get_pending_recall_dir()
    if token_limit is None:
        settings = load_settings()
        token_limit = settings.get("previousSessionRecall", {}).get("tokenLimit", 1500)

    first_prompt = extract_first_user_prompt(transcript_path)

    messages = parse_jsonl_file(transcript_path)
    if not messages:
        return

    collected = []
    running_tokens = 0

    if first_prompt:
        running_tokens += len(first_prompt) // 4 + 5

    for msg in reversed(messages):
        text = _truncate_message(msg["content"])
        msg_tokens = len(text) // 4
        if running_tokens + msg_tokens > token_limit:
            break
        collected.append(text)
        running_tokens += msg_tokens

    if not collected:
        return

    collected.reverse()

    resolved_cwd = resolve_session_path(cwd)
    projects_index = load_json_file(get_projects_index_file(), {})
    current_project = find_current_project(projects_index, resolved_cwd)
    project_name = current_project.get("name", "") if current_project else ""

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [
        "---",
        f"session_id: {session_id}",
        f"project: {project_name}",
        f"timestamp: {timestamp}",
        f"cwd: {resolved_cwd}",
        "---",
    ]
    if first_prompt:
        parts.append(f"> {first_prompt}")
        parts.append("")

    parts.extend("\n\n".join(collected).split("\n"))
    parts.append("")

    recall_dir.mkdir(parents=True, exist_ok=True)
    target = recall_dir / f"{session_id}.md"
    tmp = target.with_suffix(".tmp")
    tmp.write_text("\n".join(parts), encoding="utf-8")
    tmp.rename(target)


def main() -> None:
    """Entry point for SessionEnd hook. Reads stdin JSON."""
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, IOError):
        return

    session_id = hook_input.get("session_id")
    transcript_path = hook_input.get("transcript_path")
    cwd = hook_input.get("cwd", "")

    if not session_id or not transcript_path:
        return

    settings = load_settings()
    if not settings.get("previousSessionRecall", {}).get("enabled", True):
        return

    write_recall_file(
        session_id=session_id,
        transcript_path=Path(transcript_path),
        cwd=cwd,
    )


if __name__ == "__main__":
    main()
