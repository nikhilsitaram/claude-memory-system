# DRY & Best Practices Cleanup Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix bugs, eliminate DRY violations, and apply best practices across the memory system scripts.

**Architecture:** All work is in the `scripts/` directory. Changes are pure refactors (extract helpers, fix defaults, tighten error handling) with no behavioral changes except bug fixes #9, #10, #11. New utility functions go in `memory_utils.py`. Tests use pytest `tmp_path`, `@pytest.mark.parametrize`, and `unittest.mock.patch`.

**Tech Stack:** Python 3.9+, pytest, unittest.mock

**Working directory:** `/home/nsitaram/claude-memory-system/.worktrees/dry-cleanup`

**Test command:** `python3 -m pytest tests/ -q`

**IMPORTANT:** All file paths below are relative to the worktree root. Read the actual file before editing — line numbers are approximate.

---

### Task 1: Fix wrong default in `_calculate_token_limits` (Bug #9)

**Files:**
- Modify: `scripts/memory_utils.py` (line ~179)
- Modify: `tests/test_memory_utils.py`

**Step 1: Write the failing test**

In `tests/test_memory_utils.py`, add a test that verifies the fallback default for `projectShortTerm.workingDays` matches `DEFAULT_SETTINGS`:

```python
class TestCalculateTokenLimits:
    def test_fallback_defaults_match_default_settings(self):
        """_calculate_token_limits fallbacks must match DEFAULT_SETTINGS."""
        from memory_utils import _calculate_token_limits, DEFAULT_SETTINGS, SHORT_TERM_TOKENS_PER_DAY
        # Pass empty settings to trigger all fallbacks
        result = _calculate_token_limits({})
        expected_project_days = DEFAULT_SETTINGS["projectShortTerm"]["workingDays"]  # 5
        assert result["projectShortTerm"]["tokenLimit"] == expected_project_days * SHORT_TERM_TOKENS_PER_DAY
```

**Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_memory_utils.py::TestCalculateTokenLimits::test_fallback_defaults_match_default_settings -v
```
Expected: FAIL (gets 7 * 750 = 5250, expected 5 * 750 = 3750)

**Step 3: Fix the bug**

In `scripts/memory_utils.py`, line ~179, change:
```python
project_days = settings.get("projectShortTerm", {}).get("workingDays", 7)
```
to:
```python
project_days = settings.get("projectShortTerm", {}).get("workingDays", DEFAULT_SETTINGS["projectShortTerm"]["workingDays"])
```

Also fix the global fallback on the line above for consistency:
```python
global_days = settings.get("globalShortTerm", {}).get("workingDays", DEFAULT_SETTINGS["globalShortTerm"]["workingDays"])
```

And fix the long-term fallbacks on lines ~187-190:
```python
settings.get("globalLongTerm", {}).get("tokenLimit", DEFAULT_SETTINGS["globalLongTerm"]["tokenLimit"]) +
...
settings.get("projectLongTerm", {}).get("tokenLimit", DEFAULT_SETTINGS["projectLongTerm"]["tokenLimit"]) +
```

**Step 4: Run test to verify it passes**

```bash
python3 -m pytest tests/test_memory_utils.py::TestCalculateTokenLimits -v
```
Expected: PASS

**Step 5: Run full suite**

```bash
python3 -m pytest tests/ -q
```
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "fix: use DEFAULT_SETTINGS for _calculate_token_limits fallbacks"
```

---

### Task 2: Fix `token_usage.py` double file reads (Bug #10)

**Files:**
- Modify: `scripts/token_usage.py` (lines ~49-54, ~84)
- Modify: `tests/test_token_usage.py`

**Step 1: Write the failing test**

Add a test that verifies daily files are only read once per invocation (mock `Path.read_text` and count calls):

