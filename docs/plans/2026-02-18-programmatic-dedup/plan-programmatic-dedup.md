# Programmatic Dedup at Load Time — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a programmatic fallback in `filter_daily_content()` that skips STM entries matching LTM entries, regardless of whether synthesis marked them `[routed]`.

**Architecture:** Extract LTM entry lines after loading LTM content in `main()`, pass them through `load_daily_summaries()` and `load_project_history()` into `filter_daily_content()`, which runs `is_routed_match()` on non-`[routed]` entries. Pre-marked `[routed]` entries remain the fast path (skip match check entirely).

**Tech Stack:** Python 3.9+, existing `is_routed_match()` / `extract_entry_keywords()` helpers in `memory_utils.py`.

---

### Task 1: Add `extract_ltm_entries()` helper to `memory_utils.py`

**Files:**
- Modify: `scripts/memory_utils.py` (after `is_routed_match()`, ~line 607)
- Test: `tests/test_memory_utils.py`

**Step 1: Write the failing test**

Add to `tests/test_memory_utils.py` after the `TestRoutedMatching` class:

```python
class TestExtractLtmEntries:
    def test_extracts_dated_entries(self):
        content = """# Long-Term Memory
## Key Learnings
- (2026-02-15) [pattern] Some pattern here
- (2026-02-12) [gotcha] Some gotcha here
"""
        entries = extract_ltm_entries(content)
        assert len(entries) == 2
        assert "Some pattern here" in entries[0]

    def test_skips_non_entry_lines(self):
        content = """## Key Learnings
<!-- Subject to 30-day decay -->
Some paragraph text.
- (2026-02-15) [pattern] Real entry
"""
        entries = extract_ltm_entries(content)
        assert len(entries) == 1

    def test_empty_content(self):
        assert extract_ltm_entries("") == []

    def test_handles_pinned_entries(self):
        content = """## Pinned
- (2026-01-28) [pattern] Pinned entry
## Key Learnings
- (2026-02-15) [gotcha] Learning entry
"""
        entries = extract_ltm_entries(content)
        assert len(entries) == 2
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memory_utils.py::TestExtractLtmEntries -v`
Expected: FAIL with ImportError (extract_ltm_entries not defined)

**Step 3: Write minimal implementation**

Add to `scripts/memory_utils.py` after `is_routed_match()` (~line 607):

```python
def extract_ltm_entries(content: str) -> list[str]:
    """
    Extract all dated entry lines from LTM content.

    Matches lines starting with "- (YYYY-MM-DD)" pattern.
    Returns list of raw entry lines for use with is_routed_match().
    """
    if not content:
        return []
    return [line for line in content.splitlines() if re.match(r"^\s*-\s*\(", line)]
```

Also add the import in `tests/test_memory_utils.py`:
```python
from memory_utils import extract_ltm_entries  # add to existing imports
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memory_utils.py::TestExtractLtmEntries -v`
Expected: PASS

