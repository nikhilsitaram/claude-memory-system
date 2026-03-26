#!/usr/bin/env python3
"""
SessionStart hook - loads memory context for Claude Code.

This script runs on: startup, resume, clear, compact

It performs:
1. SQL-ranked loading from data_points (user profile, session continuity,
   project memories, global knowledge, recent activity)
2. Falls back to markdown LTM files if the database is unavailable
3. Checks for pending transcripts and prompts for synthesis

Output is printed to stdout and injected into Claude Code's context.

Requirements: Python 3.9+
"""

import json
import os
import sqlite3
import sys
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
    get_daily_dir,
    get_global_memory_file,
    get_memory_dir,
    get_project_memory_dir,
    get_projects_index_file,
    get_synthesis_error_log,
    load_json_file,
    load_settings,
    load_synthesis_state,
    resolve_project_path_to_name,
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
#   _load_from_db(project_scope) -> str | None
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



def _get_project_names_str() -> str:
    """Load registered project names from index, formatted for prompt insertion."""
    projects_index = load_json_file(get_projects_index_file(), {})
    project_names = sorted({
        data.get("name", "")
        for data in projects_index.get("projects", {}).values()
        if data.get("name")
    })
    return ", ".join(f"`{n}`" for n in project_names) if project_names else "(none registered)"


import re

# Profile sections in global LTM that are never dedup targets
_PROFILE_HEADERS_RE = re.compile(
    r"^## (?:About Me|Current Projects|Technical Environment|Patterns & Preferences)\s*$"
)


def _strip_profile_sections(content: str) -> str:
    """Strip auto-pinned profile sections from global LTM for synthesis prompts.

    Removes About Me, Current Projects, Technical Environment, and
    Patterns & Preferences sections (including their content) since these
    are never dedup targets and add ~4KB of bloat.

    Keeps: title line, ## Pinned, ## Key * sections, and all other headers.
    """
    if not content:
        return content

    lines = content.split("\n")
    result: list[str] = []
    skipping = False

    for line in lines:
        if _PROFILE_HEADERS_RE.match(line):
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            result.append(line)

    return "\n".join(result)