```python
class TestCalculateUsageEfficiency:
    def test_daily_files_read_once(self, tmp_path):
        """Each daily file should be read at most once during calculate_usage."""
        # Create a daily file with global content
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        daily_file = daily_dir / "2026-02-18.md"
        daily_file.write_text("# 2026-02-18\n## Actions\n- [global/implement] Test\n")

        read_count = 0
        original_read = daily_file.read_text

        def counting_read(*args, **kwargs):
            nonlocal read_count
            read_count += 1
            return original_read(*args, **kwargs)

        # Patch to use our tmp dirs and count reads
        with (
            mock.patch("token_usage.get_daily_dir", return_value=daily_dir),
            mock.patch("token_usage.get_global_memory_file", return_value=tmp_path / "nonexistent"),
            mock.patch("token_usage.get_projects_index_file", return_value=tmp_path / "nonexistent"),
            mock.patch("token_usage.load_json_file", return_value={}),
            mock.patch("token_usage.find_current_project", return_value=None),
            mock.patch.object(type(daily_file), "read_text", counting_read),
        ):
            # Capture stdout
            import io, contextlib
            f = io.StringIO()
            with contextlib.redirect_stdout(f):
                from token_usage import calculate_usage
                calculate_usage()

        # Should read daily file only once (not twice)
        assert read_count == 1, f"Daily file read {read_count} times, expected 1"
```

Note: The exact mock setup may need adjustment — read the actual test file first and follow existing patterns.

**Step 2: Run test to verify it fails**

Expected: FAIL (file read twice — once in loop, once in `global_short_days_actual` computation)

**Step 3: Fix the implementation**

In `scripts/token_usage.py`, track the count during the first pass instead of re-reading:

```python
    global_short_term_bytes = 0
    global_short_days_actual = 0  # Track during first pass
    for f in daily_files:
        content = f.read_text(encoding="utf-8")
        filtered = filter_daily_content(content, "global")
        if filtered:
            global_short_term_bytes += len(filtered.encode("utf-8"))
            global_short_days_actual += 1
    global_short_term_tokens = global_short_term_bytes // 4
```

Then delete the redundant line ~84:
```python
    # DELETE THIS LINE:
    # global_short_days_actual = sum(1 for f in daily_files if filter_daily_content(f.read_text(encoding="utf-8"), "global"))
```

**Step 4: Run full suite**

```bash
python3 -m pytest tests/ -q
```

**Step 5: Commit**

```bash
git add scripts/token_usage.py tests/test_token_usage.py
git commit -m "fix: eliminate double file read in token_usage.py"
```

---

### Task 3: Fix `format_transcripts_for_output` inconsistency (Bug #11)

**Files:**
- Modify: `scripts/transcript_ops.py` (lines ~192-203)
- Modify: `tests/test_transcript_ops.py`

**Step 1: Write the failing test**

Add a test that verifies truncated and non-truncated paths produce equivalent formatting:

```python
class TestFormatTranscriptsConsistency:
    def test_truncated_matches_non_truncated_format(self):
        """Truncated output should have the same structure as non-truncated."""
        from transcript_ops import format_transcripts_for_output
        messages = [{"role": "assistant", "content": f"Line {i}"} for i in range(5)]
        data = {"2026-01-01": [{"session_id": "s1", "filepath": "/tmp/t.jsonl",
                                "project_path": None, "message_count": 5, "messages": messages}]}

        # No budget (no truncation)
        full = format_transcripts_for_output(data)
        # Huge budget (no truncation either)
        with_budget = format_transcripts_for_output(data, total_line_budget=10000)

        assert full == with_budget, "Budget that doesn't trigger truncation should produce identical output"
```

**Step 2: Run test to verify it fails**

Expected: May or may not fail depending on the content — the real inconsistency is between truncated vs non-truncated code paths. Examine the code first.

**Step 3: Fix the implementation**

In `scripts/transcript_ops.py`, make both paths use the same approach. Replace the `else: output.extend(session_parts)` branch with `output.append(session_text)` to match the truncated path:

```python
            if max_lines_per_session and len(actual_lines) > max_lines_per_session:
                head = max_lines_per_session // 3
                tail = max_lines_per_session - head
                truncated = len(actual_lines) - head - tail
                output.append("\n".join(actual_lines[:head]))
                output.append(f"\n... [{truncated} lines truncated] ...")
                output.append("\n".join(actual_lines[-tail:]))
            else:
                output.append(session_text)
```

**Step 4: Run full suite**

```bash
python3 -m pytest tests/ -q
```