**Step 5: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "feat(dedup): add extract_ltm_entries helper"
```

---

### Task 2: Add programmatic dedup to `filter_daily_content()`

**Files:**
- Modify: `scripts/memory_utils.py:456-546` (`filter_daily_content()`)
- Test: `tests/test_memory_utils.py`

**Step 1: Write the failing tests**

Add to `TestFilterDailyContent` class in `tests/test_memory_utils.py`:

```python
    def test_programmatic_dedup_skips_ltm_matches(self):
        content = """# 2026-02-01
## Learnings
- [global/pattern] ETL schedule awareness REBUILDDATAWAREHOUSE runs 6 PM CT
- [global/gotcha] Something unique not in LTM
"""
        ltm_entries = [
            "- (2026-01-28) [pattern] ETL schedule awareness REBUILDDATAWAREHOUSE runs 6 PM CT"
        ]
        result = filter_daily_content(content, "global", ltm_entries=ltm_entries)
        assert "ETL schedule" not in result
        assert "Something unique" in result

    def test_programmatic_dedup_no_ltm_entries_passes_all(self):
        content = """# 2026-02-01
## Learnings
- [global/pattern] Some pattern
"""
        result = filter_daily_content(content, "global", ltm_entries=None)
        assert "Some pattern" in result

    def test_programmatic_dedup_routed_prefix_skips_match_check(self):
        """[routed] entries are already skipped; they don't need LTM match check."""
        content = """# 2026-02-01
## Learnings
- [routed][global/pattern] Already marked
- [global/gotcha] Not in LTM at all
"""
        ltm_entries = [
            "- (2026-02-01) [pattern] Already marked"
        ]
        result = filter_daily_content(content, "global", ltm_entries=ltm_entries)
        assert "[routed]" not in result
        assert "Not in LTM at all" in result

    def test_programmatic_dedup_empty_section_after_dedup_hidden(self):
        """Section with all entries deduped should not appear in output."""
        content = """# 2026-02-01
## Learnings
- [global/pattern] Exact match with LTM entry
## Actions
- [global/implement] Did something
"""
        ltm_entries = [
            "- (2026-02-01) [pattern] Exact match with LTM entry"
        ]
        result = filter_daily_content(content, "global", ltm_entries=ltm_entries)
        assert "## Learnings" not in result
        assert "## Actions" in result
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_memory_utils.py::TestFilterDailyContent::test_programmatic_dedup_skips_ltm_matches -v`
Expected: FAIL (filter_daily_content doesn't accept ltm_entries parameter yet)

**Step 3: Modify `filter_daily_content()` implementation**

Change the function signature at line ~456:

```python
def filter_daily_content(content: str, scope: str, ltm_entries: list[str] | None = None) -> str:
```

Add the dedup check after the scope match succeeds (inside the `if match:` block, ~line 521). The existing code is:

```python
            match = TAG_PATTERN.match(line)
            if match:
                entry_scope = match.group(1).lower()
                if entry_scope == scope.lower():
                    section_lines.append(line)
                    section_has_content = True
```

Change to:

```python
            match = TAG_PATTERN.match(line)
            if match:
                entry_scope = match.group(1).lower()
                if entry_scope == scope.lower():
                    # Programmatic dedup: skip entries that match LTM
                    if ltm_entries and any(
                        is_routed_match(line, ltm) for ltm in ltm_entries
                    ):
                        continue
                    section_lines.append(line)
                    section_has_content = True
```

**Step 4: Run all filter_daily_content tests**

Run: `python3 -m pytest tests/test_memory_utils.py::TestFilterDailyContent -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "feat(dedup): add programmatic LTM dedup fallback in filter_daily_content"
```

---

### Task 3: Thread `ltm_entries` through loading functions

**Files:**
- Modify: `scripts/load_memory.py:138-207` (`load_daily_summaries()`, `load_project_history()`)
- Test: `tests/test_load_memory.py`

**Step 1: Write the failing tests**

Add tests to `tests/test_load_memory.py` (check existing test structure first):

```python
class TestProgrammaticDedup:
    def test_load_daily_summaries_passes_ltm_entries(self):
        """load_daily_summaries should forward ltm_entries to filter_daily_content."""
        # Create a daily file with an entry that matches LTM
        with tempfile.TemporaryDirectory() as tmpdir:
            daily_dir = Path(tmpdir) / "daily"
            daily_dir.mkdir()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            (daily_dir / f"{today}.md").write_text(
                f"# {today}\n## Learnings\n- [global/pattern] ETL schedule REBUILDDATAWAREHOUSE runs 6 PM CT\n"
            )
            ltm_entries = [
                "- (2026-01-28) [pattern] ETL schedule REBUILDDATAWAREHOUSE runs 6 PM CT"
            ]
            with mock.patch("load_memory.get_daily_dir", return_value=daily_dir), \
                 mock.patch("load_memory.get_working_days", return_value=[today]):
                summaries, _ = load_daily_summaries(1, scope="global", ltm_entries=ltm_entries)
                # Entry should be deduped — no content returned
                assert len(summaries) == 0

    def test_load_daily_summaries_without_ltm_entries(self):
        """Without ltm_entries, all matching-scope entries pass through."""
        with tempfile.TemporaryDirectory() as tmpdir:
            daily_dir = Path(tmpdir) / "daily"
            daily_dir.mkdir()
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            (daily_dir / f"{today}.md").write_text(
                f"# {today}\n## Learnings\n- [global/pattern] Some pattern\n"
            )
            with mock.patch("load_memory.get_daily_dir", return_value=daily_dir), \
                 mock.patch("load_memory.get_working_days", return_value=[today]):
                summaries, _ = load_daily_summaries(1, scope="global")
                assert len(summaries) == 1
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_load_memory.py::TestProgrammaticDedup -v`
Expected: FAIL (ltm_entries parameter not accepted)

**Step 3: Add `ltm_entries` parameter to both functions**

In `scripts/load_memory.py`, modify `load_daily_summaries()`:

```python
def load_daily_summaries(
    days_limit: int, scope: str = "global", ltm_entries: list[str] | None = None
) -> tuple[list[tuple[str, str]], int]:
```

And thread it through to the call at line ~158:
```python
                filtered_content = filter_daily_content(raw_content, scope, ltm_entries=ltm_entries)
