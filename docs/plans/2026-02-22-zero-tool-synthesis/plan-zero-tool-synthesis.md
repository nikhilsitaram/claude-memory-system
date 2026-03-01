# Zero-Tool Synthesis Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce background synthesis from ~113s to ~40-55s by moving all file I/O from the subagent to Python, eliminating the Read round, and switching to haiku.

**Architecture:** Pre-read all files in Python, embed in prompt, subagent outputs structured text (Write to temp file), then a single Bash call runs `synthesis.py apply` which parses output, writes daily files, appends to LTM, and runs all post-processing.

**Tech Stack:** Python 3.9+, pathlib, re, argparse. No new dependencies.

**Design doc:** `docs/plans/2026-02-22-zero-tool-synthesis-design.md`

---

### Task 1: Create `scripts/synthesis.py` — Output Parser

**Files:**
- Create: `scripts/synthesis.py`
- Test: `tests/test_synthesis.py`

**Step 1: Write the failing tests for `parse_synthesis_output()`**

The parser splits structured text delimited by `===DAILY:<date>===`, `===ROUTE:<scope>:<section>===`, and `===END===`.

```python
# tests/test_synthesis.py
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from synthesis import DailyFile, RouteEntry, SynthesisResult, parse_synthesis_output


class TestParseSynthesisOutput:
    def test_single_daily_no_routes(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Did something
## Learnings
- [global/pattern] Learned something

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1
        assert result.dailies[0].date == "2026-02-22"
        assert "[global/implement] Did something" in result.dailies[0].content
        assert len(result.routes) == 0

    def test_multiple_dailies_with_routes(self):
        text = """===DAILY:2026-02-21===
# 2026-02-21
## Actions
- [proj/implement] Built feature

===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/pattern] A pattern

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] A pattern

===ROUTE:proj:Key Actions===
- (2026-02-21) [implement] Built feature

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 2
        assert result.dailies[0].date == "2026-02-21"
        assert result.dailies[1].date == "2026-02-22"
        assert len(result.routes) == 2
        assert result.routes[0].scope == "global"
        assert result.routes[0].section == "Key Learnings"
        assert len(result.routes[0].entries) == 1
        assert result.routes[1].scope == "proj"

    def test_text_before_delimiters_ignored(self):
        text = """I'll now generate the synthesis output.

===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1
        assert "I'll now generate" not in result.dailies[0].content

    def test_missing_end_marker_warns(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something"""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1  # still parsed
        assert result.warnings  # warning about missing ===END===

    def test_no_daily_blocks_returns_empty(self):
        text = "Just some random text with no structure"
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 0
        assert len(result.routes) == 0

    def test_empty_route_block_skipped(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===ROUTE:global:Key Learnings===

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.routes) == 0  # empty route skipped

    def test_route_entries_parsed_correctly(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/gotcha] A gotcha

===ROUTE:global:Key Learnings===
- (2026-02-22) [gotcha] A gotcha
- (2026-02-22) [pattern] A pattern

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.routes) == 1
        assert len(result.routes[0].entries) == 2
        assert "[gotcha] A gotcha" in result.routes[0].entries[0]
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'synthesis'`

**Step 3: Write the implementation**

```python
# scripts/synthesis.py
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

import argparse
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
    date: str
    content: str


@dataclass
class RouteEntry:
    scope: str
    section: str
    entries: list[str]


@dataclass
class SynthesisResult:
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
    """
    result = SynthesisResult()
    lines = text.split("\n")
    i = 0
    has_end = END_MARKER in text

    if not has_end and DAILY_HEADER.search(text):
        result.warnings.append("Missing ===END=== marker; processing available content")

    while i < len(lines):
        line = lines[i].strip()

        # Check for daily header
        daily_match = DAILY_HEADER.match(line)
        if daily_match:
            date = daily_match.group(1)
            content_lines = []
            i += 1
            while i < len(lines):
                if (DAILY_HEADER.match(lines[i].strip())
                        or ROUTE_HEADER.match(lines[i].strip())
                        or lines[i].strip() == END_MARKER):
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
                if (DAILY_HEADER.match(stripped)
                        or ROUTE_HEADER.match(stripped)
                        or stripped == END_MARKER):
                    break
                if stripped.startswith("- "):
                    entries.append(stripped)
                i += 1
            if entries:
                result.routes.append(RouteEntry(scope=scope, section=section, entries=entries))
            continue

        i += 1

    return result
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: All 7 tests PASS

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: add synthesis output parser (parse_synthesis_output)"
```

