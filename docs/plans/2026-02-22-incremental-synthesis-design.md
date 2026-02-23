# Incremental Synthesis via High Water Mark

**Date:** 2026-02-22
**Status:** Approved
**Branch:** feature/zero-tool-synthesis

## Problem

The synthesis pipeline re-embeds ALL pending session transcripts every run. Within a single day, sessions accumulate because `mark-captured` deliberately skips today's sessions (to allow resumed sessions to contribute new content). This means synthesis run #3 at 6 PM re-processes everything from runs #1 (10 AM) and #2 (2 PM), even if those sessions haven't changed.

Key insight: sessions are living documents that grow when resumed. The binary "captured/not-captured" model can't express "synthesized up to byte N."

## Design

### State File

New file: `~/.claude/memory/.synthesis-state.json`

```json
{
  "sessions": {
    "session-abc123": {
      "offset": 45230,
      "lines": 312,
      "last_synthesized": "2026-02-22T18:00:00Z"
    }
  }
}
```

- **offset**: byte size of transcript file when last synthesized (quick change detection via `os.path.getsize()`)
- **lines**: JSONL line count at that point (for extracting the delta)
- **last_synthesized**: ISO timestamp for diagnostics

Managed with the same `FileLock` pattern used by `.captured`.

### Extraction Changes (transcript_ops.py)

New function `extract_transcripts_incremental()`:

1. Load `.synthesis-state.json`
2. For each pending session:
   - `current_size = os.path.getsize(transcript_path)`
   - If `session_id` in state AND `current_size == state.offset` → **skip** (unchanged)
   - If `session_id` in state AND `current_size > state.offset` → **delta** (parse only lines after `state.lines`)
   - If `session_id` not in state → **full** (parse all lines)
3. Return `{date: [session_data]}` where each session_data has a `mode` field (`"full"` or `"delta"`)

Delta extraction: open file, skip first N lines (from state), parse remaining through `should_skip_message()` filter.

### Prompt Changes (load_memory.py)

`_build_preextracted_prompt()` gains incremental awareness:

- **New sessions**: embedded fully (as today)
- **Delta sessions**: embedded with header `### Session {id} (continued — new messages only)`
- **Existing daily file**: embedded as merge context when delta sessions exist for that date
- **Unchanged sessions**: not embedded (biggest savings)

Synthesis instructions updated: "When you see 'continued' sessions, merge new insights into the existing daily summary. Don't duplicate entries already in the existing summary."

### State Update (synthesis.py)

After `apply_results()` succeeds, update `.synthesis-state.json` with current file size and line count for each session included in the run. Atomic write (temp + rename).

### Relationship to .captured

| File | Purpose | Set when |
|------|---------|----------|
| `.captured` | Session is DONE, never look at it again | `mark-captured` (skips today) |
| `.synthesis-state.json` | Last synthesized at this byte offset | After every successful synthesis |

When `mark-captured` marks a session, prune it from `.synthesis-state.json` (cleanup, not required for correctness).

### What Doesn't Change

- Output format (`===DAILY:===`, `===ROUTE:===`, `===END===`)
- `synthesis.py` parsing and applying
- LTM routing, decay, mark-routed, validation
- The `.captured` workflow

## Expected Impact

- **Unchanged sessions**: 0 tokens (was: full transcript re-embedding)
- **Resumed sessions**: only new messages + existing daily summary (~10-20% of full)
- **New sessions**: unchanged (full embedding)
- **Typical 3rd synthesis run of the day**: ~80% token reduction if most sessions are idle