**Step 5: Commit**

```bash
git add scripts/transcript_ops.py tests/test_transcript_ops.py
git commit -m "fix: consistent formatting in format_transcripts_for_output"
```

---

### Task 4: Add ISO datetime helpers to `memory_utils.py` (DRY #7)

**Files:**
- Modify: `scripts/memory_utils.py`
- Modify: `tests/test_memory_utils.py`
- Then modify callers across scripts

**Step 1: Write tests**

```python
class TestIsoDatetimeHelpers:
    def test_to_iso_z_converts_utc(self):
        from memory_utils import to_iso_z
        from datetime import datetime, timezone
        dt = datetime(2026, 2, 18, 12, 0, 0, tzinfo=timezone.utc)
        result = to_iso_z(dt)
        assert result.endswith("Z")
        assert "+00:00" not in result

    def test_from_iso_z_parses_z_suffix(self):
        from memory_utils import from_iso_z
        result = from_iso_z("2026-02-18T12:00:00Z")
        assert result.tzinfo is not None
        assert result.year == 2026

    def test_from_iso_z_parses_offset(self):
        from memory_utils import from_iso_z
        result = from_iso_z("2026-02-18T12:00:00+00:00")
        assert result.tzinfo is not None

    def test_roundtrip(self):
        from memory_utils import to_iso_z, from_iso_z
        from datetime import datetime, timezone
        dt = datetime(2026, 2, 18, 15, 30, 45, tzinfo=timezone.utc)
        assert from_iso_z(to_iso_z(dt)) == dt
```

**Step 2: Implement**

In `scripts/memory_utils.py`, near the top utility section:

```python
def to_iso_z(dt: datetime) -> str:
    """Convert UTC datetime to ISO string with Z suffix."""
    return dt.isoformat().replace("+00:00", "Z")

def from_iso_z(date_str: str) -> datetime:
    """Parse ISO datetime string, handling both Z and +00:00 suffixes."""
    return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
```

Add `datetime` to the imports (already imported in files that will use these).

**Step 3: Replace callers**

Search all scripts for `.replace("+00:00", "Z")` and `.replace("Z", "+00:00")` and replace with calls to `to_iso_z()` / `from_iso_z()`. Files affected:
- `scripts/indexing.py` (~2 occurrences)
- `scripts/project_manager.py` (~4 occurrences)
- `scripts/load_memory.py` (~1 occurrence)

**Step 4: Run full suite**

```bash
python3 -m pytest tests/ -q
```

**Step 5: Commit**

```bash
git add scripts/memory_utils.py scripts/indexing.py scripts/project_manager.py scripts/load_memory.py tests/test_memory_utils.py
git commit -m "refactor: extract ISO datetime helpers to_iso_z/from_iso_z"
```

---

### Task 5: Extract LTM dated-entry regex and file collection to `memory_utils.py` (DRY #5, #6)

**Files:**
- Modify: `scripts/memory_utils.py` (add constants + helper)
- Modify: `scripts/devtools.py` (use shared helpers)
- Modify: `scripts/decay.py` (use shared constant)
- Modify: `tests/test_memory_utils.py`

**Step 1: Write tests**

```python
class TestLtmHelpers:
    def test_ltm_entry_pattern_matches_dated_entry(self):
        from memory_utils import LTM_ENTRY_PATTERN
        assert LTM_ENTRY_PATTERN.match("- (2026-02-18) [pattern] Some text")
        assert LTM_ENTRY_PATTERN.match("  - (2026-01-01) [gotcha] Indented")

    def test_ltm_entry_pattern_rejects_non_dated(self):
        from memory_utils import LTM_ENTRY_PATTERN
        assert not LTM_ENTRY_PATTERN.match("- [scope/type] No date")
        assert not LTM_ENTRY_PATTERN.match("## Section header")

    def test_collect_ltm_files(self, tmp_path):
        from memory_utils import collect_ltm_files
        # Create global + project LTM files
        global_f = tmp_path / "global-long-term-memory.md"
        global_f.write_text("# Global\n")
        proj_dir = tmp_path / "project-memory"
        proj_dir.mkdir()
        (proj_dir / "foo-long-term-memory.md").write_text("# Foo\n")

        with mock.patch("memory_utils.get_global_memory_file", return_value=global_f), \
             mock.patch("memory_utils.get_project_memory_dir", return_value=proj_dir):
            files = collect_ltm_files()

        assert len(files) == 2
        assert global_f in files
```

