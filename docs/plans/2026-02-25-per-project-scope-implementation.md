# Per-Project Scope Injection Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace per-date majority-vote scope injection with per-project scope from `===PROJECT:X===` blocks in LLM output, add inline `[LTM]` routing flag, and fix worktree resolution for unindexed worktrees.

**Architecture:** The LLM output format changes from `===DAILY:date===` with `## Section` headers and `===ROUTE:scope:section===` blocks to `===PROJECT:name===` with flat `[type]` entries and inline `[LTM]` flags. Python handles all structure: type→section mapping, scope injection, daily file assembly, and LTM routing. The parser, scope injector, daily writer, and LTM router all change; merge logic and dedup stay the same.

**Tech Stack:** Python 3, pytest, pathlib, re

**Design doc:** `docs/plans/2026-02-25-per-project-scope-design.md`

---

### Task 1: Type-to-Section Mapping Constant

**Files:**
- Modify: `scripts/synthesis.py:64-71` (constants area)
- Test: `tests/test_synthesis.py`

**Step 1: Write the failing test**

```python
class TestTypeToSection:
    def test_action_types(self):
        from synthesis import TYPE_TO_SECTION
        assert TYPE_TO_SECTION["implement"] == "Actions"
        assert TYPE_TO_SECTION["improve"] == "Actions"
        assert TYPE_TO_SECTION["document"] == "Actions"
        assert TYPE_TO_SECTION["analyze"] == "Actions"

    def test_decision_types(self):
        from synthesis import TYPE_TO_SECTION
        assert TYPE_TO_SECTION["design"] == "Decisions"
        assert TYPE_TO_SECTION["tradeoff"] == "Decisions"
        assert TYPE_TO_SECTION["scope"] == "Decisions"

    def test_learning_types(self):
        from synthesis import TYPE_TO_SECTION
        assert TYPE_TO_SECTION["gotcha"] == "Learnings"
        assert TYPE_TO_SECTION["pitfall"] == "Learnings"
        assert TYPE_TO_SECTION["pattern"] == "Learnings"

    def test_lesson_types(self):
        from synthesis import TYPE_TO_SECTION
        assert TYPE_TO_SECTION["insight"] == "Lessons"
        assert TYPE_TO_SECTION["tip"] == "Lessons"
        assert TYPE_TO_SECTION["workaround"] == "Lessons"

    def test_all_types_covered(self):
        from synthesis import TYPE_TO_SECTION, SECTION_ORDER
        assert set(TYPE_TO_SECTION.values()) == set(SECTION_ORDER)
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_synthesis.py::TestTypeToSection -v`
Expected: FAIL — `ImportError: cannot import name 'TYPE_TO_SECTION'`

**Step 3: Write minimal implementation**

Add to `scripts/synthesis.py` after line 71 (after `ROUTE_CAP`):

```python
# Type → Section mapping (deterministic)
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
```

Also add `"TYPE_TO_SECTION"` to the `__all__` list.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_synthesis.py::TestTypeToSection -v`
Expected: PASS

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: add TYPE_TO_SECTION constant for deterministic section mapping"
```

---

### Task 2: New Parser for `===PROJECT:X===` Format

**Files:**
- Modify: `scripts/synthesis.py:64-67` (add `PROJECT_HEADER` regex), `scripts/synthesis.py:74-98` (add new dataclasses), `scripts/synthesis.py:100-174` (refactor `parse_synthesis_output`)
- Test: `tests/test_synthesis.py`

The new parser must handle the new format while the old parser stays for backwards compatibility during transition. The entry point `parse_synthesis_output` should detect which format is present and dispatch accordingly.

**Step 1: Write failing tests for new data model**

```python
class TestProjectBlock:
    """Test the new ProjectBlock dataclass."""

    def test_basic_creation(self):
        from synthesis import ProjectBlock
        block = ProjectBlock(project="swyfft", entries=[
            "- [implement] Did something",
            "- [LTM][gotcha] Found a bug",
        ])
        assert block.project == "swyfft"
        assert len(block.entries) == 2
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_synthesis.py::TestProjectBlock -v`
Expected: FAIL — `ImportError: cannot import name 'ProjectBlock'`

**Step 3: Add the dataclass**

Add to `scripts/synthesis.py` after `SynthesisResult` (around line 97):

```python
@dataclass
class ProjectBlock:
    """A parsed project block from ===PROJECT:X=== output."""
    project: str
    entries: list[str] = field(default_factory=list)
```

