#!/usr/bin/env python3
"""
SessionStart hook - loads memory context for Claude Code.

This script runs on: startup, resume, clear, compact

It performs:
1. Loads global long-term memory
2. Loads project-specific long-term memory (if applicable)
3. Loads global short-term memory (recent daily summaries, filtered to [global/*] tags)
4. Loads project short-term memory (project history, filtered to [project/*] tags)
5. Checks for pending transcripts and prompts for synthesis

Output is printed to stdout and injected into Claude Code's context.

Requirements: Python 3.9+
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    check_python_version,
    filter_daily_content,
    find_current_project,
    get_daily_dir,
    get_global_memory_file,
    get_memory_dir,
    get_project_memory_dir,
    get_projects_dir,
    get_projects_index_file,
    get_synthesis_state_file,
    get_working_days,
    load_json_file,
    load_settings,
    load_synthesis_state,
    project_name_to_filename,
    remove_captured_session,
)
from transcript_ops import (
    extract_transcripts,
    extract_transcripts_incremental,
    format_transcripts_for_output,
    format_transcripts_incremental,
    get_pending_days,
)

# Maximum output lines for pre-extracted transcripts fed to the synthesis subagent
TRANSCRIPT_LINE_BUDGET = 1950

# =============================================================================
# Key Interfaces
# =============================================================================
# Entry points:
#   main()                                  SessionStart hook (stdout -> context)
# Memory loading:
#   load_global_memory() -> (str, int)
#   load_project_memory(name) -> (str, int)
#   load_daily_summaries(days, scope) -> (list[(date, content)], int)
#   load_project_history(project, days) -> (list[(date, content)], int)
# Scheduling:
#   should_synthesize(settings) -> bool
# =============================================================================


def get_last_synthesis_file() -> Path:
    """Get the path to the .last-synthesis timestamp file."""
    return get_memory_dir() / ".last-synthesis"


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
    interval_hours = settings.get("synthesis", {}).get("intervalHours", 2)

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

        # First session of day (UTC) OR >interval since last
        return last_time.date() < now.date() or hours_since > interval_hours

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
    """Build the shared synthesis instructions block (tagging, routing, dedup)."""
    return '''**Daily summary format:**

```markdown
# YYYY-MM-DD

## Actions
<!-- What was done. Tag [scope/action]. -->
- [scope/implement] What was accomplished

## Decisions
<!-- Important choices and rationale. Tag [scope/decision]. -->
- [scope/design] Choice made and why

## Learnings
<!-- Patterns, gotchas, insights. Tag [scope/type]. -->
- [scope/gotcha] Unexpected behavior discovered
- [scope/pattern] Proven method or approach

## Lessons
<!-- Actionable takeaways. Tag [scope/type]. -->
- [scope/insight] Mental model or understanding
- [scope/tip] Useful command or shortcut
```

**Tag format:** `[scope/type]` where scope is `global` or one of these registered project names: ''' + project_names_str + '''
**IMPORTANT:** Only use the project names listed above. Do NOT invent new project names from context.
**Scope rule:** Use `global` ONLY for learnings that apply across ALL projects — general dev practices, SQL patterns, OS/tool behavior, Claude Code mechanics. If a learning arose while working on a specific project (debugging that project's code, that project's architecture, tools used primarily for that project), it belongs to that project even if the concept seems general. When in doubt, use the project name — project scope is almost always correct.
**Compactness:** Final solutions only, one learning per concept, omit routine details.

**Long-term routing (be HIGHLY selective):**
Route daily entries to corresponding LTM sections:
- Daily `## Actions` → LTM `## Key Actions` (multi-day implementations, novel integrations, reusable setups)
- Daily `## Decisions` → LTM `## Key Decisions` (architecture choices, design tradeoffs, scope decisions with lasting impact)
- Daily `## Learnings` → LTM `## Key Learnings` (non-obvious gotchas, proven patterns, hard-won lessons)
- Daily `## Lessons` → LTM `## Key Lessons` (mental models, useful commands, workarounds)
Do NOT route: routine implementation, version-specific fixes, one-time configs, easily re-discoverable things, learnings that might not hold up over time.
Destinations: `[global/*]` → global LTM, `[{project-name}/*]` → project LTM
Only use registered project names for routing: ''' + project_names_str + '''
Format: `(YYYY-MM-DD) [type] Description` (remove scope from tag, file is already scoped).

**DEDUP REQUIREMENT:** Before adding ANY routed entry, check the existing LTM content provided below. If an existing entry covers the same concept — even if worded differently — do NOT add a near-duplicate. Only add entries that represent genuinely new knowledge.

**GRANULARITY CAP:** Maximum 5 routed entries per target LTM file per synthesis run. If you have more, consolidate related items (e.g., multiple gotchas from one debugging session → one summary entry). Prefer fewer, denser entries over many granular ones.

**Global LTM auto-pinned maintenance:** The global LTM has auto-pinned sections (About Me, Current Projects, Technical Environment, Patterns & Preferences) containing factual profile info. When transcripts show clear evidence of change — a project completed or cancelled, a new tool adopted, a workflow changed — update or remove the relevant entry. Be conservative: only update when clearly stale, not speculatively.'''


def _build_preextracted_prompt(
    pending_dates: list[str],
    extracted_files: dict[str, str],
    synthesis_instructions: str,
    embedded_files: dict | None = None,
) -> str:
    """Build synthesis prompt with embedded content and structured output format.

    Args:
        pending_dates: List of dates to process (YYYY-MM-DD)
        extracted_files: Dict mapping date -> extract file path (for sidecar references)
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
## Existing Daily Summaries (merge context)