**Step 2: Implement**

In `scripts/memory_utils.py`:

```python
# Pattern matching LTM dated entries: - (YYYY-MM-DD) [type] description
LTM_ENTRY_PATTERN = re.compile(r"^\s*-\s*\(\d{4}-\d{2}-\d{2}\)")


def collect_ltm_files() -> list[Path]:
    """Collect all LTM files (global + all project files)."""
    files = []
    global_f = get_global_memory_file()
    if global_f.exists():
        files.append(global_f)
    proj_dir = get_project_memory_dir()
    if proj_dir.exists():
        files.extend(proj_dir.glob("*-long-term-memory.md"))
    return files
```

**Step 3: Replace callers**

- `devtools.py:cmd_mark_routed` — replace `re.match(r"^\s*-\s*\(", line)` with `LTM_ENTRY_PATTERN.match(line)` and replace the LTM file collection block with `collect_ltm_files()`
- `devtools.py:cmd_validate_ltm` — same replacements
- `decay.py` — can optionally import `LTM_ENTRY_PATTERN` instead of its own `DATE_PATTERN` (but `DATE_PATTERN` captures the date group, so keep it; just note the relationship)

**Step 4: Run full suite**

```bash
python3 -m pytest tests/ -q
```

**Step 5: Commit**

```bash
git add scripts/memory_utils.py scripts/devtools.py tests/test_memory_utils.py
git commit -m "refactor: extract LTM_ENTRY_PATTERN and collect_ltm_files helpers"
```

---

### Task 6: Extract pre-extraction helper in `load_memory.py` (DRY #3)

**Files:**
- Modify: `scripts/load_memory.py` (extract helper, use in both paths)
- Modify: `tests/test_load_memory.py`

**Step 1: Write test**

```python
class TestPreExtractTranscripts:
    def test_returns_extracted_files_dict(self, tmp_path):
        """pre_extract_transcripts returns dict mapping date -> path."""
        from load_memory import pre_extract_transcripts
        # Mock extract_transcripts to return data for one date
        mock_data = {"2026-02-18": [{"session_id": "s1", "messages": [{"role": "assistant", "content": "hi"}]}]}
        with mock.patch("load_memory.extract_transcripts", return_value=mock_data):
            result = pre_extract_transcripts(["2026-02-18"], exclude_session_id=None, output_dir=str(tmp_path))
        assert "2026-02-18" in result
        assert Path(result["2026-02-18"]).exists()
        # Sidecar should also exist
        sidecar = Path(result["2026-02-18"]).with_suffix(".sessions")
        assert sidecar.exists()
```

**Step 2: Implement**

Extract the duplicated pattern from lines 480-499 and 604-623 into a shared function:

```python
def pre_extract_transcripts(
    pending_dates: list[str],
    exclude_session_id: str | None = None,
    output_dir: str = "/tmp",
) -> dict[str, str]:
    """Pre-extract transcripts to temp files with sidecar.

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
                sidecar_path.write_text("\n".join(session_ids) + "\n", encoding="utf-8")
                extracted_files[date] = output_path
        except Exception:
            pass
    return extracted_files
```

Then replace both call sites (lines ~480-499 and ~604-623) with calls to `pre_extract_transcripts()`.

**Step 3: Run full suite**

```bash
python3 -m pytest tests/ -q
```

**Step 4: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "refactor: extract pre_extract_transcripts helper to eliminate duplication"
```

---

### Task 7: Fix decay.py import pattern + devtools.py re-imports (DRY #8, #18)

**Files:**
- Modify: `scripts/decay.py` (lines 23-46)
- Modify: `scripts/devtools.py` (lines 227-228, 350-351)

**Step 1: Fix decay.py**

Replace the try/except ImportError block (lines 23-46) with the standard `sys.path.insert` pattern used everywhere else:

```python
import sys
from pathlib import Path

