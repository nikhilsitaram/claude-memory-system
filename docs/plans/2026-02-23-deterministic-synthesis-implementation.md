# Deterministic Synthesis Pipeline Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move merge, scope tagging, and dedup from LLM judgment to deterministic code in the synthesis pipeline.

**Architecture:** Three structural changes applied in the `synthesis.py` apply phase: (1) parse daily sections and merge programmatically, (2) inject scope tags from session CWD metadata, (3) replace substring dedup with keyword-overlap `is_routed_match(0.6)`. The LLM prompt simplifies to outputting only types and an optional `[GLOBAL]` marker — code handles all scope construction, merging, and dedup.

**Tech Stack:** Python 3.9+, pytest, existing `memory_utils.py` functions

**Design doc:** `docs/plans/2026-02-23-deterministic-synthesis-design.md`

---

### Task 1: Multi-scope filter in `memory_utils.py`

Update `filter_daily_content()` to handle pipe-delimited multi-scope tags like `[global|cartwheel/gotcha]`.

**Files:**
- Modify: `scripts/memory_utils.py:500` (TAG_PATTERN), `scripts/memory_utils.py:554-561` (scope matching in filter_daily_content)
- Test: `tests/test_memory_utils.py`

**Step 1: Write failing tests**

Add to `tests/test_memory_utils.py` in the `TestFilterDailyContent` class (or create if absent):

```python
class TestFilterDailyContentMultiScope:
    """Tests for pipe-delimited multi-scope tag filtering."""

    def test_single_scope_unchanged(self):
        """Existing single-scope tags still work."""
        content = "# 2026-02-23\n## Actions\n- [cartwheel/implement] Built OAuth\n"
        result = filter_daily_content(content, "cartwheel")
        assert "[cartwheel/implement] Built OAuth" in result

    def test_multi_scope_matches_first(self):
        """Multi-scope entry matches on first scope."""
        content = "# 2026-02-23\n## Learnings\n- [global|cartwheel/gotcha] MTU issue\n"
        result = filter_daily_content(content, "global")
        assert "MTU issue" in result

    def test_multi_scope_matches_second(self):
        """Multi-scope entry matches on second scope."""
        content = "# 2026-02-23\n## Learnings\n- [global|cartwheel/gotcha] MTU issue\n"
        result = filter_daily_content(content, "cartwheel")
        assert "MTU issue" in result

    def test_multi_scope_no_match(self):
        """Multi-scope entry doesn't match unrelated scope."""
        content = "# 2026-02-23\n## Learnings\n- [global|cartwheel/gotcha] MTU issue\n"
        result = filter_daily_content(content, "investing")
        assert result == ""

    def test_mixed_single_and_multi_scope(self):
        """File with both single and multi-scope entries filters correctly."""
        content = (
            "# 2026-02-23\n## Actions\n"
            "- [cartwheel/implement] OAuth flow\n"
            "- [global|cartwheel/implement] CI pipeline\n"
            "- [global/implement] Git hooks\n"
        )
        result = filter_daily_content(content, "cartwheel")
        assert "OAuth flow" in result
        assert "CI pipeline" in result
        assert "Git hooks" not in result
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_memory_utils.py::TestFilterDailyContentMultiScope -v`
Expected: `test_multi_scope_matches_first` and `test_multi_scope_matches_second` FAIL (current regex captures only the first scope token before `|`)

**Step 3: Update TAG_PATTERN and filter logic**

In `scripts/memory_utils.py`:

1. Update TAG_PATTERN at line 500:
```python
# Regex to extract scope(s) from tagged entries: [scope/type] or [scope1|scope2/type]
TAG_PATTERN = re.compile(r"^\s*-\s*\[([^\]/]+(?:\|[^\]/]+)*)(?:/[^\]]+)?\]")
```

2. Update `filter_daily_content()` at line 554-561 — change scope matching from single to multi:
```python
            # Check if this is a tagged entry
            match = TAG_PATTERN.match(line)
            if match:
                entry_scopes = [s.lower() for s in match.group(1).split("|")]
                # Include if any scope matches (case-insensitive)
                if scope.lower() in entry_scopes:
                    section_lines.append(line)
                    section_has_content = True
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memory_utils.py::TestFilterDailyContentMultiScope -v`
Expected: All 5 PASS

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All existing tests still pass (backwards-compatible regex)

**Step 6: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "feat: support pipe-delimited multi-scope tags in filter_daily_content"
```

---

### Task 2: `parse_daily_sections()` helper in `synthesis.py`

Add a helper that parses a daily markdown file into a structured dict of `{section_name: [entries]}`. Used by both `merge_daily_sections()` (Task 3) and scope injection (Task 5).

**Files:**
- Modify: `scripts/synthesis.py` (add function after `_extract_description` ~line 173)
- Test: `tests/test_synthesis.py`

**Step 1: Write failing tests**

Add to `tests/test_synthesis.py`:

```python
from synthesis import parse_daily_sections