These daily files already exist from a previous synthesis run. When you see sessions marked '(continued — new messages only)', merge new insights into the existing summary. Do NOT duplicate entries already present. Add only genuinely new items.

{merge_block}

"""

    # Build sidecar and extract paths for synthesis.py apply command
    sidecar_paths = []
    extract_paths = []
    for date in sorted(extracted_files.keys()):
        path = extracted_files[date]
        sidecar = path.rsplit(".", 1)[0] + ".sessions"
        sidecar_paths.append(sidecar)
        extract_paths.append(path)
    sidecars_arg = " ".join(sidecar_paths)
    extracts_arg = " ".join(extract_paths)

    # Pre-compute output filename with PID so Write and Bash use the same path
    # (using $$ would expand in Bash but stay literal in Write, causing a mismatch)
    output_filename = f"/tmp/synthesis-output-{os.getpid()}.txt"

    # Build --offsets-json arg for incremental synthesis state update
    offsets_path = embedded_files.get("offsets_path", "")
    offsets_arg = f" --offsets-json {offsets_path}" if offsets_path else ""

    first_date = sorted(pending_dates)[0]

    return f'''You are a structured data extractor. Your job is to read session transcripts and produce ONLY delimited structured output — no prose, no commentary, no summary.

## Output Format

Your output must follow this exact structure. Here is a complete realistic example:

===DAILY:2026-02-20===
# 2026-02-20
## Actions
- [myproject/implement] Built REST API endpoints for user authentication
- [global/implement] Configured pre-commit hooks for Python linting

## Decisions
- [myproject/design] JWT tokens over session cookies — stateless scales better for microservices

## Learnings
- [myproject/gotcha] SQLAlchemy async sessions need explicit `await session.close()` or connections leak
- [global/pattern] `pytest -x --tb=short` stops on first failure with compact output — faster debug cycles

## Lessons
- [global/tip] `git stash -u` includes untracked files — plain `git stash` misses them

===ROUTE:myproject:Key Learnings===
- (2026-02-20) [gotcha] SQLAlchemy async sessions need explicit `await session.close()` or connections leak

===ROUTE:global:Key Lessons===
- (2026-02-20) [tip] `git stash -u` includes untracked files — plain `git stash` misses them

===END===

Every output starts with ===DAILY:YYYY-MM-DD=== and ends with ===END===. Nothing else.

## Delivery

Only use the Write and Bash tools — no other tools.

1. Write(`{output_filename}`, <your structured output>)
2. Bash: `python3 $HOME/.claude/scripts/synthesis.py apply {output_filename} --sidecars {sidecars_arg} --extracts {extracts_arg}{offsets_arg}`

## Synthesis Instructions

{synthesis_instructions}

## Existing Long-Term Memory (for dedup)

{ltm_block}
{merge_instructions}
## Session Transcripts

**Pending dates:** {dates_str}

{transcript_block}

## Reminder

Output only the structured format shown above. Start with ===DAILY:{first_date}=== and end with ===END===.'''


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


def _find_projects_in_sidecars(extracted_files: dict[str, str]) -> set[str]:
    """Find project names from session IDs in sidecar files.

    Reads .sessions sidecar files, locates each session's project directory
    under ~/.claude/projects/, and maps to project names via projects-index.

    Returns set of project names that had sessions in the extracted files.
    """
    projects_index = load_json_file(get_projects_index_file(), {})

    # Build reverse lookup: encoded dir name -> project name
    encoded_to_name: dict[str, str] = {}
    for data in projects_index.get("projects", {}).values():
        name = data.get("name", "")
        if name:
            for enc in data.get("encodedPaths", []):
                encoded_to_name[enc] = name

    # Collect all session IDs from sidecar files
    session_ids: set[str] = set()
    for path in extracted_files.values():
        sidecar = Path(path).with_suffix(".sessions")
        if sidecar.exists():
            try:
                for line in sidecar.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        session_ids.add(line)
            except IOError:
                pass

    # Map session IDs to project names via their parent directory
    claude_projects_dir = get_projects_dir()
    result: set[str] = set()
    for sid in session_ids:
        for proj_dir in claude_projects_dir.iterdir():
            if (proj_dir / f"{sid}.jsonl").exists():
                name = encoded_to_name.get(proj_dir.name)
                if name:
                    result.add(name)
                break
    return result


def _build_embedded_files(extracted_files: dict[str, str], include_dailies: bool = False) -> dict:
    """Pre-read all files for embedding in synthesis prompt.

    Reads transcript extracts, global LTM, and project LTMs into memory
    so the synthesis prompt can embed them inline (zero tool calls).

    Args:
        extracted_files: Dict mapping date -> extract file path
        include_dailies: If True, read existing daily summary files as merge context

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
    relevant_projects = _find_projects_in_sidecars(extracted_files)
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