def _build_synthesis_instructions(project_names_str: str) -> str:
    """Build the shared synthesis instructions block."""
    return f'''**Output format:**

Group entries by project. The system handles section placement and scope injection automatically.

```
===PROJECT:projectname===
- [type] Description of what happened or was learned
- [LTM][type] Important entry to route to long-term memory
- [GLOBAL][type] Cross-project entry (useful across all projects)
- [LTM][GLOBAL][type] Cross-project entry that also routes to LTM
===PROJECT:global===
- [type] Entries not tied to any specific project
===END===
```

**Entry types:** implement, improve, document, analyze, design, tradeoff, scope, gotcha, pitfall, pattern, insight, tip, workaround.

**Project names:** Use the project names from the session headers: {project_names_str}. Use `global` for entries not tied to a specific project.

**[LTM] flag (be HIGHLY selective):** Prefix entries worth preserving long-term. Route to LTM:
- Multi-day implementations, novel integrations, reusable setups
- Architecture choices, design tradeoffs, scope decisions with lasting impact
- Non-obvious gotchas, proven patterns, hard-won lessons
- Mental models, useful commands, workarounds
Do NOT route:
- Common dev knowledge (git basics, standard SQL, well-known language features)
- Generic software patterns (DRY, separation of concerns, use env vars)
- One-time fixes unlikely to recur
- Version-specific notes, easily re-discoverable things
Maximum 5 [LTM] entries per project per synthesis run.

**[GLOBAL] flag:** Only for genuinely cross-project learnings (OS behavior, tool tips, general dev practices). Most entries should stay project-scoped.

**Compactness:** Final solutions only, one entry per concept, omit routine details.

**Global LTM auto-pinned maintenance:** The global LTM has auto-pinned sections (About Me, Current Projects, Technical Environment, Patterns & Preferences). When transcripts show clear evidence of change — a project completed, a new tool adopted — update the relevant entry. Be conservative.

**Entity extraction:** Every CRUD operation in the MEMORY_OPS block should include an `entities` array listing structured data extracted from the fact:
- Project names (e.g., "myproject", "claude-memory-system")
- Library/tool names (e.g., "pytest", "sqlite-vec", "gRPC")
- Concepts (e.g., "bi-temporal tracking", "WAL mode")
- People (e.g., "John", "@username")
- URLs (e.g., "https://github.com/...")
- Dates (e.g., "2026-03-21")

Be comprehensive but precise. Only include entities actually present in the fact.

**Memory CRUD operations (Phase 2):** After the PROJECT blocks, output a MEMORY_OPS block with explicit decisions about existing memories:

```
===MEMORY_OPS===
{{"ops": [
  {{"action": "ADD", "fact": "description of new fact", "scope": "project-name", "section": "Key Decisions", "type": "design", "entities": ["entity1", "entity2"]}},
  {{"action": "UPDATE", "id": "chunk_id_from_existing", "fact": "updated description", "entities": ["entity1"]}},
  {{"action": "DELETE", "id": "chunk_id_from_existing", "reason": "Contradicted: explanation of why this is no longer true"}},
  {{"action": "NOOP", "id": "chunk_id_from_existing", "reason": "Already accurately captured"}}
]}}
```

**Actions:**
- **ADD**: New fact not present in existing memories. Include scope, section, type, entities.
- **UPDATE**: Existing memory needs modification (enrichment, correction). Reference by `id` from Existing Memories. Include updated fact and entities.
- **DELETE**: Existing memory is contradicted by new evidence. Reference by `id`. Include reason explaining the contradiction.
- **NOOP**: Existing memory is confirmed correct by new evidence. Reference by `id`. Optional.

**Rules:**
- Reference existing memories by their `[chunk_id]` prefix from the Existing Memories section.
- Every ADD must include `entities` array with extracted structured data.
- Prefer UPDATE over ADD+DELETE when a fact is being enriched (not contradicted).
- MEMORY_OPS block is optional — omit it if no memory changes are needed.'''


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
            - "global_ltm": str - global LTM file content
            - "project_ltms": dict[project, content] - project LTM content
        vector_memories: Optional list of dicts with 'chunk_id' and 'content' from
            vector search. When provided, replaces full LTM embedding with targeted
            Existing Memories section using chunk IDs for CRUD reference.
    """
    if embedded_files is None:
        embedded_files = {}

    dates_str = ", ".join(pending_dates)
    transcripts = embedded_files.get("transcripts", {})
    global_ltm = embedded_files.get("global_ltm", "")
    project_ltms = embedded_files.get("project_ltms", {})

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

    # Build LTM sections for dedup context
    # When vector_memories is provided, use targeted retrieval instead of full LTM
    if vector_memories:
        memory_lines = [f"[{m['chunk_id']}] {m['content']}" for m in vector_memories]
        ltm_block = "## Existing Memories (reference by [chunk_id] in MEMORY_OPS)\n\n" + "\n".join(memory_lines)
    else:
        ltm_sections = []
        if global_ltm:
            ltm_sections.append(f"### Global Long-Term Memory\n{global_ltm}")
        for project, content in sorted(project_ltms.items()):
            if content:
                ltm_sections.append(f"### Project Long-Term Memory: {project}\n{content}")
        ltm_block = "\n\n".join(ltm_sections) if ltm_sections else "(no existing LTM content)"

    # Build existing daily merge context (for incremental synthesis)
    existing_dailies = embedded_files.get("existing_dailies", {})
    merge_sections = []
    for date in sorted(pending_dates):
        existing = existing_dailies.get(date, "")
        if existing:
            merge_sections.append(f"### Existing daily summary for {date}\n{existing}")

    merge_instructions = ""
    if merge_sections:
        merge_block = "\n\n".join(merge_sections)
        merge_instructions = f"""
## Existing Daily Summaries (READ-ONLY context — do NOT repeat these entries)

These daily files already exist. The system will merge your output automatically.
Output ONLY entries from new/continued sessions — do not re-state anything below.

{merge_block}

"""

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
{merge_instructions}
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
        embedded_files: Pre-read content to embed inline (transcripts, LTM content)
        vector_memories: Optional list of dicts with 'chunk_id' and 'content'
            from vector search for targeted dedup context.
    """
    project_names_str = _get_project_names_str()
    synthesis_instructions = _build_synthesis_instructions(project_names_str)

    return _build_preextracted_prompt(
        pending_dates, extracted_files, synthesis_instructions, embedded_files,
        vector_memories=vector_memories,
    )


def _find_projects_in_extracts(daily_data: dict[str, list[dict]]) -> set[str]:
    """Find project names from extracted session data.

    Delegates path-to-name resolution to
    ``memory_utils.resolve_project_path_to_name()``.

    Args:
        daily_data: Dict mapping date -> list of session dicts

    Returns set of project names that had sessions extracted.
    """
    result: set[str] = set()
    for sessions in daily_data.values():
        for s in sessions:
            pp = s.get("project_path")
            if pp:
                name = resolve_project_path_to_name(pp)
                if name:
                    result.add(name)
    return result