---

### Task 2: Add `mark_routed_entries()` to `synthesis.py`

**Files:**
- Modify: `scripts/synthesis.py`
- Test: `tests/test_synthesis.py`

**Step 1: Write the failing tests**

```python
# Add to tests/test_synthesis.py
from synthesis import mark_routed_entries


class TestMarkRoutedEntries:
    def test_marks_matching_entries(self):
        daily = DailyFile(
            date="2026-02-22",
            content="""# 2026-02-22
## Actions
- [global/implement] Built feature A
## Learnings
- [global/pattern] Pattern X is useful
- [global/gotcha] Gotcha Y is tricky"""
        )
        routes = [
            RouteEntry(scope="global", section="Key Learnings",
                       entries=["- (2026-02-22) [pattern] Pattern X is useful"]),
        ]
        result = mark_routed_entries([daily], routes)
        assert "[routed][global/pattern] Pattern X is useful" in result[0].content
        assert "[routed][global/implement]" not in result[0].content  # not routed
        assert "[routed][global/gotcha]" not in result[0].content  # not routed

    def test_no_routes_no_changes(self):
        daily = DailyFile(date="2026-02-22", content="## Actions\n- [global/implement] Something")
        result = mark_routed_entries([daily], [])
        assert result[0].content == daily.content

    def test_already_routed_not_double_marked(self):
        daily = DailyFile(
            date="2026-02-22",
            content="## Learnings\n- [routed][global/pattern] Already marked"
        )
        routes = [
            RouteEntry(scope="global", section="Key Learnings",
                       entries=["- (2026-02-22) [pattern] Already marked"]),
        ]
        result = mark_routed_entries([daily], routes)
        assert result[0].content.count("[routed]") == 1  # no double marking

    def test_multiple_dailies_marked_independently(self):
        d1 = DailyFile(date="2026-02-21", content="## Learnings\n- [proj/pattern] P1")
        d2 = DailyFile(date="2026-02-22", content="## Learnings\n- [global/pattern] P2")
        routes = [
            RouteEntry(scope="proj", section="Key Learnings",
                       entries=["- (2026-02-21) [pattern] P1"]),
        ]
        result = mark_routed_entries([d1, d2], routes)
        assert "[routed]" in result[0].content
        assert "[routed]" not in result[1].content
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestMarkRoutedEntries -v`
Expected: FAIL with `ImportError: cannot import name 'mark_routed_entries'`

**Step 3: Write the implementation**

Add to `scripts/synthesis.py`:

```python
def _extract_description(entry: str) -> str:
    """Extract the description portion from a route entry for matching.

    Route format: '- (YYYY-MM-DD) [type] Description'
    Daily format: '- [scope/type] Description'
    Returns the text after the last ] bracket.
    """
    # Find last ] and take everything after
    idx = entry.rfind("]")
    if idx >= 0:
        return entry[idx + 1:].strip()
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
            if (stripped.startswith("- [")
                    and not stripped.startswith("- [routed]")
                    and _extract_description(stripped).lower() in routed_descriptions):
                line = re.sub(r"^(\s*- )\[", r"\1[routed][", line)
            new_lines.append(line)
        marked_dailies.append(DailyFile(date=daily.date, content="\n".join(new_lines)))

    return marked_dailies
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: add deterministic [routed] marking (mark_routed_entries)"
```

---

### Task 3: Add `write_daily_files()` and `append_to_ltm()` to `synthesis.py`

**Files:**
- Modify: `scripts/synthesis.py`
- Test: `tests/test_synthesis.py`

**Step 1: Write the failing tests**

