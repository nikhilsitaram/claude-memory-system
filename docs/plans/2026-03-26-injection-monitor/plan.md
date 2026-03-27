---
status: Complete
---

# Add a live injection monitor to the web UI that shows what context the memory system injects into Claude Code sessions, with per-tier breakdown, prompt recall timeline, and auto-polling. Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use orchestrate

**Goal:** Add a live injection monitor to the web UI that shows what context the memory system injects into Claude Code sessions, with per-tier breakdown, prompt recall timeline, and auto-polling.
**Architecture:** Three layers: (1) shared JSONL logging module (injection_log.py) with fire-and-forget append + rotation, (2) hook integrations in load_memory.py and prompt_recall.py that call the logger after building context, (3) web API endpoint + Monitor tab in index.html that polls the JSONL file every 2s. Phase A builds the logging module and registers it in install.py. Phase B wires the hooks, adds the API, and builds the frontend tab.
**Tech Stack:** Python 3.9+, SQLite3 (existing), JSONL file I/O, vanilla JS with polling (no WebSocket), pytest

---

## Phase A — Injection Logging Module
**Status:** Complete (2026-03-27) | **Rationale:** The logging module must exist before hooks can import it. install.py registration must happen in the same phase to keep the file set disjoint from Phase B hook modifications.

- [x] A1: Create injection_log.py logging module with rotation — *injection_log.py exports log_session_start(), log_prompt_recall(), rotate_log(), and get_log_path(). Both log functions check settings.injectionLog.enabled (via memory_utils.load_settings()) and return immediately when false. Add 'injectionLog': {'enabled': True} to DEFAULT_SETTINGS in memory_utils.py. log_session_start appends one JSONL line with ts, session_id, hook, project_scope, tiers array (each with name/count/tokens_est/ids), total_items, total_tokens_est, latency_ms, health_alerts. log_prompt_recall appends one JSONL line with ts, session_id, hook, prompt_preview (80 char truncation), candidates, injected array, filtered array, latency_ms. rotate_log(max_lines=500, keep_lines=200) truncates when exceeded. All functions catch exceptions silently. 12+ tests pass including enabled/disabled toggle tests.*
- [x] A2: Register injection_log.py in install.py — *injection_log.py appears in the scripts_to_link list in link_scripts(). 1 new test verifies the script name is present in the list.*

## Phase B — Hook Integrations and Web UI
**Status:** Complete (2026-03-27) | **Rationale:** Hooks import injection_log.py from Phase A. load_memory, prompt_recall, web_app, and index.html are all independent file sets that can be developed in parallel within this phase.

- [x] B1: Integrate injection logging into load_memory.py — *_load_from_db returns tuple (formatted_text, tiers_metadata, health_alerts) instead of plain string. tiers_metadata is a list of dicts with name, count, tokens_est, ids. health_alerts is a list of alert strings from health_report(). main() captures stdin JSON payload to extract sessionId (fallback: session-{int(time.time())}). main() uses top-level import time (no inline import). main() calls log_session_start() with health_alerts after _load_from_db returns. main() calls rotate_log() at the start. All existing tests updated for new return type (grep for _load_from_db in test file; update TestSmartLoading and TestWorkingDayLoading — all methods that assign result = _load_from_db(...) and assert on it as a string). 4+ new tests pass.*
- [x] B2: Integrate injection logging into prompt_recall.py — *main() tracks filtered candidates (deduped items) alongside injected items. Calls log_prompt_recall() with both lists after the search/filter loop. prompt_preview is first 80 chars of prompt. 3+ new tests pass.*
- [x] B3: Add /api/injection-log endpoint to web_app.py — *GET /api/injection-log?since=<iso>&session=<id> reads .injection-log.jsonl, parses lines, filters by since (default 1 hour ago) and optional session, returns JSON array. Max 500 entries. Returns empty array if file missing. 4+ new tests pass.*
- [x] B4: Add Monitor tab to web frontend — *Monitor tab appears in nav bar alongside Dashboard/Browse/Search/Graph. Session selector dropdown populated from distinct session_ids. Auto-scroll toggle (default on). Chronological timeline grouped by session. SessionStart entries show expandable tier breakdown with item counts and token estimates. PromptRecall entries show prompt preview, injected (green) vs filtered (gray) memories. Clicking memory ID calls existing openDetail(). Polls /api/injection-log every 2s via setInterval. Pauses polling on Page Visibility API hidden event. Stops polling when navigating away from Monitor tab.*
