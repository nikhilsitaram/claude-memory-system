#!/usr/bin/env python3
"""
Synthesis output parser and applier for Claude Code Memory System.

Parses structured output from the synthesis subagent and applies results:
- Writes daily summary files
- Appends routed entries to LTM files
- Marks [routed] entries in daily files
- Runs post-processing (state pruning, decay, validation, timestamp)

Usage:
    python3 synthesis.py apply <output_file> --extracts <path1> [<path2>...]

Requirements: Python 3.9+
"""

import argparse
import contextlib
import io
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (  # noqa: E402
    extract_entry_keywords,
    get_daily_dir,
    get_global_memory_file,
    get_memory_dir,
    get_pending_recall_dir,
    get_project_memory_dir,
    get_projects_dir,
    is_routed_match,
    parse_markdown_sections,
    rebuild_projects_index_quiet,
    update_synthesis_state,
)

__all__ = [
    "DailyFile",
    "MIN_ROUTE_KEYWORDS",
    "ProjectBlock",
    "ROUTE_CAP",
    "RouteEntry",
    "SECTION_ORDER",
    "SynthesisResult",
    "TYPE_TO_SECTION",
    "build_dailies_from_project_blocks",
    "extract_routes_from_project_blocks",
    "inject_scopes",
    "merge_daily_sections",
    "parse_daily_sections",
    "parse_synthesis_output",
    "mark_routed_entries",
    "write_daily_files",
    "append_to_ltm",
    "apply_results",
    "compute_offsets_from_extracts",
    "run_mark_routed",
    "run_validate_ltm",
    "run_decay",
    "run_post_processing",
]

# Delimiter patterns
DAILY_HEADER = re.compile(r"^===DAILY:(\d{4}-\d{2}-\d{2})===$")  # Legacy format
ROUTE_HEADER = re.compile(r"^===ROUTE:([^:]+):(.+)===$")  # Legacy format
PROJECT_HEADER = re.compile(r"^===PROJECT:([^=]+)===$")
END_MARKER = "===END==="

# Routing quality gates
MIN_ROUTE_KEYWORDS = 4  # Minimum meaningful keywords for an entry to be routed
ROUTE_CAP = 5  # Maximum entries routed per LTM file per synthesis run

# Type -> Section mapping (deterministic)
TYPE_TO_SECTION = {
    "implement": "Actions",
    "improve": "Actions",
    "document": "Actions",
    "analyze": "Actions",
    "design": "Decisions",
    "tradeoff": "Decisions",
    "scope": "Decisions",
    "gotcha": "Learnings",
    "pitfall": "Learnings",
    "pattern": "Learnings",
    "insight": "Lessons",
    "tip": "Lessons",
    "workaround": "Lessons",
}


@dataclass
class DailyFile:
    """A parsed daily summary block with date and markdown content."""

    date: str
    content: str


@dataclass
class RouteEntry:
    """A parsed route block targeting a specific LTM scope and section."""

    scope: str
    section: str
    entries: list[str]


@dataclass
class ProjectBlock:
    """A parsed project block from ===PROJECT:X=== output."""

    project: str
    entries: list[str] = field(default_factory=list)


@dataclass
class SynthesisResult:
    """Complete parsed synthesis output containing dailies, routes, and warnings."""

    dailies: list[DailyFile] = field(default_factory=list)
    routes: list[RouteEntry] = field(default_factory=list)
    project_blocks: list[ProjectBlock] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _is_delimiter(line: str) -> bool:
    """Check if a line is any known delimiter (daily, route, project, or end)."""
    return bool(
        DAILY_HEADER.match(line)
        or ROUTE_HEADER.match(line)
        or PROJECT_HEADER.match(line)
        or line == END_MARKER
    )


