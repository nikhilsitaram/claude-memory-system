#!/usr/bin/env python3
"""UserPromptSubmit hook for proactive mid-session memory recall.

Reads the user's prompt from stdin, searches for relevant memories,
and injects them as stdout text for Claude to see.

Latency target: <800ms per prompt.
"""
import json
import re
import sys
import time
from pathlib import Path

script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

_CONFIRM_RE = re.compile(
    r"^(yes|yeah|yep|ok|okay|sure|go ahead|looks good|lgtm|sounds good|"
    r"do it|proceed|continue|approved|confirmed|fine|great|perfect|correct|right|ack)[\s.!]*$",
    re.IGNORECASE,
)

from memory_utils import DEFAULT_SETTINGS

_RECALL_DEFAULTS = DEFAULT_SETTINGS["recall"]
MIN_PROMPT_LENGTH = _RECALL_DEFAULTS["minPromptLength"]
MAX_PROMPT_LENGTH = _RECALL_DEFAULTS["maxPromptLength"]
MAX_INJECTIONS = _RECALL_DEFAULTS["maxInjectionsPerPrompt"]
MIN_RELEVANCE_SCORE = _RECALL_DEFAULTS.get("minRelevanceScore", 0.45)
DEDUP_WINDOW = 3  # Not user-configurable — internal dedup window


def should_search(prompt: str) -> bool:
    """Relevance gate: decide whether this prompt warrants memory search."""
    if not prompt or len(prompt.strip()) < MIN_PROMPT_LENGTH:
        return False
    if len(prompt.strip()) > MAX_PROMPT_LENGTH:
        return False
    if prompt.strip().startswith("/"):
        return False
    if _CONFIRM_RE.match(prompt.strip()):
        return False
    return True


def is_recently_injected(dp_id: str, state_file: Path, current_prompt_index: int = 0) -> bool:
    """Check if a memory was injected within the last DEDUP_WINDOW prompts."""
    if not state_file.exists():
        return False
    try:
        state = json.loads(state_file.read_text())
        for entry in state.get("injections", []):
            if entry["id"] == dp_id:
                if current_prompt_index - entry["prompt_index"] < DEDUP_WINDOW:
                    return True
        return False
    except (json.JSONDecodeError, KeyError):
        return False


def record_injection(dp_id: str, state_file: Path, prompt_index: int) -> None:
    """Record that a memory was injected at this prompt index."""
    state = {"injections": []}
    if state_file.exists():
        try:
            state = json.loads(state_file.read_text())
        except json.JSONDecodeError:
            pass

    state["injections"].append({"id": dp_id, "prompt_index": prompt_index})
    state["injections"] = state["injections"][-(MAX_INJECTIONS * DEDUP_WINDOW):]
    state_file.write_text(json.dumps(state))


def format_injection(memories: list) -> str:
    """Format memories for injection into Claude's context."""
    if not memories:
        return ""
    lines = ["[memory] Related context:"]
    for m in memories[:MAX_INJECTIONS]:
        certainty = m.get("certainty", "?")
        scope = m.get("scope", "unknown")
        content = m.get("content", "")
        lines.append(f"- (certainty: {certainty}, scope: {scope}) {content}")
    return "\n".join(lines)


def cleanup_stale_state_files(memory_dir: Path) -> None:
    """Remove prompt-recall state files older than 24 hours."""
    cutoff = time.time() - 86400
    for f in memory_dir.glob(".prompt-recall-state-*"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def main():
    """Entry point for UserPromptSubmit hook."""
    start = time.monotonic()

    try:
        input_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, IOError):
        return

    prompt = input_data.get("prompt", "")
    session_id = input_data.get("sessionId", "unknown")

    if not should_search(prompt):
        return

    try:
        from embeddings import search_hybrid
        from memory_utils import get_memory_dir, sanitize_secrets
        from storage import close_db, get_db
    except ImportError:
        return

    state_file = get_memory_dir() / f".prompt-recall-state-{session_id}"

    injected_log = []
    filtered_log = []

    conn = None
    results = []
    try:
        conn = get_db()
        results = search_hybrid(conn, prompt, scope=None, top_k=MAX_INJECTIONS + 2)

        prompt_index = 0
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                prompt_index = max((e["prompt_index"] for e in state.get("injections", [])), default=0) + 1
            except (json.JSONDecodeError, KeyError):
                pass

        memories = []
        for r in results:
            dp = r.data_point
            if r.score < MIN_RELEVANCE_SCORE:
                filtered_log.append({"id": dp.id, "content_preview": (dp.content or "")[:80], "reason": "low_relevance"})
                continue
            if is_recently_injected(dp.id, state_file, current_prompt_index=prompt_index):
                filtered_log.append({"id": dp.id, "content_preview": (dp.content or "")[:80], "reason": "deduped"})
                continue
            memories.append({
                "content": dp.content,
                "certainty": getattr(dp, "certainty", None),
                "scope": dp.scope,
            })
            injected_log.append({"id": dp.id, "content_preview": (dp.content or "")[:80], "scope": dp.scope})
            record_injection(dp.id, state_file, prompt_index)
            if len(memories) >= MAX_INJECTIONS:
                break

        output = format_injection(memories)
        if output:
            output = sanitize_secrets(output)
            print(output)

    except Exception as e:
        print(f"Warning: prompt_recall failed: {e}", file=sys.stderr)
    finally:
        if conn:
            close_db(conn)

    elapsed = time.monotonic() - start
    if elapsed > 0.8:
        print(f"Warning: prompt_recall took {elapsed:.2f}s (target <0.8s)", file=sys.stderr)

    try:
        from injection_log import log_prompt_recall
        log_prompt_recall(
            session_id=session_id,
            prompt_preview=sanitize_secrets(prompt[:80]),
            candidates=len(results),
            injected=injected_log,
            filtered=filtered_log,
            latency_ms=elapsed * 1000,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
