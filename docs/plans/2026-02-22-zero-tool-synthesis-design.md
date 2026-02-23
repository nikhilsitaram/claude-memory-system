# Zero-Tool Synthesis: Performance Optimization Design

**Date:** 2026-02-22
**Status:** Approved
**Branch:** feature/zero-tool-synthesis

## Problem

Background synthesis subagent averages 113s (1m53s) with outliers reaching 5+ minutes. The subagent makes 3 sequential API rounds (Read → Write/Edit → Bash) plus wastes 14s generating a final summary nobody reads.

### Performance Data (154 sessions sampled)

| Metric | Value |
|--------|-------|
| Mean duration | 113s |
| Median | 88s |
| P90 | ~165s |
| > 3 min | 9.1% |
| > 5 min | 3.2% |

### Time Breakdown (current)

| Phase | Avg Time | % |
|-------|----------|---|
| Read all inputs | 22s | 17% |
| LLM thinking + generation | 58s | 46% |
| Write daily files | 8s | 6% |
| Edit LTM files | 19s | 15% |
| Bash post-processing | 5s | 4% |
| Final summary (waste) | 14s | 11% |

### Root Causes for Outliers

| Root Cause | Added Time |
|-----------|-----------|
| Edit conflicts ("file modified since read") | +30-60s per retry cycle |
| Multi-date processing | +30-40s per extra date |
| Model inference on large inputs | 60-100s |

## Solution: Zero-Tool Synthesis

Move all file I/O from the subagent to Python. The subagent's only job is thinking (synthesize, route, tag). It outputs structured text that Python parses and applies.

### Architecture

```
load_memory.py (SessionStart hook)
├── Pre-extract transcripts to temp files              [existing]
├── Pre-read: transcripts, LTM files, daily files      [NEW - Python I/O]
├── Embed all content in prompt                         [NEW]
├── Spawn background Task subagent                      [existing]
│   └── Subagent (1 inference + 2 tool calls):
│       1. Think → generate structured output
│       2. Write(/tmp/synthesis-output-{pid}.txt)       [1 tool call]
│       3. Bash(synthesis.py apply <file> --sidecars)   [1 tool call]
│           └── synthesis.py:                           [Python I/O]
│               ├── Parse structured output
│               ├── Mark [routed] entries deterministically
│               ├── Write daily files (atomic rename)
│               ├── Append entries to LTM sections
│               ├── Run mark-captured
│               ├── Cleanup temp files
│               ├── Run decay
│               ├── Validate LTM
│               └── Update .last-synthesis timestamp
```

**API rounds: 3 → 2** (Write + Bash). The Read round is eliminated entirely.

### Expected Performance

| Metric | Current | Projected |
|--------|---------|-----------|
| Avg duration | 113s | 40-55s |
| API rounds | 3 | 2 |
| Subagent tool calls | ~11 | 2 |
| Edit conflict risk | Yes | No (Python writes) |
| Final summary waste | 14s | 0s |
| [routed] marking accuracy | 11% | 100% |

## Structured Output Format

The subagent generates text with delimiters that `synthesis.py` parses:

```
===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [scope/implement] What was accomplished
## Decisions
- [scope/design] Choice made and why
## Learnings
- [scope/pattern] Proven method or approach
## Lessons
- [scope/tip] Useful command or shortcut

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] Description of learning...

===ROUTE:project-name:Key Decisions===
- (2026-02-22) [design] Description of decision...

===END===
```

### Parsing Rules

| Delimiter | Action |
|-----------|--------|
| `===DAILY:<date>===` | Write complete file to `~/.claude/memory/daily/<date>.md` |
| `===ROUTE:<scope>:<section>===` | Append entries to named section in LTM file |
| `===END===` | Signals clean completion; missing = warn but process what exists |
| Text outside delimiters | Ignored (allows LLM "thinking out loud") |
| Empty route block | Skip silently |
| Unknown project in route | Skip, log warning |

### LTM Append Logic

Find section header (e.g., `## Key Learnings`), locate its comment line, insert new entries after the comment. Existing entries untouched.

## Deterministic [routed] Marking

Replaces `devtools.py mark-routed` (which depended on LLM compliance at only 11% effectiveness).