def parse_synthesis_output(text: str) -> SynthesisResult:
    """Parse structured synthesis output into daily files, route entries, and project blocks.

    Supported formats:
        ===DAILY:YYYY-MM-DD===        (legacy daily summary)
        ===ROUTE:scope:section===     (legacy LTM routing)
        ===PROJECT:name===            (new per-project block)
        ===END===                     (end marker)

    Text before the first delimiter is ignored. Missing ===END=== produces
    a warning but content is still parsed. Both formats can coexist in the
    same output (downstream decides how to handle).
    """
    result = SynthesisResult()
    lines = text.split("\n")
    has_end = END_MARKER in text

    has_daily = any(DAILY_HEADER.match(line.strip()) for line in lines)
    has_project = any(PROJECT_HEADER.match(line.strip()) for line in lines)
    if not has_end and (has_daily or has_project):
        result.warnings.append("Missing ===END=== marker; processing available content")

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Check for project header
        project_match = PROJECT_HEADER.match(line)
        if project_match:
            project_name = project_match.group(1).strip()
            entries = []
            i += 1
            while i < len(lines):
                stripped = lines[i].strip()
                if _is_delimiter(stripped):
                    break
                if stripped.startswith("- "):
                    entries.append(stripped)
                i += 1
            if entries:
                result.project_blocks.append(
                    ProjectBlock(project=project_name, entries=entries)
                )
            continue

        # Check for daily header
        daily_match = DAILY_HEADER.match(line)
        if daily_match:
            date = daily_match.group(1)
            content_lines = []
            i += 1
            while i < len(lines):
                stripped = lines[i].strip()
                if _is_delimiter(stripped):
                    break
                content_lines.append(lines[i])
                i += 1
            content = "\n".join(content_lines).strip()
            if content:
                result.dailies.append(DailyFile(date=date, content=content))
            continue

        # Check for route header
        route_match = ROUTE_HEADER.match(line)
        if route_match:
            scope = route_match.group(1)
            section = route_match.group(2)
            entries = []
            i += 1
            while i < len(lines):
                stripped = lines[i].strip()
                if _is_delimiter(stripped):
                    break
                if stripped.startswith("- "):
                    entries.append(stripped)
                i += 1
            if entries:
                result.routes.append(
                    RouteEntry(scope=scope, section=section, entries=entries)
                )
            continue

        i += 1

    return result


def _extract_description(entry: str) -> str:
    """Extract the description portion from a route entry for matching.

    Route format: '- (YYYY-MM-DD) [type] Description'
    Daily format: '- [scope/type] Description'
    Returns the text after the last ] bracket.
    """
    idx = entry.rfind("]")
    if idx >= 0:
        return entry[idx + 1 :].strip()
    return entry.strip("- ").strip()


SECTION_ORDER = ["Actions", "Decisions", "Learnings", "Lessons"]


def parse_daily_sections(content: str) -> dict:
    """Parse a daily markdown file into structured sections.

    Wraps ``memory_utils.parse_markdown_sections()`` with daily-file-specific
    logic: extracts date from ``# YYYY-MM-DD`` header, filters to known
    section names, skips HTML comments/blank lines, and collects entry lines.

    Returns dict with "date" (str) and section names mapping to entry lists.
    Skips HTML comments and blank lines. Preserves [routed] prefixes.
    """
    result: dict = {"date": ""}
    for s in SECTION_ORDER:
        result[s] = []

    if not content.strip():
        return result

    for header, lines in parse_markdown_sections(content):
        if not header:
            # Preamble — look for date header in the lines
            for line in lines:
                if line.startswith("# ") and not line.startswith("## "):
                    date_match = re.match(r"^# (\d{4}-\d{2}-\d{2})", line)
                    if date_match:
                        result["date"] = date_match.group(1)
            continue

        section_name = header[3:].strip()  # strip "## " prefix
        if section_name not in SECTION_ORDER:
            continue

        for line in lines:
            # Skip HTML comments
            if re.match(r"^\s*<!--.*-->\s*$", line):
                continue
            # Skip blank lines
            if not line.strip():
                continue
            # Entry line (starts with -)
            if line.strip().startswith("-"):
                result[section_name].append(line)

    return result


