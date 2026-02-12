# [routed] Dedup Markers Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Eliminate duplicate token loading by prefixing daily entries routed to LTM with `[routed]` and skipping them at load time.

**Architecture:** `filter_daily_content()` in `memory_utils.py` gains a `[routed]` skip check. The synthesis prompt in `load_memory.py` instructs the subagent to prefix routed entries. A one-time migration script in `devtools.py` retroactively marks existing duplicates.

**Tech Stack:** Python 3.9+, pytest, regex

---

### Task 1: Add `[routed]` skip to `filter_daily_content()`

**Files:**
- Modify: `scripts/memory_utils.py:511-519` (inside `filter_daily_content`, the tagged-entry branch)
- Test: `tests/test_memory_utils.py`

**Step 1: Write failing tests**

Add to `TestFilterDailyContent` in `tests/test_memory_utils.py`:

```python
def test_routed_entries_skipped(self):
    content = """# 2026-02-01
## Learnings
- [routed][global/pattern] Already in LTM
- [global/gotcha] Still only in STM
"""
    result = filter_daily_content(content, "global")
    assert "[routed]" not in result
    assert "[global/gotcha] Still only in STM" in result

def test_routed_entries_skipped_project_scope(self):
    content = """# 2026-02-01
## Learnings
- [routed][myproject/pattern] Already routed
- [myproject/gotcha] Not routed
"""
    result = filter_daily_content(content, "myproject")
    assert "[routed]" not in result
    assert "[myproject/gotcha] Not routed" in result

def test_routed_entries_not_counted_as_content(self):
    """A section with only routed entries should not appear in output."""
    content = """# 2026-02-01
## Learnings
- [routed][global/pattern] Already in LTM
## Actions
- [global/implement] Did something
"""
    result = filter_daily_content(content, "global")
    assert "## Learnings" not in result
    assert "## Actions" in result
    assert "[global/implement]" in result
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_memory_utils.py::TestFilterDailyContent::test_routed_entries_skipped tests/test_memory_utils.py::TestFilterDailyContent::test_routed_entries_skipped_project_scope tests/test_memory_utils.py::TestFilterDailyContent::test_routed_entries_not_counted_as_content -v`
Expected: FAIL — `[routed]` entries currently pass through the tag matcher

**Step 3: Implement the skip**

In `scripts/memory_utils.py`, inside `filter_daily_content()`, add a check before the tag match at line ~513. The `[routed]` prefix must be detected and the line skipped before the scope tag regex runs:

```python
# If we're in a section, process the line
if current_section:
    # Skip entries marked as routed to LTM
    if re.match(r"^\s*-\s*\[routed\]", line):
        continue

    # Check if this is a tagged entry
    match = TAG_PATTERN.match(line)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memory_utils.py::TestFilterDailyContent -v`
Expected: ALL PASS (including existing tests — no regressions)

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "feat(dedup): skip [routed] entries in filter_daily_content"
```

---

### Task 2: Update synthesis prompt to mark routed entries

**Files:**
- Modify: `scripts/load_memory.py:239-285` (the `synthesis_instructions` string in `_build_synthesis_prompt()`)
- Test: `tests/test_load_memory.py` (verify prompt contains the instruction)

**Step 1: Write failing test**

Add to `tests/test_load_memory.py`:

```python
class TestSynthesisPromptRoutedMarker:
    """Verify synthesis prompt instructs subagent to prefix routed entries."""

    def test_prompt_contains_routed_instruction(self):
        """The synthesis prompt must tell the subagent to prefix routed entries."""
        from load_memory import _build_synthesis_prompt
        prompt = _build_synthesis_prompt("", ["2026-02-01"])
        assert "[routed]" in prompt
        assert "prefix" in prompt.lower() or "mark" in prompt.lower()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisPromptRoutedMarker -v`
Expected: FAIL — prompt doesn't mention `[routed]` yet

**Step 3: Update synthesis prompt**

In `scripts/load_memory.py`, in the `synthesis_instructions` string, after the long-term routing paragraph (after line 278 `Format: ...`), add:

```python
**Dedup marking:** When you route an entry from the daily summary to long-term memory, prefix that entry in the daily file with `[routed]`. Example:
Before: `- [claude-memory-system/gotcha] Missing import crashed indexing`
After routing to LTM: `- [routed][claude-memory-system/gotcha] Missing import crashed indexing`
This prevents the entry from loading twice (once from LTM, once from short-term). Only prefix entries you actually route — leave non-routed entries unchanged.
```

Insert this block right after the `Format: ...` line and before `Create missing project files from template`.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisPromptRoutedMarker -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "feat(dedup): add [routed] prefix instruction to synthesis prompt"
```

---

### Task 3: One-time migration script

**Files:**
- Modify: `scripts/devtools.py` (add `mark-routed` command)
- Test: `tests/test_memory_utils.py` (test the matching helper)

