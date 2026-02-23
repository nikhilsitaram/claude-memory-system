# Remove .captured File Tracking

**Date:** 2026-02-23
**Status:** Proposed
**Branch:** feature/zero-tool-synthesis

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
def list_recent_sessions(
    max_age_days: int = 7,
    min_file_size: int = MIN_SESSION_SIZE_BYTES,
    exclude_session_id: str | None = None,
    verify_content: bool = False,
) -> list[SessionInfo]:
```

The mtime check happens inside `list_all_sessions()` (or as a filter on its output) using `session.file_mtime`. Sessions older than `max_age_days` are excluded before any state comparison occurs.

Why mtime instead of a "done" set:
- `list_all_sessions()` already stats every `.jsonl` file to get `file_size` and `file_mtime`. The mtime is free.
- A 7-day window means at most a few hundred sessions to compare against state, not thousands of historical ones.
- The state file handles the rest: unchanged sessions (offset matches) are skipped, grown sessions get delta extraction, new sessions get full extraction. No "done" list needed.

The window must be >= the maximum gap between synthesis runs. Default of 7 days is generous -- even if a user doesn't open Claude for a week, the next synthesis picks up where it left off.

### Make extract_transcripts_incremental self-contained

`extract_transcripts_incremental()` currently calls `get_captured_sessions()` and `list_pending_sessions(captured)` internally. After this change, it calls `list_recent_sessions()` instead, removing its dependency on the captured set entirely. The function signature stays the same (it already doesn't expose `captured` as a parameter).

### Remove sidecar files

Sidecar `.sessions` files exist solely to feed `mark-captured`. With mark-captured gone, there is no consumer. Sidecar creation is removed from:
- `cmd_extract()` in `indexing.py` (the `--output` path's `.sessions` companion)
- `pre_extract_transcripts()` in `load_memory.py`
- `pre_extract_transcripts_incremental()` in `load_memory.py`

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

### Remove pre_extract_transcripts (non-incremental)

The non-incremental `pre_extract_transcripts()` in `load_memory.py` was already slated for removal (zero-tool-synthesis design: "Drop auto-extract fallback"). This change completes that by removing it. All extraction goes through `pre_extract_transcripts_incremental()`.

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
| `load_memory.py` | `pre_extract_transcripts()` |

### CLI subcommands removed

| Module | Command |
|--------|---------|
| `indexing.py` | `mark-captured` |
| `indexing.py` | `uncapture` (no captured set to uncapture from) |

### Functions modified

| Module | Function | Change |
|--------|----------|--------|
| `indexing.py` | `list_pending_sessions()` | Renamed to `list_recent_sessions()`, `captured` param replaced with `max_age_days` |
| `transcript_ops.py` | `extract_transcripts_incremental()` | Calls `list_recent_sessions()` instead of `get_captured_sessions()` + `list_pending_sessions()` |
| `load_memory.py` | `pre_extract_transcripts_incremental()` | Drops sidecar file creation |
| `load_memory.py` | `_build_preextracted_prompt()` | Drops sidecar path references |
| `load_memory.py` | `_find_projects_in_sidecars()` | Renamed/refactored to get project names from extracted data directly (no sidecar files to read) |
| `load_memory.py` | `main()` | Removes `remove_captured_session()` call on resume; removes non-incremental fallback |
| `synthesis.py` | `run_post_processing()` | Removes `sidecar_paths` param, removes mark-captured subprocess call |
| `synthesis.py` | `apply_results()` | Removes `sidecar_paths` param passthrough |
| `synthesis.py` | CLI `apply` | Removes `--sidecars` argument |

## Flow Comparison

### Before (6 steps)

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
4. Update state with new offsets
```

Steps 5-6 disappear. Step 1 becomes a simple mtime comparison instead of set-difference against a flat file.

## Recency Window Details

The `max_age_days` parameter (default 7) controls how far back to look for sessions. This interacts with a few things:

**Configurable?** No, not initially. The 7-day default is hardcoded. It can be made configurable in `settings.json` later if needed. The value only affects which sessions are candidates for state comparison -- the state file is the real arbiter of what gets extracted.

**What about sessions older than 7 days?** They are ignored by `list_recent_sessions()`. If they were already synthesized, their state entry persists harmlessly. If they were never synthesized (e.g., user installed the system after those sessions), they are lost -- but this is the same behavior as today (`.captured` never included them either, and they would have been synthesized on first run).

**State file growth.** Without `prune_captured_from_state()`, state entries accumulate. This is bounded: one entry per session, ~100 bytes each. A year of daily use (~1000 sessions) is ~100KB. Pruning entries older than `max_age_days` from the state file can be added as a follow-up if this becomes a concern, but it is not required for correctness.

**Interaction with `--exclude-session`:** Unchanged. The active session is still excluded by `exclude_session_id` parameter, independent of the mtime filter.

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| First run after upgrade re-processes all recent sessions | Acceptable one-time cost. State file is empty, all sessions within the 7-day window appear "new" and get full extraction. Produces correct output. |
| Recency window misses a session | Only sessions with mtime > 7 days ago. Those were either already synthesized (state has their offset) or predate the system. No data loss for active users. |
| State file corruption | Delete `.synthesis-state.json` to force full re-process of recent sessions. Same recovery story as today without `.captured`. |
| Session resume after long gap | State tracks byte offsets. A resumed session's file grows, its mtime updates (within the window), and delta extraction picks up new content. No auto-uncapture needed. |
| Stale state entries accumulate | Bounded by session count. ~100 bytes per entry. Can add periodic pruning as a follow-up. |

## Migration Path

1. Code changes remove all `.captured` references.
2. First synthesis after upgrade creates `.synthesis-state.json` fresh (if it doesn't exist) or uses existing state.
3. `.captured` file is left in place on disk (harmless, not read by anything). `install.py` can optionally clean it up, or it can be documented as safe to delete manually.
4. No settings changes required.

## Not In Scope

- Changing synthesis scheduling logic (still interval-based via `synthesis.intervalHours`)
- Changing daily file format or LTM routing
- Adding a `--reset-state` CLI command (follow-up)
- Making `max_age_days` configurable via settings.json (follow-up)
- State file pruning of old entries (follow-up)
