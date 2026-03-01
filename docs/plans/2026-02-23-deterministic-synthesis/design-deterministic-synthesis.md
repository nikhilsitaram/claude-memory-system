# Deterministic Synthesis Pipeline

**Date:** 2026-02-23
**Status:** Approved design

## Problem

The synthesis pipeline delegates three structural tasks to the LLM that it consistently fails at:

1. **Merge:** LLM receives existing daily file + new transcripts, told to merge. Replaces instead of merging, losing earlier entries.
2. **Scope tagging:** LLM decides `[scope/type]` tags. Misattributes entries to wrong projects (e.g., Tailscale tip tagged `[claude-memory-system/...]` instead of `[global/...]`).
3. **Dedup:** LLM told to check existing LTM and avoid duplicates. Ignores this. Existing `append_to_ltm()` uses loose substring matching that is both too broad and too narrow.

## Philosophy

**LLM synthesizes and classifies. Code structures, scopes, merges, and deduplicates.**

### LLM judgment surface (reduced to what LLMs are good at)

1. Summarize transcript content into entries
2. Classify type per entry (implement/gotcha/tip/etc.)
3. Decide routability — is this entry significant enough for LTM?
4. Binary global flag — is this entry also globally useful?

### Code handles everything structural

1. Scope tagging from session CWD metadata
2. Daily file merging (section-level append)
3. Near-duplicate rejection at both daily merge and LTM write
4. Route cap enforcement (hard limit of 5 per target)

## Design

### 1. Tag Format & Scope Logic

**New tag format** — entries can have one or two scopes, separated by `|`:

```
- [cartwheel/implement] Added OAuth flow                # project only
- [global|cartwheel/gotcha] Tailscale MTU black hole    # both scopes
- [global/pattern] Git stash before rebase              # global only
```

**Auto-injection rules (in `synthesis.py` apply phase):**

1. Resolve session CWD → project name from `projects-index.json`
2. Session has project match → every entry gets `[project/type]`
3. LLM also flagged GLOBAL → becomes `[global|project/type]`
4. No project match (e.g. `~`, `/tmp`) → `[global/type]`

**LLM output format (simplified):**

```
===DAILY:2026-02-23===
## Actions
- [implement] Fixed synthesis pipeline

## Learnings
- [GLOBAL][gotcha] Tailscale MTU black hole on WSL2
```

Code transforms to final tagged format. LLM never writes scope names — only types and optional `[GLOBAL]` marker.

**Filter change in `filter_daily_content()`:**

- Split scope portion on `|` to get list of scopes
- Match if any scope in the list matches the filter
- Backwards-compatible: single-scope entries (`[cartwheel/gotcha]`) — `split("|")` returns `["cartwheel"]`

### 2. Programmatic Merge

**New contract:** LLM outputs only new entries. `synthesis.py` owns the merge.

**Prompt change:**

```
## EXISTING SUMMARY (read-only context — do NOT repeat)
<existing daily file content>

## NEW TRANSCRIPTS
<transcript content>

Output ONLY entries from new/continued sessions.
The system will merge your output with the existing summary automatically.
```

**New function `merge_daily_sections(existing, new)` in `synthesis.py`:**

1. Parse existing file into dict: `{section_name: [entries]}`
2. Parse LLM output into dict: `{section_name: [entries]}`
3. For each section: start with existing entries, append new entries that pass `is_routed_match(0.6)` dedup
4. Reassemble: date header + sections in standard order (Actions, Decisions, Learnings, Lessons)
5. If no existing file: LLM output written directly (no merge needed)

**Where it fits:** `write_daily_files()` checks for existing file → merge if exists, write directly if not.

**Edge cases:**

- First synthesis of the day: no existing file, LLM output written directly
- Second+ synthesis same day: existing file loaded, sections merged
- LLM outputs a section that doesn't exist yet: new section appended
- LLM outputs empty section: skipped, existing content preserved

### 3. Dedup at Apply Time

**Replace substring check in `append_to_ltm()` with `is_routed_match(threshold=0.6)`.**

Current (synthesis.py ~line 310):
```python
if entry.strip().lower() not in existing_content:
```

New:
```python
existing_entries = extract_section_entries(existing_content, section)
if not any(is_routed_match(entry, existing, threshold=0.6) for existing in existing_entries):
```

**Threshold rationale:** 0.6 catches short entries (3 keywords, 2/3 match = 67% > 0.6). 0.7 would let those through.

**Also applied in `merge_daily_sections()`** — same dedup when appending new entries to existing daily sections.

**Route cap enforcement:** Hard limit of 5 routed entries per target LTM file in `append_to_ltm()`. Currently only prompt guidance.

**LLM prompt change:** Remove dedup instructions entirely. Add: "The system handles dedup automatically — output all entries you think are worth capturing."

**Dedup layers after this change:**

| Layer | Mechanism | When |
|-------|-----------|------|
| Daily merge | `is_routed_match(0.6)` | Appending new entries to existing daily sections |
| LTM routing | `is_routed_match(0.6)` | Writing routes to LTM files |
| Route cap | Hard limit of 5 per file | At LTM write time |
| Routed marking | `mark_routed_entries()` | Post-processing, marks daily entries that were routed |
| Decay | `decay.py` | Archives old entries past age threshold |

## Changes by File

| File | Change | Scope |
|------|--------|-------|
| `transcript_ops.py` | Resolve session CWD → project name in `format_transcripts_incremental()`, inject `[project: name]` into session headers | Scope injection |
| `load_memory.py` | Mark existing daily as read-only context; remove merge/scope/dedup instructions; add simplified LLM contract | Prompt simplification |
| `synthesis.py` | New `merge_daily_sections()`; scope injection in apply phase; replace substring dedup with `is_routed_match(0.6)`; hard route cap | All three features |
| `memory_utils.py` | `filter_daily_content()` split scope on `\|`; new `extract_all_scopes()` helper | Filter update |

**No changes:** `indexing.py`, `decay.py`, `devtools.py`, `project_manager.py`

## Backwards Compatibility

- Old daily files with `[scope/type]` tags: `filter_daily_content()` still works (single scope, no `|`, split returns one element)
- Old LTM entries: unaffected, `is_routed_match()` operates on entry text regardless of tag format
- No migration needed. New entries get new format, old entries stay as-is, filtering handles both

## Estimated Scope

~150-200 lines of new/modified code across 4 files, plus test updates.
