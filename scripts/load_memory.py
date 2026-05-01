#!/usr/bin/env python3
"""
SessionStart hook - loads memory context for Claude Code.

This script runs on: startup, resume, clear, compact

Output sections (in order):
1. Read instruction (for truncated-output recovery)
2. Timestamp + synthesis errors
3. Previous session recall
4. Global long-term memory
5. Project long-term memory (if applicable)        — full mode only
6. Project short-term memory (filtered [project/*]) — full mode only
7. Global short-term memory (filtered [global/*])  — full mode only

In `mode: light`, only sections 1-4 are emitted; project LTM/STM and global STM
are skipped (rely on Claude Code's native project-scoped memory).

Output is printed to stdout and injected into Claude Code's context.

Requirements: Python 3.9+
"""

import json
import os
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
    filter_daily_content,
    find_current_project,
    get_daily_dir,
    get_global_memory_file,
    get_memory_dir,
    get_pending_recall_dir,
    get_project_memory_dir,
    get_projects_index_file,
    get_synthesis_error_log,
    get_working_days,
    load_json_file,
    load_settings,
    load_synthesis_state,
    project_name_to_filename,
    resolve_project_path_to_name,
    resolve_session_path,
)
from synthesis import ROUTE_CAP
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
#   main()                  SessionStart hook — outputs all memory sections to
#                           stdout; read instruction in first 2KB prompts Claude
#                           to read the full output file when Claude Code truncates
# Memory loading:
#   load_global_memory() -> (str, int)
#   load_project_memory(name) -> (str, int)
#   load_daily_summaries(days, scope) -> (list[(date, content)], int)
#   load_project_history(project, days) -> (list[(date, content)], int)
#   load_pending_recall(session_id, project, cwd) -> (str, int)
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


def load_global_memory() -> tuple[str, int]:
    """Load global long-term memory file. Returns (content, bytes)."""
    global_file = get_global_memory_file()
    if not global_file.exists():
        return "", 0

    try:
        content = global_file.read_text(encoding="utf-8")
        return content, len(content.encode("utf-8"))
    except IOError:
        return "", 0


def load_project_memory(project_name: str) -> tuple[str, int]:
    """Load project-specific long-term memory. Returns (content, bytes)."""
    project_memory_dir = get_project_memory_dir()
    filename = project_name_to_filename(project_name)
    project_file = project_memory_dir / filename

    if not project_file.exists():
        return "", 0

    try:
        content = project_file.read_text(encoding="utf-8")
        return content, len(content.encode("utf-8"))
    except IOError:
        return "", 0


def load_daily_summaries(days_limit: int, scope: str = "global") -> tuple[list[tuple[str, str]], int]:
    """
    Load recent daily summaries, filtered by scope.

    Args:
        days_limit: Maximum number of working days to load
        scope: Filter scope - "global" for global entries, or project name for project entries

    Returns (list of (date, content) tuples, total bytes).
    """
    daily_dir = get_daily_dir()
    working_days = get_working_days(days_limit)
    summaries = []
    total_bytes = 0

    for date in working_days:
        daily_file = daily_dir / f"{date}.md"
        if daily_file.exists():
            try:
                raw_content = daily_file.read_text(encoding="utf-8")
                filtered_content = filter_daily_content(raw_content, scope)
                if filtered_content:
                    summaries.append((date, filtered_content))
                    total_bytes += len(filtered_content.encode("utf-8"))
            except IOError:
                continue

    return summaries, total_bytes


def load_project_history(
    project: dict, days_limit: int
) -> tuple[list[tuple[str, str]], int]:
    """
    Load project-specific work history (days worked in this project).

    Filters content to only include entries tagged with this project's name.
    Returns (list of (date, content) tuples, total bytes).
    """
    daily_dir = get_daily_dir()
    project_name = project.get("name", "")

    if not project_name:
        return [], 0

    # Get all daily files and filter by project content
    # We scan all daily files since project work may exist on any day
    all_daily_files = sorted(daily_dir.glob("*.md"), reverse=True)

    summaries = []
    total_bytes = 0

    for daily_file in all_daily_files:
        if len(summaries) >= days_limit:
            break

        try:
            raw_content = daily_file.read_text(encoding="utf-8")
            filtered_content = filter_daily_content(raw_content, project_name)
            if filtered_content:
                date = daily_file.stem  # YYYY-MM-DD from filename
                summaries.append((date, filtered_content))
                total_bytes += len(filtered_content.encode("utf-8"))
        except IOError:
            continue

    # Output oldest first for chronological reading
    summaries.reverse()

    return summaries, total_bytes


