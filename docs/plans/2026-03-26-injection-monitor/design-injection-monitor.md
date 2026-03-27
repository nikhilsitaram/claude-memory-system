# Design: Live Session Injection Monitor

**Issue:** #102
**Date:** 2026-03-26
**Status:** Approved

## Problem

There is no visibility into what the memory system injects into Claude Code sessions. The SessionStart `<memory>` block and per-prompt recall results are invisible to the user. When memories don't surface as expected, or unexpected context appears, there's no way to diagnose without reading hook source code.

**Who's affected:** The memory system developer/operator (single user).

**Consequences of not solving:** Debugging memory surfacing requires reading raw JSONL transcripts or adding print statements to hooks. Tuning salience thresholds and recall settings is blind.

## Goal

Add an on-demand debug view to the web frontend that shows what context is being injected into the active Claude Code session, with live auto-updating when open.

## Success Criteria

1. Opening the Monitor tab in the web UI shows the most recent SessionStart injection with per-tier breakdown (item counts, token estimates, memory IDs)
2. PromptRecall injections appear in the timeline as they happen, showing injected memories and filtered candidates with reasons
3. The monitor auto-updates without manual refresh while the tab is active
4. Clicking a memory ID in the monitor opens the existing detail modal
5. Hook latency does not regress observably (verified by before/after timing of `load_memory.py` and `prompt_recall.py`)
6. Log file does not grow unboundedly (rotation on SessionStart)

## Architecture

Three layers:

```
load_memory.py ──┐
                 ├──> ~/.claude/memory/.injection-log.jsonl ──> GET /api/injection-log ──> Monitor tab
prompt_recall.py ┘                                                                        (polls 2s when active)
```

### Injection Logging Module (`scripts/injection_log.py`)

Shared helper with two public functions:
- `log_session_start(session_id, project_scope, tiers, latency_ms, health_alerts)` — appends one JSONL line
- `log_prompt_recall(session_id, prompt_preview, candidates, injected, filtered, latency_ms)` — appends one JSONL line

Both are fire-and-forget: exceptions are caught and silently ignored (logging must never break the hook). Both check `settings.injectionLog.enabled` (default `true`) before writing — when disabled, no file I/O occurs.

**Design constraint:** Each hook invocation appends a single JSONL line (<1ms). This budget is validated by before/after timing during development.

**Settings toggle:** `injectionLog.enabled` (boolean, default `true`) in `DEFAULT_SETTINGS`. Controllable via `/settings`. When `false`, hooks skip logging entirely.

**Log file:** `~/.claude/memory/.injection-log.jsonl` (dot-prefixed, alongside existing state files)

**Rotation:** `rotate_log(max_lines=500, keep_lines=200)` — called on SessionStart. If file exceeds `max_lines`, truncate to most recent `keep_lines`.

### Data Schema

**SessionStart entry:**
```json
{
  "ts": "2026-03-26T14:30:00-05:00",
  "session_id": "abc123",
  "hook": "SessionStart",
  "project_scope": "claude-memory-system",
  "tiers": [
    {"name": "Profile", "count": 5, "tokens_est": 420, "ids": ["dp_abc", "dp_def"]},
    {"name": "Session", "count": 1, "tokens_est": 180, "ids": ["dp_ghi"]},
    {"name": "Project", "count": 12, "tokens_est": 1400, "ids": ["dp_jkl"]},
    {"name": "Global", "count": 6, "tokens_est": 780, "ids": ["dp_mno"]},
    {"name": "Recent", "count": 8, "tokens_est": 950, "ids": ["dp_pqr"]}
  ],
  "total_items": 32,
  "total_tokens_est": 3730,
  "latency_ms": 62,
  "health_alerts": ["No synthesis in 8 days"]
}
```

**PromptRecall entry:**
```json
{
  "ts": "2026-03-26T14:31:15-05:00",
  "session_id": "abc123",
  "hook": "UserPromptSubmit",
  "prompt_preview": "how do I configure the...",
  "candidates": 8,
  "injected": [
    {"id": "dp_xyz", "content_preview": "zsh PATH: env vars...", "scope": "global"}
  ],
  "filtered": [
    {"id": "dp_uvw", "content_preview": "Python 3.13...", "reason": "deduped"}
  ],
  "latency_ms": 42
}
```

