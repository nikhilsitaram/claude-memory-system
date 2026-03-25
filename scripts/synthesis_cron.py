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
        "--disable-slash-commands",
        "--settings", '{"disableAllHooks": true, "mcpServers": {}}',
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

    from storage import DataPointRow, EdgeRow, insert_data_point, insert_edge, prune_session_contexts

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

    # Prune older session_contexts beyond the retention limit for this scope
    prune_session_contexts(conn, project_name)

    conn.commit()
    return dp_id


def _get_or_create_entity_in_db(conn, entity_name: str, scope: str | None) -> str:
    """Return the ID of an entity data_point, creating it if absent."""
    from storage import get_or_create_entity
    return get_or_create_entity(conn, entity_name, scope)


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


def _run_decay_v3(conn) -> None:
    """Run tiered decay on data_points. Cheap and idempotent -- runs every invocation."""
    try:
        from decay import cleanup_near_zero_salience, decay_data_points
        count = decay_data_points(conn)
        if count > 0:
            print(f"Decay: adjusted salience for {count} data_points", file=sys.stderr)
        cleaned = cleanup_near_zero_salience(conn)
        if cleaned > 0:
            print(f"Cleanup: soft-deleted {cleaned} near-zero salience data_points", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Decay failed: {e}", file=sys.stderr)


def _should_consolidate(conn, settings) -> bool:
    """Check if consolidation should run based on interval and memory count."""
    import sqlite3

    consol = settings.get("consolidation", {})
    interval_hours = consol.get("intervalHours", 24)
    min_memories = consol.get("minMemories", 5)

    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'last_consolidation'"
        ).fetchone()
    except sqlite3.OperationalError:
        return False

    if row and row[0]:
        try:
            last = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - last).total_seconds() / 3600
            if elapsed < interval_hours:
                return False
        except (ValueError, AttributeError):
            pass

    count_row = conn.execute(
        "SELECT COUNT(*) FROM data_points WHERE type = 'memory' AND salience > 0.1 "
        "AND (source_type IS NULL OR source_type != 'consolidation')"
    ).fetchone()
    if not count_row or count_row[0] < min_memories:
        return False

    return True


def _is_backfill(conn) -> bool:
    """Check if this is the first-ever consolidation run."""
    import sqlite3

    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'last_consolidation'"
        ).fetchone()
    except sqlite3.OperationalError:
        return True
    return row is None or row[0] is None


