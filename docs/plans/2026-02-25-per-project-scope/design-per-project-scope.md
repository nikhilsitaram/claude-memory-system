# Per-Project Scope Injection

**Date:** 2026-02-25
**Status:** Approved design
**Supersedes:** Scope injection section of `2026-02-23-deterministic-synthesis-design.md`

## Problem

The current `inject_scopes` function assigns one project scope per **date** via majority vote across all sessions. When a day has sessions from multiple projects (e.g., swyfft, investing, claude-memory-system), the minority projects' entries get the wrong scope.

Root cause chain:
1. `_extract_session_projects()` returns `dict[str, str | None]` keyed by date, not session
2. It picks the most common project name via `max(counter)`
3. `inject_scopes()` applies that single project to every entry for the date

Additionally, `_resolve_project_name()` fails for new worktrees not yet in `projects-index.json` because it only does exact match on `encodedPaths`.

## Philosophy

Same as deterministic synthesis: **LLM synthesizes and classifies. Code structures.**

The extraction pipeline already knows each session's project (from CWD → projects-index lookup). The LLM sees this in session headers. The output format should preserve project grouping so Python can apply scope deterministically per entry.

## Design

### New LLM Output Format

```
===PROJECT:swyfft===
- [implement] Rewrote top-producers SQL with TTM bind ranking
- [LTM][gotcha] Tableau "GWP" column is actually Written Premium, not GWP
- [design] Use bind date as date anchor for producer metrics
- [LTM][tip] Rename Tableau "GWP" to "Written Premium", add DimQuote join

===PROJECT:investing===
- [implement] Started Phase 4 TypeScript migration, completed Task 1
- [GLOBAL][pattern] Language migration cost-benefit: measure bottlenecks first

===PROJECT:global===
- [analyze] Benchmarked Python vs TypeScript for memory system
- [LTM][tip] CLAUDE_SKIP_MEMORY=1 suppresses memory injection in systemd

===END===
```

**LLM responsibilities (reduced):**
1. Summarize transcript content into entries
2. Tag each entry with `[type]` (implement, gotcha, tip, etc.)
3. Group entries under the correct `===PROJECT:X===` matching session headers
4. Optionally flag `[LTM]` for entries significant enough for long-term memory
5. Optionally flag `[GLOBAL]` for entries useful across all projects

**LLM does NOT handle:** scope names in tags, section headers (## Actions/Learnings), date prefixes on LTM entries, route target files/sections, merge, dedup.

### Entry Tag Anatomy

The LLM outputs combinations of three orthogonal flags:

```
- [type] Description              # just a type
- [LTM][type] Description         # promote to long-term memory
- [GLOBAL][type] Description      # also visible globally
- [LTM][GLOBAL][type] Description # promote + globally visible
```

### Python Processing Pipeline

**Step 1: Parse** — `parse_synthesis_output()` reads `===PROJECT:X===` blocks, extracts entries with their flags.

**Step 2: Validate project names** — Check each `===PROJECT:X===` against projects-index. Warn on unknown names.

**Step 3: Scope injection** — Apply project scope to each entry's tag:

| Project block | LLM tag | Python writes to daily |
|---|---|---|
| `===PROJECT:swyfft===` | `[gotcha]` | `[swyfft/gotcha]` |
| `===PROJECT:swyfft===` | `[GLOBAL][gotcha]` | `[global\|swyfft/gotcha]` |
| `===PROJECT:global===` | `[tip]` | `[global/tip]` |
| `===PROJECT:global===` | `[GLOBAL][tip]` | `[global/tip]` (already global) |

**Step 4: Section assignment** — Type determines section (deterministic mapping):

| Types | Section |
|---|---|
| implement, improve, document, analyze | `## Actions` |
| design, tradeoff, scope | `## Decisions` |
| gotcha, pitfall, pattern | `## Learnings` |
| insight, tip, workaround | `## Lessons` |

**Step 5: Daily file write** — Merge new entries into existing daily file sections (unchanged from current `merge_daily_sections`).

**Step 6: LTM routing** — Entries flagged `[LTM]` get routed:
- Date prefix added: `(YYYY-MM-DD) [type] Description`
- Target file: project name → `{project}-long-term-memory.md` (global → `global-long-term-memory.md`)
- Target section: same type→section mapping as step 4 but with "Key " prefix (`## Key Learnings`, etc.)
- Quality floor, keyword-overlap dedup, route cap all apply (unchanged)
- `[GLOBAL][LTM]` entries route to both global AND project LTM files

**Step 7: Mark routed** — Daily entries that were successfully routed get `[routed]` prefix (unchanged).

### Worktree Resolution Fix

`_resolve_project_name()` currently does exact match only. Add parent-prefix fallback:

```python
# After exact encodedPaths match fails:
# Try prefix match — new worktrees share the base project's encoded prefix
for _path, data in projects.items():
    for ep in data.get("encodedPaths", []):
        if project_hash.startswith(ep + "-") or ep.startswith(project_hash.rsplit("-", 1)[0]):
            return data.get("name")
```

This handles `ts-phase-4` resolving to `investing` when `ts-phase-1` through `ts-phase-3` are already indexed. The encoded path `-home-nsitaram-personal-investing--worktrees-ts-phase-4` shares the prefix `-home-nsitaram-personal-investing--worktrees-` with known paths.

More precisely: strip the last `-<segment>` from the unknown hash and check if any known encoded path starts with that prefix.

## Changes by File

| File | Change |
|---|---|
| `synthesis.py` | New `===PROJECT:X===` parser; refactor `inject_scopes` to per-entry from per-date; `[LTM]` inline flag replaces `===ROUTE===` blocks; type→section mapping function |
| `load_memory.py` | Update synthesis prompt: new output format, remove section headers instruction, add `[LTM]` flag instruction, update examples |
| `transcript_ops.py` | Fix `_resolve_project_name` with parent-prefix worktree fallback |
| `memory_utils.py` | No changes (filter_daily_content already handles pipe-delimited scopes) |

**No changes:** `indexing.py`, `decay.py`, `devtools.py`, `project_manager.py`, daily file format on disk, LTM file format on disk, `merge_daily_sections`, dedup logic, route cap logic.

## Backwards Compatibility

- Daily file format unchanged — still flat `[scope/type]` entries under section headers
- LTM file format unchanged — still `(date) [type] Description` under section headers
- Old synthesis output format (`===DAILY===` + `===ROUTE===`) can coexist during transition: parser can handle both
- `filter_daily_content()` already handles pipe-delimited scopes

## Estimated Scope

~200 lines of new/modified code across 3 files, plus test updates. Most change is in `synthesis.py` (parser + scope injection refactor) and `load_memory.py` (prompt update).