```python
from synthesis import write_daily_files, append_to_ltm


class TestWriteDailyFiles:
    def test_writes_daily_file(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        dailies = [DailyFile(date="2026-02-22", content="# 2026-02-22\n## Actions\n- something")]
        write_daily_files(dailies, daily_dir)
        written = (daily_dir / "2026-02-22.md").read_text()
        assert "# 2026-02-22" in written
        assert "- something" in written

    def test_overwrites_existing_daily(self, tmp_path):
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        (daily_dir / "2026-02-22.md").write_text("old content")
        dailies = [DailyFile(date="2026-02-22", content="new content")]
        write_daily_files(dailies, daily_dir)
        assert (daily_dir / "2026-02-22.md").read_text().strip() == "new content"

    def test_creates_daily_dir_if_missing(self, tmp_path):
        daily_dir = tmp_path / "daily"
        dailies = [DailyFile(date="2026-02-22", content="content")]
        write_daily_files(dailies, daily_dir)
        assert (daily_dir / "2026-02-22.md").exists()


class TestAppendToLtm:
    def test_appends_entries_to_section(self, tmp_path):
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Long-Term Memory\n\n"
            "## Key Learnings\n"
            "<!-- Subject to 30-day decay -->\n"
            "\n"
            "- (2026-02-01) [pattern] Existing entry\n"
        )
        routes = [RouteEntry(
            scope="global", section="Key Learnings",
            entries=["- (2026-02-22) [gotcha] New entry"],
        )]
        append_to_ltm(routes, ltm_dir=tmp_path, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "- (2026-02-01) [pattern] Existing entry" in content
        assert "- (2026-02-22) [gotcha] New entry" in content

    def test_creates_project_file_from_template(self, tmp_path):
        template = tmp_path / "templates" / "project-long-term-memory.md"
        template.parent.mkdir()
        template.write_text("# {project}\n\n## Key Learnings\n<!-- decay -->\n")
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()

        routes = [RouteEntry(
            scope="my-project", section="Key Learnings",
            entries=["- (2026-02-22) [pattern] First entry"],
        )]
        append_to_ltm(routes, ltm_dir=proj_dir, global_file=tmp_path / "global.md",
                       template_dir=template.parent)
        proj_file = proj_dir / "my-project-long-term-memory.md"
        assert proj_file.exists()
        assert "First entry" in proj_file.read_text()

    def test_section_not_found_skips(self, tmp_path):
        ltm_file = tmp_path / "global.md"
        ltm_file.write_text("# Memory\n\n## About Me\nJust about me.\n")
        routes = [RouteEntry(
            scope="global", section="Key Learnings",
            entries=["- (2026-02-22) [pattern] Orphaned"],
        )]
        # Should not crash, should skip
        warnings = append_to_ltm(routes, ltm_dir=tmp_path, global_file=ltm_file)
        assert any("Key Learnings" in w for w in warnings)

    def test_no_duplicate_append(self, tmp_path):
        ltm_file = tmp_path / "global.md"
        ltm_file.write_text(
            "## Key Learnings\n"
            "<!-- decay -->\n"
            "- (2026-02-22) [pattern] Already exists\n"
        )
        routes = [RouteEntry(
            scope="global", section="Key Learnings",
            entries=["- (2026-02-22) [pattern] Already exists"],
        )]
        append_to_ltm(routes, ltm_dir=tmp_path, global_file=ltm_file)
        content = ltm_file.read_text()
        assert content.count("Already exists") == 1  # not duplicated
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestWriteDailyFiles tests/test_synthesis.py::TestAppendToLtm -v`
Expected: FAIL

**Step 3: Write the implementation**

Add to `scripts/synthesis.py`:

```python
from memory_utils import get_daily_dir, get_global_memory_file, get_project_memory_dir, project_name_to_filename


def write_daily_files(dailies: list[DailyFile], daily_dir: Path | None = None) -> list[str]:
    """Write daily summary files atomically. Returns list of written paths."""
    if daily_dir is None:
        daily_dir = get_daily_dir()
    daily_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for daily in dailies:
        target = daily_dir / f"{daily.date}.md"
        tmp = target.with_suffix(".tmp")
        tmp.write_text(daily.content + "\n", encoding="utf-8")
        tmp.rename(target)
        written.append(str(target))
    return written


def append_to_ltm(
    routes: list[RouteEntry],
    ltm_dir: Path | None = None,
    global_file: Path | None = None,
    template_dir: Path | None = None,
) -> list[str]:
    """Append routed entries to LTM file sections. Returns warnings."""
    if global_file is None:
        global_file = get_global_memory_file()
    if ltm_dir is None:
        ltm_dir = get_project_memory_dir()
    if template_dir is None:
        from memory_utils import get_memory_dir
        template_dir = get_memory_dir() / "templates"

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
        existing_content = content.lower()

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

            # Find insertion point: after section header + comment lines
            insert_idx = section_idx + 1
            while insert_idx < len(lines) and (
                lines[insert_idx].strip().startswith("<!--")
                or lines[insert_idx].strip() == ""
            ):
                insert_idx += 1

            # Filter out entries that already exist
            new_entries = []
            for entry in route.entries:
                if entry.strip().lower() not in existing_content:
                    new_entries.append(entry)

            if new_entries:
                for entry in reversed(new_entries):
                    lines.insert(insert_idx, entry)

        target_file.write_text("\n".join(lines), encoding="utf-8")

    return warnings
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: add write_daily_files and append_to_ltm to synthesis.py"
```

