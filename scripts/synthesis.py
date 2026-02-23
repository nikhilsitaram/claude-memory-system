#!/usr/bin/env python3
"""
Synthesis output parser and applier for Claude Code Memory System.

Parses structured output from the synthesis subagent and applies results:
- Writes daily summary files
- Appends routed entries to LTM files
- Marks [routed] entries in daily files
- Runs post-processing (mark-captured, decay, validation, timestamp)

Usage:
    python3 synthesis.py apply <output_file> --sidecars <path1> [<path2>...] --extracts <path1> [<path2>...]

Requirements: Python 3.9+
"""

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

# Delimiter patterns
DAILY_HEADER = re.compile(r"^===DAILY:(\d{4}-\d{2}-\d{2})===$")
ROUTE_HEADER = re.compile(r"^===ROUTE:([^:]+):(.+)===$")
END_MARKER = "===END==="


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
class SynthesisResult:
    """Complete parsed synthesis output containing dailies, routes, and warnings."""

    dailies: list[DailyFile] = field(default_factory=list)
    routes: list[RouteEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_synthesis_output(text: str) -> SynthesisResult:
    """Parse structured synthesis output into daily files and route entries.

    Format:
        ===DAILY:YYYY-MM-DD===
        [markdown content]

        ===ROUTE:scope:section===
        - (YYYY-MM-DD) [type] Description

        ===END===

    Text before the first delimiter is ignored. Missing ===END=== produces
    a warning but content is still parsed.
    """
    result = SynthesisResult()
    lines = text.split("\n")
    has_end = END_MARKER in text

    has_daily = any(DAILY_HEADER.match(line.strip()) for line in lines)
    if not has_end and has_daily:
        result.warnings.append("Missing ===END=== marker; processing available content")

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Check for daily header
        daily_match = DAILY_HEADER.match(line)
        if daily_match:
            date = daily_match.group(1)
            content_lines = []
            i += 1
            while i < len(lines):
                stripped = lines[i].strip()
                if (
                    DAILY_HEADER.match(stripped)
                    or ROUTE_HEADER.match(stripped)
                    or stripped == END_MARKER
                ):
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
                if (
                    DAILY_HEADER.match(stripped)
                    or ROUTE_HEADER.match(stripped)
                    or stripped == END_MARKER
                ):
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