def pre_extract_transcripts(
    pending_dates: list,
    exclude_session_id: str | None = None,
    output_dir: str = "/tmp",
) -> dict:
    """Pre-extract transcripts to temp files with sidecar session lists.

    For each pending date, extracts transcripts, writes formatted output to a
    temp file, and writes a .sessions sidecar with session IDs for mark-captured.

    Returns dict mapping date -> output file path.
    """
    pid = os.getpid()
    extracted_files: dict[str, str] = {}
    for date in pending_dates:
        try:
            daily_data = extract_transcripts(date, exclude_session_id=exclude_session_id)
            if daily_data:
                output_path = f"{output_dir}/memory-extract-{date}-{pid}.txt"
                Path(output_path).write_text(
                    format_transcripts_for_output(daily_data, total_line_budget=TRANSCRIPT_LINE_BUDGET),
                    encoding="utf-8",
                )
                sidecar_path = Path(output_path).with_suffix(".sessions")
                session_ids = [
                    s["session_id"]
                    for sessions in daily_data.values()
                    for s in sessions
                ]
                sidecar_path.write_text(
                    "\n".join(session_ids) + "\n", encoding="utf-8"
                )
                extracted_files[date] = output_path
        except Exception as e:
            print(f"Warning: Failed to extract {date}: {e}", file=sys.stderr)
    return extracted_files