---

### Task 4: Add `apply_results()` CLI and post-processing to `synthesis.py`

**Files:**
- Modify: `scripts/synthesis.py`
- Test: `tests/test_synthesis.py`

This task wires together parsing, marking, writing, and post-processing into a single CLI entry point.

**Step 1: Write the failing tests**

```python
from synthesis import apply_results
from unittest.mock import patch, MagicMock


class TestApplyResults:
    def test_full_pipeline(self, tmp_path):
        """Integration test: parse -> mark_routed -> write -> append."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        global_ltm = tmp_path / "global-long-term-memory.md"
        global_ltm.write_text(
            "## Key Learnings\n<!-- decay -->\n\n"
        )

        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/pattern] Important pattern

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] Important pattern

===END==="""

        output_file = tmp_path / "synthesis-output.txt"
        output_file.write_text(output_text)

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=global_ltm), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"), \
             patch("synthesis.run_post_processing"):
            apply_results(
                output_file=str(output_file),
                sidecar_paths=[],
                extract_paths=[],
            )

        # Daily file written with [routed] marking
        daily_content = (daily_dir / "2026-02-22.md").read_text()
        assert "[routed]" in daily_content

        # LTM updated
        ltm_content = global_ltm.read_text()
        assert "Important pattern" in ltm_content

    def test_no_dailies_skips_everything(self, tmp_path):
        """If output has no ===DAILY: blocks, nothing happens."""
        output_file = tmp_path / "bad-output.txt"
        output_file.write_text("just garbage text")

        with patch("synthesis.run_post_processing") as mock_post:
            apply_results(str(output_file), [], [])
            mock_post.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestApplyResults -v`
Expected: FAIL

**Step 3: Write the implementation**

Add to `scripts/synthesis.py`:

```python
def run_post_processing(
    sidecar_paths: list[str],
    extract_paths: list[str],
) -> None:
    """Run mark-captured, cleanup, decay, validation, and timestamp update."""
    import subprocess
    from datetime import datetime, timezone

    # Mark captured sessions
    for sidecar in sidecar_paths:
        if Path(sidecar).exists():
            subprocess.run(
                [sys.executable, str(script_dir / "indexing.py"),
                 "mark-captured", "--sidecar", sidecar],
                capture_output=True, timeout=30,
            )

    # Cleanup temp files
    for path in extract_paths + sidecar_paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    # Run mark-routed (deterministic, from devtools)
    subprocess.run(
        [sys.executable, str(script_dir / "devtools.py"), "mark-routed"],
        capture_output=True, timeout=30,
    )

    # Validate LTM
    subprocess.run(
        [sys.executable, str(script_dir / "devtools.py"), "validate-ltm"],
        capture_output=True, timeout=30,
    )

    # Run decay
    subprocess.run(
        [sys.executable, str(script_dir / "decay.py")],
        capture_output=True, timeout=60,
    )

    # Update timestamp
    from memory_utils import get_memory_dir
    ts_file = get_memory_dir() / ".last-synthesis"
    ts_file.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


def apply_results(
    output_file: str,
    sidecar_paths: list[str],
    extract_paths: list[str],
) -> None:
    """Full pipeline: parse output -> mark routed -> write files -> post-process."""
    text = Path(output_file).read_text(encoding="utf-8")
    result = parse_synthesis_output(text)

    if not result.dailies:
        print("No daily blocks found in output. Synthesis may have failed.", file=sys.stderr)
        return

    for warning in result.warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    # Mark routed entries in daily files
    marked_dailies = mark_routed_entries(result.dailies, result.routes)

    # Write daily files
    written = write_daily_files(marked_dailies)
    print(f"Wrote {len(written)} daily file(s)")

    # Append to LTM
    ltm_warnings = append_to_ltm(result.routes)
    for w in ltm_warnings:
        print(f"LTM warning: {w}", file=sys.stderr)
    if result.routes:
        total_entries = sum(len(r.entries) for r in result.routes)
        print(f"Routed {total_entries} entries to LTM")

    # Post-processing
    run_post_processing(sidecar_paths, extract_paths)
    print("Post-processing complete")


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Synthesis output processor")
    sub = parser.add_subparsers(dest="command")

    apply_parser = sub.add_parser("apply", help="Apply synthesis output")
    apply_parser.add_argument("output_file", help="Path to synthesis output file")
    apply_parser.add_argument("--sidecars", nargs="*", default=[], help="Sidecar file paths")
    apply_parser.add_argument("--extracts", nargs="*", default=[], help="Extract file paths to clean up")

    args = parser.parse_args()
    if args.command == "apply":
        apply_results(args.output_file, args.sidecars, args.extracts)
        return 0
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: All tests PASS

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: add apply_results CLI and post-processing to synthesis.py"
```