def _build_embedded_files(
    extracted_files: dict[str, str],
    include_dailies: bool = False,
    daily_data: dict[str, list[dict]] | None = None,
) -> dict:
    """Pre-read all files for embedding in synthesis prompt.

    Reads transcript extracts, global LTM, and project LTMs into memory
    so the synthesis prompt can embed them inline (zero tool calls).

    Args:
        extracted_files: Dict mapping date -> extract file path
        include_dailies: If True, read existing daily summary files as merge context
        daily_data: Dict mapping date -> list of session dicts (for project detection)

    Returns:
        Dict with keys: transcripts, global_ltm, project_ltms, (existing_dailies)
    """
    embedded: dict = {"transcripts": {}, "global_ltm": "", "project_ltms": {}}
    for date, path in extracted_files.items():
        try:
            embedded["transcripts"][date] = Path(path).read_text(encoding="utf-8")
        except IOError:
            pass
    global_ltm_file = get_global_memory_file()
    if global_ltm_file.exists():
        try:
            embedded["global_ltm"] = _strip_profile_sections(
                global_ltm_file.read_text(encoding="utf-8")
            )
        except IOError:
            pass
    # Only include project LTMs for projects that had sessions extracted
    relevant_projects = _find_projects_in_extracts(daily_data or {})
    proj_dir = get_project_memory_dir()
    if proj_dir.exists():
        for f in proj_dir.glob("*-long-term-memory.md"):
            name = f.stem.replace("-long-term-memory", "")
            if name in relevant_projects:
                try:
                    embedded["project_ltms"][name] = f.read_text(encoding="utf-8")
                except IOError:
                    pass
    # Read existing daily files as merge context (for incremental synthesis)
    if include_dailies:
        daily_dir = get_daily_dir()
        embedded["existing_dailies"] = {}
        for date in extracted_files:
            daily_file = daily_dir / f"{date}.md"
            if daily_file.exists():
                try:
                    embedded["existing_dailies"][date] = daily_file.read_text(encoding="utf-8")
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

    extracted_files, session_offsets, daily_data = pre_extract_transcripts_incremental(
        pending_dates, exclude_session_id=exclude_session_id
    )

    if not extracted_files:
        print("No pending transcripts with content.")
        return

    print(f"model={model}")

    # Build one prompt per date to ensure each date gets its own daily file
    for date in sorted(extracted_files.keys()):
        single_date_files = {date: extracted_files[date]}
        single_date_data = {date: daily_data.get(date, [])} if daily_data else {}

        include_dailies = bool(session_offsets)
        embedded = _build_embedded_files(
            single_date_files, include_dailies=include_dailies,
            daily_data=single_date_data,
        )

        vector_memories = _retrieve_vector_memories(embedded.get("transcripts", {}).get(date, ""))

        prompt = _build_synthesis_prompt([date], single_date_files, embedded, vector_memories=vector_memories)

        prompt_path = f"{SYNTHESIS_PROMPT_DIR}/synthesis-prompt-{date}-{os.getpid()}.txt"
        Path(prompt_path).write_text(prompt, encoding="utf-8")

        print(f"prompt_file={prompt_path}")


# Constants for access tracking
REINFORCEMENT_ETA = 0.18  # Reinforcement rate (diminishing returns formula)