def pre_extract_transcripts_incremental(
    pending_dates: list,
    exclude_session_id: str | None = None,
    output_dir: str = "/tmp",
) -> tuple[dict[str, str], dict[str, dict]]:
    """Pre-extract transcripts incrementally using high water marks.

    Like pre_extract_transcripts but uses synthesis state to skip unchanged
    sessions and only extract delta content from grown sessions.

    Returns:
        (extracted_files, session_offsets) where:
        - extracted_files: dict mapping date -> output file path
        - session_offsets: dict mapping session_id -> {"offset": int, "lines": int}
    """
    state = load_synthesis_state()
    pid = os.getpid()
    extracted_files: dict[str, str] = {}
    session_offsets: dict[str, dict] = {}

    try:
        daily_data = extract_transcripts_incremental(
            state, exclude_session_id=exclude_session_id
        )
    except Exception as e:
        print(f"Warning: Incremental extraction failed: {e}", file=sys.stderr)
        return {}, {}

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

        sidecar_path = Path(output_path).with_suffix(".sessions")
        session_ids = [s["session_id"] for s in sessions]
        sidecar_path.write_text("\n".join(session_ids) + "\n", encoding="utf-8")

        extracted_files[date] = output_path

        for s in sessions:
            session_offsets[s["session_id"]] = {
                "offset": s["current_offset"],
                "lines": s["current_lines"],
            }

    return extracted_files, session_offsets


