# Codebase Improvements Audit

**Date:** 2026-02-22
**Scope:** Full codebase audit across speed, efficiency, design, logic, pipeline, and implementation.

---

## High Impact

### 1. `parse_jsonl_file()` drops ALL user messages from synthesis input
- **Where:** `scripts/transcript_ops.py:106`
- **What:** `if role == "user": continue` discards every user message. The synthesis subagent only sees assistant responses, making it much harder to understand what was being worked on.
- **Benefit:** Including user messages (even truncated) would dramatically improve synthesis quality. The subagent could understand intent, not just output.

### 2. Settings SKILL.md has wrong default values
- **Where:** `skills/settings/SKILL.md:51-52, 66-70`
- **What:** Lists `tokenLimit` defaults as 5,000 but code uses 3,000. Total budget shows 16,750 but actual is 11,250.
- **Benefit:** Users reading the skill would have correct expectations.

### 3. `load_project_history()` scans ALL daily files on every session start
- **Where:** `scripts/load_memory.py:185`
- **What:** `daily_dir.glob("*.md")` reads every daily file ever created, then filters for project content. With months of files, this grows linearly. Meanwhile `load_daily_summaries()` uses `get_working_days()` which only returns recent N files.
- **Benefit:** Capping the scan (e.g., last 60 days max) or using a date-based approach would keep load time constant.

### 4. No validation that synthesis subagent completed successfully
- **Where:** `scripts/load_memory.py:536-538`
- **What:** Eager timestamp write prevents duplicate synthesis but also masks failures. If the subagent crashes, the timestamp is already written. Failed dates stay pending until `intervalHours` expires or next UTC day.
- **Benefit:** Moving timestamp write to end of synthesis (or to synthesis.py apply) ensures only successful synthesis updates the clock. *(Note: the zero-tool design already addresses this -- `synthesis.py apply` writes the timestamp last.)*

### 5. Global LTM exceeds budget with no enforcement
- **Where:** `scripts/load_memory.py:109-119`
- **What:** Global LTM is 16,967 bytes (4,241 tokens) against a 3,000-token budget. `load_global_memory()` reads the entire file without truncation. Budget is informational only.
- **Benefit:** Token budget enforcement would prevent context bloat and keep memory loading predictable.

### 6. `list_all_sessions()` called multiple times per pipeline run
- **Where:** `scripts/indexing.py:155`, called via `list_pending_sessions()`, `cmd_mark_captured()`, `cmd_uncapture_date()`
- **What:** Scans every `.jsonl` file in `~/.claude/projects/`, stats each one, and parses `sessions-index.json` per folder. Called 2-3 times per synthesis run with no caching.
- **Benefit:** Caching the result would cut duplicate filesystem scans. Most impactful for users with 100+ sessions.

---

## Medium Impact

### 7. Circular import between `indexing.py` and `transcript_ops.py`
- **Where:** `transcript_ops.py:26` imports from `indexing`, `indexing.py:463,505` imports from `transcript_ops` inside functions
- **What:** `transcript_ops` cannot be used without `indexing`. Works via lazy imports but is a design smell.
- **Benefit:** Extract shared types into `memory_utils.py` or a new `session_types.py`.

### 8. `devtools.py` duplicates parsing logic from other modules
- **Where:** `scripts/devtools.py:226-329` (`mark_routed`), `332-416` (`validate_ltm`)
- **What:** Reimplements daily file parsing and LTM entry scanning instead of using `filter_daily_content()` or `parse_sections()` from existing modules.
- **Benefit:** Shared parsing logic prevents bugs from fixing in one place but not another.

### 9. `token_usage.py` duplicates loading logic from `load_memory.py`
- **Where:** `scripts/token_usage.py:26-101` vs `load_memory.py:109-207`
- **What:** Reimplements file loading and scope filtering. Changes to loading logic must be made in two places.
- **Benefit:** Have `token_usage.py` call `load_memory.py` functions directly.

### 10. Transcript budget allocation wastes space on small sessions
- **Where:** `scripts/transcript_ops.py:165-217`
- **What:** Budget is split evenly across all sessions (`total_budget // len(all_sessions)`). A day with one large session and five trivial ones gives each the same allocation, wasting budget on small sessions.
- **Benefit:** Proportional allocation based on actual session size would capture more important content.

### 11. PreToolUse hook overly broad + duplicate entry
- **Where:** `hooks/pretooluse-allow-memory.sh:37,43` and `~/.claude/settings.json`
- **What:** Auto-allows ANY operation mentioning `.claude/scripts` or `/tmp/`. Also, the hook is registered twice in settings (duplicate entry).
- **Benefit:** Tighter patterns (specific script names, `memory-extract` prefix for /tmp) and dedup would improve security and reduce ~15ms overhead per tool call.

### 12. Missing test coverage for `main()` entry points
- **Where:** `load_memory.py`, `decay.py`, `indexing.py`, `project_manager.py`
- **What:** CLI integration layer (stdin parsing, project detection, synthesis prompt generation) has zero direct test coverage.
- **Benefit:** Integration tests would catch regressions in the most complex code path.