def merge_daily_sections(existing_content: str, new_content: str) -> str:
    """Merge new daily entries into existing daily file, section by section.

    New entries that are near-duplicates of existing entries (by keyword overlap)
    are rejected. Sections are output in standard order.

    Args:
        existing_content: Current daily file content (empty string if none)
        new_content: New LLM output for same date

    Returns:
        Merged markdown content with date header and all sections.
    """
    if not existing_content.strip():
        return new_content

    existing = parse_daily_sections(existing_content)
    new = parse_daily_sections(new_content)

    date = existing["date"] or new["date"]
    merged: dict[str, list[str]] = {}

    for section in SECTION_ORDER:
        existing_entries = existing.get(section, [])
        new_entries = new.get(section, [])

        merged[section] = list(existing_entries)
        existing_stripped = {ex.strip() for ex in existing_entries}
        for entry in new_entries:
            # Reject exact duplicates
            if entry.strip() in existing_stripped:
                continue
            # Reject near-duplicates (by keyword overlap)
            if any(is_routed_match(entry, ex, threshold=0.6) for ex in existing_entries):
                continue
            merged[section].append(entry)

    # Reassemble
    lines: list[str] = []
    if date:
        lines.append(f"# {date}")
    for section in SECTION_ORDER:
        entries = merged[section]
        if entries:
            lines.append(f"## {section}")
            lines.extend(entries)
    return "\n".join(lines) + "\n"


# Regex to parse entry flags: optional [LTM] and/or [GLOBAL] in any order, required [type]
_ENTRY_FLAGS = re.compile(
    r"^(\s*-\s*)"                     # prefix
    r"(?:\[(?:LTM|GLOBAL)\]){0,2}"    # optional [LTM] and/or [GLOBAL] in any order
    r"\[(?!LTM\]|GLOBAL\])([a-zA-Z]+)\]"  # [type] (not LTM or GLOBAL)
    r"(\s+.*)$"                       # rest
)
_LTM_FLAG = re.compile(r"\[LTM\]")
_GLOBAL_FLAG = re.compile(r"\[GLOBAL\]")


def build_dailies_from_project_blocks(
    blocks: list[ProjectBlock], date: str
) -> list[DailyFile]:
    """Convert project blocks to a single DailyFile with scoped, sectioned entries.

    For each entry:
    1. Parse flags: [LTM] (stripped), [GLOBAL] (affects scope), [type] (maps to section)
    2. Apply scope: project + type -> [project/type], GLOBAL -> [global|project/type]
    3. Assign to section via TYPE_TO_SECTION

    All projects merge into one DailyFile for the date.
    """
    sections: dict[str, list[str]] = {s: [] for s in SECTION_ORDER}

    for block in blocks:
        project = block.project
        for entry in block.entries:
            match = _ENTRY_FLAGS.match(entry)
            if not match:
                continue

            prefix = match.group(1)
            entry_type = match.group(2).lower()
            rest = match.group(3)

            has_global = bool(_GLOBAL_FLAG.search(entry))

            # Build scope tag
            if project == "global" or (not project):
                scope_tag = f"[global/{entry_type}]"
            elif has_global:
                scope_tag = f"[global|{project}/{entry_type}]"
            else:
                scope_tag = f"[{project}/{entry_type}]"

            section = TYPE_TO_SECTION.get(entry_type, "Actions")
            sections[section].append(f"{prefix}{scope_tag}{rest}")

    # Assemble
    lines = [f"# {date}"]
    for section in SECTION_ORDER:
        if sections[section]:
            lines.append(f"## {section}")
            lines.extend(sections[section])
    return [DailyFile(date=date, content="\n".join(lines))]


def extract_routes_from_project_blocks(
    blocks: list[ProjectBlock], date: str
) -> list[RouteEntry]:
    """Extract [LTM]-flagged entries from project blocks as RouteEntry objects.

    - Strips [LTM] and [GLOBAL] flags, adds (date) prefix
    - Maps type to "Key {Section}" for LTM section targeting
    - [GLOBAL] entries produce routes to both project and global LTM
    - Groups entries by (scope, section)
    """
    grouped: dict[tuple[str, str], list[str]] = {}

    for block in blocks:
        for entry in block.entries:
            if not _LTM_FLAG.search(entry):
                continue
            match = _ENTRY_FLAGS.match(entry)
            if not match:
                continue

            entry_type = match.group(2).lower()
            rest = match.group(3)
            has_global = bool(_GLOBAL_FLAG.search(entry))

            section = f"Key {TYPE_TO_SECTION.get(entry_type, 'Actions')}"
            formatted = f"- ({date}) [{entry_type}]{rest}"

            # Route to project (or global if project is global)
            scope = block.project if block.project else "global"
            grouped.setdefault((scope, section), []).append(formatted)

            # GLOBAL flag: also route to global LTM (if not already global)
            if has_global and scope != "global":
                grouped.setdefault(("global", section), []).append(formatted)

    return [
        RouteEntry(scope=scope, section=section, entries=entries)
        for (scope, section), entries in grouped.items()
    ]


