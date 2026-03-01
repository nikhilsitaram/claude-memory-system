# Remove .captured File Tracking

**Date:** 2026-02-23
**Status:** Approved
**Branch:** capture-refactor

## Problem

The memory system maintains two redundant mechanisms for tracking which sessions have been processed:

1. **`.captured` file** (`~/.claude/memory/.captured`) -- a flat text file listing session IDs that have been fully processed. `list_pending_sessions()` takes this set and filters it out. `mark-captured` (in `indexing.py`) appends IDs after synthesis, using sidecar `.sessions` files as input. Today's sessions are deliberately skipped by `mark-captured` to allow resumed sessions to contribute new content.

2. **`.synthesis-state.json`** (`~/.claude/memory/.synthesis-state.json`) -- per-session byte offset and line count (high water marks). Added by the incremental synthesis feature. Categorizes each session as unchanged (`offset == file_size`), grown (`offset < file_size`), or new (absent from state).

The state file subsumes the captured file:

| `.captured` concept | `.synthesis-state.json` equivalent |
|---------------------|-------------------------------------|
| Session is "captured" (done) | `offset == file_size` (unchanged) |
| Session is not captured (pending) | Not in state (new) |
| Session resumed after capture | N/A -- offset tracks position, delta extracts new content automatically |

With `.synthesis-state.json` in place, `.captured` adds no information. It does add complexity: sidecar files, `mark-captured` command, `prune_captured_from_state()`, auto-uncapture on resume, and a two-set filtering pipeline (`captured` set then state comparison).

## Design

### Replace captured-set filtering with mtime-based recency window

`list_pending_sessions()` currently accepts a `captured: set[str]` parameter and filters against it. The replacement, `list_recent_sessions()`, drops that parameter and instead filters by file modification time:

```python
DEFAULT_RECENCY_WINDOW_DAYS = 7

def list_recent_sessions(
    max_age_days: int = DEFAULT_RECENCY_WINDOW_DAYS,
    min_file_size: int = MIN_SESSION_SIZE_BYTES,
    exclude_session_id: str | None = None,
    verify_content: bool = False,
) -> list[SessionInfo]:
```

The mtime check happens inside `list_recent_sessions()` as a filter on `list_all_sessions()` output using `session.file_mtime`. Sessions older than `max_age_days` are excluded before any state comparison occurs.

Why mtime instead of a "done" set:
- `list_all_sessions()` already stats every `.jsonl` file to get `file_size` and `file_mtime`. The mtime is free.
- A 7-day window means at most a few hundred sessions to compare against state, not thousands of historical ones.
- The state file handles the rest: unchanged sessions (offset matches) are skipped, grown sessions get delta extraction, new sessions get full extraction. No "done" list needed.

The window must be >= the maximum gap between synthesis runs. Default of 7 days is generous -- even if a user doesn't open Claude for a week, the next synthesis picks up where it left off.

### Make extract_transcripts_incremental self-contained