The migration needs to:
1. Collect all entry descriptions from all LTM files (global + project)
2. For each daily file, find Learnings/Lessons entries that match an LTM entry
3. Prefix matches with `[routed]`

**Step 1: Write the matching helper test**

Add a new helper function `is_routed_match()` to `memory_utils.py` and test it. This function extracts keywords from two entry strings and checks overlap.

Add to `tests/test_memory_utils.py`:

```python
from memory_utils import extract_entry_keywords, is_routed_match

class TestRoutedMatching:
    def test_extract_keywords_strips_tags_and_stopwords(self):
        keywords = extract_entry_keywords(
            "- [claude-memory-system/gotcha] Missing defaultdict import crashed build_projects_index()"
        )
        assert "defaultdict" in keywords
        assert "crashed" in keywords
        assert "claude-memory-system" not in keywords  # tag stripped
        assert "the" not in keywords  # stopword stripped

    def test_match_same_concept_different_wording(self):
        stm = "- [claude-memory-system/gotcha] Missing defaultdict import crashed build_projects_index()"
        ltm = "- (2026-02-12) [gotcha] Missing imports cause cascading failures in indexing — defaultdict missing from build_projects_index()"
        assert is_routed_match(stm, ltm) is True

    def test_no_match_different_concepts(self):
        stm = "- [claude-memory-system/pattern] FileLock prevents concurrent file corruption"
        ltm = "- (2026-02-12) [gotcha] Missing imports cause cascading failures in indexing"
        assert is_routed_match(stm, ltm) is False

    def test_match_with_high_keyword_overlap(self):
        stm = "- [global/pattern] ETL schedule awareness - REBUILDDATAWAREHOUSE runs 6 PM CT"
        ltm = "- (2026-01-28) [pattern] ETL schedule awareness - REBUILDDATAWAREHOUSE runs 6 PM CT"
        assert is_routed_match(stm, ltm) is True

    def test_already_routed_entry_ignored(self):
        """extract_entry_keywords should handle [routed] prefix gracefully."""
        keywords = extract_entry_keywords(
            "- [routed][global/pattern] Already marked"
        )
        assert "already" in keywords
        assert "routed" not in keywords
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_memory_utils.py::TestRoutedMatching -v`
Expected: FAIL — functions don't exist yet

**Step 3: Implement matching helpers in `memory_utils.py`**

Add to `scripts/memory_utils.py` after `filter_daily_content()`:

```python
# Stopwords for keyword extraction (common English words that don't help matching)
_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "can", "shall", "to", "of", "in", "for",
    "on", "with", "at", "by", "from", "as", "into", "through", "during",
    "before", "after", "above", "below", "between", "out", "off", "over",
    "under", "again", "further", "then", "once", "that", "this", "these",
    "those", "not", "no", "nor", "or", "and", "but", "if", "so", "than",
    "too", "very", "just", "about", "up", "it", "its", "use", "when",
})

# Regex to strip tag prefixes: [routed], [scope/type], (YYYY-MM-DD)
_ENTRY_PREFIX_PATTERN = re.compile(
    r"^\s*-\s*(?:\[routed\])?\s*(?:\[[^\]]+\])?\s*(?:\(\d{4}-\d{2}-\d{2}\))?\s*(?:\[[^\]]+\])?\s*"
)


def extract_entry_keywords(entry: str) -> set[str]:
    """
    Extract meaningful keywords from a memory entry line.

    Strips tag prefixes ([scope/type], [routed], (date)), stopwords,
    and short tokens. Returns lowercase keyword set.
    """
    # Remove tag/date prefixes
    text = _ENTRY_PREFIX_PATTERN.sub("", entry)
    # Tokenize: split on non-alphanumeric, lowercase
    tokens = re.findall(r"[a-z0-9_]+", text.lower())
    # Filter stopwords and short tokens
    return {t for t in tokens if t not in _STOPWORDS and len(t) > 2}


def is_routed_match(stm_entry: str, ltm_entry: str, threshold: float = 0.5) -> bool:
    """
    Check if a short-term memory entry matches a long-term memory entry.

    Uses keyword overlap: if >= threshold of the smaller set's keywords
    appear in the larger set, it's a match.

    Args:
        stm_entry: Daily file entry line (e.g., "- [scope/type] Description")
        ltm_entry: LTM entry line (e.g., "- (2026-02-12) [type] Description")
        threshold: Minimum overlap ratio (0.0-1.0) to consider a match

    Returns:
        True if entries are conceptual duplicates
    """
    stm_kw = extract_entry_keywords(stm_entry)
    ltm_kw = extract_entry_keywords(ltm_entry)

    if not stm_kw or not ltm_kw:
        return False

    overlap = len(stm_kw & ltm_kw)
    smaller = min(len(stm_kw), len(ltm_kw))

    return overlap / smaller >= threshold
```