# --- Legacy: old ===DAILY=== format support ---
# The following regexes and functions support the ===DAILY:date=== format.
# They are used by the backwards-compatible path in apply_results().
# Once all synthesis output uses ===PROJECT:X=== format, these can be removed.

# Pattern to detect LLM's simplified output: - [type] or - [GLOBAL][type]
_UNSCOPED_ENTRY = re.compile(
    r"^(\s*-\s*)"           # prefix: "- "
    r"(?:\[GLOBAL\])?"      # optional [GLOBAL] marker
    r"\[([a-z]+)\]"         # [type] (lowercase type name)
    r"(\s+.*)$"             # rest of entry
)
_GLOBAL_MARKER = re.compile(r"^\s*-\s*\[GLOBAL\]")
# Pattern to detect already-scoped entries: - [scope/type] or - [scope|scope/type]
# Scope names must be lowercase alphanumeric + hyphens (rejects placeholders like {name})
_SCOPED_ENTRY = re.compile(r"^\s*-\s*(?:\[routed\])?\s*\[[a-z0-9-]+(?:\|[a-z0-9-]+)*/[^\]]+\]")


def inject_scopes(
    dailies: list[DailyFile],
    session_projects: dict[str, str | None],
) -> list[DailyFile]:
    """Inject project scope tags into daily entries based on session metadata.

    Legacy: used by the ===DAILY:date=== backwards-compatible path in apply_results().

    Transforms LLM's simplified output:
    - [type] Description       -> [project/type] Description
    - [GLOBAL][type] Desc      -> [global|project/type] Desc (or [global/type] if no project)

    Args:
        dailies: Parsed daily files from LLM output
        session_projects: Dict mapping date -> project name (None = global)

    Returns:
        New list of DailyFile objects with scope-injected content.
    """
    result = []
    for daily in dailies:
        project = session_projects.get(daily.date)
        new_lines = []
        for line in daily.content.split("\n"):
            # Skip non-entry lines (headers, blanks)
            if not line.strip().startswith("-"):
                new_lines.append(line)
                continue

            # Strip LLM-leaked placeholder scopes like [{name}/type] -> [type]
            line = re.sub(
                r"(\s*-\s*(?:\[GLOBAL\])?)\[\{[^}]*\}/([a-z]+)\]",
                r"\1[\2]",
                line,
            )

            # Already scoped — pass through
            if _SCOPED_ENTRY.match(line):
                new_lines.append(line)
                continue

            match = _UNSCOPED_ENTRY.match(line)
            if match:
                prefix = match.group(1)
                entry_type = match.group(2)
                rest = match.group(3)
                has_global = bool(_GLOBAL_MARKER.match(line))

                if project:
                    if has_global:
                        tag = f"[global|{project}/{entry_type}]"
                    else:
                        tag = f"[{project}/{entry_type}]"
                else:
                    tag = f"[global/{entry_type}]"

                new_lines.append(f"{prefix}{tag}{rest}")
            else:
                new_lines.append(line)

        result.append(DailyFile(date=daily.date, content="\n".join(new_lines)))
    return result


_PROJECT_HEADER = re.compile(r"Session:\s+\S+\s+\[project:\s+([^\]]+)\]")


def _extract_session_projects(extract_paths: list[str]) -> dict[str, str | None]:
    """Extract date -> project mapping from transcript extract files.

    Legacy: used by the ===DAILY:date=== backwards-compatible path in apply_results().
    """
    from collections import Counter

    date_projects: dict[str, Counter] = {}
    for path in extract_paths:
        try:
            content = Path(path).read_text(encoding="utf-8")
        except IOError:
            continue
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", Path(path).name)
        if not date_match:
            continue
        date = date_match.group(1)
        if date not in date_projects:
            date_projects[date] = Counter()

        for match in _PROJECT_HEADER.finditer(content):
            name = match.group(1).strip()
            date_projects[date][name] += 1

    result: dict[str, str | None] = {}
    for date, counter in date_projects.items():
        if not counter:
            result[date] = None
        else:
            real = {k: v for k, v in counter.items() if k != "global"}
            if real:
                result[date] = max(real, key=lambda k: real[k])
            else:
                result[date] = None
    return result