---

### Task 5: Rewrite prompt builders in `load_memory.py`

**Files:**
- Modify: `scripts/load_memory.py`
- Test: `tests/test_load_memory.py`

This is the core change: embed file contents in the prompt, switch to structured output format, and eliminate the auto-extract fallback.

**Step 1: Write the failing tests**

Add tests to `tests/test_load_memory.py` that verify the new prompt format:

```python
class TestBuildPreextractedPromptNew:
    """Tests for the rewritten pre-extracted prompt with embedded content."""

    def test_embeds_transcript_content(self, tmp_path):
        """Transcript content should be embedded inline, not as file paths."""
        extract_file = tmp_path / "extract.txt"
        extract_file.write_text("SESSION DATA HERE")

        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": str(extract_file)},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={"transcripts": {"2026-02-22": "SESSION DATA HERE"}},
        )
        assert "SESSION DATA HERE" in prompt
        assert "===DAILY:" in prompt  # structured output format
        assert "===ROUTE:" in prompt
        assert "===END===" in prompt
        assert "Step 1: Read" not in prompt  # no Read instructions

    def test_embeds_ltm_content(self, tmp_path):
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={
                "transcripts": {"2026-02-22": "transcript"},
                "global_ltm": "## Key Learnings\n- existing",
                "project_ltms": {"proj": "## Key Learnings\n- proj existing"},
            },
        )
        assert "## Key Learnings" in prompt
        assert "- existing" in prompt
        assert "- proj existing" in prompt

    def test_structured_output_instructions(self, tmp_path):
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={"transcripts": {"2026-02-22": "data"}},
        )
        assert "Write(" in prompt or "synthesis.py apply" in prompt
        assert "Do NOT generate a summary" in prompt
        assert "Do NOT use any other tools" in prompt

    def test_no_auto_extract_fallback(self):
        """_build_autoextract_prompt should no longer exist."""
        import load_memory
        assert not hasattr(load_memory, "_build_autoextract_prompt")
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_load_memory.py::TestBuildPreextractedPromptNew -v`
Expected: FAIL

**Step 3: Rewrite the prompt builders**

In `scripts/load_memory.py`:

1. Delete `_build_autoextract_prompt()` entirely
2. Add `pre_read_files()` function that reads LTM + daily files into memory
3. Rewrite `_build_preextracted_prompt()` to embed all content and use structured output format
4. Simplify `_build_synthesis_prompt()` to remove the auto-extract branch
5. Update `_build_synthesis_instructions()` to remove tool-call-specific language (Write/Edit references)
6. Update `pre_extract_transcripts()` to also return file contents (avoid double-read)
7. Update `main()` to pass embedded content and remove fallback path

Key changes to the prompt:
- Replace Step 1 (Read all inputs) with embedded `## Inputs` section
- Replace Step 2 (Write/Edit) with `## Output Format` section (structured `===DAILY:===` / `===ROUTE:===` / `===END===`)
- Replace Step 3 (Bash chain) with: `Write to temp file, then Bash synthesis.py apply`
- Remove "Return a summary" instruction

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_load_memory.py -v`
Expected: All tests PASS (including existing tests, which may need updates for the new function signatures)

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "feat: rewrite synthesis prompt to embed inputs and use structured output"
```

---

### Task 6: Update `main()` flow in `load_memory.py`

**Files:**
- Modify: `scripts/load_memory.py:530-570`

**Step 1: Update the synthesis trigger block in `main()`**

