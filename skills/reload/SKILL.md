---
name: reload
description: Synthesize pending transcripts and load memory context. Use after /clear to process recent work and restore memory.
user-invocable: true
---

# Reload Memory Skill

Synthesize transcripts then load memory into the current conversation. Use after `/clear` to capture recent work and restore context.

## Instructions

### Step 1: Synthesize Pending Transcripts

1. Check for unprocessed transcripts in `~/.claude/memory/transcripts/`
2. For each day with transcripts:
   - Use the extraction script to parse JSONL files
   - Create/update daily summary in `~/.claude/memory/daily/YYYY-MM-DD.md`
   - Delete processed transcript files
3. Update `~/.claude/memory/LONG_TERM.md` if patterns emerge

**Extraction script**:
```bash
python3 ~/.claude/skills/synthesize/extract_transcripts.py
```

### Step 2: Load Memory

1. Read and output `~/.claude/memory/LONG_TERM.md`
2. Read and output last 7 days from `~/.claude/memory/daily/`

## Output Format

```
## Synthesis Results
Processed X transcript(s) from [dates]

## Long-Term Memory
[contents of LONG_TERM.md]

## Recent Daily Summaries

### 2026-01-29
[contents]
...
```

## When to Use

- After `/clear` - captures the session work, restores memory
- Workaround for GitHub issue #21578 (`/clear` has no hook)

**Not needed for**: Compaction (PreCompact hook handles it), session start (SessionStart hook handles it)

## Notes

- If no transcripts to process, skips straight to loading
- Combines `/synthesize` + load in one step
- Safe to run multiple times (idempotent)