class TestParseDailySections:
    def test_all_four_sections(self):
        content = (
            "# 2026-02-23\n"
            "## Actions\n"
            "- [impl] Did A\n"
            "- [impl] Did B\n"
            "## Decisions\n"
            "- [design] Chose X\n"
            "## Learnings\n"
            "- [gotcha] Found bug\n"
            "## Lessons\n"
            "- [tip] Use Y\n"
        )
        result = parse_daily_sections(content)
        assert result["date"] == "2026-02-23"
        assert result["Actions"] == ["- [impl] Did A", "- [impl] Did B"]
        assert result["Decisions"] == ["- [design] Chose X"]
        assert result["Learnings"] == ["- [gotcha] Found bug"]
        assert result["Lessons"] == ["- [tip] Use Y"]

    def test_missing_sections_are_empty_lists(self):
        content = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        result = parse_daily_sections(content)
        assert result["Actions"] == ["- [impl] Did A"]
        assert result["Decisions"] == []
        assert result["Learnings"] == []
        assert result["Lessons"] == []

    def test_preserves_routed_prefix(self):
        content = "# 2026-02-23\n## Actions\n- [routed][proj/impl] Old entry\n- [proj/impl] New entry\n"
        result = parse_daily_sections(content)
        assert len(result["Actions"]) == 2
        assert "[routed]" in result["Actions"][0]

    def test_skips_html_comments(self):
        content = "# 2026-02-23\n## Actions\n<!-- template hint -->\n- [impl] Did A\n"
        result = parse_daily_sections(content)
        assert len(result["Actions"]) == 1

    def test_empty_content(self):
        result = parse_daily_sections("")
        assert result["date"] == ""
        assert all(result[s] == [] for s in ["Actions", "Decisions", "Learnings", "Lessons"])

    def test_no_date_header(self):
        content = "## Actions\n- [impl] Did A\n"
        result = parse_daily_sections(content)
        assert result["date"] == ""
        assert result["Actions"] == ["- [impl] Did A"]
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestParseDailySections -v`
Expected: ImportError — `parse_daily_sections` doesn't exist yet

**Step 3: Implement `parse_daily_sections()`**

Add to `scripts/synthesis.py` after `_extract_description()` (~line 174):

```python
SECTION_ORDER = ["Actions", "Decisions", "Learnings", "Lessons"]


def parse_daily_sections(content: str) -> dict:
    """Parse a daily markdown file into structured sections.

    Returns dict with "date" (str) and section names mapping to entry lists.
    Skips HTML comments and blank lines. Preserves [routed] prefixes.
    """
    result: dict = {"date": ""}
    for s in SECTION_ORDER:
        result[s] = []

    if not content.strip():
        return result

    current_section = None
    for line in content.split("\n"):
        # Date header
        if line.startswith("# ") and not line.startswith("## "):
            date_match = re.match(r"^# (\d{4}-\d{2}-\d{2})", line)
            if date_match:
                result["date"] = date_match.group(1)
            continue

        # Section header
        if line.startswith("## "):
            section_name = line[3:].strip()
            if section_name in SECTION_ORDER:
                current_section = section_name
            else:
                current_section = None
            continue

        if current_section is None:
            continue

        # Skip HTML comments
        if re.match(r"^\s*<!--.*-->\s*$", line):
            continue

        # Skip blank lines
        if not line.strip():
            continue

        # Entry line (starts with -)
        if line.strip().startswith("-"):
            result[current_section].append(line)

    return result
```

Also add to `__all__`: `"parse_daily_sections"` and `"SECTION_ORDER"`.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis.py::TestParseDailySections -v`
Expected: All 6 PASS

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: add parse_daily_sections helper for structured daily file parsing"
```

---

### Task 3: `merge_daily_sections()` in `synthesis.py`

Programmatic merge: takes existing daily content + new LLM output, returns merged content with dedup.

**Files:**
- Modify: `scripts/synthesis.py` (add function after `parse_daily_sections`)
- Test: `tests/test_synthesis.py`

**Step 1: Write failing tests**

```python
from synthesis import merge_daily_sections


class TestMergeDailySections:
    def test_no_existing_returns_new(self):
        new = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        result = merge_daily_sections("", new)
        assert "- [impl] Did A" in result

    def test_appends_new_entries_to_existing(self):
        existing = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        new = "# 2026-02-23\n## Actions\n- [impl] Did B\n"
        result = merge_daily_sections(existing, new)
        assert "- [impl] Did A" in result
        assert "- [impl] Did B" in result

    def test_dedup_rejects_near_duplicate(self):
        existing = "# 2026-02-23\n## Learnings\n- [gotcha] Tailscale MTU black hole drops packets\n"
        new = "# 2026-02-23\n## Learnings\n- [gotcha] Tailscale MTU black hole silently drops packets\n"
        result = merge_daily_sections(existing, new)
        # Should only have one entry (near-duplicate rejected)
        assert result.count("Tailscale MTU") == 1

    def test_preserves_existing_when_new_is_empty(self):
        existing = "# 2026-02-23\n## Actions\n- [impl] Did A\n## Lessons\n- [tip] Use X\n"
        new = "# 2026-02-23\n"
        result = merge_daily_sections(existing, new)
        assert "- [impl] Did A" in result
        assert "- [tip] Use X" in result

    def test_new_section_not_in_existing(self):
        existing = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        new = "# 2026-02-23\n## Lessons\n- [tip] Use Y\n"
        result = merge_daily_sections(existing, new)
        assert "- [impl] Did A" in result
        assert "- [tip] Use Y" in result

    def test_preserves_section_order(self):
        existing = "# 2026-02-23\n## Lessons\n- [tip] Use X\n"
        new = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        result = merge_daily_sections(existing, new)
        actions_pos = result.index("## Actions")
        lessons_pos = result.index("## Lessons")
        assert actions_pos < lessons_pos

    def test_preserves_routed_entries(self):
        existing = "# 2026-02-23\n## Actions\n- [routed][proj/impl] Old entry\n"
        new = "# 2026-02-23\n## Actions\n- [impl] New entry\n"
        result = merge_daily_sections(existing, new)
        assert "[routed]" in result
        assert "New entry" in result

    def test_uses_existing_date_header(self):
        existing = "# 2026-02-23\n## Actions\n- [impl] Did A\n"
        new = "## Actions\n- [impl] Did B\n"
        result = merge_daily_sections(existing, new)
        assert result.startswith("# 2026-02-23")
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestMergeDailySections -v`
Expected: ImportError — `merge_daily_sections` doesn't exist yet

**Step 3: Implement `merge_daily_sections()`**

Add to `scripts/synthesis.py` after `parse_daily_sections()`:

```python
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
    from memory_utils import is_routed_match

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
        for entry in new_entries:
            # Reject near-duplicates
            if any(is_routed_match(entry, ex, threshold=0.6) for ex in existing_entries):
                continue
            merged[section].append(entry)

    # Reassemble
    lines = []
    if date:
        lines.append(f"# {date}")
    for section in SECTION_ORDER:
        entries = merged[section]
        if entries:
            lines.append(f"## {section}")
            lines.extend(entries)
    return "\n".join(lines)