def _batch_update_data_point_access(conn: sqlite3.Connection, dp_ids: list[str]) -> None:
    """Increment access_count, update last_accessed, reinforce salience, and boost neighbors.

    Applies diminishing-returns salience reinforcement (REINFORCEMENT_ETA = 0.18)
    and associative graph-neighbor boosting for connected entity data_points.

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


def _load_from_db(project_scope: str) -> str | None:
    """Load memory context via SQL queries against data_points.

    Executes 5 query tiers in priority order with dedup:
    1. User profile (scope='user', salience>0.5)
    2. Session continuity (type='session_context', most recent for project, last 7 days)
    3. Project memories (type='memory', scope=project, salience>0.4, top 20)
    4. Global knowledge (type='memory', scope='global', salience>0.6, top 10)
    5. Recent activity (created_at > 3 days ago, top 15)

    Returns formatted context string, empty string if DB is empty, or None to
    signal that the DB is not at v3 (caller should use legacy markdown loading).
    """
    try:
        from storage import close_db, get_db
    except ImportError:
        return None

    try:
        conn = get_db()
    except FileNotFoundError:
        return ""

    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 3:
            return None  # Signal: use legacy loading

        seen_ids: set[str] = set()
        sections: list[str] = []

        # Tier 1: User profile
        profiles = conn.execute(
            "SELECT id, content FROM data_points WHERE scope='user' AND salience > 0.5 "
            "ORDER BY salience DESC"
        ).fetchall()
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

        # Tier 3: Project memories
        if project_scope:
            rows = conn.execute(
                "SELECT id, content FROM data_points "
                "WHERE scope=? AND type='memory' AND salience > 0.4 "
                "ORDER BY salience DESC, last_accessed DESC LIMIT 20",
                (project_scope,),
            ).fetchall()
            new_rows = [(r[0], r[1]) for r in rows if r[0] not in seen_ids]
            if new_rows:
                mem_text = "\n".join(f"- {r[1]}" for r in new_rows if r[1])
                if mem_text.strip():
                    sections.append(f"## Project Memory: {project_scope}\n{mem_text}")
                seen_ids.update(r[0] for r in new_rows)

        # Tier 4: Global knowledge
        rows = conn.execute(
            "SELECT id, content FROM data_points "
            "WHERE scope='global' AND type='memory' AND salience > 0.6 "
            "ORDER BY salience DESC, last_accessed DESC LIMIT 10",
        ).fetchall()
        new_rows = [(r[0], r[1]) for r in rows if r[0] not in seen_ids]
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
        if new_rows:
            mem_text = "\n".join(f"- {r[1]}" for r in new_rows if r[1])
            if mem_text.strip():
                sections.append(f"## Recent Activity\n{mem_text}")
            seen_ids.update(r[0] for r in new_rows)

        # Access tracking for all served data_points
        if seen_ids:
            try:
                _batch_update_data_point_access(conn, list(seen_ids))
                conn.commit()
            except Exception as e:
                print(f"Warning: Access tracking failed: {e}", file=sys.stderr)

        try:
            from health import health_alerts, health_report
            hr = health_report(conn)
            alerts = health_alerts(hr)
            if alerts:
                sections.append("\n## Health Alerts")
                for alert in alerts:
                    sections.append(f"- {alert}")
        except Exception:
            pass

        from memory_utils import sanitize_secrets
        return sanitize_secrets("\n\n".join(sections))

    finally:
        close_db(conn)


def main() -> None:
    """Main entry point - outputs memory context to stdout."""
    check_python_version()

    # Skip memory injection if explicitly disabled (e.g., ccblocks ping sessions)
    if os.environ.get("CLAUDE_SKIP_MEMORY"):
        return

    # Parse session_id from SessionStart hook stdin JSON
    current_session_id = None
    try:
        if not sys.stdin.isatty():
            hook_input = json.load(sys.stdin)
            current_session_id = hook_input.get("session_id")
    except (json.JSONDecodeError, IOError):
        pass  # Not called from hook, or invalid input — safe to continue

    # Clean up stale prompt-recall state files
    try:
        from prompt_recall import cleanup_stale_state_files
        cleanup_stale_state_files(get_memory_dir())
    except Exception:
        pass

    # Load settings
    settings = load_settings()

    # Start output
    print("<memory>")

    # Include current local time for context
    now = datetime.now(timezone.utc).astimezone()
    utc_offset_hours = now.utcoffset().total_seconds() / 3600
    offset_sign = "+" if utc_offset_hours >= 0 else ""
    print(f"Current time: {now.strftime('%Y-%m-%d %H:%M')} (UTC{offset_sign}{utc_offset_hours:.0f})")
    print()

    # Surface synthesis errors from deferred runs
    error_alert = check_synthesis_errors()
    if error_alert:
        print(error_alert)
        print()

    # Check for pending transcripts (only if synthesis scheduling allows)
    # Exclude current session — it's still active and shouldn't be synthesized
    pending_dates = get_recent_days(exclude_session_id=current_session_id)
    synthesis_deferred = settings.get("synthesis", {}).get("deferred", True)
    # v3 schema requires deferred synthesis (synthesis_cron.py handles MEMORY_OPS pipeline).
    # The in-session v2 prompt format (===PROJECT:=== blocks + synthesis.py apply) is incompatible
    # with v3's DB-only writes. Force deferred mode when on v3.
    if not synthesis_deferred:
        try:
            from storage import _get_schema_version, close_db, get_db
            _check_conn = get_db()
            if _get_schema_version(_check_conn) >= 3:
                synthesis_deferred = True
            close_db(_check_conn)
        except Exception:
            pass
    if pending_dates and should_synthesize(settings) and not synthesis_deferred:
        # Write timestamp eagerly to prevent duplicate synthesis when multiple
        # sessions start simultaneously (all would see stale timestamp otherwise)
        # Format: ISO format with UTC timezone (e.g., "2026-02-03T14:30:00+00:00")
        get_last_synthesis_file().write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )

        synthesis_model = settings.get("synthesis", {}).get("model", "sonnet")
        synthesis_background = settings.get("synthesis", {}).get("background", True)

        # Pre-extract all transcripts before launching subagent (faster, fewer tool calls)
        extracted_files, session_offsets, daily_data = pre_extract_transcripts_incremental(
            pending_dates, exclude_session_id=current_session_id
        )

        if extracted_files:
            # Build one prompt per date to ensure separate daily files
            prompt_paths = []
            for date in sorted(extracted_files.keys()):
                single_date_files = {date: extracted_files[date]}
                single_date_data = {date: daily_data.get(date, [])} if daily_data else {}

                include_dailies = bool(session_offsets)
                embedded = _build_embedded_files(
                    single_date_files, include_dailies=include_dailies,
                    daily_data=single_date_data,
                )

                vector_memories = _retrieve_vector_memories(embedded.get("transcripts", {}).get(date, ""))

                synth_prompt = _build_synthesis_prompt(
                    [date], single_date_files, embedded, vector_memories=vector_memories
                )

                prompt_path = f"{SYNTHESIS_PROMPT_DIR}/synthesis-prompt-{date}-{os.getpid()}.txt"
                Path(prompt_path).write_text(synth_prompt, encoding="utf-8")
                prompt_paths.append(prompt_path)

            print("## AUTO-SYNTHESIZE REQUIRED")
            print(f"There are {len(pending_dates)} pending date(s): {', '.join(pending_dates)}.")
            print()
            if len(prompt_paths) == 1:
                prompt_path = prompt_paths[0]
                if synthesis_background:
                    print("**IMPORTANT**: Spawn a **background** subagent immediately, then respond to the user.")
                else:
                    print("**IMPORTANT**: Before responding to the user, spawn a subagent to process transcripts.")
                print(f"Read the prompt file at `{prompt_path}` and use it as the subagent prompt:")
                print(f'Task(subagent_type="general-purpose", model="{synthesis_model}"'
                      f'{", run_in_background=true" if synthesis_background else ""}, prompt=<contents of {prompt_path}>)')
                print()
            else:
                # Multiple dates: run sequentially to avoid LTM write conflicts
                if synthesis_background:
                    print("**IMPORTANT**: Spawn a **single background** subagent that processes ALL prompt files **sequentially** (one at a time).")
                else:
                    print("**IMPORTANT**: Spawn a **single** subagent that processes ALL prompt files **sequentially**.")
                print("Do NOT spawn parallel subagents — they write to shared LTM files.")
                print()
                for i, prompt_path in enumerate(prompt_paths, 1):
                    print(f"  {i}. Read `{prompt_path}` and execute it")
                print()
                # Combine all prompts into a single wrapper prompt
                combined_prompt_path = f"{SYNTHESIS_PROMPT_DIR}/synthesis-combined-{os.getpid()}.txt"
                combined_parts = []
                for prompt_path in prompt_paths:
                    combined_parts.append(Path(prompt_path).read_text(encoding="utf-8"))
                combined_prompt = "\n\n---\n\nAfter completing the above, proceed to the next synthesis task:\n\n".join(combined_parts)
                Path(combined_prompt_path).write_text(combined_prompt, encoding="utf-8")
                print(f'Task(subagent_type="general-purpose", model="{synthesis_model}"'
                      f'{", run_in_background=true" if synthesis_background else ""}, prompt=<contents of {combined_prompt_path}>)')
                print()

    # Detect current project (resolve worktree/subdir to repo root for matching)
    pwd = resolve_session_path(os.getcwd())
    projects_index = load_json_file(get_projects_index_file(), {})
    current_project = find_current_project(projects_index, pwd)
    project_scope = current_project.get("name", "") if current_project else ""

    # DB-first loading (v3 schema); falls back to empty context if no v3 DB
    db_content = _load_from_db(project_scope)
    if db_content is not None and db_content.strip():
        print(db_content)
        print()
    print("</memory>")


if __name__ == "__main__":
    # --synthesis-prompt: output just the subagent prompt (for /synthesize skill)
    if len(sys.argv) > 1 and sys.argv[1] == "--synthesis-prompt":
        exclude_id = None
        if len(sys.argv) > 3 and sys.argv[2] == "--exclude-session":
            exclude_id = sys.argv[3]
        write_synthesis_prompt(exclude_session_id=exclude_id)
    else:
        main()