`extract_transcripts_incremental()` currently calls `get_captured_sessions()` and `list_pending_sessions(captured)` internally. After this change, it calls `list_recent_sessions()` instead, removing its dependency on the captured set entirely. The function signature stays the same (it already doesn't expose `captured` as a parameter).

### Remove non-incremental extraction entirely

The non-incremental `extract_transcripts()` in `transcript_ops.py` and its caller `pre_extract_transcripts()` in `load_memory.py` are removed. All extraction goes through the incremental path (`extract_transcripts_incremental()` / `pre_extract_transcripts_incremental()`). The `extract` CLI command in `indexing.py` is also removed.

### Remove sidecar files

Sidecar `.sessions` files exist solely to feed `mark-captured`. With mark-captured gone, there is no consumer. Sidecar creation is removed from:
- `cmd_extract()` in `indexing.py` (deleted entirely with the `extract` CLI command)
- `pre_extract_transcripts()` in `load_memory.py` (deleted entirely)
- `pre_extract_transcripts_incremental()` in `load_memory.py`

### Replace _find_projects_in_sidecars with direct extraction

`_find_projects_in_sidecars()` currently reads session IDs from sidecar `.sessions` files, looks them up in `projects-index.json`, and returns project names. Without sidecars, this is replaced by `_find_projects_in_extracts()` which gets session IDs directly from the `extracted_files` data structure (each session dict already contains `session_id` and `project_path`). The replacement reads project names from the `project_path` field of each session in the extraction output, eliminating the sidecar→index lookup entirely.

### Remove auto-uncapture on resume

`load_memory.py` currently calls `remove_captured_session(current_session_id)` on resume events so that a resumed session can contribute new content. This is unnecessary when state tracks byte offsets -- a resumed session naturally grows past its stored offset and gets delta-extracted.

### Simplify post-processing

`run_post_processing()` in `synthesis.py` currently:
1. Runs `mark-captured --sidecar` for each sidecar path
2. Cleans up temp files (extracts + sidecars + offsets JSON)
3. Runs `mark-routed`
4. Validates LTM
5. Runs decay
6. Updates `.last-synthesis` timestamp

After this change, step 1 is removed. Step 2 simplifies (no sidecar paths to clean). The `--sidecars` CLI argument to `synthesis.py apply` is removed.

### Add state file pruning

Without `prune_captured_from_state()`, stale entries accumulate in `.synthesis-state.json`. Add `prune_stale_state_entries(max_age_days)` that removes entries for sessions whose files have mtime older than `max_age_days` (or no longer exist). Called during post-processing where `prune_captured_from_state()` used to be.

```python
def prune_stale_state_entries(max_age_days: int = DEFAULT_RECENCY_WINDOW_DAYS) -> int:
    """Remove state entries for sessions older than max_age_days or missing from disk.

    Returns number of entries pruned.
    """
```

### Rename list-pending CLI to list-recent

`list-pending` CLI semantics change from "not yet captured" to "recently modified". Rename to `list-recent` to match. The underlying function changes from `get_pending_days()` to a new implementation using `list_recent_sessions()`.

### Update devtools.py extract-debug

`cmd_extract_debug()` uses `get_captured_sessions()` to display captured/pending status. Replace with state-based status (unchanged/grown/new) which is more informative.

## What Gets Removed

### Functions deleted entirely

| Module | Function |
|--------|----------|
| `memory_utils.py` | `get_captured_file()` |
| `memory_utils.py` | `get_captured_sessions()` |
| `memory_utils.py` | `add_captured_session()` |
| `memory_utils.py` | `remove_captured_session()` |
| `memory_utils.py` | `prune_captured_from_state()` |
| `indexing.py` | `cmd_mark_captured()` |
| `indexing.py` | `cmd_uncapture()` |
| `indexing.py` | `cmd_uncapture_date()` |
| `indexing.py` | `cmd_extract()` |
| `transcript_ops.py` | `extract_transcripts()` |
| `transcript_ops.py` | `get_pending_days()` |
| `load_memory.py` | `pre_extract_transcripts()` |
| `load_memory.py` | `_find_projects_in_sidecars()` |

### CLI subcommands removed

| Module | Command |
|--------|---------|
| `indexing.py` | `extract` |
| `indexing.py` | `mark-captured` |
| `indexing.py` | `uncapture` |
| `indexing.py` | `uncapture-date` |

### CLI subcommands renamed

| Module | Old | New |
|--------|-----|-----|
| `indexing.py` | `list-pending` | `list-recent` |

### Functions modified

| Module | Function | Change |
|--------|----------|--------|
| `indexing.py` | `list_pending_sessions()` | Renamed to `list_recent_sessions()`, `captured` param replaced with `max_age_days` |
| `indexing.py` | `cmd_list_pending()` | Renamed to `cmd_list_recent()`, calls `list_recent_sessions()` directly |
| `transcript_ops.py` | `extract_transcripts_incremental()` | Calls `list_recent_sessions()` instead of `get_captured_sessions()` + `list_pending_sessions()` |
| `load_memory.py` | `pre_extract_transcripts_incremental()` | Drops sidecar file creation |
| `load_memory.py` | `_build_preextracted_prompt()` | Drops sidecar path references from synthesis.py apply command |
| `load_memory.py` | (new) `_find_projects_in_extracts()` | Replaces `_find_projects_in_sidecars()`, reads project_path from extracted data |
| `load_memory.py` | `_build_embedded_files()` | Calls `_find_projects_in_extracts()` instead of `_find_projects_in_sidecars()` |
| `load_memory.py` | `main()` | Removes `remove_captured_session()` call on resume; removes non-incremental fallback |
| `synthesis.py` | `run_post_processing()` | Removes `sidecar_paths` param, removes mark-captured call, adds `prune_stale_state_entries()` call |
| `synthesis.py` | `apply_results()` | Removes `sidecar_paths` param passthrough |
| `synthesis.py` | CLI `apply` | Removes `--sidecars` argument |
| `devtools.py` | `cmd_extract_debug()` | Replaces captured/pending display with state-based status (unchanged/grown/new) |

### Functions added

| Module | Function | Purpose |
|--------|----------|---------|
| `memory_utils.py` | `prune_stale_state_entries()` | Remove state entries for sessions older than recency window |
| `load_memory.py` | `_find_projects_in_extracts()` | Get project names from extracted data instead of sidecar files |

### __all__ exports updated

| Module | Removed | Added |
|--------|---------|-------|
| `memory_utils.py` | `get_captured_sessions`, `add_captured_session`, `remove_captured_session`, `prune_captured_from_state` | `prune_stale_state_entries` |
| `indexing.py` | `list_pending_sessions` | `list_recent_sessions`, `DEFAULT_RECENCY_WINDOW_DAYS` |
| `transcript_ops.py` | `extract_transcripts`, `get_pending_days` | (none) |

### Tests updated

| Test File | Changes |
|-----------|---------|
| `test_memory_utils.py` | Remove tests for captured functions; add tests for `prune_stale_state_entries()` |
| `test_indexing.py` | `TestListPendingSessions` → `TestListRecentSessions` (mtime-based); remove `TestMarkCapturedPrunesState`; remove extract/uncapture/uncapture-date tests |
| `test_transcript_ops.py` | Update `TestExtractTranscriptsIncremental` to not mock captured; remove tests for `extract_transcripts()` and `get_pending_days()` |
| `test_load_memory.py` | Remove `TestPreExtractTranscripts`; update `TestPreExtractTranscriptsIncremental` (no sidecar assertions); replace `TestFindProjectsInSidecars` with `TestFindProjectsInExtracts`; update `TestBuildEmbeddedFiles` |
| `test_synthesis.py` | Update `TestRunPostProcessing` (no mark-captured, add prune assertions); update `TestApplyResults` (no sidecars param) |

## Flow Comparison

### Before (7 steps)

```
1. get_captured_sessions()           → read .captured file into set
2. list_pending_sessions(captured)   → list all sessions, exclude captured set
3. extract_transcripts_incremental() → compare to state for delta/full/skip
4. Synthesize                        → write daily files, route to LTM
5. mark-captured via sidecar files   → append IDs to .captured
6. prune_captured_from_state()       → remove captured IDs from state file
7. Update state with new offsets
```

### After (4 steps)

```
1. list_recent_sessions(days=7)      → list sessions with recent mtime
2. extract_transcripts_incremental() → compare to state for delta/full/skip
3. Synthesize                        → write daily files, route to LTM
4. Update state with new offsets + prune stale entries
```

Steps 5-6 disappear. Step 1 becomes a simple mtime comparison instead of set-difference against a flat file.

## Recency Window Details

The `max_age_days` parameter (default 7, defined as `DEFAULT_RECENCY_WINDOW_DAYS`) controls how far back to look for sessions. This interacts with a few things:

**Configurable?** No, not initially. The 7-day default is a constant in `indexing.py`. It can be made configurable in `settings.json` later if needed. The value only affects which sessions are candidates for state comparison -- the state file is the real arbiter of what gets extracted.

**What about sessions older than 7 days?** They are ignored by `list_recent_sessions()`. If they were already synthesized, their state entry gets pruned (harmless). If they were never synthesized (e.g., user installed the system after those sessions), they are lost -- but this is the same behavior as today (`.captured` never included them either, and they would have been synthesized on first run).

**State file pruning.** `prune_stale_state_entries()` removes entries where the session file mtime is older than `max_age_days` or the file no longer exists. Called during post-processing. This replaces `prune_captured_from_state()` and keeps the state file bounded.

**Interaction with `--exclude-session`:** Unchanged. The active session is still excluded by `exclude_session_id` parameter, independent of the mtime filter.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| First run after upgrade re-processes all recent sessions | Acceptable one-time cost. State file is empty, all sessions within the 7-day window appear "new" and get full extraction. Produces correct output. |
| Recency window misses a session | Only sessions with mtime > 7 days ago. Those were either already synthesized (state has their offset) or predate the system. No data loss for active users. |
| State file corruption | Delete `.synthesis-state.json` to force full re-process of recent sessions. Same recovery story as today without `.captured`. |
| Session resume after long gap | State tracks byte offsets. A resumed session's file grows, its mtime updates (within the window), and delta extraction picks up new content. No auto-uncapture needed. |
| Removing `extract` CLI breaks user workflows | The `extract` command is a diagnostic tool documented in CLAUDE.md. Update CLAUDE.md to remove references. Users who need raw transcript data can access `.jsonl` files directly. |

## Migration Path

1. Code changes remove all `.captured` references.
2. First synthesis after upgrade creates `.synthesis-state.json` fresh (if it doesn't exist) or uses existing state.
3. `.captured` file is left in place on disk (harmless, not read by anything). `install.py` can optionally clean it up, or it can be documented as safe to delete manually.
4. CLAUDE.md updated to remove `extract`, `mark-captured`, `uncapture` CLI references and update `list-pending` to `list-recent`.
5. No settings changes required.

## Not In Scope

- Changing synthesis scheduling logic (still interval-based via `synthesis.intervalHours`)
- Changing daily file format or LTM routing
- Adding a `--reset-state` CLI command (follow-up)
- Making `max_age_days` configurable via settings.json (follow-up)