def mark_routed_entries(
    dailies: list[DailyFile],
    routes: list[RouteEntry],
) -> list[DailyFile]:
    """Mark daily entries as [routed] when they appear in route blocks.

    Matches by description text (the part after [scope/type] or [type]).
    Returns new list of DailyFile with [routed] prefix applied.
    """
    if not routes:
        return dailies

    # Collect all routed descriptions (lowercased for fuzzy match)
    routed_descriptions: set[str] = set()
    for route in routes:
        for entry in route.entries:
            desc = _extract_description(entry).lower()
            if desc:
                routed_descriptions.add(desc)

    if not routed_descriptions:
        return dailies

    marked_dailies = []
    for daily in dailies:
        new_lines = []
        for line in daily.content.split("\n"):
            stripped = line.strip()
            # Only mark tagged entries that aren't already routed
            if (
                stripped.startswith("- [")
                and not stripped.startswith("- [routed]")
                and _extract_description(stripped).lower() in routed_descriptions
            ):
                line = re.sub(r"^(\s*- )\[", r"\1[routed][", line)
            new_lines.append(line)
        marked_dailies.append(
            DailyFile(date=daily.date, content="\n".join(new_lines))
        )

    return marked_dailies


def write_daily_files(dailies: list[DailyFile], daily_dir: Path | None = None) -> list[str]:
    """Write daily summary files atomically. Merges with existing if present.

    Returns list of written file paths.
    """
    if daily_dir is None:
        daily_dir = get_daily_dir()
    daily_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    for daily in dailies:
        target = daily_dir / f"{daily.date}.md"

        if target.exists():
            existing = target.read_text(encoding="utf-8")
            merged = merge_daily_sections(existing, daily.content)
        else:
            merged = daily.content

        tmp = target.with_suffix(".tmp")
        tmp.write_text(merged + "\n", encoding="utf-8")
        tmp.rename(target)
        written.append(str(target))
    return written


def append_to_ltm(
    routes: list[RouteEntry],
    ltm_dir: Path | None = None,
    global_file: Path | None = None,
    template_dir: Path | None = None,
) -> list[str]:
    """Append routed entries to LTM file sections. Returns warnings list.

    For each RouteEntry, finds the target file (global LTM or project LTM),
    locates the matching section header, and inserts new entries after the
    header/comment block. Skips entries that already exist (dedup by exact match).
    Creates project files from template if missing.
    """
    if global_file is None:
        global_file = get_global_memory_file()
    if ltm_dir is None:
        ltm_dir = get_project_memory_dir()
    if template_dir is None:
        template_dir = get_memory_dir() / "templates"

    from memory_utils import project_name_to_filename

    warnings: list[str] = []

    # Group routes by target file
    file_routes: dict[Path, list[RouteEntry]] = {}
    for route in routes:
        if route.scope == "global":
            target = global_file
        else:
            filename = project_name_to_filename(route.scope)
            target = ltm_dir / filename
        file_routes.setdefault(target, []).append(route)

    for target_file, file_route_list in file_routes.items():
        # Create from template if missing
        if not target_file.exists():
            template = template_dir / "project-long-term-memory.md"
            if template.exists():
                target_file.parent.mkdir(parents=True, exist_ok=True)
                content = template.read_text(encoding="utf-8")
                scope = file_route_list[0].scope
                content = content.replace("{project}", scope)
                target_file.write_text(content, encoding="utf-8")
            else:
                warnings.append(f"No template and no file for {target_file.name}")
                continue

        content = target_file.read_text(encoding="utf-8")
        lines = content.split("\n")

        # Collect ALL entries across all sections (including Pinned) for cross-section dedup
        all_file_entries = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("- ") and not stripped.startswith("<!--"):
                all_file_entries.append(line)

        # Track entries added per file for route cap
        entries_added_to_file = 0

        for route in file_route_list:
            section_header = f"## {route.section}"
            # Find the section
            section_idx = None
            for idx, line in enumerate(lines):
                if line.strip() == section_header:
                    section_idx = idx
                    break

            if section_idx is None:
                warnings.append(f"Section '{route.section}' not found in {target_file.name}")
                continue

            # Find insertion point: after section header + comment lines + blank lines
            insert_idx = section_idx + 1
            while insert_idx < len(lines) and (
                lines[insert_idx].strip().startswith("<!--") or lines[insert_idx].strip() == ""
            ):
                insert_idx += 1

            # Filter: quality floor + cross-section keyword dedup + route cap
            new_entries = []
            for entry in route.entries:
                # Quality floor: reject entries with too few meaningful keywords
                entry_keywords = extract_entry_keywords(entry)
                if len(entry_keywords) < MIN_ROUTE_KEYWORDS:
                    warnings.append(
                        f"Entry below quality floor ({len(entry_keywords)} keywords), "
                        f"skipping: {entry[:80]}"
                    )
                    continue

                if entries_added_to_file >= ROUTE_CAP:
                    warnings.append(
                        f"Route cap ({ROUTE_CAP}) reached for {target_file.name}, "
                        f"skipping remaining entries"
                    )
                    break
                if not any(
                    is_routed_match(entry, existing, threshold=0.6)
                    for existing in all_file_entries
                ):
                    new_entries.append(entry)
                    all_file_entries.append(entry)  # Prevent intra-batch cross-section dupes
                    entries_added_to_file += 1

            if new_entries:
                for entry in reversed(new_entries):
                    lines.insert(insert_idx, entry)

        target_file.write_text("\n".join(lines), encoding="utf-8")

    return warnings