script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    check_python_version,
    get_global_memory_file,
    get_memory_dir,
    get_project_memory_dir,
    get_projects_index_file,
    load_json_file,
    load_settings,
    project_name_to_filename,
)
```

**Step 2: Fix devtools.py**

Move `import re` to module-level imports (line ~16). Remove the `import re` from inside `cmd_mark_routed` (line ~227) and `cmd_validate_ltm` (line ~350).

Also move `import dataclasses` to module-level in `project_manager.py` (line ~1472).

**Step 3: Run full suite**

```bash
python3 -m pytest tests/ -q
```

**Step 4: Commit**

```bash
git add scripts/decay.py scripts/devtools.py scripts/project_manager.py
git commit -m "refactor: standardize import patterns across scripts"
```

---

### Task 8: Best practices cleanup (Issues #12-16)

**Files:**
- Modify: `scripts/load_memory.py` (line ~500)
- Modify: `scripts/project_manager.py` (lines ~718, ~1145, ~1340)
- Modify: `scripts/memory_utils.py` (lines ~393-411)
- Modify: `scripts/transcript_ops.py` (line ~42)

**Step 1: Fix bare except in load_memory.py**

Line ~500: Replace `except Exception: pass` with:
```python
except Exception as e:
    print(f"Warning: Failed to extract {date}: {e}", file=sys.stderr)
```
(Or, if Task 6 was done, this moves into the extracted helper.)

**Step 2: Narrow exception handling in project_manager.py**

Lines ~1145 and ~1340: Replace `except Exception as e:` with:
```python
except (IOError, OSError, shutil.Error) as e:
```

**Step 3: Fix variable shadowing in project_manager.py**

Line ~718: Rename the loop variable:
```python
for filepath in files:
    filepath = Path(filepath)
```

**Step 4: Add type annotation in transcript_ops.py**

Line ~42: Add `Any` type:
```python
def extract_text_content(content: Any) -> str:
```
(Import `Any` from `typing` at the top.)

**Step 5: Use context manager for FileLock in memory_utils.py**

Lines ~393-411 in `add_captured_session`: Replace manual acquire/release with `with`:
```python
with FileLock(captured_file.parent / ".captured.lock", timeout=5.0):
    if captured_set is not None:
        if session_id in captured_set:
            return
    else:
        captured = get_captured_sessions()
        if session_id in captured:
            return
    with open(captured_file, "a", encoding="utf-8") as f:
        f.write(f"{session_id}\n")
```

**Step 6: Run full suite**

```bash
python3 -m pytest tests/ -q
```

**Step 7: Commit**

```bash
git add scripts/load_memory.py scripts/project_manager.py scripts/memory_utils.py scripts/transcript_ops.py
git commit -m "refactor: best practices cleanup (error handling, types, shadowing)"
```

---

### Task 9: Create GitHub issues for deferred items

Create issues for items deferred from the audit:

1. **install.py duplicates memory_utils.py** (#1) — install.py redefines `get_claude_dir()`, `get_memory_dir()`, `load_json_file()`, `save_json_file()`, `check_python_version()`, `MIN_PYTHON`. Refactoring is risky because install.py runs before symlinks exist.

2. **sessions-index.json parsing duplicated** (#4) — `indexing.py:_load_sessions_index()` and `project_manager.py:get_original_path_from_folder()` parse the same file with overlapping logic.

3. **Add `__all__` exports** (#19) — Define public API in `memory_utils.py` and other modules.

4. **Split `_build_synthesis_prompt`** (#20) — The 200-line function could be split into smaller focused functions.

```bash
gh issue create --title "..." --body "..."
```

---

## Dependency Order

Tasks 1-3 are bug fixes (independent, highest priority).
Task 4 is a prerequisite for Task 5 (ISO helpers used by devtools).
Tasks 5-8 are independent of each other.
Task 9 is independent (just GitHub issues).

Parallelizable groups:
- **Group A** (bugs, independent): Tasks 1, 2, 3
- **Group B** (DRY, sequential): Task 4, then Task 5
- **Group C** (independent cleanup): Tasks 6, 7, 8
- **Group D** (GitHub issues): Task 9
