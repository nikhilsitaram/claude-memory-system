---
name: reload
description: Synthesize pending transcripts and load memory context. Use after /clear to process recent work and restore memory.
user-invocable: true
---

# Reload Memory Skill

Synthesize transcripts then load memory into the current conversation. Use after `/clear` to capture recent work and restore context.

## Instructions

### Step 1: Check for Pending Transcripts

```bash
ls ~/.claude/memory/transcripts/*.jsonl 2>/dev/null | wc -l
```

### Step 2: Synthesize (if transcripts exist)

**Spawn a subagent** to process transcripts without bloating main context:

```
Use the Task tool with subagent_type="general-purpose" and prompt:
"Process pending memory transcripts using the /synthesize skill instructions.
Read ~/.claude/skills/synthesize/SKILL.md for the full process.
Extract transcripts, create daily summaries, update LONG_TERM.md if needed,
and delete processed transcript files. Return a brief summary of what was processed."
```

Wait for subagent to complete before proceeding.

### Step 3: Load Memory

After synthesis completes:
1. Read and output `~/.claude/memory/LONG_TERM.md`
2. Read and output last 7 days from `~/.claude/memory/daily/`

## Output Format

```
## Synthesis Results
[Subagent summary: "Processed X transcript(s) from [dates]"]

## Long-Term Memory
[contents of LONG_TERM.md]

## Recent Daily Summaries

### 2026-02-02
[contents]
...
```

## When to Use

- After `/clear` - captures the session work, restores memory
- Workaround for GitHub issue #21578 (`/clear` has no hook)

**Not needed for**: Compaction (PreCompact hook handles it), session start (SessionStart hook handles it)

## Why Subagent?

Raw transcripts can be 30k+ tokens. Processing them in the main conversation would bloat context permanently. The subagent processes transcripts in isolation and returns only a brief summary, keeping the main conversation lean.

## Notes

- If no transcripts to process, skip straight to loading
- Safe to run multiple times (idempotent)