def run_mark_routed() -> None:
    """Run mark-routed dedup as function call (no subprocess)."""
    try:
        from devtools import cmd_mark_routed

        with contextlib.redirect_stdout(io.StringIO()):
            cmd_mark_routed(argparse.Namespace(dry_run=False))
    except Exception:
        pass  # Non-critical


def run_validate_ltm() -> None:
    """Run LTM validation as function call (no subprocess)."""
    try:
        from devtools import cmd_validate_ltm

        with contextlib.redirect_stdout(io.StringIO()):
            cmd_validate_ltm(argparse.Namespace())
    except Exception:
        pass  # Non-critical


def run_decay() -> None:
    """Run decay as function call (no subprocess)."""
    try:
        from decay import run as decay_run

        with contextlib.redirect_stdout(io.StringIO()):
            decay_run(dry_run=False)
    except Exception:
        pass  # Non-critical


def run_post_processing(
    extract_paths: list[str],
    offsets_json: str | None = None,
    session_ids: list[str] | None = None,
) -> None:
    """Run cleanup, decay, validation, and timestamp update."""
    from datetime import datetime, timezone

    # Cleanup temp files
    paths_to_clean = list(extract_paths)
    if offsets_json:
        paths_to_clean.append(offsets_json)
    for path in paths_to_clean:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    # Delete pending-recall files for processed sessions
    if session_ids:
        recall_dir = get_pending_recall_dir()
        if recall_dir.exists():
            for sid in session_ids:
                recall_file = recall_dir / f"{sid}.md"
                try:
                    recall_file.unlink(missing_ok=True)
                except OSError:
                    pass

    # Rebuild projects index so decay sees current work days
    rebuild_projects_index_quiet()

    # Direct function calls instead of subprocesses
    run_mark_routed()
    run_validate_ltm()
    run_decay()

    # Update timestamp
    ts_file = get_memory_dir() / ".last-synthesis"
    ts_file.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


_SESSION_HEADER_RE = re.compile(r"^Session:\s+(\S+)")


def _count_nonblank_lines(filepath: Path) -> int:
    """Count non-blank lines in a file."""
    count = 0
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def compute_offsets_from_extracts(extract_paths: list[str]) -> dict[str, dict]:
    """Compute session offsets from extract files and JSONL sources on disk.

    Parses session IDs from extract file headers, locates the corresponding
    JSONL transcript files in ~/.claude/projects/, and computes current file
    size and line count.

    This eliminates the dependency on --offsets-json being passed via CLI,
    which required the LLM to faithfully reproduce the argument.

    Returns:
        Dict mapping session_id -> {"offset": file_size, "lines": line_count}
    """
    # Parse session IDs from extract files
    session_ids: set[str] = set()
    for path in extract_paths:
        try:
            for line in Path(path).open(encoding="utf-8"):
                match = _SESSION_HEADER_RE.match(line)
                if match:
                    session_ids.add(match.group(1))
        except IOError:
            continue

    if not session_ids:
        return {}

    # Find JSONL files and compute offsets
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return {}

    offsets: dict[str, dict] = {}
    for sid in session_ids:
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            session_file = proj_dir / f"{sid}.jsonl"
            if session_file.exists():
                offsets[sid] = {
                    "offset": session_file.stat().st_size,
                    "lines": _count_nonblank_lines(session_file),
                }
                break

    return offsets