Add `"ProjectBlock"` to `__all__`.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_synthesis.py::TestProjectBlock -v`
Expected: PASS

**Step 5: Write failing tests for new format parsing**

```python
class TestParseProjectFormat:
    """Test parsing the new ===PROJECT:X=== format."""

    def test_single_project(self):
        from synthesis import parse_synthesis_output
        text = """===PROJECT:swyfft===
- [implement] Rewrote SQL
- [gotcha] Tableau mislabeled

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.project_blocks) == 1
        assert result.project_blocks[0].project == "swyfft"
        assert len(result.project_blocks[0].entries) == 2

    def test_multiple_projects(self):
        from synthesis import parse_synthesis_output
        text = """===PROJECT:swyfft===
- [implement] Rewrote SQL

===PROJECT:investing===
- [implement] Started Phase 4

===PROJECT:global===
- [analyze] Benchmarked Python vs TS

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.project_blocks) == 3
        projects = [b.project for b in result.project_blocks]
        assert projects == ["swyfft", "investing", "global"]

    def test_ltm_and_global_flags_preserved(self):
        from synthesis import parse_synthesis_output
        text = """===PROJECT:swyfft===
- [LTM][gotcha] Important bug
- [GLOBAL][pattern] Cross-project pattern
- [LTM][GLOBAL][tip] Global LTM tip

===END==="""
        result = parse_synthesis_output(text)
        entries = result.project_blocks[0].entries
        assert entries[0] == "- [LTM][gotcha] Important bug"
        assert entries[1] == "- [GLOBAL][pattern] Cross-project pattern"
        assert entries[2] == "- [LTM][GLOBAL][tip] Global LTM tip"

    def test_skips_non_entry_lines(self):
        from synthesis import parse_synthesis_output
        text = """===PROJECT:swyfft===
Some preamble text
- [implement] Real entry

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.project_blocks[0].entries) == 1

    def test_empty_project_block_skipped(self):
        from synthesis import parse_synthesis_output
        text = """===PROJECT:swyfft===