def load_pending_recall(
    current_session_id: str | None,
    current_project_name: str,
    resolved_cwd: str,
) -> tuple[str, int]:
    """Load most recent pending recall for current project.

    Two-tier matching:
    1. If current_project_name is set, match by project field
    2. Otherwise, match by resolved CWD path

    Deletes files older than 24 hours (safety net for unprocessed sessions).
    Returns (formatted_section, byte_count).
    """
    settings = load_settings()
    if not settings.get("previousSessionRecall", {}).get("enabled", True):
        return "", 0

    recall_dir = get_pending_recall_dir()
    if not recall_dir.exists():
        return "", 0

    now = time.time()
    cutoff = 24 * 3600  # 24 hours

    candidates = []
    for f in recall_dir.glob("*.md"):
        # Safety net: delete old files
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if now - mtime > cutoff:
            try:
                f.unlink()
            except OSError:
                pass
            continue

        # Skip current session
        session_id = f.stem
        if session_id == current_session_id:
            continue

        # Parse frontmatter for matching
        try:
            file_content = f.read_text(encoding="utf-8")
        except IOError:
            continue

        # Line-by-line frontmatter parser (avoids index("---") substring false matches)
        project = ""
        cwd = ""
        timestamp = ""
        if file_content.startswith("---"):
            in_frontmatter = False
            for line in file_content.splitlines():
                if line.strip() == "---":
                    if not in_frontmatter:
                        in_frontmatter = True
                        continue
                    else:
                        break
                if in_frontmatter:
                    if line.startswith("project:"):
                        project = line.split(":", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("cwd:"):
                        cwd = line.split(":", 1)[1].strip()
                    elif line.startswith("timestamp:"):
                        timestamp = line.split(":", 1)[1].strip()

        # Two-tier matching
        if current_project_name:
            if project != current_project_name:
                continue
        else:
            if cwd != resolved_cwd:
                continue

        candidates.append((mtime, f, file_content, timestamp))

    if not candidates:
        return "", 0

    # Sort by mtime descending, take most recent
    candidates.sort(key=lambda x: x[0], reverse=True)
    _, _, file_content, timestamp = candidates[0]

    # Strip frontmatter for display (line-by-line to avoid --- substring false matches)
    body = file_content.strip()
    if file_content.startswith("---"):
        lines = file_content.splitlines()
        dash_count = 0
        body_start = 0
        for i, line in enumerate(lines):
            if line.strip() == "---":
                dash_count += 1
                if dash_count == 2:
                    body_start = i + 1
                    break
        if dash_count == 2:
            body = "\n".join(lines[body_start:]).strip()

    # Format timestamp for display
    timestamp_line = ""
    if timestamp:
        try:
            ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            timestamp_line = f"*From {ts.astimezone().strftime('%Y-%m-%d %H:%M %Z')}:*\n"
        except ValueError:
            pass

    section = f"## Previous Session Recall\n{timestamp_line}\n{body}"
    return section, len(section.encode("utf-8"))


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

**[LTM] flag (VERY rare — most entries should NOT be routed):** Only prefix entries that would save significant time or prevent a real mistake if encountered again months from now. Route to LTM:
- Architecture choices or design tradeoffs with lasting impact across sessions
- Gotchas that caused >30 min of debugging and aren't googleable
- Patterns proven across multiple sessions, not just discovered once
Do NOT route:
- Common dev knowledge (git, SQL, well-known language/framework features)
- Generic software patterns (DRY, separation of concerns, use env vars)
- One-time fixes or bugs unlikely to recur
- Tool-specific tips that are easily re-discoverable (man pages, --help, docs)
- Implementation details (built X, fixed Y) — these belong in short-term memory
- Gotchas with obvious error messages that point to the solution
- Version-specific notes or workarounds for known issues with tracked fixes
Tag up to {ROUTE_CAP + 2} [LTM] candidates per project, ordered by importance (most important first). Only the top {ROUTE_CAP} will be kept — the rest are discarded. This means you must rank: put the entry most worth preserving long-term first.

**[GLOBAL] flag:** Only for genuinely cross-project learnings (OS behavior, tool tips, general dev practices). Most entries should stay project-scoped.

**Compactness:** Final solutions only, one entry per concept, omit routine details.

**Global LTM auto-pinned maintenance:** The global LTM has auto-pinned sections (About Me, Current Projects, Technical Environment, Patterns & Preferences). When transcripts show clear evidence of change — a project completed, a new tool adopted — update the relevant entry. Be conservative.'''


def _build_preextracted_prompt(
    pending_dates: list[str],
    extracted_files: dict[str, str],
    synthesis_instructions: str,
    embedded_files: dict | None = None,
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
) -> str:
    """
    Build the embedded synthesis prompt for the subagent.

    Pre-extraction is required. If pre-extraction fails, synthesis is skipped
    (no auto-extract fallback).

    Args:
        pending_dates: List of pending date strings (YYYY-MM-DD)
        extracted_files: Dict mapping date -> file path (pre-extracted)
        embedded_files: Pre-read content to embed inline (transcripts, LTM content)
    """
    project_names_str = _get_project_names_str()
    synthesis_instructions = _build_synthesis_instructions(project_names_str)

    return _build_preextracted_prompt(
        pending_dates, extracted_files, synthesis_instructions, embedded_files
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

        prompt = _build_synthesis_prompt([date], single_date_files, embedded)

        prompt_path = f"{SYNTHESIS_PROMPT_DIR}/synthesis-prompt-{date}-{os.getpid()}.txt"
        Path(prompt_path).write_text(prompt, encoding="utf-8")

        print(f"prompt_file={prompt_path}")


def emit_project_memory(project_name_arg: str | None = None) -> int:
    """Emit project LTM + project STM sections to stdout.

    Used by the /load-project-memory skill to inject project-scoped memory
    on demand mid-session (most useful in light mode, where these sections
    are skipped at SessionStart).

    Args:
        project_name_arg: Explicit project name. If None, detect from cwd via
            the projects index.

    Returns:
        Exit code (0 on success, 1 on failure — either no project detected
        for the cwd or no memory found for the requested project).
    """
    check_python_version()

    settings = load_settings()
    project_days = settings["projectShortTerm"]["workingDays"]

    if project_name_arg:
        project_name = project_name_arg
        project = {"name": project_name}
    else:
        pwd = resolve_session_path(os.getcwd())
        projects_index = load_json_file(get_projects_index_file(), {})
        current_project = find_current_project(projects_index, pwd)
        if not current_project:
            print(
                f"No project detected for cwd: {pwd}\n"
                "Pass an explicit project name (e.g. /load-project-memory <name>).",
                file=sys.stderr,
            )
            return 1
        project = current_project
        project_name = project.get("name", "")

    project_content, _ = load_project_memory(project_name)
    project_history, _ = load_project_history(project, project_days)

    if not project_content and not project_history:
        print(
            f"No project memory found for: {project_name}",
            file=sys.stderr,
        )
        return 1

    print("<project-memory>")
    print(f"Project: {project_name}")
    print()

    if project_content:
        print(f"## Project Long-Term Memory: {project_name}")
        print(project_content)
        print()

    if project_history:
        print(f"## Project Short-Term Memory: {project_name}")
        print()
        for date, content in project_history:
            print(f"### {date}")
            print(content)
            print()

    print("</project-memory>")
    return 0


def main() -> None:
    """Main entry point - outputs memory context to stdout.

    Output order (most important first so the 2KB preview is maximally useful):
    1. Read instruction (for when Claude Code truncates large output to a file)
    2. Timestamp + synthesis errors
    3. Previous session recall (highest-value context)
    4. Global LTM
    5. Project LTM
    6. Project STM
    7. Global STM

    When output exceeds ~10K chars, Claude Code saves the full content to a
    session-specific file and shows the path + first 2KB as a preview. The read
    instruction in the first 2KB prompts Claude to read the full file.
    """
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

    # Detect current project early (needed for recall matching and project memory)
    pwd = resolve_session_path(os.getcwd())
    projects_index = load_json_file(get_projects_index_file(), {})
    current_project = find_current_project(projects_index, pwd)
    project_name = current_project.get("name", "") if current_project else ""

    # Load settings
    settings = load_settings()
    short_term_days = settings["globalShortTerm"]["workingDays"]
    project_days = settings["projectShortTerm"]["workingDays"]
    total_budget = settings["totalTokenBudget"]
    mode = settings.get("mode", "full")

    # Track total bytes for token estimation
    total_bytes = 0

    # Load recall before starting output (placed early so it lands in the 2KB preview)
    recall_section, recall_bytes = load_pending_recall(
        current_session_id=current_session_id,
        current_project_name=project_name,
        resolved_cwd=pwd,
    )
    total_bytes += recall_bytes

    # Start output
    print("<memory>")

    # Read instruction: when output is large, Claude Code saves it to a file and
    # shows a path + 2KB preview. This instruction prompts Claude to read the full file.
    print("If you see \"Full output saved to: ...\" above this text, read that file now for complete memory context.")
    print()

    # Include current local time for context
    now = datetime.now(timezone.utc).astimezone()
    offset = now.utcoffset() or timedelta(0)
    utc_offset_hours = offset.total_seconds() / 3600
    offset_sign = "+" if utc_offset_hours >= 0 else ""
    print(f"Current time: {now.strftime('%Y-%m-%d %H:%M')} (UTC{offset_sign}{utc_offset_hours:.0f})")
    print()

    # Surface synthesis errors from deferred runs
    error_alert = check_synthesis_errors()
    if error_alert:
        print(error_alert)
        print()

    # Previous session recall (most valuable: what you were just working on)
    if recall_section:
        print(recall_section)
        print()

    # Load global long-term memory
    global_content, global_bytes = load_global_memory()
    total_bytes += global_bytes

    if global_content:
        print("## Long-Term Memory")
        print(global_content)
        print()

    # In light mode, stop after global LTM — skip project LTM, project STM, global STM.
    # Native Claude Code memory handles project-scoped context; global LTM is the unique
    # cross-project value of this system.
    # Fail-safe to full on unknown values (typos like "Full"/"lite") so users don't
    # silently lose project context from a config mistake.
    if mode != "light":
        # Load project-specific long-term memory
        if project_name:
            project_content, project_bytes = load_project_memory(project_name)
            total_bytes += project_bytes

            if project_content:
                print(f"## Project Long-Term Memory: {project_name}")
                print(project_content)
                print()

        # Load project short-term memory (project history, filtered to [project/*] tags)
        if current_project:
            project_history, history_bytes = load_project_history(current_project, project_days)
            total_bytes += history_bytes

            if project_history:
                print(f"## Project Short-Term Memory: {project_name}")
                print()
                for date, content in project_history:
                    print(f"### {date}")
                    print(content)
                    print()

        # Load global short-term memory (recent daily summaries, filtered to [global/*] tags)
        global_summaries, global_daily_bytes = load_daily_summaries(short_term_days, scope="global")
        total_bytes += global_daily_bytes

        if global_summaries:
            print("## Global Short-Term Memory")
            for date, content in global_summaries:
                print(f"### {date}")
                print(content)
                print()

    print("</memory>")

    # Token estimation (informational)
    estimated_tokens = total_bytes // 4
    if estimated_tokens > total_budget:
        print(f"<!-- Memory usage: ~{estimated_tokens} tokens (budget: {total_budget}) -->")
        print("<!-- Consider running /synthesize to consolidate older sessions -->")


if __name__ == "__main__":
    # --synthesis-prompt: output just the subagent prompt (for /synthesize skill)
    if len(sys.argv) > 1 and sys.argv[1] == "--synthesis-prompt":
        exclude_id = None
        if len(sys.argv) > 3 and sys.argv[2] == "--exclude-session":
            exclude_id = sys.argv[3]
        write_synthesis_prompt(exclude_session_id=exclude_id)
    elif len(sys.argv) > 1 and sys.argv[1] == "--project-memory":
        # --project-memory [name]: emit project LTM + project STM (for /load-project-memory skill)
        name_arg = sys.argv[2] if len(sys.argv) > 2 else None
        sys.exit(emit_project_memory(name_arg))
    else:
        main()
