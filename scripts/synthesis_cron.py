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
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from load_memory import (
    get_last_synthesis_file,
    should_synthesize,
    write_synthesis_prompt,
)
from memory_utils import get_synthesis_error_log, load_settings

SYNTHESIS_ERROR_LOG = get_synthesis_error_log()

# Common English stopwords for topic extraction
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "shall", "can", "need", "dare", "ought",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
    "they", "them", "their", "this", "that", "these", "those",
    "and", "but", "or", "nor", "not", "so", "yet", "both", "either",
    "for", "of", "to", "from", "in", "on", "at", "by", "with", "about",
    "into", "through", "during", "before", "after", "above", "below",
    "if", "then", "else", "when", "where", "how", "what", "which", "who",
    "all", "each", "every", "some", "any", "no", "more", "most", "other",
    "just", "also", "very", "too", "quite", "really", "still", "already",
})

MAX_TOPICS = 20


def extract_topics(text: str, max_topics: int = MAX_TOPICS) -> list:
    """Extract key topics from transcript text using term frequency.

    Algorithmic extraction (no LLM call). Tokenizes text, removes stopwords,
    and returns the most frequent meaningful terms.

    Args:
        text: Raw transcript text.
        max_topics: Maximum number of topics to return.

    Returns:
        List of topic strings, ordered by frequency (most frequent first).
    """
    if not text or not text.strip():
        return []
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_.-]*", text)
    meaningful = [t for t in tokens if t.lower() not in _STOPWORDS and len(t) > 2]
    counts = Counter(meaningful)
    return [term for term, _ in counts.most_common(max_topics)]


def retrieve_existing_memories(
    transcript_text: str,
    scope: str | None = None,
    top_k: int = 10,
) -> list:
    """Vector-search for existing memories relevant to transcript content.

    Returns list of dicts with 'chunk_id' and 'content' keys,
    formatted for inclusion in synthesis prompt.

    Falls back to empty list if embeddings module is unavailable.
    """
    try:
        from embeddings import search_similar
        from storage import get_db, close_db
    except ImportError:
        return []

    topics = extract_topics(transcript_text)
    if not topics:
        return []

    query_text = " ".join(topics[:10])
    seen_ids = set()
    results = []

    conn = None
    try:
        conn = get_db()
        similar = search_similar(conn, query_text, top_k=top_k, scope=scope)
        for scored_chunk in similar:
            chunk = scored_chunk.chunk
            if chunk.id not in seen_ids:
                seen_ids.add(chunk.id)
                results.append({
                    "chunk_id": chunk.id,
                    "content": chunk.content,
                })
    except Exception:
        pass
    finally:
        if conn:
            close_db(conn)

    return results


def should_run_deferred_synthesis() -> bool:
    """Check if deferred synthesis should run now.

    Returns False if:
    - synthesis.deferred is not True
    - should_synthesize() says it's not time yet (recently ran)

    Returns True if deferred mode is enabled and synthesis is due.
    """
    settings = load_settings()
    if not settings.get("synthesis", {}).get("deferred", True):
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
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        write_synthesis_prompt()

    output = captured.getvalue()

    if "No pending" in output:
        print("No pending transcripts")
        return 0

    # Parse model and prompt files (one per date) from output
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
        print("Error: No prompt files generated", file=sys.stderr)
        return 1

    # Write eager timestamp to prevent concurrent runs
    get_last_synthesis_file().write_text(
        datetime.now(timezone.utc).isoformat(),
        encoding="utf-8",
    )

    # Run claude -p for each date's prompt file
    cmd_base = build_claude_command(model)
    env = os.environ.copy()
    env["CLAUDECODE"] = ""  # Unset nesting guard

    failed = False
    for prompt_file in prompt_files:
        date_label = Path(prompt_file).stem  # e.g. synthesis-prompt-2026-02-26-1234
        print(f"Running synthesis for {date_label} with model={model}")
        try:
            with open(prompt_file, encoding="utf-8") as f:
                result = subprocess.run(
                    cmd_base,
                    stdin=f,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=300,  # 5 minute timeout
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            msg = f"Synthesis failed for {date_label}: {exc}"
            print(f"Error: {msg}", file=sys.stderr)
            _log_error(msg)
            failed = True
            continue

        if result.returncode != 0:
            msg = f"claude -p exited {result.returncode} for {date_label}"
            if result.stderr:
                msg += f": {result.stderr[:200]}"
            print(f"Error: {msg}", file=sys.stderr)
            _log_error(msg)
            failed = True
            continue

        print(f"Synthesis complete for {date_label}")

    if failed:
        # Don't clear timestamp — some dates may have succeeded.
        # Next run will re-extract only dates that still have pending sessions.
        return 1

    print("All synthesis runs complete")
    return 0


def main() -> int:
    """CLI entry point for deferred synthesis."""
    parser = argparse.ArgumentParser(description="Deferred synthesis runner")
    parser.add_argument("--force", action="store_true", help="Skip schedule check")
    args = parser.parse_args()
    return run_synthesis(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
