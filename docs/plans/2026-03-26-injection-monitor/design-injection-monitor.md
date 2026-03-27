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
5. Hook latency increases by less than 5ms (one JSONL file append per invocation)
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

Both are fire-and-forget: exceptions are caught and silently ignored (logging must never break the hook).

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
  "hook": "PromptRecall",
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
- Click any memory ID → opens existing detail modal
- Polls every 2s via `setInterval`, pauses when tab not visible (Page Visibility API)
- Stops polling when navigating away from Monitor tab

## Key Decisions

1. **JSONL file over SQLite table** — the injection log is a debug artifact, not persistent data. No schema migration needed, file can be deleted anytime.
2. **Always-on logging, on-demand viewing** — the write overhead is negligible (<1ms per append). The polling only runs when the Monitor tab is active.
3. **Rotation on SessionStart** — simple truncation keeps ~1 day of history. No separate rotation daemon.
4. **Content previews in log, full content on-demand** — keeps log lines small while allowing drill-down.

## Non-Goals

- Persistent historical analytics (this is a live debug tool)
- Alerting or notifications based on injection patterns
- Modifying memory salience/content from the Monitor tab (use existing Browse/Detail views)
- Real-time WebSocket/SSE (polling at 2s is sufficient for debugging)

## Implementation Approach

**Single phase** — all changes are additive with no dependency layers:

1. New `scripts/injection_log.py` — logging helpers + rotation
2. Modify `scripts/load_memory.py` — collect tier metadata during `_load_from_db()`, call `log_session_start()` after
3. Modify `scripts/prompt_recall.py` — call `log_prompt_recall()` after search/filter
4. Modify `scripts/web_app.py` — add `GET /api/injection-log` endpoint
5. Modify `templates/web/index.html` — add Monitor tab with polling JS and timeline rendering
6. Tests for injection_log module, API endpoint, rotation, and hook integration