```

Similarly modify `load_project_history()`:

```python
def load_project_history(
    project: dict, days_limit: int, ltm_entries: list[str] | None = None
) -> tuple[list[tuple[str, str]], int]:
```

And thread at line ~196:
```python
            filtered_content = filter_daily_content(raw_content, project_name, ltm_entries=ltm_entries)
```

**Step 4: Run tests**

Run: `python3 -m pytest tests/test_load_memory.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "feat(dedup): thread ltm_entries through daily/project loading functions"
```

---

### Task 4: Wire up in `main()` — collect LTM entries, pass to loaders

**Files:**
- Modify: `scripts/load_memory.py:424-579` (`main()`)
- No new tests (integration-level wiring)

**Step 1: Add `extract_ltm_entries` import**

Add to the existing imports from `memory_utils` at line ~30:

```python
from memory_utils import (
    ...
    extract_ltm_entries,
    ...
)
```

**Step 2: Collect LTM entries after loading LTM content**

After line ~528 (after global LTM is loaded and printed), add:

```python
    # Collect LTM entries for programmatic dedup of short-term memory
    all_ltm_entries = extract_ltm_entries(global_content)
```

After line ~545 (after project LTM is loaded and printed), add to the project block:

```python
            if project_content:
                all_ltm_entries.extend(extract_ltm_entries(project_content))
```

**Step 3: Pass LTM entries to short-term loaders**

Change line ~549:
```python
    global_summaries, global_daily_bytes = load_daily_summaries(
        short_term_days, scope="global", ltm_entries=all_ltm_entries
    )
```

Change line ~562:
```python
        project_history, history_bytes = load_project_history(
            current_project, project_days, ltm_entries=all_ltm_entries
        )
```

**Step 4: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: ALL PASS

**Step 5: Run token_usage.py to verify improvement**

Run: `python3 scripts/token_usage.py`
Expected: project STM tokens should decrease (entries matching LTM now filtered at load time)

**Step 6: Verify no routed entries in output**

Run: `echo '{"session_id": "test"}' | python3 scripts/load_memory.py 2>/dev/null | grep -c "\[routed\]"`
Expected: 0 (excluding synthesis prompt text — grep the memory sections only)

**Step 7: Commit**

```bash
git add scripts/load_memory.py
git commit -m "feat(dedup): wire programmatic LTM dedup into main() loading flow"
```

---

### Task 5: Update ticket acceptance criteria

**Files:**
- Modify: `docs/tickets/routed-dedup-incomplete.md`

Mark acceptance criteria as checked and close the ticket.

```bash
git add docs/tickets/routed-dedup-incomplete.md
git commit -m "docs: mark routed-dedup ticket acceptance criteria complete"
```