def main() -> None:
    """Main entry point - outputs memory context to stdout."""
    check_python_version()

    # Parse session_id and source from SessionStart hook stdin JSON
    current_session_id = None
    source = None
    try:
        if not sys.stdin.isatty():
            hook_input = json.load(sys.stdin)
            current_session_id = hook_input.get("session_id")
            source = hook_input.get("source")
    except (json.JSONDecodeError, IOError):
        pass  # Not called from hook, or invalid input — safe to continue

    # Auto-uncapture on session resume (user may add new content to resumed session)
    if source == "resume" and current_session_id:
        remove_captured_session(current_session_id)

    # Load settings
    settings = load_settings()
    short_term_days = settings["globalShortTerm"]["workingDays"]
    project_days = settings["projectShortTerm"]["workingDays"]
    include_subdirs = settings["projectSettings"]["includeSubdirectories"]
    total_budget = settings["totalTokenBudget"]

    # Track total bytes for token estimation
    total_bytes = 0

    # Start output
    print("<memory>")

    # Include current local time for context
    now = datetime.now()
    utc_now = datetime.now(timezone.utc)
    utc_offset_hours = (now - utc_now.replace(tzinfo=None)).total_seconds() / 3600
    offset_sign = "+" if utc_offset_hours >= 0 else ""
    print(f"Current time: {now.strftime('%Y-%m-%d %H:%M')} (UTC{offset_sign}{utc_offset_hours:.0f})")
    print()

    # Check for pending transcripts (only if synthesis scheduling allows)
    # Exclude current session — it's still active and shouldn't be synthesized
    pending_dates = get_pending_days(exclude_session_id=current_session_id)
    if pending_dates and should_synthesize(settings):
        # Write timestamp eagerly to prevent duplicate synthesis when multiple
        # sessions start simultaneously (all would see stale timestamp otherwise)
        get_last_synthesis_file().write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )

        synthesis_model = settings.get("synthesis", {}).get("model", "sonnet")
        synthesis_background = settings.get("synthesis", {}).get("background", True)

        # Pre-extract all transcripts before launching subagent (faster, fewer tool calls)
        session_offsets = None

        if get_synthesis_state_file().exists():
            extracted_files, session_offsets = pre_extract_transcripts_incremental(
                pending_dates, exclude_session_id=current_session_id
            )
        else:
            extracted_files = pre_extract_transcripts(
                pending_dates, exclude_session_id=current_session_id
            )

        if extracted_files:
            # Pre-read all files for embedding in prompt (zero tool calls for subagent)
            include_dailies = session_offsets is not None
            embedded = _build_embedded_files(extracted_files, include_dailies=include_dailies)

            # Write session offsets to temp file for synthesis.py state update
            if session_offsets:
                offsets_path = f"/tmp/synthesis-offsets-{os.getpid()}.json"
                Path(offsets_path).write_text(json.dumps(session_offsets), encoding="utf-8")
                embedded["offsets_path"] = offsets_path

            synth_prompt = _build_synthesis_prompt(
                list(extracted_files.keys()), extracted_files, embedded
            )

            print("## AUTO-SYNTHESIZE REQUIRED")
            print(f"There are {len(pending_dates)} pending date(s): {', '.join(pending_dates)}.")
            print()
            if synthesis_background:
                print("**IMPORTANT**: Spawn a **background** subagent immediately, then respond to the user.")
            else:
                print("**IMPORTANT**: Before responding to the user, spawn a subagent to process transcripts.")
            print("```")
            print(f'Use the Task tool with subagent_type="general-purpose", model="{synthesis_model}"'
                  f'{", run_in_background=true" if synthesis_background else ""} and this prompt:')
            print(synth_prompt)
            print("```")
            print()

    # Load global long-term memory
    global_content, global_bytes = load_global_memory()
    total_bytes += global_bytes

    if global_content:
        print("## Long-Term Memory")
        print(global_content)
        print()

    # Detect current project
    pwd = os.getcwd()
    projects_index = load_json_file(get_projects_index_file(), {})
    current_project = find_current_project(projects_index, pwd, include_subdirs)

    # Load project-specific long-term memory
    if current_project:
        project_name = current_project.get("name", "")
        if project_name:
            project_content, project_bytes = load_project_memory(project_name)
            total_bytes += project_bytes

            if project_content:
                print(f"## Project Long-Term Memory: {project_name}")
                print(project_content)
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

    # Load project short-term memory (project history, filtered to [project/*] tags)
    if current_project:
        project_name = current_project.get("name", "unknown")
        project_history, history_bytes = load_project_history(current_project, project_days)
        total_bytes += history_bytes

        if project_history:
            print(f"## Project Short-Term Memory: {project_name}")
            print()
            for date, content in project_history:
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

        settings = load_settings()
        model = settings.get("synthesis", {}).get("model", "sonnet")

        # Pre-compute pending dates
        pending_dates = get_pending_days(exclude_session_id=exclude_id)
        if not pending_dates:
            print("No pending transcripts.")
            sys.exit(0)

        # Pre-extract transcripts (check for incremental mode)
        session_offsets = None

        if get_synthesis_state_file().exists():
            extracted_files, session_offsets = pre_extract_transcripts_incremental(
                pending_dates, exclude_session_id=exclude_id
            )
        else:
            extracted_files = pre_extract_transcripts(pending_dates, exclude_session_id=exclude_id)

        if not extracted_files:
            print("No pending transcripts with content.")
            sys.exit(0)

        # Pre-read all files for embedding in prompt
        include_dailies = session_offsets is not None
        embedded = _build_embedded_files(extracted_files, include_dailies=include_dailies)

        # Write session offsets to temp file for synthesis.py state update
        if session_offsets:
            offsets_path = f"/tmp/synthesis-offsets-{os.getpid()}.json"
            Path(offsets_path).write_text(json.dumps(session_offsets), encoding="utf-8")
            embedded["offsets_path"] = offsets_path

        print(f"model={model}")
        print(_build_synthesis_prompt(list(extracted_files.keys()), extracted_files, embedded))
    else:
        main()