===PROJECT:investing===
- [implement] Real entry

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.project_blocks) == 1
        assert result.project_blocks[0].project == "investing"

    def test_missing_end_marker_warns(self):
        from synthesis import parse_synthesis_output
        text = """===PROJECT:swyfft===
- [implement] Did something"""
        result = parse_synthesis_output(text)
        assert len(result.project_blocks) == 1
        assert any("END" in w for w in result.warnings)

    def test_old_daily_format_still_works(self):
        """Backwards compatibility: ===DAILY=== format still parses."""
        from synthesis import parse_synthesis_output
        text = """===DAILY:2026-02-25===
## Actions
- [implement] Did something

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1
        assert result.dailies[0].date == "2026-02-25"
```

**Step 6: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestParseProjectFormat -v`
Expected: FAIL — `AttributeError: 'SynthesisResult' has no attribute 'project_blocks'`

**Step 7: Implement the parser**

1. Add `project_blocks: list[ProjectBlock] = field(default_factory=list)` to `SynthesisResult`.
2. Add regex: `PROJECT_HEADER = re.compile(r"^===PROJECT:([^=]+)===$")`
3. In `parse_synthesis_output`, add handling for `PROJECT_HEADER` alongside existing `DAILY_HEADER` and `ROUTE_HEADER`. The function already loops through lines — add a branch:

```python
# Check for project header
project_match = PROJECT_HEADER.match(line)
if project_match:
    project_name = project_match.group(1).strip()
    entries = []
    i += 1
    while i < len(lines):
        stripped = lines[i].strip()
        if (
            PROJECT_HEADER.match(stripped)
            or DAILY_HEADER.match(stripped)
            or ROUTE_HEADER.match(stripped)
            or stripped == END_MARKER
        ):
            break
        if stripped.startswith("- "):
            entries.append(stripped)
        i += 1
    if entries:
        result.project_blocks.append(
            ProjectBlock(project=project_name, entries=entries)
        )
    continue
```

**Step 8: Run all tests**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: ALL PASS (new + old)

**Step 9: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: parse ===PROJECT:X=== blocks in synthesis output"
```

---

### Task 3: Build Daily Files from Project Blocks

Convert `list[ProjectBlock]` → `list[DailyFile]` using `TYPE_TO_SECTION`. This function takes a date (from extract metadata) and project blocks, and produces daily files with properly scoped, sectioned entries.

**Files:**
- Modify: `scripts/synthesis.py` (new function `build_dailies_from_project_blocks`)
- Test: `tests/test_synthesis.py`

**Step 1: Write failing tests**

```python
class TestBuildDailiesFromProjectBlocks:
    def test_single_project_sections(self):
        from synthesis import ProjectBlock, build_dailies_from_project_blocks
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [implement] Rewrote SQL",
            "- [gotcha] Tableau mislabeled",
            "- [design] Use bind date",
            "- [tip] Rename GWP column",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert len(dailies) == 1
        content = dailies[0].content
        assert dailies[0].date == "2026-02-24"
        assert "## Actions\n- [swyfft/implement] Rewrote SQL" in content
        assert "## Learnings\n- [swyfft/gotcha] Tableau mislabeled" in content
        assert "## Decisions\n- [swyfft/design] Use bind date" in content
        assert "## Lessons\n- [swyfft/tip] Rename GWP column" in content

    def test_global_project_scope(self):
        from synthesis import ProjectBlock, build_dailies_from_project_blocks
        blocks = [ProjectBlock(project="global", entries=[
            "- [analyze] Benchmarked Python vs TS",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert "- [global/analyze] Benchmarked Python vs TS" in dailies[0].content

    def test_global_flag_produces_pipe_scope(self):
        from synthesis import ProjectBlock, build_dailies_from_project_blocks
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [GLOBAL][pattern] Cross-project pattern",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert "- [global|swyfft/pattern] Cross-project pattern" in dailies[0].content

    def test_global_flag_on_global_project_stays_global(self):
        from synthesis import ProjectBlock, build_dailies_from_project_blocks
        blocks = [ProjectBlock(project="global", entries=[
            "- [GLOBAL][tip] Some tip",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert "- [global/tip] Some tip" in dailies[0].content

    def test_ltm_flag_stripped_from_daily(self):
        from synthesis import ProjectBlock, build_dailies_from_project_blocks
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [LTM][gotcha] Important bug",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert "[LTM]" not in dailies[0].content
        assert "- [swyfft/gotcha] Important bug" in dailies[0].content

    def test_multiple_projects_merge_into_one_daily(self):
        from synthesis import ProjectBlock, build_dailies_from_project_blocks
        blocks = [
            ProjectBlock(project="swyfft", entries=["- [implement] Swyfft work"]),
            ProjectBlock(project="investing", entries=["- [implement] Investing work"]),
        ]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        assert len(dailies) == 1
        content = dailies[0].content
        assert "- [swyfft/implement] Swyfft work" in content
        assert "- [investing/implement] Investing work" in content

    def test_unknown_type_goes_to_actions(self):
        from synthesis import ProjectBlock, build_dailies_from_project_blocks
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [investigate] Something new",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        content = dailies[0].content
        assert "## Actions" in content
        assert "- [swyfft/investigate] Something new" in content

    def test_section_order_is_standard(self):
        from synthesis import ProjectBlock, build_dailies_from_project_blocks, SECTION_ORDER
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [tip] A lesson",
            "- [implement] An action",
            "- [gotcha] A learning",
            "- [design] A decision",
        ])]
        dailies = build_dailies_from_project_blocks(blocks, "2026-02-24")
        content = dailies[0].content
        for i, section in enumerate(SECTION_ORDER):
            assert f"## {section}" in content
        # Verify order: Actions before Decisions before Learnings before Lessons
        assert content.index("## Actions") < content.index("## Decisions")
        assert content.index("## Decisions") < content.index("## Learnings")
        assert content.index("## Learnings") < content.index("## Lessons")
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestBuildDailiesFromProjectBlocks -v`
Expected: FAIL — `ImportError: cannot import name 'build_dailies_from_project_blocks'`

**Step 3: Implement**

Add to `scripts/synthesis.py`:

```python
# Regex to parse entry flags: optional [LTM], optional [GLOBAL], required [type]
_ENTRY_FLAGS = re.compile(
    r"^(\s*-\s*)"                # prefix
    r"(?:\[LTM\])?"              # optional [LTM]
    r"(?:\[GLOBAL\])?"           # optional [GLOBAL]
    r"\[([a-zA-Z]+)\]"           # [type]
    r"(\s+.*)$"                  # rest
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

            has_ltm = bool(_LTM_FLAG.search(entry))
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
```

Add `"build_dailies_from_project_blocks"` to `__all__`.

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_synthesis.py::TestBuildDailiesFromProjectBlocks -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: build daily files from project blocks with per-entry scope"
```

---

### Task 4: Extract LTM Routes from Project Blocks

Convert `[LTM]`-flagged entries from `list[ProjectBlock]` → `list[RouteEntry]` so the existing `append_to_ltm()` can consume them unchanged.

**Files:**
- Modify: `scripts/synthesis.py` (new function `extract_routes_from_project_blocks`)
- Test: `tests/test_synthesis.py`

**Step 1: Write failing tests**

```python
class TestExtractRoutesFromProjectBlocks:
    def test_ltm_entries_become_routes(self):
        from synthesis import ProjectBlock, extract_routes_from_project_blocks
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [implement] Normal entry",
            "- [LTM][gotcha] Important bug",
            "- [LTM][tip] Useful command",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        assert len(routes) == 2
        # gotcha → Key Learnings, tip → Key Lessons
        scopes = {(r.scope, r.section) for r in routes}
        assert ("swyfft", "Key Learnings") in scopes
        assert ("swyfft", "Key Lessons") in scopes

    def test_date_prefix_added(self):
        from synthesis import ProjectBlock, extract_routes_from_project_blocks
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [LTM][gotcha] Important bug",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        assert routes[0].entries[0] == "- (2026-02-24) [gotcha] Important bug"

    def test_global_project_routes_to_global(self):
        from synthesis import ProjectBlock, extract_routes_from_project_blocks
        blocks = [ProjectBlock(project="global", entries=[
            "- [LTM][tip] Global tip",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        assert routes[0].scope == "global"

    def test_global_flag_routes_to_both(self):
        from synthesis import ProjectBlock, extract_routes_from_project_blocks
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [LTM][GLOBAL][pattern] Cross-project pattern",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        scopes = {r.scope for r in routes}
        assert "swyfft" in scopes
        assert "global" in scopes

    def test_no_ltm_entries_no_routes(self):
        from synthesis import ProjectBlock, extract_routes_from_project_blocks
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [implement] Normal entry",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        assert routes == []

    def test_routes_grouped_by_scope_and_section(self):
        from synthesis import ProjectBlock, extract_routes_from_project_blocks
        blocks = [ProjectBlock(project="swyfft", entries=[
            "- [LTM][gotcha] Bug one",
            "- [LTM][gotcha] Bug two",
        ])]
        routes = extract_routes_from_project_blocks(blocks, "2026-02-24")
        # Both gotchas grouped into one RouteEntry for swyfft:Key Learnings
        assert len(routes) == 1
        assert len(routes[0].entries) == 2
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestExtractRoutesFromProjectBlocks -v`
Expected: FAIL — `ImportError`

**Step 3: Implement**

```python
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
```

Add `"extract_routes_from_project_blocks"` to `__all__`.

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_synthesis.py::TestExtractRoutesFromProjectBlocks -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: extract LTM routes from inline [LTM] flags in project blocks"
```

---

### Task 5: Worktree Prefix Resolution in `_resolve_project_name`

**Files:**
- Modify: `scripts/transcript_ops.py:29-56` (`_resolve_project_name`)
- Test: `tests/test_transcript_ops.py`

**Step 1: Write failing tests**

```python
class TestResolveProjectNameWorktreePrefix:
    """Test that unindexed worktrees resolve via prefix match."""

    def test_new_worktree_resolves_to_parent(self, tmp_path):
        """ts-phase-4 should resolve to 'investing' when ts-phase-1..3 are indexed."""
        from transcript_ops import _resolve_project_name
        index = {
            "projects": {
                "/home/user/investing": {
                    "name": "investing",
                    "encodedPaths": [
                        "-home-user-investing",
                        "-home-user-investing--worktrees-ts-phase-1",
                        "-home-user-investing--worktrees-ts-phase-3",
                    ],
                }
            }
        }
        with patch("transcript_ops.load_json_file", return_value=index), \
             patch("transcript_ops.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(
                None, project_hash="-home-user-investing--worktrees-ts-phase-4"
            )
        assert result == "investing"

    def test_exact_match_takes_precedence_over_prefix(self, tmp_path):
        """If exact match exists, don't fall through to prefix."""
        from transcript_ops import _resolve_project_name
        index = {
            "projects": {
                "/home/user/investing": {
                    "name": "investing",
                    "encodedPaths": [
                        "-home-user-investing--worktrees-ts-phase-4",
                    ],
                }
            }
        }
        with patch("transcript_ops.load_json_file", return_value=index), \
             patch("transcript_ops.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(
                None, project_hash="-home-user-investing--worktrees-ts-phase-4"
            )
        assert result == "investing"

    def test_no_prefix_match_returns_none(self, tmp_path):
        """Completely unrelated hash returns None."""
        from transcript_ops import _resolve_project_name
        index = {
            "projects": {
                "/home/user/investing": {
                    "name": "investing",
                    "encodedPaths": ["-home-user-investing"],
                }
            }
        }
        with patch("transcript_ops.load_json_file", return_value=index), \
             patch("transcript_ops.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(
                None, project_hash="-home-user-totally-different"
            )
        assert result is None

    def test_worktree_prefix_matches_base_project(self, tmp_path):
        """Worktree hash shares base encoded path prefix with project."""
        from transcript_ops import _resolve_project_name
        index = {
            "projects": {
                "/home/user/myproject": {
                    "name": "myproject",
                    "encodedPaths": ["-home-user-myproject"],
                }
            }
        }
        with patch("transcript_ops.load_json_file", return_value=index), \
             patch("transcript_ops.get_projects_index_file", return_value=tmp_path / "idx.json"):
            result = _resolve_project_name(
                None, project_hash="-home-user-myproject--worktrees-feature-x"
            )
        assert result == "myproject"
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_transcript_ops.py::TestResolveProjectNameWorktreePrefix -v`
Expected: FAIL — last two tests fail (new worktree and base prefix don't match)

**Step 3: Implement**

In `scripts/transcript_ops.py`, modify `_resolve_project_name` to add a prefix fallback after the exact match loop (around line 53):

```python
def _resolve_project_name(
    project_path: str | None,
    project_hash: str | None = None,
) -> str | None:
    """Resolve a session's project_path to a project name via projects-index.

    Falls back to matching project_hash against encodedPaths when
    project_path is unavailable. If no exact match, tries prefix matching
    for unindexed worktrees (e.g., ts-phase-4 when ts-phase-3 is indexed).
    """
    if not project_path and not project_hash:
        return None
    try:
        from memory_utils import get_projects_index_file, load_json_file
        index = load_json_file(get_projects_index_file(), {})
        projects = index.get("projects", {})
        # Primary: direct path lookup
        if project_path:
            data = projects.get(project_path)
            if data and data.get("name"):
                return data["name"]
        # Fallback 1: exact match on encoded folder name
        if project_hash:
            for _path, data in projects.items():
                if project_hash in data.get("encodedPaths", []):
                    return data.get("name")
            # Fallback 2: prefix match for unindexed worktrees
            # e.g., -home-user-project--worktrees-new-branch matches
            #        -home-user-project or -home-user-project--worktrees-old-branch
            for _path, data in projects.items():
                for ep in data.get("encodedPaths", []):
                    # Check if unknown hash starts with known base path
                    # (base--worktrees- prefix or base itself)
                    if (
                        project_hash.startswith(ep + "--worktrees-")
                        or ep.startswith(project_hash.rsplit("--worktrees-", 1)[0] + "--worktrees-")
                    ):
                        return data.get("name")
    except Exception:
        pass
    return None
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_transcript_ops.py::TestResolveProjectNameWorktreePrefix -v`
Expected: ALL PASS

Also run existing tests: `python3 -m pytest tests/test_transcript_ops.py::TestResolveProjectName -v`
Expected: ALL PASS (no regressions)

**Step 5: Commit**

```bash
git add scripts/transcript_ops.py tests/test_transcript_ops.py
git commit -m "fix: resolve unindexed worktrees via prefix match in _resolve_project_name"
```

---

### Task 6: Wire New Pipeline into `apply_results`

Update `apply_results()` to use the new project-block path when project blocks are present, falling back to the old daily/route path for backwards compatibility.

**Files:**
- Modify: `scripts/synthesis.py:737-788` (`apply_results`)
- Test: `tests/test_synthesis.py`

**Step 1: Write failing integration test**

```python
class TestApplyResultsProjectBlocks:
    """Integration test: apply_results with new ===PROJECT=== format."""

    def test_project_blocks_produce_scoped_daily(self, tmp_path):
        from synthesis import apply_results
        output_file = tmp_path / "output.txt"
        output_file.write_text("""===PROJECT:swyfft===
- [implement] Rewrote SQL
- [LTM][gotcha] Tableau mislabeled
- [design] Use bind date

===PROJECT:global===
- [analyze] Benchmarked Python vs TS

===END===
""")
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        # Need to patch paths and provide extract for date detection
        extract_file = tmp_path / "extract-2026-02-24.txt"
        extract_file.write_text("Session: abc123 [project: swyfft]\n")

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "global-ltm.md"), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.compute_offsets_from_extracts", return_value={}), \
             patch("synthesis.update_synthesis_state"), \
             patch("synthesis.run_post_processing"):
            apply_results(str(output_file), [str(extract_file)])

        daily_file = daily_dir / "2026-02-24.md"
        assert daily_file.exists()
        content = daily_file.read_text()
        assert "[swyfft/implement] Rewrote SQL" in content
        assert "[swyfft/gotcha] Tableau mislabeled" in content
        assert "[swyfft/design] Use bind date" in content
        assert "[global/analyze] Benchmarked Python vs TS" in content

    def test_old_format_still_works(self, tmp_path):
        """Backwards compatibility: ===DAILY=== format still processes."""
        from synthesis import apply_results
        output_file = tmp_path / "output.txt"
        output_file.write_text("""===DAILY:2026-02-24===
# 2026-02-24
## Actions
- [swyfft/implement] Did something

===END===
""")
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        with patch("synthesis.get_daily_dir", return_value=daily_dir), \
             patch("synthesis.get_global_memory_file", return_value=tmp_path / "global-ltm.md"), \
             patch("synthesis.get_project_memory_dir", return_value=tmp_path / "project-memory"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.compute_offsets_from_extracts", return_value={}), \
             patch("synthesis.update_synthesis_state"), \
             patch("synthesis.run_post_processing"):
            apply_results(str(output_file), [str(extract_file)])

        content = (daily_dir / "2026-02-24.md").read_text()
        assert "[swyfft/implement] Did something" in content
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestApplyResultsProjectBlocks -v`
Expected: FAIL — project blocks not wired into apply_results yet

**Step 3: Implement**

Modify `apply_results()` in `scripts/synthesis.py`:

```python
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
        # Determine date from extract files
        date = _extract_date_from_extracts(extract_paths)

        dailies = build_dailies_from_project_blocks(result.project_blocks, date)
        routes = extract_routes_from_project_blocks(result.project_blocks, date)

        # Mark routed entries in dailies
        marked_dailies = mark_routed_entries(dailies, routes)
        written = write_daily_files(marked_dailies)

    elif result.dailies:
        # Old format: ===DAILY:date=== blocks (backwards compat)
        marked_dailies = mark_routed_entries(result.dailies, result.routes)
        session_projects = _extract_session_projects(extract_paths)
        scoped_dailies = inject_scopes(marked_dailies, session_projects)
        written = write_daily_files(scoped_dailies)
        routes = result.routes
    else:
        print("No daily or project blocks found. Synthesis may have failed.",
              file=sys.stderr)
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

    # Update synthesis state
    if offsets_json:
        try:
            offsets = json.loads(Path(offsets_json).read_text(encoding="utf-8"))
            update_synthesis_state(offsets)
        except (IOError, json.JSONDecodeError) as e:
            print(f"Warning: Could not update synthesis state: {e}", file=sys.stderr)
    else:
        offsets = compute_offsets_from_extracts(extract_paths)
        if offsets:
            update_synthesis_state(offsets)

    run_post_processing(extract_paths, offsets_json=offsets_json)
    print("Post-processing complete")
```

Also add helper to get date from extract filenames:

```python
def _extract_date_from_extracts(extract_paths: list[str]) -> str:
    """Extract date from extract file names (format: *YYYY-MM-DD*)."""
    for path in extract_paths:
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", Path(path).name)
        if date_match:
            return date_match.group(1)
    # Fallback: today
    from datetime import date
    return date.today().isoformat()
```

**Step 4: Run all synthesis tests**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: wire project blocks into apply_results with old-format fallback"
```

---

### Task 7: Update LLM Prompt

Update `_build_synthesis_instructions` and `_build_preextracted_prompt` in `load_memory.py` to output the new `===PROJECT:X===` format.

**Files:**
- Modify: `scripts/load_memory.py:265-451` (prompt builder functions)
- Test: `tests/test_load_memory.py`

**Step 1: Write failing tests**

```python
class TestSynthesisPromptProjectFormat:
    def test_instructions_mention_project_blocks(self):
        from load_memory import _build_synthesis_instructions
        instructions = _build_synthesis_instructions("`swyfft`, `investing`")
        assert "===PROJECT:" in instructions
        assert "[LTM]" in instructions
        assert "===DAILY:" not in instructions
        assert "===ROUTE:" not in instructions

    def test_example_shows_project_format(self):
        from load_memory import _build_synthesis_instructions
        instructions = _build_synthesis_instructions("`swyfft`")
        assert "===PROJECT:" in instructions
        assert "===END===" in instructions

    def test_preextracted_prompt_uses_project_format(self, tmp_path):
        from load_memory import _build_preextracted_prompt
        instructions = "test instructions"
        prompt = _build_preextracted_prompt(
            ["2026-02-24"],
            {"2026-02-24": str(tmp_path / "extract.txt")},
            instructions,
            {"transcripts": {"2026-02-24": "session content"},
             "global_ltm": "", "project_ltms": {}},
        )
        assert "===PROJECT:" in prompt
        assert "===DAILY:" not in prompt or "===PROJECT:" in prompt
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisPromptProjectFormat -v`
Expected: FAIL

**Step 3: Update the prompt**

Rewrite `_build_synthesis_instructions()` to describe the new format:
- Replace the `===DAILY:` example with `===PROJECT:X===` example
- Replace `===ROUTE:` section with `[LTM]` inline flag description
- Remove section headers instruction (## Actions etc.) — Python handles this
- Keep: entry type list, `[GLOBAL]` description, compactness, routing selectivity guidance, granularity cap

Rewrite the example in `_build_preextracted_prompt()`:
- Change from `===DAILY:2026-02-20===` with sections to `===PROJECT:X===` with flat entries
- Remove `===ROUTE:` example

Update the reminder line at the end:
- Change "Start with ===DAILY:{first_date}===" to "Start with ===PROJECT:..."

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_load_memory.py -v`
Expected: ALL PASS (including existing prompt tests — update any that assert old format)

**Step 5: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "feat: update synthesis prompt for ===PROJECT:X=== output format"
```

---

### Task 8: Remove Dead Code

Remove old functions and regexes that are no longer used with the new format. Keep them if the old-format fallback path in `apply_results` still references them.

**Files:**
- Modify: `scripts/synthesis.py`
- Test: `tests/test_synthesis.py`

**Step 1: Identify dead code**

The old-format fallback in `apply_results` still uses:
- `inject_scopes`, `_extract_session_projects`, `_inject_route_scopes` — keep (backwards compat)
- `_UNSCOPED_ENTRY`, `_SCOPED_ENTRY`, `_GLOBAL_MARKER` — keep (used by `inject_scopes`)
- `_PROJECT_HEADER` (line 366) — keep (used by `_extract_session_projects`)
- `ROUTE_HEADER` — keep (used by parser for old format)

Nothing to remove yet — all old code is still reachable via the fallback path. Mark with `# Legacy: old ===DAILY=== format support` comments for future cleanup.

**Step 2: Clean up `__all__`**

Add new exports, verify all listed names exist.

**Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: ALL PASS

**Step 4: Commit**

```bash
git add scripts/synthesis.py
git commit -m "chore: annotate legacy format code, update __all__ exports"
```

---

### Task 9: End-to-End Verification

Run the full pipeline manually to verify everything works together.

**Step 1: Run full test suite**

```bash
python3 -m pytest tests/ -q
```
Expected: ALL PASS

**Step 2: Reinstall**

```bash
python3 install.py
```

**Step 3: Test synthesis prompt generation**

```bash
python3 ~/.claude/scripts/load_memory.py --synthesis-prompt
```
Verify: prompt file contains `===PROJECT:` examples, not `===DAILY:` examples.

**Step 4: Manual synthesis dry run**

Run `/synthesize` and verify:
- LLM output uses `===PROJECT:X===` format
- Daily file has correctly scoped entries per project
- LTM routes go to correct project files
- No scope misattribution

**Step 5: Commit any fixes, then tag**

```bash
git add -A && git commit -m "fix: adjustments from end-to-end verification"
```