def _update_consolidation_timestamp(conn):
    """Update the last_consolidation metadata key."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    conn.execute(
        "INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_consolidation', ?)",
        (now,)
    )
    conn.commit()


def _run_consolidation_post_step(conn, settings):
    """Run consolidation as a post-step after synthesis, if gate conditions met."""
    if not _should_consolidate(conn, settings):
        return

    try:
        from consolidation import run_consolidation
        backfill = _is_backfill(conn)
        stats = run_consolidation(conn, settings=settings, backfill=backfill)
        print(f"Consolidation: merged={stats['clusters_merged']}, skipped={stats['clusters_skipped']}", file=sys.stderr)
        _update_consolidation_timestamp(conn)
    except Exception as e:
        print(f"Warning: Consolidation failed: {e}", file=sys.stderr)


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
            _run_decay_v3(conn)
            if success:
                settings = load_settings()
                _run_consolidation_post_step(conn, settings)
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


def _group_sessions_by_project(sessions):
    """Group sessions by resolved project name."""
    from memory_utils import resolve_project_path_to_name

    groups = {}
    for session in sessions:
        project_name = "global"
        if session.project_path:
            resolved = resolve_project_path_to_name(session.project_path)
            if resolved:
                project_name = resolved
        elif session.project_hash:
            resolved = resolve_project_path_to_name(None, project_hash=session.project_hash)
            if resolved:
                project_name = resolved
        groups.setdefault(project_name, []).append(session)
    return groups


def _run_claude_backfill(prompt: str, model: str) -> str | None:
    """Run headless claude -p for backfill synthesis."""
    import tempfile

    cmd = build_claude_command(model)
    env = os.environ.copy()
    env["CLAUDECODE"] = ""

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(prompt)
        tmp_path = tmp.name

    try:
        with open(tmp_path, encoding="utf-8") as f:
            result = subprocess.run(
                cmd, stdin=f, env=env, capture_output=True, text=True, timeout=300,
            )
        if result.returncode == 0:
            return result.stdout
        print(f"  claude -p exited {result.returncode}: {result.stderr[:200]}", file=sys.stderr)
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        print(f"  Synthesis error: {exc}", file=sys.stderr)
        return None
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def run_backfill(days=None, import_from=None) -> int:
    """Run backfill synthesis on historical sessions."""
    from indexing import get_session_date, list_recent_sessions
    from load_memory import _build_synthesis_instructions_v3
    from memory_utils import (
        get_project_working_days,
        load_settings,
        load_synthesis_state,
        update_synthesis_state,
    )
    from storage import ensure_db
    from synthesis import apply_memory_ops_v3, parse_synthesis_output
    from transcript_ops import parse_jsonl_file_from_line

    settings = load_settings()

    # Step 1: Optional session import
    if import_from:
        from session_import import import_sessions

        result = import_sessions(import_from)
        print(f"Imported {result.copied} sessions from {result.projects} projects "
              f"({result.skipped} skipped as duplicates)")
        for m in result.mismatches:
            print(f"  Mismatch: {m}")

        # Rebuild index so imported sessions are discoverable
        from memory_utils import rebuild_projects_index_quiet

        rebuild_projects_index_quiet()

    # Step 2: Discover sessions
    max_age = days if days is not None else None
    all_sessions = list_recent_sessions(max_age_days=max_age)
    if not all_sessions:
        print("No sessions found for backfill.")
        return 0

    # Step 3: Group by project
    project_sessions = _group_sessions_by_project(all_sessions)

    # Step 4: Compute model assignments per project
    backfill_wd = (
        settings.get("synthesis", {}).get("backfill", {}).get("recentWorkingDays", 7)
    )
    default_model = settings.get("synthesis", {}).get("model", "sonnet")

    project_batches = {}
    for project_name, sessions in project_sessions.items():
        working_days = get_project_working_days(project_name, backfill_wd)
        wd_set = set(working_days)
        sonnet, haiku = [], []
        for s in sessions:
            (sonnet if get_session_date(s) in wd_set else haiku).append(s)
        project_batches[project_name] = {
            "sonnet": sonnet, "haiku": haiku, "total": len(sessions),
        }

    # Step 5: Print scope report
    total = sum(b["total"] for b in project_batches.values())
    total_sonnet = sum(len(b["sonnet"]) for b in project_batches.values())
    total_haiku = sum(len(b["haiku"]) for b in project_batches.values())

    print(f"\nBackfill scope: {len(project_batches)} projects, {total} sessions")
    for pname, batch in sorted(project_batches.items(), key=lambda x: -x[1]["total"]):
        print(f"  {pname:40s} {batch['total']:4d} sessions "
              f"({len(batch['sonnet'])} {default_model}, {len(batch['haiku'])} haiku)")
    print(f"\nModel split: {total_sonnet} {default_model}, {total_haiku} haiku")

    # Step 6: Confirmation
    try:
        answer = input("Proceed? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted.")
            return 0
    except EOFError:
        print("Non-interactive mode — proceeding automatically.")

    # Step 7: Process each project
    conn = ensure_db()
    state = load_synthesis_state()
    sessions_state = state.get("sessions", {})
    v3_instructions = _build_synthesis_instructions_v3()

    try:
        for project_name, batch in project_batches.items():
            print(f"\n  {project_name} ({batch['total']} sessions)...")
            project_updates = {}

            for model_tier, model_name in [("sonnet", default_model), ("haiku", "haiku")]:
                tier_sessions = batch[model_tier]
                if not tier_sessions:
                    continue

                # Group by date for batching
                by_date = {}
                for s in tier_sessions:
                    by_date.setdefault(get_session_date(s), []).append(s)

                for day, day_sessions in sorted(by_date.items()):
                    transcripts = []
                    for s in day_sessions:
                        sid = s.session_id
                        prev = sessions_state.get(sid)
                        if prev and s.file_size == prev.get("offset", 0):
                            continue

                        start_line = prev.get("lines", 0) if prev else 0
                        messages, total_lines = parse_jsonl_file_from_line(
                            s.transcript_path, start_line=start_line
                        )
                        if messages:
                            transcripts.append({"session_id": sid, "messages": messages})
                            project_updates[sid] = {
                                "offset": s.file_size, "lines": total_lines,
                            }

                    if not transcripts:
                        continue

                    # Build prompt
                    parts = [f"You are synthesizing transcripts for project "
                             f'"{project_name}" from {day}.\n\n{v3_instructions}\n\n## Transcripts\n']
                    for t in transcripts:
                        parts.append(f"### Session: {t['session_id']}")
                        for msg in t["messages"]:
                            role = msg.get("role", "unknown")
                            content = msg.get("content", "")
                            if isinstance(content, list):
                                content = " ".join(
                                    b.get("text", "") for b in content if isinstance(b, dict)
                                )
                            parts.append(f"**{role}:** {content[:2000]}")

                    output = _run_claude_backfill("\n".join(parts), model_name)
                    if output:
                        sr = parse_synthesis_output(output)
                        if sr and sr.memory_ops:
                            apply_memory_ops_v3(conn, sr.memory_ops)

                print(f"    {model_name}: {len(tier_sessions)} sessions processed")

            # Flush session state after each project for resumability
            if project_updates:
                update_synthesis_state(project_updates)
                sessions_state.update(project_updates)

        _run_decay_v3(conn)
        print("\nBackfill complete.")
        return 0

    except Exception as e:
        print(f"Backfill error: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def main() -> int:
    """CLI entry point for deferred synthesis."""
    parser = argparse.ArgumentParser(description="Deferred synthesis runner")
    parser.add_argument("--force", action="store_true", help="Skip schedule check")
    parser.add_argument("--backfill", action="store_true",
                        help="Backfill: synthesize all historical sessions")
    parser.add_argument("--days", type=int, default=None,
                        help="With --backfill: limit to last N calendar days")
    parser.add_argument("--import-from", type=str, default=None, dest="import_from",
                        help="With --backfill: import sessions from external path first")
    args = parser.parse_args()

    if args.backfill:
        return run_backfill(days=args.days, import_from=args.import_from)
    return run_synthesis(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