### 13. `filter_daily_content()` drops multi-line continuation entries
- **Where:** `scripts/memory_utils.py:567-648`
- **What:** Processes line-by-line, assumes entries are single-line. Continuation lines (indented, no bullet) are dropped for project scope.
- **Benefit:** Handling continuation lines would preserve complete entries.

### 14. Worktrees fail project detection with default settings
- **Where:** `scripts/memory_utils.py:711-735`
- **What:** With `includeSubdirectories=False` (default), worktree paths don't match the project index. User must manually change settings.
- **Benefit:** Default to `True` or add worktree-specific detection.

### 15. Worktree folders registered as separate projects in index
- **Where:** `scripts/indexing.py` (build-index)
- **What:** `.worktrees/` paths get indexed as independent projects, fragmenting history (e.g., `[edgar-integration/*]` vs `[investing/*]`).
- **Benefit:** Worktree-to-parent mapping would keep project history unified.

### 16. `get_captured_sessions()` re-reads `.captured` file multiple times
- **Where:** `scripts/memory_utils.py:478`, called from `transcript_ops.py:136,227` and `indexing.py:544,571,603`
- **What:** Read from disk at least 3 times per synthesis run.
- **Benefit:** Read once, pass through the call chain.

### 17. `devtools.py mark-routed` uses O(n*m) comparison
- **Where:** `scripts/devtools.py:269-271`
- **What:** Each daily entry checked against ALL LTM entries. 100 LTM entries x 50 daily entries = 5,000+ keyword-set comparisons.
- **Benefit:** Hash-based exact dedup first, then keyword matching only for non-exact matches.

### 18. `project_manager.py` is 1,538 lines with mixed responsibilities
- **Where:** `scripts/project_manager.py`
- **What:** Path encoding, discovery, validation, planning, execution, backup, index management, and CLI all in one file.
- **Benefit:** Splitting into focused modules would improve maintainability.

### 19. No mechanism to recover from corrupted daily files
- **Where:** Entire synthesis pipeline
- **What:** Malformed daily files (partial writes from crashes) persist and cause issues in subsequent synthesis runs.
- **Benefit:** Basic validation (check for `# YYYY-MM-DD` header, required sections) on load.

### 20. Project index not updated on session start
- **Where:** `scripts/load_memory.py` -- no call to `build_projects_index()`
- **What:** New project directories aren't recognized until next `install.py` or manual `build-index`.
- **Benefit:** Lightweight check on SessionStart: if PWD not in index, add it.

---

## Low Impact

### 21. `uninstall.py` duplicates helpers from `memory_utils.py`
- **Where:** `uninstall.py:24-44`

### 22. `sys.path` manipulation scattered across every script
- **Where:** Every script file's header

### 23. Race condition in `add_captured_session()` with pre-loaded set
- **Where:** `memory_utils.py:490-513`

### 24. `remove_captured_session()` not atomic
- **Where:** `memory_utils.py:516-538`

### 25. `should_synthesize()` false-positive around UTC midnight
- **Where:** `load_memory.py:103`

### 26. Token estimation inconsistency (characters vs bytes)
- **Where:** `memory_utils.py:297` vs `load_memory.py:117`

### 27. `_deep_merge()` shallow copy shares nested dicts
- **Where:** `memory_utils.py:277`

### 28. `_build_preextracted_prompt()` re-reads files to count lines
- **Where:** `load_memory.py:291-295`

### 29. No settings validation (invalid values cause crashes)
- **Where:** `memory_utils.py:223-246`

### 30. `devtools.py` docstring says "NOT installed" but it IS installed
- **Where:** `devtools.py:6` vs `install.py:121`

### 31. Global STM perpetually 0 tokens (all entries routed)
- **Where:** `memory_utils.py:615` filtering, routed entries always skipped
- **What:** 1,500 tokens of budget permanently unused because `[routed]` marking works correctly.

### 32. No test for `save_settings()` round-trip
- **Where:** `memory_utils.py:288`

### 33. Missing type hints on `devtools.py` and `token_usage.py`

### 34. `decode_path_best_effort()` used despite being deprecated
- **Where:** `project_manager.py:160,335`

### 35. No test coverage for PreToolUse bash hook

---

## Quick Wins (minimal effort, clear benefit)

| # | Fix | Effort | Impact |
|---|-----|--------|--------|
| 2 | Fix SKILL.md default values | 5 min | Correct user docs |
| 11 | Remove duplicate PreToolUse hook entry | 5 min | -15ms per tool call |
| 28 | Return line counts from pre_extract instead of re-reading | 10 min | Eliminate redundant I/O |
| 30 | Fix devtools.py docstring | 1 min | Accurate docs |
| 16 | Pass `captured_set` through call chain | 15 min | Eliminate 2 file reads |
| 25 | Check `should_synthesize()` BEFORE `get_pending_days()` | 5 min | Skip expensive scan when synthesis not due |
