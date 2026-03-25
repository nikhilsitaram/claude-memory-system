# Design: Synthesis Backfill & Working-Day Improvements

## Problem

New users install the memory system and have weeks/months of Claude Code session history in `~/.claude/projects/` that never gets synthesized. The DB starts empty despite rich history existing. Three root causes:

1. **No backfill mechanism** — `list_recent_sessions` has a 7 calendar-day window (`DEFAULT_RECENCY_WINDOW_DAYS`). Older sessions are invisible to synthesis.
2. **No session import** — users migrating from another machine have sessions in a backup directory with different path prefixes. No tooling exists to import them into `~/.claude/projects/` with correct path remapping.
3. **Calendar-day windows** — discovery, session continuity loading, and recent activity loading all use `timedelta(days=N)`. If a user takes a week off, sessions age out before synthesis runs. Working days (days with actual activity) are the right unit.
4. **Consolidated memories are immortal** — `consolidated=1` exempts memories from decay entirely. This creates permanently stale entries when the underlying knowledge becomes outdated. (Note: decay already correctly uses `COALESCE(last_accessed, created_at)` for age calculation — only the consolidated immunity needs fixing.)

## Goal

Users can synthesize their full session history into the memory DB with a single command, and the system uses activity-based time windows throughout.

## Success Criteria

1. A user with 300+ sessions across 2 months can run `/synthesize --backfill` and get a populated memory DB with correctly scoped memories per project.
2. Sessions from inactive periods (vacations, weekends) are still discovered and synthesized — they don't silently age out of the 7-day window.
3. Backfill uses sonnet for sessions within the last 7 project working days, haiku for everything older — reducing cost for bulk historical processing. A project you haven't touched in weeks gets haiku regardless of recent activity on other projects.
4. Session continuity loads the most recent session_context based on the last N project working days, not calendar days.
5. Recent activity loads memories from the last N global working days, not calendar days.
6. A consolidated memory that hasn't been accessed in 30+ days decays like any other memory.
8. A user can import sessions from a backup directory on a different machine with `--import-from <path>`, and sessions are correctly remapped to the current machine's path prefix and deduplicated.

## Architecture

### Working-Day Infrastructure