**Step 4: Run matching helper tests**

Run: `python3 -m pytest tests/test_memory_utils.py::TestRoutedMatching -v`
Expected: ALL PASS

**Step 5: Commit helpers**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "feat(dedup): add keyword matching helpers for routed entry detection"
```

**Step 6: Implement migration command in `devtools.py`**

Add a `mark-routed` subcommand to `scripts/devtools.py`. This is repo-local (not installed), run once.

```python
def cmd_mark_routed(args):
    """One-time migration: mark daily entries that exist in LTM with [routed] prefix."""
    from memory_utils import (
        get_global_memory_file, get_project_memory_dir, get_daily_dir,
        extract_entry_keywords, is_routed_match,
    )

    dry_run = "--dry-run" in args

    # 1. Collect all LTM entries (global + all project files)
    ltm_entries = []

    global_ltm = get_global_memory_file()
    if global_ltm.exists():
        for line in global_ltm.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\s*-\s*\(", line):  # Lines starting with "- (YYYY-MM-DD)"
                ltm_entries.append(line)

    project_dir = get_project_memory_dir()
    if project_dir.exists():
        for pfile in project_dir.glob("*-long-term-memory.md"):
            for line in pfile.read_text(encoding="utf-8").splitlines():
                if re.match(r"^\s*-\s*\(", line):
                    ltm_entries.append(line)

    print(f"Collected {len(ltm_entries)} LTM entries across all files")

    # 2. Process each daily file
    daily_dir = get_daily_dir()
    total_marked = 0

    for daily_file in sorted(daily_dir.glob("*.md")):
        lines = daily_file.read_text(encoding="utf-8").splitlines()
        modified = False
        file_marked = 0
        new_lines = []

        in_learnings_or_lessons = False
        for line in lines:
            # Track if we're in a Learnings or Lessons section
            if line.startswith("## "):
                section = line.strip("# ").strip()
                in_learnings_or_lessons = section in ("Learnings", "Lessons")

            # Only check entries in Learnings/Lessons sections
            if (in_learnings_or_lessons
                    and re.match(r"^\s*-\s*\[(?!routed)", line)  # tagged entry, not already routed
                    and any(is_routed_match(line, ltm) for ltm in ltm_entries)):
                new_lines.append(re.sub(r"^(\s*-\s*)", r"\1[routed]", line))
                modified = True
                file_marked += 1
            else:
                new_lines.append(line)

        if modified:
            if dry_run:
                print(f"  {daily_file.name}: would mark {file_marked} entries")
            else:
                daily_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                print(f"  {daily_file.name}: marked {file_marked} entries")
            total_marked += file_marked

    action = "Would mark" if dry_run else "Marked"
    print(f"\n{action} {total_marked} entries across all daily files")
```

**Step 7: Run migration dry-run, then for real**

```bash
python3 scripts/devtools.py mark-routed --dry-run    # Preview changes
python3 scripts/devtools.py mark-routed               # Apply changes
```

**Step 8: Verify token savings**

```bash
echo '{"session_id": "test-verify"}' | python3 scripts/load_memory.py 2>/dev/null | wc -c
```

Expected: ~30,300 bytes (down from ~33,800)

**Step 9: Commit migration + results**

```bash
git add scripts/devtools.py
git commit -m "feat(dedup): add mark-routed migration command to devtools"
```

---

### Task 4: Update CLAUDE.md and install

**Files:**
- Modify: `CLAUDE.md` (document `[routed]` format in daily file format section)

**Step 1: Update CLAUDE.md**

In the "Daily file format" section, add a note about `[routed]` prefix:

```markdown
**Routed entries:** Entries promoted to long-term memory during synthesis are prefixed with `[routed]` in the daily file (e.g., `- [routed][scope/type] Description`). These are skipped during short-term memory loading to avoid duplication with LTM.
```

**Step 2: Install and verify end-to-end**

```bash
python3 install.py
echo '{"session_id": "test-final"}' | python3 ~/.claude/scripts/load_memory.py 2>/dev/null | wc -c
```

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document [routed] dedup marker format in CLAUDE.md"
```

---

### Task 5: Full verification

**Step 1: Run full test suite**

```bash
python3 -m pytest tests/ -v
```

Expected: All pass, no regressions

**Step 2: Verify memory loads correctly**

```bash
echo '{"session_id": "test-e2e"}' | python3 scripts/load_memory.py 2>/dev/null | grep -c "\[routed\]"
```

Expected: 0 (no `[routed]` entries in output — they're all filtered)

**Step 3: Spot-check a daily file**

```bash
grep "\[routed\]" ~/.claude/memory/daily/2026-02-12.md
```

Expected: Several `[routed]` lines for entries that exist in LTM
