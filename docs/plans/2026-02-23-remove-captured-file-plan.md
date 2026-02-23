# Remove .captured File Tracking — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove the redundant `.captured` file tracking mechanism, replacing it with mtime-based recency filtering and relying on `.synthesis-state.json` as the sole source of truth for session processing state.

**Architecture:** Replace `list_pending_sessions(captured_set)` with `list_recent_sessions(max_age_days=7)` that filters by file mtime. Remove all sidecar `.sessions` file creation and mark-captured workflows. Add `prune_stale_state_entries()` to bound state file growth. Remove the non-incremental extraction path entirely.

**Tech Stack:** Python 3.11+, pytest, pathlib

**Design doc:** `docs/plans/2026-02-23-remove-captured-file-design.md`

---

### Task 1: Add `prune_stale_state_entries()` to memory_utils.py

**Files:**
- Modify: `scripts/memory_utils.py:761-766` (replace `prune_captured_from_state`)
- Test: `tests/test_memory_utils.py`

**Step 1: Write the failing test**

In `tests/test_memory_utils.py`, replace the `test_prune_captured_from_state` test (line 829) and add a new test class:

```python
class TestPruneStaleStateEntries:
    """Tests for prune_stale_state_entries."""

    def test_removes_old_entries(self, tmp_path):
        """Entries with mtime older than max_age_days are pruned."""
        from memory_utils import prune_stale_state_entries, save_synthesis_state

        state_file = tmp_path / ".synthesis-state.json"
        state = {"sessions": {
            "old-sess": {"offset": 100, "lines": 10, "last_synthesized": "2026-01-01T10:00:00Z"},
            "recent-sess": {"offset": 200, "lines": 20, "last_synthesized": "2026-02-22T10:00:00Z"},
        }}
        state_file.write_text(json.dumps(state))

        # Create session files: one old, one recent
        projects_dir = tmp_path / "projects" / "proj-hash"
        projects_dir.mkdir(parents=True)
        old_file = projects_dir / "old-sess.jsonl"
        old_file.write_text("data")
        import os, time
        old_time = time.time() - (8 * 86400)  # 8 days ago
        os.utime(old_file, (old_time, old_time))

        recent_file = projects_dir / "recent-sess.jsonl"
        recent_file.write_text("data")

        with mock.patch("memory_utils.get_synthesis_state_file", return_value=state_file), \
             mock.patch("memory_utils.get_projects_dir", return_value=tmp_path / "projects"):
            pruned = prune_stale_state_entries(max_age_days=7)

        assert pruned == 1
        result = json.loads(state_file.read_text())
        assert "old-sess" not in result["sessions"]
        assert "recent-sess" in result["sessions"]

    def test_removes_entries_for_missing_files(self, tmp_path):
        """Entries whose session files no longer exist are pruned."""
        from memory_utils import prune_stale_state_entries

        state_file = tmp_path / ".synthesis-state.json"
        state = {"sessions": {
            "gone-sess": {"offset": 100, "lines": 10, "last_synthesized": "2026-02-20T10:00:00Z"},
        }}
        state_file.write_text(json.dumps(state))

        # Empty projects dir — no session file exists
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        with mock.patch("memory_utils.get_synthesis_state_file", return_value=state_file), \
             mock.patch("memory_utils.get_projects_dir", return_value=projects_dir):
            pruned = prune_stale_state_entries(max_age_days=7)

        assert pruned == 1
        result = json.loads(state_file.read_text())
        assert result["sessions"] == {}

    def test_noop_when_all_recent(self, tmp_path):
        """No entries pruned when all are recent."""
        from memory_utils import prune_stale_state_entries

        state_file = tmp_path / ".synthesis-state.json"
        state = {"sessions": {
            "sess-1": {"offset": 100, "lines": 10, "last_synthesized": "2026-02-22T10:00:00Z"},
        }}
        state_file.write_text(json.dumps(state))

        projects_dir = tmp_path / "projects" / "proj-hash"
        projects_dir.mkdir(parents=True)
        (projects_dir / "sess-1.jsonl").write_text("data")

        with mock.patch("memory_utils.get_synthesis_state_file", return_value=state_file), \
             mock.patch("memory_utils.get_projects_dir", return_value=projects_dir):
            pruned = prune_stale_state_entries(max_age_days=7)

        assert pruned == 0

    def test_empty_state_noop(self, tmp_path):
        """Empty state file is a no-op."""
        from memory_utils import prune_stale_state_entries

        state_file = tmp_path / ".synthesis-state.json"
        state_file.write_text('{"sessions": {}}')

        with mock.patch("memory_utils.get_synthesis_state_file", return_value=state_file), \
             mock.patch("memory_utils.get_projects_dir", return_value=tmp_path / "projects"):
            pruned = prune_stale_state_entries(max_age_days=7)

        assert pruned == 0
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_memory_utils.py::TestPruneStaleStateEntries -v`
Expected: ImportError or AttributeError (function doesn't exist yet)

**Step 3: Implement `prune_stale_state_entries()` and remove captured functions**

In `scripts/memory_utils.py`:

1. **Remove these functions** (delete entirely):
   - `get_captured_file()` (lines 187-189)
   - `get_captured_sessions()` (lines 484-493)
   - `add_captured_session()` (lines 496-519)
   - `remove_captured_session()` (lines 522-544)
   - `prune_captured_from_state()` (lines 761-766)

2. **Update `__all__`** (lines 20-70): Remove `"get_captured_sessions"`, `"add_captured_session"`, `"remove_captured_session"`, `"prune_captured_from_state"`. Add `"prune_stale_state_entries"`.

3. **Add new function** where `prune_captured_from_state` was:

```python
def prune_stale_state_entries(max_age_days: int = 7) -> int:
    """Remove state entries for sessions older than max_age_days or missing from disk.

    Scans .synthesis-state.json and removes entries where the session's .jsonl
    file has mtime older than max_age_days or no longer exists on disk.

    Returns number of entries pruned.
    """
    state = load_synthesis_state()
    sessions = state.get("sessions", {})
    if not sessions:
        return 0

    cutoff = datetime.now(timezone.utc).timestamp() - (max_age_days * 86400)
    projects_dir = get_projects_dir()
    to_remove = []

    for sid in sessions:
        # Find the session file across project dirs
        found = False
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            session_file = proj_dir / f"{sid}.jsonl"
            if session_file.exists():
                found = True
                if session_file.stat().st_mtime < cutoff:
                    to_remove.append(sid)
                break
        if not found:
            to_remove.append(sid)

    for sid in to_remove:
        sessions.pop(sid, None)

    if to_remove:
        save_synthesis_state(state)

    return len(to_remove)
```

**Step 4: Remove captured test class, run all tests**

In `tests/test_memory_utils.py`:
- Remove `TestCapturedSessions` class (lines 210-257)
- Remove `test_prune_captured_from_state` method (lines 829-843)
- Remove imports: `add_captured_session`, `get_captured_sessions`, `remove_captured_session`, `prune_captured_from_state`
- Add import: `prune_stale_state_entries`

Run: `python3 -m pytest tests/test_memory_utils.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "refactor: replace captured functions with prune_stale_state_entries in memory_utils"
```

---

### Task 2: Rename `list_pending_sessions()` → `list_recent_sessions()` in indexing.py

**Files:**
- Modify: `scripts/indexing.py:63,65-77,239-269,462-698`
- Test: `tests/test_indexing.py`

**Step 1: Write the failing test**

In `tests/test_indexing.py`, replace `TestListPendingSessions` (lines 188-224) with:

```python
class TestListRecentSessions:
    def _make_sessions(self, ages_days=None):
        """Build sessions with controlled mtimes."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        return [
            make_session_info("recent-1", file_size=2000,
                              file_mtime=now - timedelta(days=1)),
            make_session_info("recent-2", file_size=3000,
                              file_mtime=now - timedelta(days=3)),
            make_session_info("old-1", file_size=2000,
                              file_mtime=now - timedelta(days=10)),
            make_session_info("small-1", file_size=500,
                              file_mtime=now - timedelta(days=1)),
        ]

    def test_filters_old_sessions(self):
        from indexing import list_recent_sessions
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(max_age_days=7)
            ids = {s.session_id for s in result}
            assert "recent-1" in ids
            assert "recent-2" in ids
            assert "old-1" not in ids

    def test_filters_small_sessions(self):
        from indexing import list_recent_sessions
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(max_age_days=7)
            ids = {s.session_id for s in result}
            assert "small-1" not in ids

    def test_excludes_session_id(self):
        from indexing import list_recent_sessions
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(
                max_age_days=7, exclude_session_id="recent-1"
            )
            ids = {s.session_id for s in result}
            assert "recent-1" not in ids
            assert "recent-2" in ids

    def test_default_window(self):
        from indexing import DEFAULT_RECENCY_WINDOW_DAYS
        assert DEFAULT_RECENCY_WINDOW_DAYS == 7

    def test_min_session_size_constant(self):
        assert MIN_SESSION_SIZE_BYTES == 1000
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_indexing.py::TestListRecentSessions -v`
Expected: ImportError (function doesn't exist yet)

**Step 3: Implement changes in indexing.py**

1. **Add constant** near `MIN_SESSION_SIZE_BYTES` (line 63):
```python
DEFAULT_RECENCY_WINDOW_DAYS = 7
```

2. **Replace `list_pending_sessions()`** (lines 239-269) with:
```python
def list_recent_sessions(
    max_age_days: int = DEFAULT_RECENCY_WINDOW_DAYS,
    min_file_size: int = MIN_SESSION_SIZE_BYTES,
    exclude_session_id: str | None = None,
    verify_content: bool = False,
) -> list[SessionInfo]:
    """
    List recent sessions eligible for synthesis.

    Filters by file modification time instead of a captured set.

    Args:
        max_age_days: Only include sessions modified within this many days
        min_file_size: Minimum file size in bytes (default MIN_SESSION_SIZE_BYTES)
        exclude_session_id: Optional session ID to exclude (e.g., the active session)
        verify_content: If True, parse JSONL to verify at least one assistant message exists

    Returns list of SessionInfo for sessions that:
    - Have mtime within max_age_days
    - Meet minimum file size threshold
    - Are not the excluded session
    - (If verify_content) contain at least one assistant message
    """
    all_sessions = list_all_sessions()
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    return [
        s
        for s in all_sessions
        if s.file_mtime >= cutoff
        and s.file_size >= min_file_size
        and s.session_id != exclude_session_id
        and (not verify_content or has_assistant_message(s.transcript_path))
    ]
```

3. **Add `timedelta` import** (line 38): Add `timedelta` to the datetime import.

4. **Update `__all__`** (lines 65-77): Replace `"list_pending_sessions"` with `"list_recent_sessions"`, `"DEFAULT_RECENCY_WINDOW_DAYS"`.

5. **Remove imports from memory_utils** (lines 48-60): Remove `add_captured_session`, `get_captured_sessions`, `prune_captured_from_state`, `remove_captured_session`.

6. **Delete these functions entirely:**
   - `cmd_extract()` (lines 462-494)
   - `cmd_mark_captured()` (lines 517-592)
   - `cmd_uncapture()` (lines 595-607)
   - `cmd_uncapture_date()` (lines 610-638)

7. **Rename `cmd_list_pending()`** (lines 504-514) to `cmd_list_recent()`:
```python
def cmd_list_recent(args: argparse.Namespace) -> int:
    """Handle list-recent command."""
    from transcript_ops import get_recent_days
    days = get_recent_days()
    if days:
        print("Recent transcript days:")
        for day in days:
            print(f"  {day}")
    else:
        print("No recent transcripts.")
    return 0
```

8. **Update argparse subparsers** (lines 651-698): Remove `extract`, `mark-captured`, `uncapture`, `uncapture-date` parsers. Rename `list-pending` to `list-recent`:
```python
    list_parser = subparsers.add_parser(
        "list-recent", help="List days with recent transcripts"
    )
    list_parser.set_defaults(func=cmd_list_recent)
```

**Step 4: Update test imports and remove old tests**

In `tests/test_indexing.py`:
- Remove `TestListPendingSessions` class (lines 188-224)
- Remove `TestMarkCapturedPrunesState` class (lines 668-714)
- Update imports: remove `list_pending_sessions`, add nothing (tests import inline)

Run: `python3 -m pytest tests/test_indexing.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add scripts/indexing.py tests/test_indexing.py
git commit -m "refactor: rename list_pending_sessions to list_recent_sessions with mtime filtering"
```

---

### Task 3: Update transcript_ops.py — remove old functions, use `list_recent_sessions()`

**Files:**
- Modify: `scripts/transcript_ops.py:15-41,178-284,404-420`
- Test: `tests/test_transcript_ops.py`

**Step 1: Update tests first**

In `tests/test_transcript_ops.py`:

1. **Remove** `TestExtractTranscripts` class (lines 25-63) — function being deleted
2. **Remove** `TestGetPendingDays` class (lines 70-101) — function being deleted
3. **Update** `TestExtractTranscriptsIncremental` — change mocks from `get_captured_sessions` + `list_pending_sessions` to `list_recent_sessions`:

For every test in `TestExtractTranscriptsIncremental`, replace:
```python
        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
```
with:
```python
        with mock.patch("transcript_ops.list_recent_sessions", return_value=[session]), \
```

And for `test_exclude_session_id`, update the assertion:
```python
    def test_exclude_session_id(self, tmp_path):
        """Exclude session ID is forwarded to list_recent_sessions."""
        from transcript_ops import extract_transcripts_incremental

        state = {"sessions": {}}

        with mock.patch("transcript_ops.list_recent_sessions", return_value=[]) as mock_lrs:
            extract_transcripts_incremental(state, exclude_session_id="skip-me")

        mock_lrs.assert_called_once()
        assert mock_lrs.call_args[1].get("exclude_session_id") == "skip-me"
```

4. **Update imports** in test file: Remove `extract_transcripts`, `get_pending_days` from imports.

**Step 2: Update transcript_ops.py**

1. **Update imports** (lines 26-27): Replace:
```python
from indexing import get_session_date, list_pending_sessions
from memory_utils import get_captured_sessions
```
with:
```python
from indexing import get_session_date, list_recent_sessions
```

2. **Delete** `extract_transcripts()` (lines 178-218)

3. **Update** `extract_transcripts_incremental()` (lines 242-243): Replace:
```python
    captured = get_captured_sessions()
    pending = list_pending_sessions(captured, exclude_session_id=exclude_session_id)
```
with:
```python
    pending = list_recent_sessions(exclude_session_id=exclude_session_id)
```

4. **Delete** `get_pending_days()` (lines 404-420)

5. **Update `__all__`** (lines 29-41): Remove `"extract_transcripts"` and `"get_pending_days"`.

**Step 3: Add `get_recent_days()` replacement**

Add after `extract_transcripts_incremental()`:

```python
def get_recent_days(exclude_session_id: str | None = None) -> list[str]:
    """List all days that have recent transcripts.

    Args:
        exclude_session_id: Optional session ID to exclude
    """
    recent = list_recent_sessions(
        exclude_session_id=exclude_session_id, verify_content=True
    )
    days = set()
    for session in recent:
        days.add(get_session_date(session))
    return sorted(days)
```

Add `"get_recent_days"` to `__all__`.

**Step 4: Add test for `get_recent_days()`**

```python
class TestGetRecentDays:
    def test_returns_sorted_dates(self):
        from transcript_ops import get_recent_days
        sessions = [make_session_info("s1"), make_session_info("s2")]
        with mock.patch("transcript_ops.list_recent_sessions", return_value=sessions), \
             mock.patch("transcript_ops.get_session_date", side_effect=["2026-02-05", "2026-02-03"]):
            result = get_recent_days()
            assert result == ["2026-02-03", "2026-02-05"]

    def test_deduplicates_dates(self):
        from transcript_ops import get_recent_days
        sessions = [make_session_info("s1"), make_session_info("s2")]
        with mock.patch("transcript_ops.list_recent_sessions", return_value=sessions), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-05"):
            result = get_recent_days()
            assert result == ["2026-02-05"]

    def test_empty_when_none_recent(self):
        from transcript_ops import get_recent_days
        with mock.patch("transcript_ops.list_recent_sessions", return_value=[]):
            result = get_recent_days()
            assert result == []
```

**Step 5: Run tests**

Run: `python3 -m pytest tests/test_transcript_ops.py -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add scripts/transcript_ops.py tests/test_transcript_ops.py
git commit -m "refactor: remove non-incremental extraction, use list_recent_sessions in transcript_ops"
```

---

### Task 4: Update synthesis.py — remove sidecar handling, add state pruning

**Files:**
- Modify: `scripts/synthesis.py:318-374,377-419,422-444`
- Test: `tests/test_synthesis.py`

**Step 1: Update tests**

In `tests/test_synthesis.py`:

1. **Update `TestRunPostProcessing`** (lines 382-443):
   - Remove `test_marks_captured_sessions` — mark-captured no longer called
   - Update `test_cleans_up_temp_files` — no sidecar_paths param
   - Remove `test_skips_missing_sidecar` — no sidecars
   - Add test for state pruning

```python
class TestRunPostProcessing:
    def test_cleans_up_temp_files(self, tmp_path):
        """Removes extract temp files."""
        extract = tmp_path / "extract.txt"
        extract.write_text("data")

        with patch("synthesis.subprocess.run"), \
             patch("synthesis.prune_stale_state_entries"):
            run_post_processing(extract_paths=[str(extract)])

        assert not extract.exists()

    def test_prunes_stale_state(self):
        """Calls prune_stale_state_entries during post-processing."""
        with patch("synthesis.subprocess.run"), \
             patch("synthesis.prune_stale_state_entries") as mock_prune:
            run_post_processing(extract_paths=[])

        mock_prune.assert_called_once()

    def test_updates_timestamp(self, tmp_path):
        """Writes .last-synthesis timestamp file."""
        with patch("synthesis.subprocess.run"), \
             patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path):
            run_post_processing(extract_paths=[])

        ts_file = tmp_path / ".last-synthesis"
        assert ts_file.exists()
```

2. **Update `TestApplyResults`** (lines 445-516): Remove `sidecar_paths` from `apply_results()` calls:
```python
        apply_results(output_file=str(output_file), extract_paths=[])
```
and:
```python
        apply_results(str(output_file), [])
```

3. **Update `TestApplyResultsWithOffsets`** similarly — remove `sidecar_paths` param.

4. **Update imports**: Add `prune_stale_state_entries` mock target.

**Step 2: Update synthesis.py**

1. **Add import**: `from memory_utils import prune_stale_state_entries` (near existing memory_utils imports).

2. **Update `run_post_processing()`** signature and body:
```python
def run_post_processing(
    extract_paths: list[str],
    offsets_json: str | None = None,
) -> None:
    """Run state pruning, cleanup, decay, validation, and timestamp update."""
    from datetime import datetime, timezone

    # Prune stale state entries
    try:
        prune_stale_state_entries()
    except Exception:
        pass  # Non-critical

    # Cleanup temp files
    paths_to_clean = list(extract_paths)
    if offsets_json:
        paths_to_clean.append(offsets_json)
    for path in paths_to_clean:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    # Run mark-routed (deterministic, from devtools)
    subprocess.run(
        [sys.executable, str(script_dir / "devtools.py"), "mark-routed"],
        capture_output=True,
        timeout=30,
    )

    # Validate LTM
    subprocess.run(
        [sys.executable, str(script_dir / "devtools.py"), "validate-ltm"],
        capture_output=True,
        timeout=30,
    )

    # Run decay
    subprocess.run(
        [sys.executable, str(script_dir / "decay.py")],
        capture_output=True,
        timeout=60,
    )

    # Update timestamp
    ts_file = get_memory_dir() / ".last-synthesis"
    ts_file.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
```

3. **Update `apply_results()`** signature:
```python
def apply_results(
    output_file: str,
    extract_paths: list[str],
    offsets_json: str | None = None,
) -> None:
```
And update the `run_post_processing` call at the end:
```python
    run_post_processing(extract_paths, offsets_json=offsets_json)
```

4. **Update CLI `apply` subcommand** — remove `--sidecars`:
```python
    apply_parser = sub.add_parser("apply", help="Apply synthesis output")
    apply_parser.add_argument("output_file", help="Path to synthesis output file")
    apply_parser.add_argument("--extracts", nargs="*", default=[], help="Extract file paths to clean up")
    apply_parser.add_argument("--offsets-json", default=None, help="Path to session offsets JSON for state update")
```

5. **Update CLI dispatch** — remove `args.sidecars`:
```python
    if args.command == "apply":
        apply_results(
            args.output_file,
            args.extracts,
            offsets_json=getattr(args, "offsets_json", None),
        )
```

**Step 3: Run tests**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "refactor: remove sidecar handling from synthesis, add state pruning"
```

---

### Task 5: Update load_memory.py — remove sidecars, captured, non-incremental path

**Files:**
- Modify: `scripts/load_memory.py` (multiple sections)
- Test: `tests/test_load_memory.py`

This is the largest task. It has 5 sub-steps.

**Step 5a: Remove `pre_extract_transcripts()` and update imports**

Delete `pre_extract_transcripts()` (lines 583-618).

Remove from imports:
- `remove_captured_session` from memory_utils imports (line 46)
- `extract_transcripts`, `format_transcripts_for_output`, `get_pending_days` from transcript_ops imports (lines 48-54)

Add to transcript_ops imports:
- `get_recent_days` (replacing `get_pending_days`)

Remove from test imports and delete `TestPreExtractTranscripts` class (lines 427-507).

**Step 5b: Update `pre_extract_transcripts_incremental()` — drop sidecar creation**

In `pre_extract_transcripts_incremental()` (lines 621-673), remove the sidecar creation block (lines 661-663):
```python
        # DELETE THESE LINES:
        sidecar_path = Path(output_path).with_suffix(".sessions")
        session_ids = [s["session_id"] for s in sessions]
        sidecar_path.write_text("\n".join(session_ids) + "\n", encoding="utf-8")
```

Update test `TestPreExtractTranscriptsIncremental`:
- Remove `test_creates_sidecar` test
- Keep other tests (they don't depend on sidecars)

**Step 5c: Replace `_find_projects_in_sidecars()` with `_find_projects_in_extracts()`**

Delete `_find_projects_in_sidecars()` (lines 487-528). Add replacement:

```python
def _find_projects_in_extracts(daily_data: dict[str, list[dict]]) -> set[str]:
    """Find project names from extracted session data.

    Reads project_path from each session dict in the extraction output
    and maps to project names via projects-index.

    Args:
        daily_data: Dict mapping date -> list of session dicts
            (each with 'session_id' and 'project_path' keys)

    Returns set of project names that had sessions extracted.
    """
    projects_index = load_json_file(get_projects_index_file(), {})

    # Build reverse lookup: encoded dir name -> project name
    encoded_to_name: dict[str, str] = {}
    for data in projects_index.get("projects", {}).values():
        name = data.get("name", "")
        if name:
            for enc in data.get("encodedPaths", []):
                encoded_to_name[enc] = name

    # Collect session IDs and their project hashes
    session_project_hashes: set[str] = set()
    for sessions in daily_data.values():
        for s in sessions:
            # project_path is the encoded dir name from SessionInfo
            pp = s.get("project_path")
            if pp:
                session_project_hashes.add(pp)

    # Map encoded paths to project names
    result: set[str] = set()
    for encoded in session_project_hashes:
        name = encoded_to_name.get(encoded)
        if name:
            result.add(name)
    return result
```

Note: The `project_path` field in session dicts may be the original path (e.g., `/home/user/project`) or encoded name depending on the extraction path. Check the actual data flow: `extract_transcripts_incremental()` puts `session.project_path` which comes from `SessionInfo.project_path` (the original path like `/home/user/project`). The projects-index keys are original paths. So the lookup is simpler:

```python
def _find_projects_in_extracts(daily_data: dict[str, list[dict]]) -> set[str]:
    """Find project names from extracted session data.

    Args:
        daily_data: Dict mapping date -> list of session dicts

    Returns set of project names that had sessions extracted.
    """
    projects_index = load_json_file(get_projects_index_file(), {})
    projects = projects_index.get("projects", {})

    # Collect unique project paths from sessions
    project_paths: set[str] = set()
    for sessions in daily_data.values():
        for s in sessions:
            pp = s.get("project_path")
            if pp:
                project_paths.add(pp)

    # Map to project names
    result: set[str] = set()
    for path in project_paths:
        data = projects.get(path)
        if data and data.get("name"):
            result.add(data["name"])
    return result
```

**Step 5d: Update `_build_embedded_files()` and `_build_preextracted_prompt()`**

In `_build_embedded_files()` (lines 531-580):
- Change `_find_projects_in_sidecars(extracted_files)` to `_find_projects_in_extracts(daily_data)`
- This requires passing `daily_data` instead of (or in addition to) `extracted_files`
- Update the function signature to accept `daily_data: dict[str, list[dict]] | None = None`

In `_build_preextracted_prompt()` (lines 315-460):
- Remove the sidecar path computation block (lines 385-392)
- Remove `--sidecars {sidecars_arg}` from the synthesis.py apply command (line 442)
- The Bash command becomes: `python3 $HOME/.claude/scripts/synthesis.py apply {output_filename} --extracts {extracts_arg}{offsets_arg}`

**Step 5e: Update `main()` — remove resume uncapture, remove non-incremental fallback**

In `main()`:
1. Remove the auto-uncapture block (lines 691-693):
```python
    # DELETE:
    if source == "resume" and current_session_id:
        remove_captured_session(current_session_id)
```

2. Remove the non-incremental fallback (lines 732-739). Replace with just the incremental path:
```python
        extracted_files, session_offsets = pre_extract_transcripts_incremental(
            pending_dates, exclude_session_id=current_session_id
        )
```

3. Update the call to `get_pending_days` to `get_recent_days`.

**Step 5f: Update all tests in test_load_memory.py**

- Remove `TestPreExtractTranscripts` (lines 427-507)
- Update `TestPreExtractTranscriptsIncremental` — remove sidecar assertions
- Replace `TestFindProjectsInSidecars` (lines 901-1002) with `TestFindProjectsInExtracts`:

```python
class TestFindProjectsInExtracts:
    """Tests for _find_projects_in_extracts helper."""

    def test_maps_project_path_to_name(self, tmp_path):
        index = {"projects": {
            "/home/user/myproject": {"name": "myproject", "encodedPaths": ["-home-user-myproject"]}
        }}
        with mock.patch("load_memory.get_projects_index_file") as mock_idx:
            mock_idx.return_value = tmp_path / "index.json"
            (tmp_path / "index.json").write_text(json.dumps(index))
            result = _find_projects_in_extracts({
                "2026-02-01": [{"session_id": "s1", "project_path": "/home/user/myproject"}]
            })
        assert result == {"myproject"}

    def test_multiple_projects(self, tmp_path):
        index = {"projects": {
            "/proj/a": {"name": "alpha", "encodedPaths": []},
            "/proj/b": {"name": "beta", "encodedPaths": []},
        }}
        with mock.patch("load_memory.get_projects_index_file") as mock_idx:
            mock_idx.return_value = tmp_path / "index.json"
            (tmp_path / "index.json").write_text(json.dumps(index))
            result = _find_projects_in_extracts({
                "2026-02-01": [
                    {"session_id": "s1", "project_path": "/proj/a"},
                    {"session_id": "s2", "project_path": "/proj/b"},
                ]
            })
        assert result == {"alpha", "beta"}

    def test_empty_data_returns_empty(self):
        with mock.patch("load_memory.get_projects_index_file") as mock_idx:
            mock_idx.return_value = Path("/nonexistent/index.json")
            result = _find_projects_in_extracts({})
        assert result == set()

    def test_unknown_project_skipped(self, tmp_path):
        index = {"projects": {}}
        with mock.patch("load_memory.get_projects_index_file") as mock_idx:
            mock_idx.return_value = tmp_path / "index.json"
            (tmp_path / "index.json").write_text(json.dumps(index))
            result = _find_projects_in_extracts({
                "2026-02-01": [{"session_id": "s1", "project_path": "/unknown/path"}]
            })
        assert result == set()
```

- Update `TestBuildEmbeddedFiles` — mock `_find_projects_in_extracts` instead of `_find_projects_in_sidecars`
- Update imports: remove `pre_extract_transcripts`, `_find_projects_in_sidecars`; add `_find_projects_in_extracts`

**Step 5g: Run all tests**

Run: `python3 -m pytest tests/test_load_memory.py -v`
Expected: All PASS

**Step 5h: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "refactor: remove sidecars and captured tracking from load_memory"
```

---

### Task 6: Update devtools.py — replace captured/pending display with state-based status

**Files:**
- Modify: `scripts/devtools.py:172-224,432-435`
- Test: `tests/test_devtools.py` (if extract-debug tests exist)

**Step 1: Update `cmd_extract_debug()`**

Replace the captured-status display (lines 178-202) with state-based status:

```python
def cmd_extract_debug(args: argparse.Namespace) -> int:
    """Debug transcript extraction for a specific day."""
    do_all = args.mode == "all"
    day = args.day or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    sys.path.insert(0, str(REPO_DIR / "scripts"))
    from indexing import get_session_date, list_all_sessions
    from memory_utils import load_synthesis_state

    state = load_synthesis_state()
    sessions_state = state.get("sessions", {})

    if do_all or args.mode in ("sessions", "state"):
        all_sessions = list_all_sessions()
        day_sessions = [s for s in all_sessions if get_session_date(s) == day]

        if args.mode != "state":
            print(f"Sessions for {day}:")
            for s in day_sessions:
                prev = sessions_state.get(s.session_id)
                if prev and s.file_size == prev.get("offset", 0):
                    status = "unchanged"
                elif prev and s.file_size > prev.get("offset", 0):
                    status = "grown"
                elif prev:
                    status = "shrunk"
                else:
                    status = "new"
                print(f"  {s.session_id[:12]}...  {s.file_size:>8,} bytes  [{status}]")
            if not day_sessions:
                print("  None")
            print()

        if do_all or args.mode == "state":
            print(f"Synthesized sessions for {day}:")
            synth = [s for s in day_sessions if s.session_id in sessions_state]
            for s in synth:
                prev = sessions_state[s.session_id]
                print(f"  {s.session_id}  offset={prev.get('offset')} lines={prev.get('lines')}")
            if not synth:
                print("  None")
            print()

    if do_all or args.mode in ("extract", "content"):
        from transcript_ops import extract_transcripts_incremental
        print(f"Extracting transcripts for {day}...")
        daily_data = extract_transcripts_incremental(state)
        if day in daily_data:
            sessions = daily_data[day]
            print(f"  {len(sessions)} session(s) with content")
            for s in sessions:
                print(f"  {s['session_id'][:12]}...  {s['message_count']} messages  [{s['mode']}]")
                if do_all or args.mode == "content":
                    for msg in s["messages"][:2]:
                        preview = msg["content"][:200]
                        if len(msg["content"]) > 200:
                            preview += "..."
                        print(f"    [{msg['role'].upper()}] {preview}")
        else:
            print("  No extractable content")
        print()

    return 0
```

2. **Update argparse** — replace `"captured"` choice with `"state"`:
```python
    ed.add_argument("--mode", choices=["all", "sessions", "extract", "state", "content"], default="all")
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/test_devtools.py -v`
Expected: All PASS (devtools extract-debug tests may need updating if they exist)

**Step 3: Commit**

```bash
git add scripts/devtools.py tests/test_devtools.py
git commit -m "refactor: replace captured/pending with state-based status in devtools"
```

---

### Task 7: Update CLAUDE.md documentation

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update CLAUDE.md**

1. Remove references to `extract`, `mark-captured`, `uncapture` CLI commands from the testing section
2. Update `list-pending` to `list-recent`
3. Update the "Features Summary" table — remove "Safe capture workflow" row, update wording
4. Update "Key Implementation Details" section if it references `.captured`

Specifically in the testing bash block, replace:
```bash
python3 ~/.claude/scripts/indexing.py extract 2026-02-06 --output /tmp/test.txt  # Test extract (no marking)
python3 ~/.claude/scripts/indexing.py mark-captured --sidecar /tmp/test.sessions  # Test marking
```
with:
```bash
python3 ~/.claude/scripts/indexing.py list-recent  # Test recent session listing
```

**Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for captured file removal"
```

---

### Task 8: Run full test suite and verify

**Step 1: Run all tests**

Run: `python3 -m pytest tests/ -v`
Expected: All PASS, no import errors, no references to removed functions

**Step 2: Grep for any remaining captured references**

Run: `grep -r "captured" scripts/ tests/ --include="*.py" -l`
Expected: No hits (or only false positives in string literals like "captured" in log messages)

**Step 3: Verify clean import**

Run: `python3 -c "from memory_utils import *; from indexing import *; from transcript_ops import *; from synthesis import *; print('OK')"`
Expected: `OK`

**Step 4: Final commit if any stragglers found**

```bash
git add -A && git commit -m "chore: clean up remaining captured references"
```

---

## Execution Order Summary

| Task | Module | Key Change | Depends On |
|------|--------|------------|------------|
| 1 | memory_utils.py | Add `prune_stale_state_entries`, remove captured functions | — |
| 2 | indexing.py | Rename `list_pending_sessions` → `list_recent_sessions`, remove CLI commands | Task 1 |
| 3 | transcript_ops.py | Remove `extract_transcripts`, `get_pending_days`, use `list_recent_sessions` | Task 2 |
| 4 | synthesis.py | Remove sidecar handling, add state pruning call | Task 1 |
| 5 | load_memory.py | Remove sidecars, captured, non-incremental path | Tasks 1-4 |
| 6 | devtools.py | Replace captured/pending with state-based status | Tasks 1-2 |
| 7 | CLAUDE.md | Update documentation | Tasks 1-6 |
| 8 | Full verification | Run all tests, grep for stragglers | Tasks 1-7 |