```

Also add to `__all__`: `"merge_daily_sections"`.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis.py::TestMergeDailySections -v`
Expected: All 8 PASS

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: add merge_daily_sections for programmatic daily file merging"
```

---

### Task 4: Integrate merge into `write_daily_files()`

Wire `merge_daily_sections()` into the existing write pipeline so second+ synthesis runs on the same day merge instead of overwrite.

**Files:**
- Modify: `scripts/synthesis.py:219-232` (write_daily_files)
- Test: `tests/test_synthesis.py`

**Step 1: Write failing test**

```python
class TestWriteDailyFilesMerge:
    def test_first_write_creates_file(self, tmp_path):
        dailies = [DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [impl] Did A")]
        written = write_daily_files(dailies, daily_dir=tmp_path)
        assert len(written) == 1
        assert "- [impl] Did A" in Path(written[0]).read_text()

    def test_second_write_merges_not_overwrites(self, tmp_path):
        # First write
        first = [DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [impl] Did A")]
        write_daily_files(first, daily_dir=tmp_path)

        # Second write with different entries
        second = [DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [impl] Did B")]
        write_daily_files(second, daily_dir=tmp_path)

        content = (tmp_path / "2026-02-23.md").read_text()
        assert "- [impl] Did A" in content
        assert "- [impl] Did B" in content

    def test_second_write_deduplicates(self, tmp_path):
        first = [DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [impl] Did A")]
        write_daily_files(first, daily_dir=tmp_path)

        # Same entry again
        second = [DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [impl] Did A")]
        write_daily_files(second, daily_dir=tmp_path)

        content = (tmp_path / "2026-02-23.md").read_text()
        assert content.count("- [impl] Did A") == 1
```

Need to add import for `Path` at top of test file if not present:
```python
from pathlib import Path
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestWriteDailyFilesMerge -v`
Expected: `test_second_write_merges_not_overwrites` FAILS (current code overwrites)

**Step 3: Update `write_daily_files()`**

Modify `scripts/synthesis.py:219-232`:

```python
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
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis.py::TestWriteDailyFilesMerge -v`
Expected: All 3 PASS

**Step 5: Run full synthesis tests**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: All existing tests still pass

**Step 6: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: write_daily_files merges with existing instead of overwriting"
```

---

### Task 5: Scope injection in `synthesis.py` apply phase

Add `inject_scopes()` that transforms LLM's `[type]` / `[GLOBAL][type]` entries into `[project/type]` / `[global|project/type]` using session CWD metadata.

**Files:**
- Modify: `scripts/synthesis.py` (add function, modify `apply_results`)
- Modify: `scripts/transcript_ops.py:325-334` (inject project name into session headers)
- Test: `tests/test_synthesis.py`
- Test: `tests/test_transcript_ops.py`

**Step 5a: Add project name to session headers in `transcript_ops.py`**

**Write failing test:**

```python
# In tests/test_transcript_ops.py
class TestFormatTranscriptsIncrementalProjectHeader:
    def test_session_header_includes_project_name(self):
        daily_data = {
            "2026-02-23": [{
                "session_id": "abc123",
                "message_count": 1,
                "messages": [{"role": "assistant", "content": "hello"}],
                "mode": "full",
                "project_name": "cartwheel",
            }]
        }
        result = format_transcripts_incremental(daily_data)
        assert "[project: cartwheel]" in result

    def test_session_header_global_when_no_project(self):
        daily_data = {
            "2026-02-23": [{
                "session_id": "abc123",
                "message_count": 1,
                "messages": [{"role": "assistant", "content": "hello"}],
                "mode": "full",
                "project_name": None,
            }]
        }
        result = format_transcripts_incremental(daily_data)
        assert "[project: global]" in result
```

Run: `python3 -m pytest tests/test_transcript_ops.py::TestFormatTranscriptsIncrementalProjectHeader -v`
Expected: FAIL (no `[project: ...]` in output)

**Implement in `transcript_ops.py:325-334`:**

Change the session header formatting in `format_transcripts_incremental()`:

```python
        for session in sessions:
            output.append(f"\n{'─'*70}")
            mode = session.get("mode", "full")
            project_name = session.get("project_name") or "global"
            header = f"Session: {session['session_id']} [project: {project_name}]"
            if mode == "delta":
                header += " (continued — new messages only)"
            output.append(header)
            output.append(f"{'─'*70}")
```

Run: `python3 -m pytest tests/test_transcript_ops.py::TestFormatTranscriptsIncrementalProjectHeader -v`
Expected: PASS

**Step 5b: Resolve project_path → project_name during extraction**

Add project name resolution in `extract_transcripts_incremental()` (transcript_ops.py:226-237).

After building each session dict, resolve project_path → project_name:

In `transcript_ops.py`, add a helper at the top of the file (after imports):

```python
def _resolve_project_name(project_path: str | None) -> str | None:
    """Resolve a session's project_path to a project name via projects-index."""
    if not project_path:
        return None
    try:
        from memory_utils import get_projects_index_file, load_json_file
        index = load_json_file(get_projects_index_file(), {})
        projects = index.get("projects", {})
        data = projects.get(project_path)
        if data and data.get("name"):
            return data["name"]
    except Exception:
        pass
    return None
```

Then in `extract_transcripts_incremental()`, add `project_name` to the session dict (line ~228-237):

```python
        if messages:
            day = get_session_date(session)
            daily_data[day].append({
                "session_id": sid,
                "filepath": str(session.transcript_path),
                "project_path": session.project_path,
                "project_name": _resolve_project_name(session.project_path),
                "message_count": len(messages),
                "messages": messages,
                "mode": mode,
                "current_offset": current_size,
                "current_lines": total_lines,
            })
```

**Step 5c: `inject_scopes()` in `synthesis.py`**

**Write failing tests:**

```python
from synthesis import inject_scopes


class TestInjectScopes:
    def test_type_only_gets_project_scope(self):
        """[type] becomes [project/type] when session has project."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Actions\n- [implement] Built OAuth\n"
        )
        session_projects = {"2026-02-23": "cartwheel"}
        result = inject_scopes([daily], session_projects)
        assert "- [cartwheel/implement] Built OAuth" in result[0].content

    def test_global_marker_creates_dual_scope(self):
        """[GLOBAL][type] becomes [global|project/type]."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Learnings\n- [GLOBAL][gotcha] MTU issue\n"
        )
        session_projects = {"2026-02-23": "cartwheel"}
        result = inject_scopes([daily], session_projects)
        assert "- [global|cartwheel/gotcha] MTU issue" in result[0].content

    def test_no_project_defaults_to_global(self):
        """[type] becomes [global/type] when no project match."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Lessons\n- [tip] Use stash\n"
        )
        session_projects = {"2026-02-23": None}
        result = inject_scopes([daily], session_projects)
        assert "- [global/tip] Use stash" in result[0].content

    def test_global_marker_no_project_stays_global(self):
        """[GLOBAL][type] with no project becomes [global/type] (not [global|global/type])."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Lessons\n- [GLOBAL][tip] Use stash\n"
        )
        session_projects = {"2026-02-23": None}
        result = inject_scopes([daily], session_projects)
        assert "- [global/tip] Use stash" in result[0].content

    def test_already_scoped_entries_unchanged(self):
        """Entries with existing [scope/type] format pass through unchanged."""
        daily = DailyFile(
            date="2026-02-23",
            content="# 2026-02-23\n## Actions\n- [cartwheel/implement] Built OAuth\n"
        )
        session_projects = {"2026-02-23": "cartwheel"}
        result = inject_scopes([daily], session_projects)
        assert "- [cartwheel/implement] Built OAuth" in result[0].content

    def test_multiple_sessions_different_projects(self):
        """Different dates can have different projects."""
        dailies = [
            DailyFile(date="2026-02-22", content="# 2026-02-22\n## Actions\n- [implement] Did A\n"),
            DailyFile(date="2026-02-23", content="# 2026-02-23\n## Actions\n- [implement] Did B\n"),
        ]
        session_projects = {"2026-02-22": "cartwheel", "2026-02-23": "investing"}
        result = inject_scopes(dailies, session_projects)
        assert "- [cartwheel/implement] Did A" in result[0].content
        assert "- [investing/implement] Did B" in result[1].content
```

Run: `python3 -m pytest tests/test_synthesis.py::TestInjectScopes -v`
Expected: ImportError

**Implement `inject_scopes()`:**

Add to `scripts/synthesis.py`:

```python
# Pattern to detect LLM's simplified output: - [type] or - [GLOBAL][type]
_UNSCOPED_ENTRY = re.compile(
    r"^(\s*-\s*)"           # prefix: "- "
    r"(?:\[GLOBAL\])?"      # optional [GLOBAL] marker
    r"\[([a-z]+)\]"         # [type] (lowercase type name)
    r"(\s+.*)$"             # rest of entry
)
_GLOBAL_MARKER = re.compile(r"^\s*-\s*\[GLOBAL\]")
# Pattern to detect already-scoped entries: - [scope/type] or - [scope|scope/type]
_SCOPED_ENTRY = re.compile(r"^\s*-\s*(?:\[routed\])?\s*\[[^\]]+/[^\]]+\]")


def inject_scopes(
    dailies: list[DailyFile],
    session_projects: dict[str, str | None],
) -> list[DailyFile]:
    """Inject project scope tags into daily entries based on session metadata.

    Transforms LLM's simplified output:
    - [type] Description       → [project/type] Description
    - [GLOBAL][type] Desc      → [global|project/type] Desc (or [global/type] if no project)

    Args:
        dailies: Parsed daily files from LLM output
        session_projects: Dict mapping date → project name (None = global)

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
```

Add to `__all__`: `"inject_scopes"`.

Run: `python3 -m pytest tests/test_synthesis.py::TestInjectScopes -v`
Expected: All 6 PASS

**Step 5d: Build `session_projects` mapping in `apply_results()`**

This requires `apply_results()` to know which projects were active on which dates. Two options:
1. Parse project names from the transcript extract files (session headers now include `[project: name]`)
2. Pass it as a new CLI argument

Option 1 is simpler — parse from the extract files that are already passed as `--extracts`.

Add helper:

```python
_PROJECT_HEADER = re.compile(r"Session:\s+\S+\s+\[project:\s+([^\]]+)\]")


def _extract_session_projects(extract_paths: list[str]) -> dict[str, str | None]:
    """Extract date → project mapping from transcript extract files.

    Reads session headers like 'Session: abc123 [project: cartwheel]' from
    extract files. If multiple projects appear for the same date, uses the
    most common one. Returns None for dates with only 'global' sessions.
    """
    from collections import Counter

    date_projects: dict[str, Counter] = {}
    for path in extract_paths:
        try:
            content = Path(path).read_text(encoding="utf-8")
        except IOError:
            continue
        # Extract date from filename: memory-extract-YYYY-MM-DD-*.txt
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
            # Filter out "global" — use most common real project
            real = {k: v for k, v in counter.items() if k != "global"}
            if real:
                result[date] = max(real, key=lambda k: real[k])
            else:
                result[date] = None
    return result
```

**Step 5e: Wire into `apply_results()`**

Modify `apply_results()` (synthesis.py:402-443):

After `marked_dailies = mark_routed_entries(result.dailies, result.routes)` (line 419), add scope injection:

```python
    # Inject scope tags from session metadata
    session_projects = _extract_session_projects(extract_paths)
    scoped_dailies = inject_scopes(marked_dailies, session_projects)

    # Also inject scopes into route entries
    scoped_routes = _inject_route_scopes(result.routes, session_projects)

    # Write daily files (merges with existing if present)
    written = write_daily_files(scoped_dailies)
```

And update the LTM append call to use scoped routes:
```python
    ltm_warnings = append_to_ltm(scoped_routes)
```

Add `_inject_route_scopes()` helper:

```python
def _inject_route_scopes(
    routes: list[RouteEntry],
    session_projects: dict[str, str | None],
) -> list[RouteEntry]:
    """Inject project scope into route entries if scope is a bare type.

    Routes already have scope from LLM output (e.g., 'global' or project name).
    This is a pass-through for now — route scopes come from the daily entry tags
    which are already scoped by inject_scopes(). Future: validate route scopes
    match session metadata.
    """
    # Routes reference ===ROUTE:scope:section=== — scope is set by LLM.
    # After inject_scopes(), daily entries have correct tags.
    # Route scope should match the daily entry scope.
    # For now, pass through — the LLM still sets route scope.
    return routes
```

**Step 5f: Commit**

```bash
git add scripts/synthesis.py scripts/transcript_ops.py tests/test_synthesis.py tests/test_transcript_ops.py
git commit -m "feat: inject project scopes from session CWD metadata into synthesis output"
```

---

### Task 6: Keyword-overlap dedup in `append_to_ltm()`

Replace substring matching with `is_routed_match(0.6)` and add hard route cap.

**Files:**
- Modify: `scripts/synthesis.py:283-319` (append_to_ltm dedup logic)
- Test: `tests/test_synthesis.py`

**Step 1: Write failing tests**

```python
class TestAppendToLtmKeywordDedup:
    def test_rejects_near_duplicate_by_keyword_overlap(self, tmp_path):
        """Near-duplicate with different wording is rejected."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n## Key Learnings\n"
            "<!-- gotchas -->\n"
            "- (2026-02-20) [gotcha] Tailscale MTU black hole drops packets silently\n"
        )
        routes = [RouteEntry(
            scope="global",
            section="Key Learnings",
            entries=["- (2026-02-23) [gotcha] Tailscale MTU black hole silently drops packets"],
        )]
        warnings = append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        assert content.count("Tailscale MTU") == 1  # Not added

    def test_allows_genuinely_different_entry(self, tmp_path):
        """Different entry passes dedup check."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n## Key Learnings\n"
            "<!-- gotchas -->\n"
            "- (2026-02-20) [gotcha] Tailscale MTU black hole\n"
        )
        routes = [RouteEntry(
            scope="global",
            section="Key Learnings",
            entries=["- (2026-02-23) [pattern] pytest conftest.py shared fixtures"],
        )]
        warnings = append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        assert "pytest conftest" in content

    def test_route_cap_enforced(self, tmp_path):
        """Max 5 entries per file per synthesis run."""
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text("# Global LTM\n## Key Lessons\n<!-- tips -->\n")
        entries = [f"- (2026-02-23) [tip] Tip number {i}" for i in range(8)]
        routes = [RouteEntry(scope="global", section="Key Lessons", entries=entries)]
        warnings = append_to_ltm(routes, global_file=ltm_file)
        content = ltm_file.read_text()
        # Only 5 should be added
        assert content.count("Tip number") == 5
        assert any("cap" in w.lower() or "limit" in w.lower() for w in warnings)
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestAppendToLtmKeywordDedup -v`
Expected: `test_rejects_near_duplicate_by_keyword_overlap` FAILS (substring match doesn't catch rephrasing), `test_route_cap_enforced` FAILS (no cap exists)

**Step 3: Update `append_to_ltm()`**

Modify `scripts/synthesis.py:283-319`. Replace the dedup section and add route cap:

```python
        content = target_file.read_text(encoding="utf-8")
        lines = content.split("\n")

        # Track entries added per file for route cap
        entries_added_to_file = 0
        ROUTE_CAP = 5

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

            # Collect existing entries in this section for keyword dedup
            existing_entries = []
            for idx in range(insert_idx, len(lines)):
                if lines[idx].startswith("## "):
                    break
                if lines[idx].strip().startswith("-"):
                    existing_entries.append(lines[idx])

            # Filter: keyword-overlap dedup + route cap
            new_entries = []
            for entry in route.entries:
                if entries_added_to_file >= ROUTE_CAP:
                    warnings.append(
                        f"Route cap ({ROUTE_CAP}) reached for {target_file.name}, "
                        f"skipping remaining entries"
                    )
                    break
                if not any(
                    is_routed_match(entry, existing, threshold=0.6)
                    for existing in existing_entries
                ):
                    new_entries.append(entry)
                    existing_entries.append(entry)  # Prevent intra-batch dupes
                    entries_added_to_file += 1

            if new_entries:
                for entry in reversed(new_entries):
                    lines.insert(insert_idx, entry)

        target_file.write_text("\n".join(lines), encoding="utf-8")
```

Add import at top of `append_to_ltm()`:
```python
    from memory_utils import is_routed_match
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis.py::TestAppendToLtmKeywordDedup -v`
Expected: All 3 PASS

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: keyword-overlap dedup and route cap in append_to_ltm"
```

---

### Task 7: Simplify synthesis prompt in `load_memory.py`

Update the LLM prompt to reflect the new contract: LLM outputs types + optional GLOBAL marker, no scope tagging, no merge responsibility, no dedup responsibility.

**Files:**
- Modify: `scripts/load_memory.py:265-311` (_build_synthesis_instructions)
- Modify: `scripts/load_memory.py:364-381` (merge instructions in _build_preextracted_prompt)
- Modify: `scripts/load_memory.py:399-429` (output format example)
- Test: `tests/test_load_memory.py`

**Step 1: Write failing test**

```python
# In tests/test_load_memory.py
class TestSynthesisPromptSimplified:
    def test_no_scope_tagging_instructions(self):
        """Prompt should not instruct LLM to add scope tags."""
        from load_memory import _build_synthesis_instructions
        result = _build_synthesis_instructions("cartwheel, investing")
        assert "scope is `global` or one of" not in result
        assert "[scope/type]" not in result or "[type]" in result

    def test_no_merge_instructions(self):
        """Prompt should not instruct LLM to merge."""
        from load_memory import _build_synthesis_instructions
        result = _build_synthesis_instructions("cartwheel, investing")
        assert "merge new insights" not in result.lower()

    def test_no_dedup_instructions(self):
        """Prompt should not instruct LLM to check for duplicates."""
        from load_memory import _build_synthesis_instructions
        result = _build_synthesis_instructions("cartwheel, investing")
        assert "DEDUP REQUIREMENT" not in result

    def test_global_marker_documented(self):
        """Prompt should document [GLOBAL] marker usage."""
        from load_memory import _build_synthesis_instructions
        result = _build_synthesis_instructions("cartwheel, investing")
        assert "[GLOBAL]" in result

    def test_existing_dailies_marked_readonly(self):
        """Existing daily content labeled as read-only context."""
        from load_memory import _build_preextracted_prompt
        result = _build_preextracted_prompt(
            pending_dates=["2026-02-23"],
            extracted_files={"2026-02-23": "/tmp/test.txt"},
            synthesis_instructions="test instructions",
            embedded_files={
                "transcripts": {"2026-02-23": "transcript content"},
                "existing_dailies": {"2026-02-23": "# 2026-02-23\n## Actions\n- [proj/impl] Old"},
            },
        )
        assert "read-only" in result.lower() or "READ-ONLY" in result
        assert "do NOT repeat" in result or "do not repeat" in result.lower()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisPromptSimplified -v`
Expected: FAIL (current prompt has scope/merge/dedup instructions)

**Step 3: Rewrite `_build_synthesis_instructions()`**

Replace `scripts/load_memory.py:265-311`:

```python
def _build_synthesis_instructions(project_names_str: str) -> str:
    """Build the shared synthesis instructions block."""
    return '''**Daily summary format:**

```markdown
# YYYY-MM-DD

## Actions
<!-- What was done. Use [type] only — scope is injected automatically. -->
- [implement] What was accomplished

## Decisions
<!-- Important choices and rationale. -->
- [design] Choice made and why

## Learnings
<!-- Patterns, gotchas, insights. -->
- [gotcha] Unexpected behavior discovered
- [pattern] Proven method or approach

## Lessons
<!-- Actionable takeaways. -->
- [insight] Mental model or understanding
- [tip] Useful command or shortcut
```

**Entry format:** `- [type] Description` where type is one of: implement, improve, document, analyze, design, tradeoff, scope, gotcha, pitfall, pattern, insight, tip, workaround.
**Scope injection:** Do NOT add scope/project names to tags. The system injects scope automatically from session metadata.
**Global marker:** If a learning is genuinely useful across ALL projects (general dev practices, OS behavior, tool tips), prefix with `[GLOBAL]`: `- [GLOBAL][tip] Description`. Otherwise omit — the system defaults to project scope.
**Compactness:** Final solutions only, one learning per concept, omit routine details.

**Long-term routing (be HIGHLY selective):**
Route daily entries to corresponding LTM sections:
- Daily `## Actions` → LTM `## Key Actions` (multi-day implementations, novel integrations, reusable setups)
- Daily `## Decisions` → LTM `## Key Decisions` (architecture choices, design tradeoffs, scope decisions with lasting impact)
- Daily `## Learnings` → LTM `## Key Learnings` (non-obvious gotchas, proven patterns, hard-won lessons)
- Daily `## Lessons` → LTM `## Key Lessons` (mental models, useful commands, workarounds)
Do NOT route: routine implementation, version-specific fixes, one-time configs, easily re-discoverable things.
The system handles dedup automatically — output all entries you think are worth routing.
Format: `(YYYY-MM-DD) [type] Description` (no scope in routes — the system injects it).

**GRANULARITY CAP:** Maximum 5 routed entries per target LTM file per synthesis run.

**Global LTM auto-pinned maintenance:** The global LTM has auto-pinned sections (About Me, Current Projects, Technical Environment, Patterns & Preferences) containing factual profile info. When transcripts show clear evidence of change — a project completed, a new tool adopted — update or remove the relevant entry. Be conservative.'''
```

**Step 4: Update merge instructions in `_build_preextracted_prompt()`**

Replace `scripts/load_memory.py:371-381`:

```python
    merge_instructions = ""
    if merge_sections:
        merge_block = "\n\n".join(merge_sections)
        merge_instructions = f"""
## Existing Daily Summaries (READ-ONLY context — do NOT repeat these entries)

These daily files already exist. The system will merge your output automatically.
Output ONLY entries from new/continued sessions — do not re-state anything below.

{merge_block}

"""
```

**Step 5: Update output format example**

Replace the example in `_build_preextracted_prompt()` at ~lines 405-427 to show new format:

```python
===DAILY:2026-02-20===
# 2026-02-20
## Actions
- [implement] Built REST API endpoints for user authentication
- [implement] Configured pre-commit hooks for Python linting

## Decisions
- [design] JWT tokens over session cookies — stateless scales better

## Learnings
- [gotcha] SQLAlchemy async sessions need explicit await session.close()
- [GLOBAL][pattern] pytest -x --tb=short stops on first failure with compact output

## Lessons
- [GLOBAL][tip] git stash -u includes untracked files

===ROUTE:global:Key Lessons===
- (2026-02-20) [tip] git stash -u includes untracked files

===END===
```

Note: Route blocks still use `scope:section` format — the LLM sets route scope based on whether it used `[GLOBAL]` or not. Route scope = `global` if `[GLOBAL]` was present, otherwise the system injects project scope at apply time.

Actually, simplify further — routes should also use bare types since the apply phase will inject scope:

Update route instruction to tell LLM: use `global` for `[GLOBAL]` entries, use `project` (literal) for project entries. The apply phase will resolve `project` to the actual project name.

**Step 6: Run tests**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisPromptSimplified -v`
Expected: All 5 PASS

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 7: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "feat: simplify synthesis prompt — remove scope/merge/dedup from LLM contract"
```

---

### Task 8: Update `_ENTRY_PREFIX_PATTERN` for multi-scope tags

The prefix-stripping regex in `memory_utils.py:600-601` needs to handle `[scope1|scope2/type]` format for `extract_entry_keywords()` to work correctly on new-format entries.

**Files:**
- Modify: `scripts/memory_utils.py:600-601`
- Test: `tests/test_memory_utils.py`

**Step 1: Write failing test**

```python
class TestExtractEntryKeywordsMultiScope:
    def test_strips_multi_scope_tag(self):
        entry = "- [global|cartwheel/gotcha] Tailscale MTU black hole"
        keywords = extract_entry_keywords(entry)
        assert "tailscale" in keywords
        assert "global" not in keywords
        assert "cartwheel" not in keywords
        assert "gotcha" not in keywords

    def test_strips_single_scope_unchanged(self):
        entry = "- [cartwheel/implement] Built OAuth flow"
        keywords = extract_entry_keywords(entry)
        assert "oauth" in keywords
        assert "cartwheel" not in keywords
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/test_memory_utils.py::TestExtractEntryKeywordsMultiScope -v`
Expected: May pass or fail depending on whether `|` trips the existing regex. Test to confirm.

**Step 3: Update regex if needed**

Current pattern:
```python
_ENTRY_PREFIX_PATTERN = re.compile(
    r"^\s*-\s*(?:\[routed\])?\s*(?:\[[^\]]+\])?\s*(?:\(\d{4}-\d{2}-\d{2}\))?\s*(?:\[[^\]]+\])?\s*"
)
```

The `[^\]]+` already matches any content inside brackets, including `|`. So `[global|cartwheel/gotcha]` would be matched by `(?:\[[^\]]+\])?`. This should work already. Run the test to confirm — if it passes, no code change needed, just add the test.

**Step 4: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "test: verify keyword extraction handles multi-scope tags"
```

---

### Task 9: Integration test — full pipeline

End-to-end test: LLM-style output → parse → inject scopes → mark routed → merge → write → append LTM.

**Files:**
- Test: `tests/test_synthesis.py`

**Step 1: Write integration test**

```python
class TestFullPipelineIntegration:
    def test_end_to_end_with_scope_injection_and_merge(self, tmp_path):
        """Full pipeline: parse → inject scopes → mark routed → merge → write → LTM."""
        # Setup: existing daily file from earlier synthesis
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        existing_daily = daily_dir / "2026-02-23.md"
        existing_daily.write_text(
            "# 2026-02-23\n## Actions\n- [cartwheel/implement] Built OAuth\n"
        )

        # Setup: existing LTM
        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n## Pinned\n\n## Key Learnings\n<!-- gotchas -->\n\n"
            "## Key Lessons\n<!-- tips -->\n"
        )

        # LLM output (simplified format — no scopes, just types)
        llm_output = """===DAILY:2026-02-23===
# 2026-02-23
## Actions
- [implement] Added rate limiting

## Learnings
- [GLOBAL][gotcha] Tailscale MTU black hole on WSL2

===ROUTE:global:Key Learnings===
- (2026-02-23) [gotcha] Tailscale MTU black hole on WSL2

===END==="""

        # Parse
        result = parse_synthesis_output(llm_output)
        assert len(result.dailies) == 1

        # Inject scopes
        session_projects = {"2026-02-23": "cartwheel"}
        scoped = inject_scopes(result.dailies, session_projects)
        assert "- [cartwheel/implement] Added rate limiting" in scoped[0].content
        assert "- [global|cartwheel/gotcha] Tailscale MTU" in scoped[0].content

        # Mark routed
        marked = mark_routed_entries(scoped, result.routes)

        # Write (merges with existing)
        written = write_daily_files(marked, daily_dir=daily_dir)
        daily_content = existing_daily.read_text()
        assert "- [cartwheel/implement] Built OAuth" in daily_content  # preserved
        assert "- [cartwheel/implement] Added rate limiting" in daily_content  # merged
        assert "Tailscale MTU" in daily_content  # merged

        # Append to LTM
        warnings = append_to_ltm(result.routes, global_file=ltm_file)
        ltm_content = ltm_file.read_text()
        assert "Tailscale MTU" in ltm_content

    def test_dedup_across_merge_and_ltm(self, tmp_path):
        """Dedup prevents duplicates in both daily merge and LTM append."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        existing = daily_dir / "2026-02-23.md"
        existing.write_text(
            "# 2026-02-23\n## Learnings\n- [global/gotcha] Tailscale MTU drops packets\n"
        )

        ltm_file = tmp_path / "global-long-term-memory.md"
        ltm_file.write_text(
            "# Global LTM\n## Key Learnings\n<!-- gotchas -->\n"
            "- (2026-02-20) [gotcha] Tailscale MTU black hole drops packets\n"
        )

        # LLM outputs near-duplicate
        llm_output = """===DAILY:2026-02-23===
# 2026-02-23
## Learnings
- [gotcha] Tailscale MTU black hole silently drops packets

===ROUTE:global:Key Learnings===
- (2026-02-23) [gotcha] Tailscale MTU black hole silently drops packets

===END==="""

        result = parse_synthesis_output(llm_output)
        session_projects = {"2026-02-23": None}
        scoped = inject_scopes(result.dailies, session_projects)
        marked = mark_routed_entries(scoped, result.routes)
        write_daily_files(marked, daily_dir=daily_dir)
        append_to_ltm(result.routes, global_file=ltm_file)

        # Daily: should have only 1 Tailscale entry (dedup rejected near-dupe)
        daily_content = existing.read_text()
        assert daily_content.count("Tailscale MTU") == 1

        # LTM: should have only 1 Tailscale entry (keyword dedup rejected)
        ltm_content = ltm_file.read_text()
        assert ltm_content.count("Tailscale MTU") == 1
```

**Step 2: Run integration tests**

Run: `python3 -m pytest tests/test_synthesis.py::TestFullPipelineIntegration -v`
Expected: All PASS (relies on Tasks 1-8 being complete)

**Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 4: Commit**

```bash
git add tests/test_synthesis.py
git commit -m "test: add end-to-end integration tests for deterministic synthesis pipeline"
```

---

### Task 10: Install and verify

Apply changes and run a manual smoke test.

**Step 1: Run installer**

```bash
python3 /home/nsitaram/claude-memory-system/install.py
```

**Step 2: Verify memory loads**

```bash
python3 ~/.claude/scripts/load_memory.py
```

**Step 3: Run full test suite one final time**

```bash
python3 -m pytest /home/nsitaram/claude-memory-system/tests/ -q
```

**Step 4: Commit any final fixes**

If tests or smoke test revealed issues, fix and commit.
