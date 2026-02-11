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
    get_memory_dir,
    get_daily_dir,
    get_project_memory_dir,
    get_global_memory_file,
    get_projects_index_file,
    load_settings,
    load_json_file,
    project_name_to_filename,
    get_working_days,
    remove_captured_session,
)

from transcript_ops import get_pending_days, extract_transcripts, format_transcripts_for_output

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


def _build_synthesis_prompt(
    exclude_flag: str,
    pending_dates: list[str],
    extracted_files: dict[str, str] | None = None,
) -> str:
    """
    Build the embedded synthesis prompt for the subagent.

    Two modes:
    - Pre-extracted (manual /synthesize): files already on disk, skip extraction
    - Dates-only (auto-synthesis): subagent extracts each date

    Args:
        exclude_flag: The --exclude-session flag string (or empty)
        pending_dates: List of pending date strings (YYYY-MM-DD)
        extracted_files: Optional dict mapping date -> file path (pre-extracted)
    """
    dates_str = ", ".join(pending_dates)

    # Load valid project names from index for scope tagging
    projects_index = load_json_file(get_projects_index_file(), {})
    project_names = sorted({
        data.get("name", "")
        for data in projects_index.get("projects", {}).values()
        if data.get("name")
    })
    project_names_str = ", ".join(f"`{n}`" for n in project_names) if project_names else "(none registered)"

    # Common synthesis instructions (shared by both paths)
    synthesis_instructions = '''**Daily summary** — Write to `~/.claude/memory/daily/YYYY-MM-DD.md`:

ALWAYS use a single **Write** call per daily file — even if the file already exists.
If an earlier version exists (from Step 1 Read), merge its content into your output, then Write the complete file.
Never use multiple Edit calls on daily files.

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
**Compactness:** Final solutions only, one learning per concept, omit routine details.

**Long-term routing (be HIGHLY selective):**
Route TO long-term memory ONLY: fundamental patterns, hard-won lessons, safety-critical info, non-obvious gotchas, architecture decisions with lasting impact.
Do NOT route: routine implementation, version-specific fixes, one-time configs, easily re-discoverable things, learnings that might not hold up over time.
Destinations: `[global/*]` → `~/.claude/memory/global-long-term-memory.md`, `[{project-name}/*]` → `~/.claude/memory/project-memory/{project-name}-long-term-memory.md`
Only use registered project names for routing: ''' + project_names_str + '''
Format: `(YYYY-MM-DD) [type] Description` (remove scope from tag, file is already scoped). Check for duplicates before adding.
Create missing project files from template at `~/.claude/memory/templates/project-long-term-memory.md`.

**CRITICAL batching requirement**: Collect ALL items to route across all dates, then make ONE Edit call per target file with ALL new entries at once.
Do NOT make separate Edit calls per learning — batch them into a single Edit per file.
Example: old_string ends with section header + comment, new_string = same header + comment + all new entries appended.'''

    if extracted_files:
        # Pre-extracted path: files already on disk
        # Count lines per file for Read limit hints
        file_metadata: list[tuple[str, str, int]] = []
        for date, path in sorted(extracted_files.items()):
            try:
                line_count = Path(path).read_text(encoding="utf-8").count("\n") + 1
            except IOError:
                line_count = 2000
            file_metadata.append((date, path, line_count))

        file_list = "\n".join(
            f"- **{date}**: `{path}` ({lines} lines)"
            for date, path, lines in file_metadata
        )
        read_transcript_lines = "\n".join(
            f"- Read(`{path}`, limit={lines + 100}) — transcript for {date}"
            for date, path, lines in file_metadata
        )
        read_daily_lines = "\n".join(
            f"- Read(`~/.claude/memory/daily/{d}.md`) — may not exist, Read error is expected"
            for d in pending_dates
        )
        mark_captured_lines = "\n".join(
            f'python3 $HOME/.claude/scripts/indexing.py mark-captured --sidecar {path.rsplit(".", 1)[0]}.sessions && rm {path} {path.rsplit(".", 1)[0]}.sessions &&'
            for date, path in sorted(extracted_files.items())
        )
        return f'''Process pre-extracted memory transcripts into daily summaries and route key learnings to long-term memory.

**CRITICAL: Process all dates in a single pass. If a tool call fails, handle the error and continue. Do NOT restart the synthesis process from the beginning.**

Pending dates: {dates_str}

Pre-extracted transcript files:
{file_list}

## Tool Guidelines

**File operations** - use tilde paths (`~/.claude/...`):
- `Read(~/.claude/memory/daily/YYYY-MM-DD.md)`
- `Read(~/.claude/memory/global-long-term-memory.md)`
- `Edit(~/.claude/memory/...)` for updates

## Process

### Step 1: Read all inputs

Make exactly ONE parallel tool call with ALL of these Read calls simultaneously:
{read_transcript_lines}
- Read(`~/.claude/memory/global-long-term-memory.md`)
{read_daily_lines}

If you already know which projects are referenced, include their LTM files in this SAME parallel call:
- Read(`~/.claude/memory/project-memory/{{project}}-long-term-memory.md`)

**Do NOT read files one at a time. All reads MUST be in a single parallel call.**

### Step 2: Synthesize + Route + Write

In **one reasoning step**, produce daily summaries AND long-term routing edits for ALL {len(pending_dates)} dates.
Then make ALL file Write/Edit calls in a single parallel tool call:
- Write each daily summary file (one per date)
- Edit global LTM ONCE with ALL global-scope learnings from ALL dates batched together
- Edit project LTM ONCE per project with ALL project-scope learnings batched together

CRITICAL: Do NOT make separate Edit calls per learning. Collect all items first, batch into one Edit per file.

{synthesis_instructions}

### Step 3: Mark captured, clean up & finalize

Run ALL of this in a **single Bash call**:
```bash
{mark_captured_lines}
python3 $HOME/.claude/scripts/decay.py && python3 -c "from datetime import datetime, timezone; from pathlib import Path; Path.home().joinpath('.claude/memory/.last-synthesis').write_text(datetime.now(timezone.utc).isoformat())"
```

Return a summary: "Processed N days. Created/updated daily summaries for [dates]. Routed X items to long-term memory (list them). Archived Y old items."'''
    else:
        # Dates-only path: subagent must extract
        return f'''Process pending memory transcripts into daily summaries and route key learnings to long-term memory.

**CRITICAL: Process all dates in a single pass. If a tool call fails, handle the error and continue. Do NOT restart the synthesis process from the beginning.**

Pending dates to process: {dates_str}

## Tool Guidelines

**File operations** - use tilde paths (`~/.claude/...`):
- `Read(~/.claude/memory/daily/YYYY-MM-DD.md)`
- `Read(~/.claude/memory/global-long-term-memory.md)`
- `Edit(~/.claude/memory/...)` for updates

**Transcript extraction** - use `$HOME` in bash with `--output` to write to temp file, then Read (Bash output truncates at 30K chars; temp file avoids this):
```
python3 $HOME/.claude/scripts/indexing.py extract YYYY-MM-DD{exclude_flag} --output /tmp/memory-extract-YYYY-MM-DD-$$.txt
```
Then read the content and sidecar:
```
Read(/tmp/memory-extract-YYYY-MM-DD-$$.txt)            # transcript content
```

## Process

For each pending date ({dates_str}), do a **single combined pass** (extract + summarize + route + mark):

### Step 1: Extract & Read

Run Bash to extract transcripts to temp file. Then in a **single parallel tool call**, Read all of:
- `/tmp/memory-extract-YYYY-MM-DD-$$.txt` (transcript content)
- `~/.claude/memory/global-long-term-memory.md`
- Any existing daily file: `~/.claude/memory/daily/YYYY-MM-DD.md`
- `~/.claude/memory/project-memory/{{project}}-long-term-memory.md` (if project entries exist in transcript)

### Step 2: Summarize + Route + Write

In **one reasoning step**, produce BOTH the daily summary AND any long-term routing edits.
Then make ALL Write/Edit calls in a single parallel tool call.
CRITICAL: Do NOT make separate Edit calls per learning — batch all items into one Edit per target file.

{synthesis_instructions}

### Step 3: Mark captured, clean up & finalize

Run ALL of this in a **single Bash call** — mark captured, remove temp files, run decay, and update timestamp:
```bash
python3 $HOME/.claude/scripts/indexing.py mark-captured --sidecar /tmp/memory-extract-YYYY-MM-DD-$$.sessions && rm /tmp/memory-extract-YYYY-MM-DD-$$.txt /tmp/memory-extract-YYYY-MM-DD-$$.sessions && python3 $HOME/.claude/scripts/decay.py && python3 -c "from datetime import datetime, timezone; from pathlib import Path; Path.home().joinpath('.claude/memory/.last-synthesis').write_text(datetime.now(timezone.utc).isoformat())"
```

Return a summary: "Processed N days. Created/updated daily summaries for [dates]. Routed X items to long-term memory (list them). Archived Y old items."'''


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
        synthesis_model = settings.get("synthesis", {}).get("model", "sonnet")
        synthesis_background = settings.get("synthesis", {}).get("background", True)

        # Build exclude flag for subagent
        exclude_flag = ""
        if current_session_id:
            exclude_flag = f" --exclude-session {current_session_id}"

        # Pre-extract all transcripts before launching subagent (faster, fewer tool calls)
        pid = os.getpid()
        extracted_files: dict[str, str] = {}
        for date in pending_dates:
            try:
                daily_data = extract_transcripts(date, exclude_session_id=current_session_id)
                if daily_data:
                    output_path = f"/tmp/memory-extract-{date}-{pid}.txt"
                    Path(output_path).write_text(
                        format_transcripts_for_output(daily_data, total_line_budget=TRANSCRIPT_LINE_BUDGET), encoding="utf-8"
                    )
                    sidecar_path = output_path.rsplit(".", 1)[0] + ".sessions"
                    session_ids = [
                        s["session_id"]
                        for sessions in daily_data.values()
                        for s in sessions
                    ]
                    Path(sidecar_path).write_text(
                        "\n".join(session_ids) + "\n", encoding="utf-8"
                    )
                    extracted_files[date] = output_path
            except Exception:
                pass  # Fall through — date will use dates-only path if all fail

        if extracted_files:
            synth_prompt = _build_synthesis_prompt(
                exclude_flag, list(extracted_files.keys()), extracted_files
            )
        else:
            # Fallback: subagent extracts (slower but handles edge cases)
            synth_prompt = _build_synthesis_prompt(exclude_flag, pending_dates)

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
        exclude_flag = ""
        if len(sys.argv) > 3 and sys.argv[2] == "--exclude-session":
            exclude_id = sys.argv[3]
            exclude_flag = f" --exclude-session {sys.argv[3]}"

        settings = load_settings()
        model = settings.get("synthesis", {}).get("model", "sonnet")

        # Pre-compute pending dates
        pending_dates = get_pending_days(exclude_session_id=exclude_id)
        if not pending_dates:
            print("No pending transcripts.")
            sys.exit(0)

        # Pre-extract transcripts (manual path — user is already waiting)
        pid = os.getpid()
        extracted_files: dict[str, str] = {}
        for date in pending_dates:
            daily_data = extract_transcripts(date, exclude_session_id=exclude_id)
            if daily_data:
                output_path = f"/tmp/memory-extract-{date}-{pid}.txt"
                Path(output_path).write_text(
                    format_transcripts_for_output(daily_data, total_line_budget=TRANSCRIPT_LINE_BUDGET), encoding="utf-8"
                )
                # Write sidecar with session IDs
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

        if not extracted_files:
            print("No pending transcripts with content.")
            sys.exit(0)

        print(f"model={model}")
        print(_build_synthesis_prompt(exclude_flag, list(extracted_files.keys()), extracted_files))
    else:
        main()