def _extract_date_from_extracts(extract_paths: list[str]) -> str:
    """Extract date from extract file names (format: *YYYY-MM-DD*).

    Scans file names for a YYYY-MM-DD pattern and returns the first match.
    Falls back to today's date if no date is found.
    """
    for path in extract_paths:
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", Path(path).name)
        if date_match:
            return date_match.group(1)
    # Fallback: today
    from datetime import date

    return date.today().isoformat()


def apply_results(
    output_file: str,
    extract_paths: list[str],
    offsets_json: str | None = None,
) -> None:
    """Full pipeline: parse output -> scope/section -> write files -> post-process."""
    text = Path(output_file).read_text(encoding="utf-8")
    result = parse_synthesis_output(text)

    if result.project_blocks:
        # New format: ===PROJECT:X=== blocks
        date = _extract_date_from_extracts(extract_paths)

        dailies = build_dailies_from_project_blocks(result.project_blocks, date)
        routes = extract_routes_from_project_blocks(result.project_blocks, date)

        # Mark routed entries in dailies
        marked_dailies = mark_routed_entries(dailies, routes)
        written = write_daily_files(marked_dailies)

    elif result.dailies:
        # Legacy format: ===DAILY:date=== blocks (backwards compat)
        marked_dailies = mark_routed_entries(result.dailies, result.routes)
        session_projects = _extract_session_projects(extract_paths)
        scoped_dailies = inject_scopes(marked_dailies, session_projects)
        written = write_daily_files(scoped_dailies)
        routes = result.routes
    else:
        print(
            "No daily or project blocks found. Synthesis may have failed.",
            file=sys.stderr,
        )
        return

    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    print(f"Wrote {len(written)} daily file(s)")

    # Append to LTM
    ltm_warnings = append_to_ltm(routes)
    for w in ltm_warnings:
        print(f"LTM warning: {w}", file=sys.stderr)
    if routes:
        total_entries = sum(len(r.entries) for r in routes)
        print(f"Routed {total_entries} entries to LTM")

    # Update synthesis state with new high water marks
    offsets = {}
    if offsets_json:
        # Legacy path: offsets passed via --offsets-json CLI arg
        try:
            offsets = json.loads(Path(offsets_json).read_text(encoding="utf-8"))
            update_synthesis_state(offsets)
        except (IOError, json.JSONDecodeError) as e:
            print(f"Warning: Could not update synthesis state: {e}", file=sys.stderr)
    else:
        # Primary path: compute offsets directly from extract files and JSONL sources
        offsets = compute_offsets_from_extracts(extract_paths)
        if offsets:
            update_synthesis_state(offsets)

    # Post-processing: pass session IDs for pending-recall cleanup.
    # If offsets is empty (state write failed or no sessions found), session_ids
    # is None — recall files are left for the 24h safety net in load_memory.py.
    session_ids = list(offsets.keys()) if offsets else None
    run_post_processing(extract_paths, offsets_json=offsets_json, session_ids=session_ids)
    print("Post-processing complete")


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Synthesis output processor")
    sub = parser.add_subparsers(dest="command")

    apply_parser = sub.add_parser("apply", help="Apply synthesis output")
    apply_parser.add_argument("output_file", help="Path to synthesis output file")
    apply_parser.add_argument("--extracts", nargs="*", default=[], help="Extract file paths to clean up")
    apply_parser.add_argument("--offsets-json", default=None, help="Path to session offsets JSON for state update")

    args = parser.parse_args()
    if args.command == "apply":
        apply_results(
            args.output_file,
            args.extracts,
            offsets_json=getattr(args, "offsets_json", None),
        )
        return 0
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
