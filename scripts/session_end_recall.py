#!/usr/bin/env python3
"""
SessionEnd hook - writes pending-recall file for instant context in next session.

Reads {session_id, transcript_path, cwd} from stdin JSON (delivered by Claude Code).
Writes ~/.claude/memory/pending-recall/{session_id}.md with:
- YAML-like frontmatter (session_id, project, timestamp, cwd)
- First user prompt as blockquote
- Assistant messages with head/tail budget split: oldest-first (1/3 of remaining
  budget) + newest-last (2/3), with "[N messages omitted]" marker for the
  skipped middle. Always emits chronological order.

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
    DEFAULT_SETTINGS,
    find_current_project,
    get_pending_recall_dir,
    get_projects_index_file,
    load_json_file,
    load_settings,
    resolve_session_path,
)
from transcript_ops import extract_first_user_prompt, parse_jsonl_file

MAX_MESSAGE_LINES = 30


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


def _select_messages(
    messages: list[dict], remaining_budget: int
) -> tuple[list[str], list[str], int]:
    """Select head + tail assistant messages within remaining_budget tokens.

    Per-message truncation is applied to all candidates first. The tail (newest)
    is always primary; if everything fits, it all goes in tail_texts and
    head_texts is empty. When the budget is tight, allocates 2/3 to tail
    (newest) and 1/3 to head (oldest); head and tail are disjoint contiguous
    slices and omitted_count is the gap between them.

    The latest message is always force-included even if it alone exceeds the
    tail allocation — empty recall is worse than a small overshoot.
    """
    truncated = [_truncate_message(m["content"]) for m in messages]
    n = len(truncated)
    if n == 0:
        return [], [], 0

    total_tokens = sum(len(t) // 4 for t in truncated)
    if total_tokens <= remaining_budget:
        return [], truncated, 0

    tail_budget = (remaining_budget * 2) // 3
    head_budget = remaining_budget - tail_budget

    tail_start = n
    used = 0
    for i in range(n - 1, -1, -1):
        msg_tokens = len(truncated[i]) // 4
        if tail_start < n and used + msg_tokens > tail_budget:
            break
        tail_start = i
        used += msg_tokens

    head_end = 0
    used = 0
    for i in range(tail_start):
        msg_tokens = len(truncated[i]) // 4
        if used + msg_tokens > head_budget:
            break
        head_end = i + 1
        used += msg_tokens

    omitted = tail_start - head_end
    return truncated[:head_end], truncated[tail_start:], omitted


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
        default_limit = DEFAULT_SETTINGS["previousSessionRecall"]["tokenLimit"]
        token_limit = int(
            settings.get("previousSessionRecall", {}).get("tokenLimit", default_limit)
            or default_limit
        )

    first_prompt = extract_first_user_prompt(transcript_path)
    messages = parse_jsonl_file(transcript_path)

    prompt_tokens = len(first_prompt) // 4 + 5 if first_prompt else 0
    remaining_budget = max(0, token_limit - prompt_tokens)
    head_texts, tail_texts, omitted_count = _select_messages(messages, remaining_budget)

    if not head_texts and not tail_texts and not first_prompt:
        return

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
        parts.extend(f"> {line}" for line in first_prompt.splitlines())
        parts.append("")

    sections = []
    if head_texts:
        sections.append("\n\n".join(head_texts))
    if omitted_count > 0:
        sections.append(f"... [{omitted_count} messages omitted] ...")
    if tail_texts:
        sections.append("\n\n".join(tail_texts))
    if sections:
        parts.extend("\n\n".join(sections).split("\n"))
    parts.append("")

    recall_dir.mkdir(parents=True, exist_ok=True)
    target = recall_dir / f"{session_id}.md"
    tmp = target.with_suffix(".tmp")
    tmp.write_text("\n".join(parts), encoding="utf-8")
    tmp.replace(target)


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