Token estimation: `len(content) // 4`. Content previews truncated to 80 chars in log; full content fetched on-demand from DB via existing detail modal.

### API Endpoint

`GET /api/injection-log?since=<iso-timestamp>&session=<session_id>`

- Returns JSON array of log entries newer than `since` (default: 1 hour ago)
- Optional `session` filter
- Reads JSONL file, parses, filters, returns
- Max 500 entries per response

### Frontend Tab

New "Monitor" tab alongside Dashboard, Browse, Search, Graph.

- Session selector dropdown (populated from distinct session_ids in log)
- Auto-scroll toggle (on by default)
- Chronological timeline grouped by session
- SessionStart entries: expandable tier breakdown with item counts and token estimates
- PromptRecall entries: prompt preview, injected (green) vs filtered (gray) memories
- Click any memory ID → calls existing `openModal(dpId)` with the `id` string from the log entry's `ids` array or `injected[].id` field
- Polls every 2s via `setInterval`, pauses when tab not visible (Page Visibility API)
- Stops polling when navigating away from Monitor tab

## Key Decisions

1. **JSONL file over SQLite table** — the injection log is a debug artifact, not persistent data. No schema migration needed, file can be deleted anytime.
2. **Always-on logging, on-demand viewing** — the write overhead is negligible (<1ms per append). The polling only runs when the Monitor tab is active.
3. **Rotation on SessionStart** — simple truncation keeps ~1 day of history. No separate rotation daemon.
4. **Content previews in log, full content on-demand** — keeps log lines small while allowing drill-down.

## Alternatives Considered

- **Tail stderr/log file** — hooks could write to stderr and the user tails a log. Rejected: no structured breakdown, no clickable memory IDs, requires a dedicated terminal window.
- **CLI command** (`python3 injection_log.py tail`) — simpler than a web tab. Rejected: loses the ability to click memory IDs into the existing detail modal and view tier breakdowns visually.
- **Extend existing `/api/data_points` with "recently accessed" sort** — approximates SessionStart visibility. Rejected: doesn't show per-tier grouping, prompt recall history, or filtered candidates.

The web UI was chosen because it provides structured tier breakdowns, clickable memory IDs linking to the existing detail modal, and auto-refresh — all in the already-running web frontend.

## Non-Goals

- Persistent historical analytics (this is a live debug tool)
- Alerting or notifications based on injection patterns
- Modifying memory salience/content from the Monitor tab (use existing Browse/Detail views)
- Real-time WebSocket/SSE (polling at 2s is sufficient for debugging)

## Implementation Approach

**Single phase** — all changes are additive with no dependency layers:

1. New `scripts/injection_log.py` — logging helpers + rotation. Add `injection_log.py` to the `scripts_to_link` list in `scripts/install.py` `link_scripts()`.
2. Modify `scripts/load_memory.py`:
   - **Return type change:** `_load_from_db()` currently returns a formatted string. Refactor to build a `tiers` list alongside `sections`, tracking `(name, ids, content_text)` per tier. Return a tuple `(formatted_text, tiers_metadata)` instead of a plain string. Token estimates computed as `sum(len(content) // 4 for each item in tier)`.
   - **Session ID capture:** `main()` currently discards the stdin JSON payload. Capture it and extract `payload["sessionId"]` (string) for passing to `log_session_start()`. Fall back to `f"session-{int(time.time())}"` if the key is absent.
   - Call `log_session_start()` after `_load_from_db()` returns.
3. Modify `scripts/prompt_recall.py`:
   - **Candidate tracking:** Refactor the recall loop to track filtered candidates. Before the loop, record `candidates = len(results)`. Inside the loop, when `is_recently_injected()` returns True, append `{"id": dp.id, "content_preview": dp.content[:80], "reason": "deduped"}` to a `filtered` list.
   - Call `log_prompt_recall()` with both `injected` and `filtered` lists after the search/filter loop.
4. Modify `scripts/web_app.py` — add `GET /api/injection-log` endpoint
5. Modify `templates/web/index.html` — add Monitor tab with polling JS and timeline rendering
6. Tests for injection_log module, API endpoint, rotation, and hook integration
