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
python3 ~/.claude/scripts/indexing.py list-pending
```

### Step 2: Synthesize (if transcripts exist)

**Spawn a subagent** to process transcripts without bloating main context:

```
Use the Task tool with subagent_type="general-purpose", model="haiku" and prompt:
"Process pending memory transcripts using the /synthesize skill instructions.
Read ~/.claude/skills/synthesize/SKILL.md for the full process.
Extract transcripts, create daily summaries, update long-term memory files,
and delete processed transcript files. Return a brief summary of what was processed."
```

Wait for subagent to complete before proceeding.

### Step 3: Load Memory

After synthesis completes:
1. Read and output `~/.claude/memory/global-long-term-memory.md`
2. Read project-specific memory if in a project: `~/.claude/memory/project-memory/{project}-long-term-memory.md`
3. Read and output last 7 days from `~/.claude/memory/daily/`

## Output Format

```
## Synthesis Results
[Subagent summary: "Processed X transcript(s) from [dates]"]

## Long-Term Memory
[contents of global-long-term-memory.md]

## Project Memory: {project}
[contents of project-long-term-memory.md, if applicable]

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