Since `synthesis.py` receives both daily content and route entries in the same structured output, it knows exactly which entries were routed. It marks them `[routed]` at daily file write time:

1. Collect all routed entry descriptions from `===ROUTE:===` blocks
2. Before writing each daily file, find matching entries and prepend `[routed]`
3. 100% deterministic, no LLM dependency

## Quick Wins (bundled)

1. **Default model → haiku** — Synthesis is structured categorization/tagging. Haiku handles this well and is ~2-3x faster inference. User can override back to sonnet in settings.json.

2. **Kill final summary** — Remove "Return a summary: Processed N days..." from prompt. The 14s of generation goes to background Task output that nobody reads.

3. **Consolidate post-processing** — `mark-captured`, `validate-ltm`, `decay`, and timestamp update all move into `synthesis.py apply`. Eliminates 4 Python interpreter startups.

4. **Eliminate `devtools.py mark-routed`** — Replaced by deterministic marking at write time (see above).

5. **Drop auto-extract fallback** — Always require pre-extraction. If `pre_extract_transcripts()` fails, skip synthesis and retry next session. One code path instead of two.

## Error Handling

| Scenario | Behavior |
|----------|----------|
| `===END===` missing but `===DAILY:` found | Process what was parsed, log warning |
| No `===DAILY:` blocks found | Synthesis failed; don't mark-captured (retry next session) |
| Unknown project in route | Skip that route, log warning |
| Daily file write fails | Continue with other files, don't mark that date captured |
| LTM section header not found | Skip append, log warning |
| LTM file doesn't exist | Create from template, then append |
| `synthesis.py apply` crashes | Uncaptured sessions stay pending, retry next session |

### Atomic Safety

- Daily files written to temp path, then renamed into place (atomic on POSIX)
- `.last-synthesis` timestamp written LAST, only on success
- No retry within same run; next session retries automatically

## New Module: `scripts/synthesis.py`

```
scripts/synthesis.py
├── parse_synthesis_output(text) → SynthesisResult
│   ├── dailies: list[DailyFile(date, content)]
│   └── routes: list[RouteEntry(scope, section, entries)]
├── mark_routed_entries(dailies, routes) → modified dailies
├── apply_results(result, sidecar_paths, extract_paths)
│   ├── write_daily_files(dailies)
│   ├── append_to_ltm(routes)
│   ├── mark_captured(sidecar_paths)
│   ├── cleanup_temp_files(extract_paths, sidecar_paths)
│   ├── run_decay()
│   ├── validate_ltm()
│   └── update_timestamp()
└── CLI: python3 synthesis.py apply <output_file> --sidecars <paths> --extracts <paths>
```

### Changes to Existing Files

**`load_memory.py`:**
- `_build_pre_extracted_prompt()` → embeds file contents inline + requests structured output format
- `_build_dates_only_prompt()` → removed (no auto-extract fallback)
- `_build_synthesis_instructions()` → updated with structured output format
- `_assemble_prompt()` → simplified (one path)
- `pre_extract_transcripts()` → also reads files into memory for embedding

**`templates/settings.json`:**
- `synthesis.model` default: `"sonnet"` → `"haiku"`

## Prompt Template (new)

```
Synthesize these session transcripts into daily summaries and route key learnings.

## Inputs

### Pending Dates: {dates}

### Transcript: {date}
{embedded transcript content}

### Existing Daily: {date}
{embedded daily file content, if exists}

### Global Long-Term Memory
{embedded global LTM}

### Project Long-Term Memory: {project}
{embedded project LTM}

## Instructions
{synthesis_instructions - tagging, scoping, routing criteria, dedup, granularity cap}

## Output Format
Generate EXACTLY this structure - nothing else:

===DAILY:YYYY-MM-DD===
[full daily file markdown]

===ROUTE:scope:section===
- (YYYY-MM-DD) [type] Description

===END===

Then deliver your output:
Step 1: Write(/tmp/synthesis-output-{pid}.txt, <your structured output above>)
Step 2: Bash(python3 $HOME/.claude/scripts/synthesis.py apply /tmp/synthesis-output-{pid}.txt --sidecars {sidecar_paths} --extracts {extract_paths})

Do NOT generate a summary. Do NOT use any other tools. Only Write + Bash.
```
