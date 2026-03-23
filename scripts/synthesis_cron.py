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
        from storage import close_db, get_db
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
            chunk = scored_chunk.data_point
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


def _write_session_context(
    conn,
    project_name: str,
    topics: list,
    session_id: str,
    entities: list | None = None,
) -> str:
    """Write a session_context data_point for a completed synthesis session.

    Idempotent: checks for existing session_context with the same session_id.
    Creates context_for edges to entity data_points and a continues edge to
    the prior session_context for the same project.

    Args:
        conn: Open SQLite connection (v3 schema).
        project_name: Scope / project name.
        topics: Key topics extracted from transcripts.
        session_id: Unique session identifier (used for idempotency).
        entities: Optional list of entity names to link via context_for edges.

    Returns:
        The data_point ID of the session_context.
    """
    import json as _json

    from storage import DataPointRow, EdgeRow, insert_data_point, insert_edge

    # Idempotency check: escape LIKE wildcards in session_id to prevent injection
    safe_session_id = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    existing = conn.execute(
        "SELECT id FROM data_points WHERE type='session_context' AND properties LIKE ? ESCAPE '\\'",
        (f'%"session_id": "{safe_session_id}"%',),
    ).fetchone()
    if existing:
        return existing[0]

    content = f"Working on {project_name}. Topics: {', '.join(topics[:5])}."
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    props = _json.dumps({"session_id": session_id, "status": "completed"})

    dp = DataPointRow(
        type="session_context", content=content, scope=project_name,
        salience=0.8, source_type="session_end", created_at=now,
        properties=props,
    )
    dp_id = insert_data_point(conn, dp)

    # context_for edges to entities
    if entities:
        seen_entity_ids = set()
        for entity_name in entities:
            entity_id = _get_or_create_entity_in_db(conn, entity_name, project_name)
            if entity_id not in seen_entity_ids:
                seen_entity_ids.add(entity_id)
                try:
                    insert_edge(conn, EdgeRow(
                        source=dp_id, target=entity_id, type="context_for",
                        created_at=now,
                    ))
                except Exception:
                    pass  # FK violations or duplicates — skip

    # continues edge to prior session_context
    prior = conn.execute(
        "SELECT id FROM data_points WHERE type='session_context' "
        "AND scope=? AND id != ? ORDER BY created_at DESC LIMIT 1",
        (project_name, dp_id),
    ).fetchone()
    if prior:
        try:
            insert_edge(conn, EdgeRow(
                source=dp_id, target=prior[0], type="continues",
                created_at=now,
            ))
        except Exception:
            pass

    conn.commit()
    return dp_id


def _get_or_create_entity_in_db(conn, entity_name: str, scope: str | None) -> str:
    """Return the ID of an entity data_point, creating it if absent.

    Separate from synthesis._get_or_create_entity to avoid circular import.
    """
    from storage import DataPointRow, _content_hash, insert_data_point  # noqa: F401

    content_hash = _content_hash(f"entity:{entity_name}")
    row = conn.execute(
        "SELECT id FROM data_points WHERE type='entity' AND content_hash=?",
        (content_hash,),
    ).fetchone()
    if row:
        return row[0]
    return insert_data_point(conn, DataPointRow(
        type="entity", name=entity_name, scope=scope,
        content=entity_name, source_type="synthesis_v3",
        salience=0.5,
    ))


def _get_schema_version(conn) -> int:
    """Return the schema version from PRAGMA user_version."""
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _run_synthesis_v2(model: str, prompt_files: list) -> bool:
    """Run the existing v2 synthesis pipeline (claude -p with PROJECT blocks).

    Returns True on success, False if any prompt failed.
    """
    cmd_base = build_claude_command(model)
    env = os.environ.copy()
    env["CLAUDECODE"] = ""

    failed = False
    for prompt_file in prompt_files:
        date_label = Path(prompt_file).stem
        print(f"Running v2 synthesis for {date_label} with model={model}")
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

    return not failed