Key changes:
- After `pre_extract_transcripts()`, also read file contents for embedding
- Read global LTM, project LTM files, existing daily files
- Pass embedded content to the prompt builder
- Remove the fallback path (no more `else` branch at line 556)
- If `pre_extract_transcripts()` returns empty, skip synthesis (no fallback)

```python
# In main(), around line 548:
extracted_files = pre_extract_transcripts(pending_dates, exclude_session_id=current_session_id)

if extracted_files:
    # Pre-read all files for embedding in prompt
    embedded = {"transcripts": {}, "global_ltm": "", "project_ltms": {}, "dailies": {}}
    for date, path in extracted_files.items():
        try:
            embedded["transcripts"][date] = Path(path).read_text(encoding="utf-8")
        except IOError:
            pass
    global_ltm_file = get_global_memory_file()
    if global_ltm_file.exists():
        embedded["global_ltm"] = global_ltm_file.read_text(encoding="utf-8")
    # Read existing daily files for merging
    daily_dir = get_daily_dir()
    for date in extracted_files:
        daily_file = daily_dir / f"{date}.md"
        if daily_file.exists():
            embedded["dailies"][date] = daily_file.read_text(encoding="utf-8")
    # Read project LTMs
    proj_dir = get_project_memory_dir()
    if proj_dir.exists():
        for f in proj_dir.glob("*-long-term-memory.md"):
            name = f.stem.replace("-long-term-memory", "")
            embedded["project_ltms"][name] = f.read_text(encoding="utf-8")

    synth_prompt = _build_synthesis_prompt(
        pending_dates=list(extracted_files.keys()),
        extracted_files=extracted_files,
        embedded_files=embedded,
    )
    # ... rest of synthesis output unchanged
```

**Step 2: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All tests PASS

**Step 3: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "feat: embed file contents in synthesis prompt, drop auto-extract fallback"
```

---

### Task 7: Update settings default, install.py, and fix audit items #2 and #30

**Files:**
- Modify: `templates/settings.json`
- Modify: `install.py:114-123`
- Modify: `scripts/devtools.py:6` (audit #30: fix "NOT installed" docstring)
- Modify: `skills/settings/SKILL.md` (audit #2: fix wrong default values)

**Step 1: Change default synthesis model to haiku**

In `templates/settings.json`, change line 26:
```json
"model": "haiku",
```
Update the comment accordingly.

**Step 2: Add `synthesis.py` to install.py `link_scripts()`**

In `install.py:114-123`, add `"synthesis.py"` to the `scripts_to_link` list.

**Step 3: Update devtools.py verify-install + fix docstring (audit #30)**

In `scripts/devtools.py:61-63`, add `"synthesis.py"` to the `scripts` list.

Fix the docstring at line 6. Change:
```
Repo-local utility — NOT installed to ~/.claude/scripts/.
```
to:
```
Dev diagnostics and mark-routed dedup migration. Installed to ~/.claude/scripts/ via symlink.
```

**Step 4: Fix settings SKILL.md wrong defaults (audit #2)**

In `skills/settings/SKILL.md`, fix the incorrect default values:
- Change `globalLongTerm.tokenLimit` default from `5,000` to `3,000`
- Change `projectLongTerm.tokenLimit` default from `5,000` to `3,000`
- Update the Total budget calculation from `16,750` to `11,250`
- Update the calculated limits table to match: `3000 + 1500 + 3000 + 3750 = 11,250`

**Step 5: Run install and verify**

```bash
python3 install.py
ls -la ~/.claude/scripts/synthesis.py  # verify symlink exists
```

**Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All tests PASS

**Step 7: Commit**

```bash
git add templates/settings.json install.py scripts/devtools.py skills/settings/SKILL.md
git commit -m "chore: default synthesis model to haiku, add synthesis.py to installer, fix audit #2 and #30"
```

---

### Task 8: Update `__all__` exports and CLAUDE.md

**Files:**
- Modify: `scripts/synthesis.py` (add `__all__`)
- Modify: `CLAUDE.md`

**Step 1: Add `__all__` to synthesis.py**

```python
__all__ = [
    "DailyFile",
    "RouteEntry",
    "SynthesisResult",
    "parse_synthesis_output",
    "mark_routed_entries",
    "write_daily_files",
    "append_to_ltm",
    "apply_results",
    "run_post_processing",
]
```

**Step 2: Update CLAUDE.md**

Add `synthesis.py` to the repo structure table:
```
│   ├── synthesis.py          # Synthesis output parser and applier
```

Update the `run_post_processing` section in `devtools.py` description if applicable.

Update the features table to reflect the new architecture.

**Step 3: Commit**

```bash
git add scripts/synthesis.py CLAUDE.md
git commit -m "docs: update CLAUDE.md and add __all__ exports to synthesis.py"
```

---

### Task 9: Integration Test — End-to-End Synthesis

**Files:**
- Test: `tests/test_synthesis.py`

**Step 1: Write an integration test**

```python
class TestEndToEnd:
    """Full pipeline integration test with real file operations."""

    def test_cli_apply(self, tmp_path):
        """Test the CLI entry point with a realistic output file."""
        import subprocess

        # Set up directory structure
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        template_dir = tmp_path / "templates"
        template_dir.mkdir()
        template_dir.joinpath("project-long-term-memory.md").write_text(
            "# {project}\n\n## Key Learnings\n<!-- decay -->\n\n## Key Lessons\n<!-- decay -->\n"
        )

        global_ltm = memory_dir / "global-long-term-memory.md"
        global_ltm.write_text(
            "# Global LTM\n\n## Key Learnings\n<!-- decay -->\n\n## Key Lessons\n<!-- decay -->\n"
        )

        output_file = tmp_path / "output.txt"
        output_file.write_text("""===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Built something cool
## Learnings
- [global/pattern] Pattern is useful

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] Pattern is useful

===END===
""")

        # Verify the parser works end-to-end
        from synthesis import parse_synthesis_output, mark_routed_entries, write_daily_files, append_to_ltm

        result = parse_synthesis_output(output_file.read_text())
        assert len(result.dailies) == 1
        assert len(result.routes) == 1

        marked = mark_routed_entries(result.dailies, result.routes)
        assert "[routed]" in marked[0].content

        write_daily_files(marked, daily_dir)
        assert (daily_dir / "2026-02-22.md").exists()
        assert "[routed]" in (daily_dir / "2026-02-22.md").read_text()

        warnings = append_to_ltm(result.routes, ltm_dir=proj_dir, global_file=global_ltm,
                                  template_dir=template_dir)
        assert len(warnings) == 0
        assert "Pattern is useful" in global_ltm.read_text()