Replace the legacy `get_working_days()` (scans daily/*.md files) with two new functions that derive working days from session transcripts in `~/.claude/projects/`:

```
get_global_working_days(n) -> list[str]
    Returns the last N dates (YYYY-MM-DD) that had any session activity.
    Scans all project folders for .jsonl file mtimes.

get_project_working_days(project_scope, n) -> list[str]
    Returns the last N dates with activity in a specific project.
    Scans project folder(s) matching the scope (including worktrees).
```

Both return dates sorted newest-first. Cached per-session to avoid repeated filesystem scans.

### Changes to Existing Pipelines

**1. `list_recent_sessions` (indexing.py)**

Current: `cutoff = now - timedelta(days=max_age_days)` with `DEFAULT_RECENCY_WINDOW_DAYS = 7`

New: Default mode uses `get_global_working_days(7)` — collects the 7 most recent active dates, filters sessions to those dates. New setting `synthesis.recentWorkingDays` (default 7) replaces `DEFAULT_RECENCY_WINDOW_DAYS`.

Add `max_age_days=None` mode for backfill (no age filter at all).

`get_recent_days()` in `transcript_ops.py` wraps `list_recent_sessions()` — it must also accept and forward a `max_age_days=None` parameter for the backfill path.

**2. Session continuity loading (load_memory.py Tier 2)**

Current: `timedelta(days=7)` — loads session_context created within 7 calendar days.

New: `get_project_working_days(project_scope, 5)` — loads session_context from the last 5 project working days. Falls back to calendar if no working days found (new project).

**3. Recent activity loading (load_memory.py Tier 5)**

Current: `timedelta(days=3)` — loads memories created within 3 calendar days.

New: `get_global_working_days(3)` — loads memories from the last 3 globally active dates.

**4. Decay: remove consolidated immunity (decay.py)**

Current: `decay_data_points()` already correctly uses `COALESCE(last_accessed, created_at)` for age calculation. However, it skips `consolidated=1` memories entirely.

Change: Remove `AND consolidated != 1` from the decay query in `decay_data_points()` and `cleanup_near_zero_salience()`. Consolidated memories participate in decay like all others. Access reinforcement keeps valuable ones alive. `scope='user'` remains the only true exemption (profile data).

### Backfill Feature

New `/synthesize --backfill` command and `synthesis_cron.py --backfill` CLI flag.

**Scope options:**
- `--backfill` or `--backfill --all` — process all sessions regardless of age
- `--backfill --days N` — process sessions from the last N calendar days only

**Backfill bypasses the normal `get_recent_days()` → `extract_transcripts_incremental()` pipeline** (which has the 7-day working-day filter baked in). Instead, it calls `list_recent_sessions(max_age_days=None)` (or `max_age_days=N` for `--days N`) directly and builds its own project-grouped extraction flow.

**Flow:**
1. Discover sessions via `list_recent_sessions(max_age_days=None)` (or `max_age_days=N`)
2. Group sessions by project (resolved via `resolve_project_path_to_name`). Sessions that cannot be resolved to a named project fall into a `global` fallback bucket.
3. For each project, compute its last 7 working days via `get_project_working_days(scope, 7)`
4. **Report scope and ask for confirmation before proceeding:**
   ```
   Backfill scope: 12 projects, 325 sessions
     claude-caliper:    246 sessions (7 sonnet, 239 haiku)
     swyfft:             89 sessions (0 sonnet, 89 haiku)
     claude-memory-system: 44 sessions (5 sonnet, 39 haiku)
     ...
   Estimated API calls: 48 (12 sonnet, 36 haiku)
   Proceed? [y/N]
   ```
5. For each project-date batch, select model based on the session date:
   - Session date is within the project's last 7 working days → sonnet
   - Session date is older → haiku
6. Build synthesis prompt and run `claude -p` with the selected model
7. Apply ops via `apply_memory_ops_v3`, write session_context, run decay + consolidation
8. Update synthesis state so processed sessions aren't re-synthesized

**New setting:** `synthesis.backfill.recentWorkingDays` (default 7) — per project, sessions within this many of the project's working days get sonnet, older get haiku.

**Progress reporting:**
```
Backfill: 12 projects, 325 sessions (recent 7 working days: sonnet, older: haiku)
  claude-caliper (246 sessions)... 7 batches [5 sonnet, 2 haiku]
  swyfft (12 sessions)... 3 batches [0 sonnet, 3 haiku]
  claude-memory-system (44 sessions)... 5 batches [3 sonnet, 2 haiku]
  ...
Done: 436 memories, 312 entities, 15 session_contexts
```

### Batch Strategy

Current synthesis batches by date (one prompt per day across all projects). Backfill batches **by project then date** because:
- Model choice is per-project-date (sonnet for that project's recent working days, haiku for older) — batching by project naturally groups same-model sessions
- Scope coherence is better (LLM sees one project's context at a time)
- Progress is more meaningful per-project

For ongoing synthesis (non-backfill), keep the current date-batching — it's simpler and always uses the configured model.

## Key Decisions

1. **Working days from session mtimes, not daily files** — daily/*.md is legacy v1/v2. Session transcripts in `~/.claude/projects/` are the v3 source of truth for activity.
2. **Remove consolidated immunity from decay** — decay already uses `COALESCE(last_accessed, created_at)` correctly. The only change is removing the `consolidated != 1` exemption. Access reinforcement is the correct mechanism to keep valuable memories alive, not a blanket flag.
3. **Per-project working-day cutoff for model selection** — sessions within the last 7 of that project's working days get sonnet, older get haiku. A project idle for weeks gets cheap haiku processing even if other projects are active.
4. **Backfill is explicit, not automatic** — users opt in via `--backfill`. Normal synthesis continues using the working-day recency window.
5. **Copy, don't move, for session import** — source directories (backup drives, cloud storage) may not always be available. Always copy with mtime preservation.

### Session Import

New `--import-from <path>` flag on `/synthesize --backfill` and `synthesis_cron.py --backfill`.

**Problem:** Users migrating from another machine (or restoring from backup) have session transcripts in a non-standard directory with different path prefixes. Example: backup from a Linux machine has folders like `-home-nsitaram-swyfft/` but the current macOS machine uses `-Users-nsitaram-swyfft/`.

**Flow:**
1. Scan `<source-path>` for project folders containing `.jsonl` files
2. For each folder, detect the home directory prefix (e.g., `-home-nsitaram-`) by finding the common prefix across all folder names
3. Map to the current machine's prefix (e.g., `-Users-nsitaram-`) using `Path.home()` to derive the current prefix
4. For each source folder:
   - Compute the target folder name (remapped prefix)
   - Create target folder in `~/.claude/projects/` if it doesn't exist
   - Copy `.jsonl` files that don't already exist in target (deduplicate by session UUID filename)
   - Preserve original mtimes for accurate date detection
5. Report: `Imported 442 sessions from 15 projects (87 skipped as duplicates)`
6. Rebuild projects index via `indexing.py build-index`
7. Continue with normal backfill synthesis on the combined session set

**Path prefix detection:** The source directory's folder names encode filesystem paths. The home prefix is the longest common prefix of all folder names that ends before a project-distinguishing segment. For example, given folders `-home-nsitaram-swyfft/` and `-home-nsitaram-personal-investing/`, the prefix is `-home-nsitaram-`.

**Edge cases:**
- Source and target have the same prefix (same machine backup) — copies proceed without remapping
- Mixed prefixes in source — group by prefix, remap each independently
- Session UUID collision (same session in both) — skip, log as duplicate
- Source folder has no `.jsonl` files — skip silently
- **Directory structure mismatch** — the project may have been at a different path on the old machine (e.g., `/home/nsitaram/claude-memory-system/` vs `/Users/nsitaram/personal/claude-memory-system/`). Simple prefix swap produces `-Users-nsitaram-claude-memory-system` but the current machine has `-Users-nsitaram-personal-claude-memory-system`. Handle via: (1) check if remapped folder exists in target, (2) if not, fuzzy-match by project suffix (last path segment), (3) if still no match, create the remapped folder as-is and let project resolution handle it at synthesis time. Log mismatches for user review.

**No symlinks, no moves** — always copy. The source directory (backup drive, OneDrive, etc.) may not always be available.

## Non-Goals

- Changing the decay formula or tier multipliers — only what triggers decay changes.
- Changing consolidation logic — only its decay immunity changes.

## Implementation Approach

**Single phase** — all changes are in the same dependency layer (scripts/). No new infrastructure, no schema changes.

**Settings updates** — add to `DEFAULT_SETTINGS` in `memory_utils.py`:
- `synthesis.recentWorkingDays`: 7 (replaces `DEFAULT_RECENCY_WINDOW_DAYS`)
- `synthesis.backfill.recentWorkingDays`: 7 (sonnet/haiku cutoff — per project, sessions within this many of the project's working days get sonnet)

Tasks:
1. Working-day functions (`memory_utils.py`) — `get_global_working_days`, `get_project_working_days` with caching + new settings in `DEFAULT_SETTINGS`
2. Switch `list_recent_sessions` to working days + add `max_age_days=None` mode + update `get_recent_days` to forward parameter
3. Switch session continuity loading (Tier 2) to project working days
4. Switch recent activity loading (Tier 5) to global working days
5. Decay: remove consolidated immunity from `decay_data_points` and `cleanup_near_zero_salience`
6. Session import (`synthesis_cron.py` or new `import_sessions.py`) — copy sessions from external directory with path prefix remapping
7. Backfill command: project-grouped batching with per-project model selection
8. `/synthesize --backfill` skill integration (with optional `--import-from` argument)
9. Tests for all of the above

**Testing strategy:**
- Working-day functions: mock `get_projects_dir()` → `tmp_path` with `.jsonl` files at controlled mtimes. Test global vs per-project, caching behavior, empty dirs, single-day activity.
- `list_recent_sessions` working-day mode: create sessions spanning 14 calendar days but only 5 active dates. Verify all 5 dates are included. Verify `max_age_days=None` returns everything.
- Session continuity / recent activity: mock DB with session_context and memory rows at various timestamps. Verify working-day filtering returns correct results.
- Decay consolidated immunity removal: create consolidated + non-consolidated data_points, run decay, verify both are subject to salience reduction.
- Session import: create a source directory with `-home-user-` prefixed folders, run import targeting a tmp_path `projects/` dir. Verify prefix remapping, deduplication, mtime preservation.
- Backfill: mock `claude -p` subprocess calls. Verify project grouping, model selection per project (sonnet vs haiku based on recency), synthesis state updates.
- All unit tests use `tmp_path` and mock path helpers. No integration tests requiring actual `claude -p` calls.