def _run_synthesis_v3(conn, model: str, prompt_files: list) -> bool:
    """Run the v3 DB-primary synthesis pipeline.

    Uses _build_synthesis_instructions_v3 prompt and apply_memory_ops_v3.

    Steps:
    1. Build v3 prompt for each date
    2. Call claude -p
    3. Parse MEMORY_OPS output
    4. Apply ops using apply_memory_ops_v3 (no markdown writes)
    5. Write session_context data_points

    Returns True on success, False if any date failed.
    """
    from load_memory import _build_synthesis_instructions_v3
    from synthesis import apply_memory_ops_v3, parse_synthesis_output

    cmd_base = build_claude_command(model)
    env = os.environ.copy()
    env["CLAUDECODE"] = ""

    failed = False
    for prompt_file in prompt_files:
        date_label = Path(prompt_file).stem
        print(f"Running v3 synthesis for {date_label} with model={model}")

        try:
            prompt_text = Path(prompt_file).read_text(encoding="utf-8")
        except IOError as exc:
            _log_error(f"Cannot read prompt file {prompt_file}: {exc}")
            failed = True
            continue

        # Replace v2 instructions with v3 instructions in the prompt
        v3_instructions = _build_synthesis_instructions_v3()
        if "## Synthesis Instructions" in prompt_text:
            parts = prompt_text.split("## Synthesis Instructions", 1)
            if len(parts) == 2:
                after_header = parts[1]
                next_section = after_header.find("\n## ")
                if next_section != -1:
                    rest = after_header[next_section:]
                else:
                    rest = ""
                prompt_text = parts[0] + "## Synthesis Instructions\n\n" + v3_instructions + rest

        # Strip v2-only sections that contradict v3 MEMORY_OPS-only output.
        # ## Output Format (shows ===PROJECT:name=== example),
        # ## Delivery (tells LLM to use Write/Bash tools),
        # ## Reminder (says to start with ===PROJECT:===).
        # These sections appear before ## Synthesis Instructions so they may
        # still be present in the reconstructed prompt_text above.
        for section_header in ("## Output Format", "## Delivery", "## Reminder"):
            if section_header in prompt_text:
                sec_parts = prompt_text.split(section_header, 1)
                if len(sec_parts) == 2:
                    after = sec_parts[1]
                    next_sec = after.find("\n## ")
                    if next_sec != -1:
                        prompt_text = sec_parts[0] + after[next_sec + 1:]
                    else:
                        prompt_text = sec_parts[0]

        import tempfile
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(prompt_text)
            tmp_path = tmp.name

        try:
            with open(tmp_path, encoding="utf-8") as f:
                result = subprocess.run(
                    cmd_base,
                    stdin=f,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            msg = f"V3 synthesis failed for {date_label}: {exc}"
            print(f"Error: {msg}", file=sys.stderr)
            _log_error(msg)
            failed = True
            Path(tmp_path).unlink(missing_ok=True)
            continue
        finally:
            Path(tmp_path).unlink(missing_ok=True)

        if result.returncode != 0:
            msg = f"claude -p exited {result.returncode} for {date_label} (v3)"
            if result.stderr:
                msg += f": {result.stderr[:200]}"
            print(f"Error: {msg}", file=sys.stderr)
            _log_error(msg)
            failed = True
            continue

        # Parse and apply the v3 MEMORY_OPS output
        synthesis_result = parse_synthesis_output(result.stdout)
        if synthesis_result.memory_ops:
            apply_results = apply_memory_ops_v3(conn, synthesis_result.memory_ops)
            print(f"Applied {len(apply_results)} memory ops for {date_label}")

            # Extract the most common non-global project scope from ops.
            # date_label (prompt filename stem) is NOT the project name.
            scope_counts = Counter(
                op.scope for op in synthesis_result.memory_ops
                if op.scope and op.scope != "global"
            )
            project_scope = scope_counts.most_common(1)[0][0] if scope_counts else "global"

            # Write session_context data_point
            topics = extract_topics(prompt_text)
            entities = [
                e for op in synthesis_result.memory_ops
                if op.entities for e in op.entities
            ]
            session_id = date_label
            _write_session_context(conn, project_scope, topics, session_id, entities)
        else:
            print(f"No MEMORY_OPS in v3 synthesis output for {date_label}")

    return not failed


def run_synthesis(force: bool = False) -> int:
    """Run the full deferred synthesis pipeline.

    Detects schema version and dispatches to the appropriate apply function:
    - v3+: Uses _run_synthesis_v3 with MEMORY_OPS-only output and DB writes
    - v2: Uses _run_synthesis_v2 with PROJECT blocks and markdown writes

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

    # Detect schema version and dispatch
    conn = None
    try:
        from storage import close_db, get_db
        conn = get_db()
        version = _get_schema_version(conn)
    except Exception:
        version = 0  # No DB yet — fall back to v2
        conn = None

    try:
        if version >= 3 and conn is not None:
            success = _run_synthesis_v3(conn, model, prompt_files)
        else:
            if conn:
                close_db(conn)
                conn = None
            success = _run_synthesis_v2(model, prompt_files)
    finally:
        if conn:
            try:
                from storage import close_db
                close_db(conn)
            except Exception:
                pass

    if not success:
        _clear_eager_timestamp()
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