```

**Step 2: Run the integration test**

Run: `python3 -m pytest tests/test_synthesis.py::TestEndToEnd -v`
Expected: PASS

**Step 3: Run full suite one final time**

Run: `python3 -m pytest tests/ -q`
Expected: All tests PASS

**Step 4: Commit**

```bash
git add tests/test_synthesis.py
git commit -m "test: add end-to-end integration test for synthesis pipeline"
```

---

### Task 10: Run install and manual smoke test

**Step 1: Install**

```bash
python3 install.py
```

**Step 2: Verify synthesis.py is installed**

```bash
ls -la ~/.claude/scripts/synthesis.py
```

**Step 3: Test with a dry run**

```bash
# Generate a test synthesis output
cat > /tmp/test-synthesis-output.txt << 'EOF'
===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Test action
## Learnings
- [global/pattern] Test pattern

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] Test pattern

===END===
EOF

# Run the apply command (will write to real paths — review output first!)
python3 ~/.claude/scripts/synthesis.py apply /tmp/test-synthesis-output.txt
```

**Step 4: Verify results**

```bash
cat ~/.claude/memory/daily/2026-02-22.md  # should contain test content
```

**Step 5: Clean up test data and commit any final fixes**

---

## Summary

| Task | What | Files | Tests |
|------|------|-------|-------|
| 1 | Parser: `parse_synthesis_output()` | synthesis.py | 7 |
| 2 | Routed marking: `mark_routed_entries()` | synthesis.py | 4 |
| 3 | File I/O: `write_daily_files()`, `append_to_ltm()` | synthesis.py | 5 |
| 4 | CLI + post-processing: `apply_results()` | synthesis.py | 2 |
| 5 | Rewrite prompt builders | load_memory.py | 4 |
| 6 | Update `main()` flow | load_memory.py | existing |
| 7 | Settings + installer + audit fixes #2, #30 | settings.json, install.py, devtools.py, SKILL.md | - |
| 8 | Exports + docs | synthesis.py, CLAUDE.md | - |
| 9 | Integration test | test_synthesis.py | 1 |
| 10 | Manual smoke test | - | manual |
