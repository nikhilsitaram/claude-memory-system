#!/usr/bin/env python3
"""
SessionStart hook - loads memory context for Claude Code.

This script runs on: startup, resume, clear, compact

It performs:
1. SQL-ranked loading from data_points (user profile, session continuity,
   project memories, global knowledge, recent activity)
2. Surfaces synthesis errors from deferred runs

Output is printed to stdout and injected into Claude Code's context.
Synthesis is handled exclusively by deferred mode (synthesis_cron.py).

Requirements: Python 3.9+
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    DEFAULT_SETTINGS,
    check_python_version,
    find_current_project,
    get_memory_dir,
    get_projects_index_file,
    get_synthesis_error_log,
    load_json_file,
    load_settings,
    load_synthesis_state,
    resolve_session_path,
)
from transcript_ops import (
    extract_transcripts_incremental,
    format_transcripts_incremental,
    get_recent_days,
)

# Maximum output lines for pre-extracted transcripts fed to the synthesis subagent
TRANSCRIPT_LINE_BUDGET = 1950

# Directory for synthesis prompt temp files (avoids 30K Bash truncation)
SYNTHESIS_PROMPT_DIR = "/tmp"

# =============================================================================
# Key Interfaces
# =============================================================================
# Entry points:
#   main()                                  SessionStart hook (stdout -> context)
# DB-first loading (v3):
#   _load_from_db(project_scope) -> tuple[str, list, list] | None
# Scheduling:
#   should_synthesize(settings) -> bool
# =============================================================================


SYNTHESIS_ERROR_LOG = get_synthesis_error_log()


def get_last_synthesis_file() -> Path:
    """Get the path to the .last-synthesis timestamp file."""
    return get_memory_dir() / ".last-synthesis"


def check_synthesis_errors() -> str | None:
    """Check for recent synthesis errors and return alert text.

    Reads the error log, surfaces its contents, and clears the log
    so the alert only appears once.

    Returns:
        Alert text if errors found, None otherwise.
    """
    if not SYNTHESIS_ERROR_LOG.exists():
        return None
    content = SYNTHESIS_ERROR_LOG.read_text(encoding="utf-8").strip()
    if not content:
        return None
    # Clear the log after reading so alert surfaces once
    SYNTHESIS_ERROR_LOG.unlink()
    lines = content.splitlines()
    return (
        "## Synthesis Error Alert\n"
        "Deferred synthesis has been failing. Recent errors:\n"
        + "\n".join(f"- `{line}`" for line in lines[-5:])
        + "\n\nCheck `journalctl --user -u claude-memory-synthesis.service` for details.\n"
        "Inform the user about this error.\n"
    )


def should_synthesize(settings: dict) -> bool:
    """
    Determine if synthesis should run based on scheduling rules.

    Synthesis runs if:
    1. .last-synthesis file doesn't exist (never synthesized)
    2. Last synthesis was on a different day (UTC) - first session of day
    3. More than intervalHours since last synthesis

    .last-synthesis file format: ISO format with UTC timezone (e.g., "2026-02-03T14:30:00+00:00")
    This file is written by load_memory.py when synthesis is triggered.

    Args:
        settings: Memory settings dict with synthesis.intervalHours

    Returns:
        True if synthesis should run, False otherwise
    """
    last_synthesis_file = get_last_synthesis_file()
    interval_hours = settings.get("synthesis", {}).get(
        "intervalHours", DEFAULT_SETTINGS["synthesis"]["intervalHours"]
    )

    try:
        if not last_synthesis_file.exists():
            return True  # Never synthesized

        last_time_str = last_synthesis_file.read_text(encoding="utf-8").strip()
        last_time = datetime.fromisoformat(last_time_str)

        # Ensure timezone awareness
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        hours_since = (now - last_time).total_seconds() / 3600

        # First session of day (local time) OR >interval since last
        return last_time.astimezone().date() < datetime.now().date() or hours_since > interval_hours

    except (ValueError, OSError, IOError):
        return True  # Fallback: always synthesize if file missing/invalid





def _build_synthesis_instructions_v3() -> str:
    """Build v3 synthesis instructions for MEMORY_OPS-only output (no PROJECT blocks)."""
    return '''**Output format:**

Output a single MEMORY_OPS JSON block with explicit decisions for each extracted fact.

```
===MEMORY_OPS===
{"ops": [
  {"action": "ADD", "fact": "description", "scope": "project-name",
   "type": "design", "salience": 0.8, "entities": ["entity1", "entity2"],
   "supersedes": "dp_id", "reason": "why this replaces the old memory"},
  {"action": "UPDATE", "id": "dp_id", "fact": "updated description",
   "salience": 0.7, "entities": ["entity1"]},
  {"action": "DELETE", "id": "dp_id", "reason": "Contradicted: explanation"},
  {"action": "NOOP", "id": "dp_id", "reason": "Already captured"}
]}
===END===
```

**Salience (0.0-1.0):** Assign a salience score to each ADD operation:
- 0.3-0.5: Transient facts (one-time fixes, version-specific notes, routine tasks)
- 0.5-0.7: Moderately useful (implementation details, standard patterns)
- 0.7-0.9: Important knowledge (architecture decisions, hard-won lessons, reusable patterns)
- 1.0: Permanent (user preferences, profile info — only for scope="user")

**Scopes:**
- `user`: Personal preferences, profile info (always loaded, never decays)
- `global`: Cross-project knowledge (loaded every session, ranked by salience)
- `{project-name}`: Project-specific knowledge (loaded when CWD matches)

**Entry types:** implement, improve, document, analyze, design, tradeoff, scope, gotcha, pitfall, pattern, insight, tip, workaround.

**Entity extraction:** Every operation must include an `entities` array: project names, library/tool names, concepts, people, URLs, dates.

**Provenance:** When a new fact replaces or refines an old one, include `supersedes` with the old data_point ID and `reason` explaining the change. Relationship types: supersedes, contradicts, led_to, refines, supports.

**Rules:**
- Reference existing memories by their `[id]` prefix from the Existing Memories section.
- Prefer UPDATE over ADD+DELETE when enriching (not contradicting).
- NOOP confirms existing memory is still accurate.
- Omit MEMORY_OPS block entirely if no memory changes needed.'''


def _build_preextracted_prompt(
    pending_dates: list[str],
    extracted_files: dict[str, str],
    synthesis_instructions: str,
    embedded_files: dict | None = None,
    vector_memories: list | None = None,
) -> str:
    """Build synthesis prompt with embedded content and structured output format.

    Args:
        pending_dates: List of dates to process (YYYY-MM-DD)
        extracted_files: Dict mapping date -> extract file path
        synthesis_instructions: Shared instructions block
        embedded_files: Pre-read content to embed inline:
            - "transcripts": dict[date, content] - transcript text per date
        vector_memories: Optional list of dicts with 'chunk_id' and 'content' from
            vector search. Used to build Existing Memories section for CRUD reference.
    """
    if embedded_files is None:
        embedded_files = {}

    dates_str = ", ".join(pending_dates)
    transcripts = embedded_files.get("transcripts", {})

    # Build embedded transcript sections
    transcript_sections = []
    for date in sorted(pending_dates):
        content = transcripts.get(date, "")
        if not content:
            # Fallback: read from extract file if not embedded
            path = extracted_files.get(date, "")
            if path:
                try:
                    content = Path(path).read_text(encoding="utf-8")
                except IOError:
                    content = "(transcript unavailable)"
        transcript_sections.append(f"### Transcript: {date}\n{content}")
    transcript_block = "\n\n".join(transcript_sections)

    # Build existing memories section from vector retrieval
    if vector_memories:
        memory_lines = [f"[{m['chunk_id']}] {m['content']}" for m in vector_memories]
        ltm_block = "## Existing Memories (reference by [chunk_id] in MEMORY_OPS)\n\n" + "\n".join(memory_lines)
    else:
        ltm_block = "(no existing memories)"

    # Build extract paths for synthesis.py apply command
    extract_paths = []
    for date in sorted(extracted_files.keys()):
        extract_paths.append(extracted_files[date])
    extracts_arg = " ".join(extract_paths)

    # Pre-compute output filename with PID so Write and Bash use the same path
    # (using $$ would expand in Bash but stay literal in Write, causing a mismatch)
    output_filename = f"/tmp/synthesis-output-{os.getpid()}.txt"

    return f'''You are a structured data extractor. Your job is to read session transcripts and produce ONLY delimited structured output — no prose, no commentary, no summary.

## Output Format

Your output must follow this exact structure. Here is a complete realistic example:

===PROJECT:myproject===
- [implement] Built REST API endpoints for user authentication
- [design] JWT tokens over session cookies — stateless scales better
- [LTM][gotcha] SQLAlchemy async sessions need explicit await session.close() or connections leak
- [LTM][GLOBAL][tip] git stash -u includes untracked files

===PROJECT:global===
- [pattern] pytest -x --tb=short stops on first failure with compact output

===END===

Every output uses ===PROJECT:name=== blocks and ends with ===END===. Nothing else.

## Delivery

Only use the Write and Bash tools — no other tools.

1. Write(`{output_filename}`, <your structured output>)
2. Bash: `python3 $HOME/.claude/scripts/synthesis.py apply {output_filename} --extracts {extracts_arg}`

## Synthesis Instructions

{synthesis_instructions}

## Existing Long-Term Memory (for dedup)

{ltm_block}

## Session Transcripts

**Pending dates:** {dates_str}

{transcript_block}

## Reminder

Output only the structured format shown above. Start with ===PROJECT:...=== and end with ===END===.'''


def _build_synthesis_prompt(
    pending_dates: list[str],
    extracted_files: dict[str, str],
    embedded_files: dict | None = None,
    vector_memories: list | None = None,
) -> str:
    """
    Build the embedded synthesis prompt for the subagent.

    Pre-extraction is required. If pre-extraction fails, synthesis is skipped
    (no auto-extract fallback).

    Args:
        pending_dates: List of pending date strings (YYYY-MM-DD)
        extracted_files: Dict mapping date -> file path (pre-extracted)
        embedded_files: Pre-read content to embed inline (transcripts)
        vector_memories: Optional list of dicts with 'chunk_id' and 'content'
            from vector search for targeted dedup context.
    """
    synthesis_instructions = _build_synthesis_instructions_v3()

    return _build_preextracted_prompt(
        pending_dates, extracted_files, synthesis_instructions, embedded_files,
        vector_memories=vector_memories,
    )



def _build_embedded_files(extracted_files: dict[str, str]) -> dict:
    """Pre-read transcript extract files for embedding in synthesis prompt.

    Args:
        extracted_files: Dict mapping date -> extract file path

    Returns:
        Dict with key: transcripts (dict[date, content])
    """
    embedded: dict = {"transcripts": {}}
    for date, path in extracted_files.items():
        try:
            embedded["transcripts"][date] = Path(path).read_text(encoding="utf-8")
        except IOError:
            pass
    return embedded


def _retrieve_vector_memories(transcript_text: str) -> list | None:
    """Retrieve existing memories relevant to transcript via vector search.

    Returns list of dicts with 'chunk_id' and 'content', or None if
    embeddings are unavailable or transcript is empty.
    """
    if not transcript_text or not transcript_text.strip():
        return None
    try:
        from synthesis_cron import retrieve_existing_memories
        memories = retrieve_existing_memories(transcript_text)
        return memories if memories else None
    except (ImportError, AttributeError):
        return None


def pre_extract_transcripts_incremental(
    pending_dates: list,
    exclude_session_id: str | None = None,
    output_dir: str = "/tmp",
) -> tuple[dict[str, str], dict[str, dict], dict[str, list[dict]]]:
    """Pre-extract transcripts incrementally using high water marks.

    Uses synthesis state to skip unchanged sessions and only extract delta
    content from grown sessions.

    Returns:
        (extracted_files, session_offsets, daily_data) where:
        - extracted_files: dict mapping date -> output file path
        - session_offsets: dict mapping session_id -> {"offset": int, "lines": int}
        - daily_data: dict mapping date -> list of session dicts (for project detection)
    """
    settings = load_settings()
    min_msgs = settings.get("synthesis", {}).get("minSessionMessages", 0)
    state = load_synthesis_state()
    pid = os.getpid()
    extracted_files: dict[str, str] = {}
    session_offsets: dict[str, dict] = {}

    try:
        daily_data = extract_transcripts_incremental(
            state, exclude_session_id=exclude_session_id,
            min_session_messages=min_msgs,
        )
    except Exception as e:
        print(f"Warning: Incremental extraction failed: {e}", file=sys.stderr)
        return {}, {}, {}

    for date in sorted(pending_dates):
        sessions = daily_data.get(date)
        if not sessions:
            continue

        output_path = f"{output_dir}/memory-extract-{date}-{pid}.txt"
        date_data = {date: sessions}
        Path(output_path).write_text(
            format_transcripts_incremental(date_data, total_line_budget=TRANSCRIPT_LINE_BUDGET),
            encoding="utf-8",
        )

        extracted_files[date] = output_path

        for s in sessions:
            session_offsets[s["session_id"]] = {
                "offset": s["current_offset"],
                "lines": s["current_lines"],
            }

    return extracted_files, session_offsets, daily_data


def write_synthesis_prompt(exclude_session_id: str | None = None) -> None:
    """Generate per-date synthesis prompts and write to temp files.

    Produces one prompt file per pending date so each date gets its own
    daily summary file (prevents multi-day collapse into a single file).

    Prints to stdout:
        model=<model>
        prompt_file=<path>     (one line per date)
    """
    settings = load_settings()
    model = settings.get("synthesis", {}).get("model", "sonnet")

    pending_dates = get_recent_days(exclude_session_id=exclude_session_id)
    if not pending_dates:
        print("No pending transcripts.")
        return

    extracted_files, _, _ = pre_extract_transcripts_incremental(
        pending_dates, exclude_session_id=exclude_session_id
    )

    if not extracted_files:
        print("No pending transcripts with content.")
        return

    print(f"model={model}")

    # Build one prompt per date to ensure each date gets its own daily file
    for date in sorted(extracted_files.keys()):
        single_date_files = {date: extracted_files[date]}

        embedded = _build_embedded_files(single_date_files)

        vector_memories = _retrieve_vector_memories(embedded.get("transcripts", {}).get(date, ""))

        prompt = _build_synthesis_prompt([date], single_date_files, embedded, vector_memories=vector_memories)

        prompt_path = f"{SYNTHESIS_PROMPT_DIR}/synthesis-prompt-{date}-{os.getpid()}.txt"
        Path(prompt_path).write_text(prompt, encoding="utf-8")

        print(f"prompt_file={prompt_path}")


# Constants for access tracking
REINFORCEMENT_ETA = 0.18  # Reinforcement rate (diminishing returns formula)


def _batch_update_data_point_access(
    conn: sqlite3.Connection, dp_ids: list[str], passive: bool = False
) -> None:
    """Increment access_count, update last_accessed, reinforce salience, and boost neighbors.

    Applies diminishing-returns salience reinforcement (REINFORCEMENT_ETA = 0.18)
    and associative graph-neighbor boosting for connected entity data_points.

    When passive=True, only access_count and last_accessed are updated (useful for
    SessionStart auto-loading where we want analytics but not salience inflation).
    When passive=False (default), full salience reinforcement and neighbor boosting apply
    (used by MCP search and other active access paths).

    Note: Does not commit. Caller must commit.
    """
    if not dp_ids:
        return
    timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    placeholders = ",".join("?" for _ in dp_ids)
    conn.execute(
        f"UPDATE data_points SET access_count = access_count + 1, "
        f"last_accessed = ? WHERE id IN ({placeholders})",
        [timestamp] + list(dp_ids),
    )

    # Skip salience reinforcement and neighbor boosting for passive loads
    if passive:
        return

    # Batch-fetch saliences to avoid N+1 queries
    salience_rows = conn.execute(
        f"SELECT id, salience FROM data_points WHERE id IN ({placeholders})",
        list(dp_ids),
    ).fetchall()
    salience_map = {row[0]: row[1] for row in salience_rows}

    for dp_id in dp_ids:
        if dp_id not in salience_map:
            continue
        current = salience_map[dp_id] if salience_map[dp_id] is not None else 1.0
        new_sal = min(1.0, current + REINFORCEMENT_ETA * (1.0 - current))
        conn.execute(
            "UPDATE data_points SET salience = ? WHERE id = ?",
            (new_sal, dp_id)
        )

        neighbors = conn.execute(
            "SELECT DISTINCT e.target, dp2.salience, e.weight FROM edges e "
            "JOIN data_points dp2 ON dp2.id = e.target "
            "WHERE e.source = ? AND e.valid_to IS NULL "
            "AND dp2.type = 'entity'",
            (dp_id,)
        ).fetchall()
        for target_id, neighbor_sal, edge_weight in neighbors:
            boost = REINFORCEMENT_ETA * (edge_weight if edge_weight is not None else 1.0) * new_sal
            new_neighbor_sal = min(1.0, (neighbor_sal if neighbor_sal is not None else 0.5) + boost)
            conn.execute(
                "UPDATE data_points SET salience = ? WHERE id = ?",
                (new_neighbor_sal, target_id)
            )


def _load_from_db(project_scope: str) -> tuple[str, list[dict], list[str]] | None:
    """Load memory context via SQL queries against data_points.

    Executes 5 query tiers in priority order with dedup:
    1. User profile (scope='user', salience>0.5)
    2. Session continuity (type='session_context', most recent for project, last 7 days)
    3. Project memories (type='memory', scope=project, salience>0.4, top 20)
    4. Global knowledge (type='memory', scope='global', salience>0.6, top 10)
    5. Recent activity (created_at > 3 days ago, top 15)

    Returns:
        (formatted_text, tiers_metadata, health_alerts) for v3 DBs,
        ("", [], []) if DB is empty/missing,
        None to signal that the DB is not at v3 (caller should use legacy loading).
    """
    try:
        from storage import close_db, get_db
    except ImportError:
        return None

    try:
        conn = get_db()
    except FileNotFoundError:
        return ("", [], [])

    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 3:
            return None  # Signal: use legacy loading

        seen_ids: set[str] = set()
        sections: list[str] = []
        tiers_metadata: list[dict] = []
        alerts: list[str] = []

        # Tier 1: User profile
        profiles = conn.execute(
            "SELECT id, content FROM data_points WHERE scope='user' AND salience > 0.5 "
            "ORDER BY salience DESC"
        ).fetchall()
        tier_ids = [row[0] for row in profiles]
        tier_tokens = sum(len(row[1] or "") // 4 for row in profiles)
        tiers_metadata.append({"name": "Profile", "count": len(tier_ids), "tokens_est": tier_tokens, "ids": tier_ids})
        if profiles:
            profile_text = "\n".join(row[1] for row in profiles if row[1])
            if profile_text.strip():
                sections.append(f"## Your Profile\n{profile_text}")
            seen_ids.update(row[0] for row in profiles)

        # Tier 2: Session continuity (E2)
        if project_scope:
            from memory_utils import get_project_working_days

            working_days = get_project_working_days(project_scope, 5)
            if working_days:
                continuity_cutoff = working_days[-1] + "T00:00:00Z"
            else:
                continuity_cutoff = (
                    datetime.now(timezone.utc) - timedelta(days=7)
                ).isoformat().replace("+00:00", "Z")
            context_row = conn.execute(
                "SELECT id, content, properties FROM data_points "
                "WHERE type='session_context' AND scope=? AND created_at > ? "
                "ORDER BY created_at DESC LIMIT 1",
                (project_scope, continuity_cutoff),
            ).fetchone()
            session_tier_ids = [context_row[0]] if context_row else []
            session_tier_tokens = sum(len(context_row[1] or "") // 4 for _ in [1]) if context_row else 0
            tiers_metadata.append({"name": "Session", "count": len(session_tier_ids), "tokens_est": session_tier_tokens, "ids": session_tier_ids})
            if context_row:
                ctx_id, ctx_content, ctx_props = context_row
                seen_ids.add(ctx_id)

                # Get connected entities via context_for edges
                edges = conn.execute(
                    "SELECT dp.name FROM edges e JOIN data_points dp ON dp.id = e.target "
                    "WHERE e.source = ? AND e.type = 'context_for' AND e.valid_to IS NULL",
                    (ctx_id,),
                ).fetchall()
                entity_names = [e[0] for e in edges if e[0]]

                # Extract status from properties JSON
                status = ""
                if ctx_props:
                    try:
                        props = json.loads(ctx_props)
                        status = props.get("status", "")
                    except json.JSONDecodeError:
                        pass

                section = f"## Last Session\nYou were working on: {ctx_content}"
                if entity_names:
                    section += f"\nEntities: {', '.join(entity_names)}"
                if status:
                    section += f"\nStatus: {status}"
                sections.append(section)
        else:
            tiers_metadata.append({"name": "Session", "count": 0, "tokens_est": 0, "ids": []})

        # Tier 3: Project memories
        if project_scope:
            rows = conn.execute(
                "SELECT id, content FROM data_points "
                "WHERE scope=? AND type='memory' AND salience > 0.4 "
                "ORDER BY salience DESC, last_accessed DESC LIMIT 20",
                (project_scope,),
            ).fetchall()
            new_rows = [(r[0], r[1]) for r in rows if r[0] not in seen_ids]
            project_tier_ids = [r[0] for r in new_rows]
            project_tier_tokens = sum(len(r[1] or "") // 4 for r in new_rows)
            tiers_metadata.append({"name": "Project", "count": len(project_tier_ids), "tokens_est": project_tier_tokens, "ids": project_tier_ids})
            if new_rows:
                mem_text = "\n".join(f"- {r[1]}" for r in new_rows if r[1])
                if mem_text.strip():
                    sections.append(f"## Project Memory: {project_scope}\n{mem_text}")
                seen_ids.update(r[0] for r in new_rows)
        else:
            tiers_metadata.append({"name": "Project", "count": 0, "tokens_est": 0, "ids": []})

        # Tier 4: Global knowledge
        rows = conn.execute(
            "SELECT id, content FROM data_points "
            "WHERE scope='global' AND type='memory' AND salience > 0.6 "
            "ORDER BY salience DESC, last_accessed DESC LIMIT 10",
        ).fetchall()
        new_rows = [(r[0], r[1]) for r in rows if r[0] not in seen_ids]
        global_tier_ids = [r[0] for r in new_rows]
        global_tier_tokens = sum(len(r[1] or "") // 4 for r in new_rows)
        tiers_metadata.append({"name": "Global", "count": len(global_tier_ids), "tokens_est": global_tier_tokens, "ids": global_tier_ids})
        if new_rows:
            mem_text = "\n".join(f"- {r[1]}" for r in new_rows if r[1])
            if mem_text.strip():
                sections.append(f"## Global Knowledge\n{mem_text}")
            seen_ids.update(r[0] for r in new_rows)

        # Tier 5: Recent activity (last 3 working days)
        from memory_utils import get_global_working_days

        global_working = get_global_working_days(3)
        if global_working:
            three_days_ago = global_working[-1] + "T00:00:00Z"
        else:
            three_days_ago = (
                datetime.now(timezone.utc) - timedelta(days=3)
            ).isoformat().replace("+00:00", "Z")
        scope_param = project_scope or "global"
        rows = conn.execute(
            "SELECT id, content FROM data_points "
            "WHERE scope IN ('global', ?) AND type='memory' "
            "AND created_at > ? "
            "ORDER BY created_at DESC LIMIT 15",
            (scope_param, three_days_ago),
        ).fetchall()
        new_rows = [(r[0], r[1]) for r in rows if r[0] not in seen_ids]
        recent_tier_ids = [r[0] for r in new_rows]
        recent_tier_tokens = sum(len(r[1] or "") // 4 for r in new_rows)
        tiers_metadata.append({"name": "Recent", "count": len(recent_tier_ids), "tokens_est": recent_tier_tokens, "ids": recent_tier_ids})
        if new_rows:
            mem_text = "\n".join(f"- {r[1]}" for r in new_rows if r[1])
            if mem_text.strip():
                sections.append(f"## Recent Activity\n{mem_text}")
            seen_ids.update(r[0] for r in new_rows)

        # Access tracking for all served data_points
        if seen_ids:
            try:
                _batch_update_data_point_access(conn, list(seen_ids), passive=True)
                conn.commit()
            except Exception as e:
                print(f"Warning: Access tracking failed: {e}", file=sys.stderr)

        try:
            from health import health_alerts as _health_alerts, health_report  # noqa: I001
            hr = health_report(conn)
            alerts = _health_alerts(hr)
            if alerts:
                sections.append("\n## Health Alerts")
                for alert in alerts:
                    sections.append(f"- {alert}")
        except Exception:
            pass

        from memory_utils import sanitize_secrets
        return (sanitize_secrets("\n\n".join(sections)), tiers_metadata, alerts)

    finally:
        close_db(conn)


def main() -> None:
    """Main entry point - outputs memory context to stdout."""
    check_python_version()

    # Skip memory injection if explicitly disabled (e.g., ccblocks ping sessions)
    if os.environ.get("CLAUDE_SKIP_MEMORY"):
        return

    # Consume stdin JSON from SessionStart hook (prevents broken pipe)
    stdin_payload = {}
    try:
        if not sys.stdin.isatty():
            stdin_payload = json.load(sys.stdin)
    except (json.JSONDecodeError, IOError):
        pass
    session_id = stdin_payload.get("sessionId", f"session-{int(time.time())}")

    # Rotate injection log
    try:
        from injection_log import rotate_log
        rotate_log()
    except Exception:
        pass

    # Clean up stale prompt-recall state files
    try:
        from prompt_recall import cleanup_stale_state_files
        cleanup_stale_state_files(get_memory_dir())
    except Exception:
        pass

    # Start output
    print("<memory>")

    # Include current local time for context
    now = datetime.now(timezone.utc).astimezone()
    utc_offset = now.utcoffset() or timedelta(0)
    utc_offset_hours = utc_offset.total_seconds() / 3600
    offset_sign = "+" if utc_offset_hours >= 0 else ""
    print(f"Current time: {now.strftime('%Y-%m-%d %H:%M')} (UTC{offset_sign}{utc_offset_hours:.0f})")
    print()

    # Surface synthesis errors from deferred runs
    error_alert = check_synthesis_errors()
    if error_alert:
        print(error_alert)
        print()

    # Detect current project (resolve worktree/subdir to repo root for matching)
    pwd = resolve_session_path(os.getcwd())
    projects_index = load_json_file(get_projects_index_file(), {})
    current_project = find_current_project(projects_index, pwd)
    project_scope = current_project.get("name", "") if current_project else ""

    # DB-first loading (v3 schema); falls back to empty context if no v3 DB
    _t0 = time.monotonic()
    db_result = _load_from_db(project_scope)
    _latency_ms = (time.monotonic() - _t0) * 1000

    if db_result is None:
        db_content, tiers_meta, health_alerts = None, [], []
    else:
        db_content, tiers_meta, health_alerts = db_result

    if db_content is not None and db_content.strip():
        print(db_content)
        print()
    print("</memory>")

    # Fire-and-forget injection logging
    try:
        from injection_log import log_session_start
        log_session_start(
            session_id=session_id,
            project_scope=project_scope,
            tiers=tiers_meta,
            latency_ms=_latency_ms,
            health_alerts=health_alerts,
        )
    except Exception:
        pass


if __name__ == "__main__":
    # --synthesis-prompt: output just the subagent prompt (for /synthesize skill)
    if len(sys.argv) > 1 and sys.argv[1] == "--synthesis-prompt":
        exclude_id = None
        if len(sys.argv) > 3 and sys.argv[2] == "--exclude-session":
            exclude_id = sys.argv[3]
        write_synthesis_prompt(exclude_session_id=exclude_id)
    else:
        main()
