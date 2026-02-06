---
name: synthesize
description: Process raw session transcripts into daily summaries and update long-term memory with patterns and insights. Run during first session of the day, when prompted, or according to a set schedule.
user-invocable: true
---

# Synthesize Skill

Launch a subagent to process memory transcripts into daily summaries and selectively route key learnings to long-term memory.

## Execution

**IMPORTANT:** This skill MUST be executed by launching a subagent. Do NOT run synthesis in the main context.

1. Check for pending transcripts:
   ```bash
   python3 $HOME/.claude/scripts/indexing.py list-pending
   ```

2. If transcripts exist, launch a synthesis subagent:
   ```
   Task(
     subagent_type: "general-purpose",
     model: "haiku",
     prompt: <include full SUBAGENT_PROMPT below with the pending dates>
   )
   ```

3. Report the subagent's summary to the user.

---

## SUBAGENT_PROMPT

Copy this entire prompt when launching the subagent, replacing `{PENDING_DATES}` with the actual dates:

```
You are synthesizing memory transcripts for: {PENDING_DATES}

## Tool Guidelines

**File operations** - use tilde paths (`~/.claude/...`):
- `Read(~/.claude/memory/daily/YYYY-MM-DD.md)`
- `Read(~/.claude/memory/global-long-term-memory.md)`
- `Edit(~/.claude/memory/...)` for updates

**Transcript operations** - use `$HOME` in bash:
- `python3 $HOME/.claude/scripts/indexing.py extract YYYY-MM-DD` - Extract and mark as captured
- `python3 $HOME/.claude/scripts/indexing.py extract --no-mark YYYY-MM-DD` - Preview only
- `python3 $HOME/.claude/scripts/decay.py` - Run decay after routing

## Process

> **Note:** The subagent runs Phases 1-3 below. Additional infrastructure steps (indexing, migration, timestamp update) are handled by the calling script and `load_memory.py`, not the subagent.

### Phase 1: Create Daily Summaries

For each date, extract transcripts and create/update `~/.claude/memory/daily/YYYY-MM-DD.md`:

```markdown
# YYYY-MM-DD

## Actions
<!-- What was done. Tag [scope/action]. -->
- [project-name/implement] What was accomplished

## Decisions
<!-- Important choices and rationale. Tag [scope/decision]. -->
- [project-name/design] Choice made and why

## Learnings
<!-- Patterns, gotchas, insights. Tag [scope/type]. -->
- [scope/gotcha] Unexpected behavior discovered
- [scope/pattern] Proven method or approach

## Lessons
<!-- Actionable takeaways. Tag [scope/type]. -->
- [scope/insight] Mental model or understanding
- [scope/tip] Useful command or shortcut
```

**Compactness rules:**
- Final solutions only - no debugging narratives
- One learning per concept - deduplicate
- Omit routine details

**Tag format:** `[scope/type]` where scope is `global` or `{project-name}`

### Phase 2: Selective Long-Term Routing

**CRITICAL: Be highly selective.** Long-term memory is for enduring knowledge, not a log of everything learned.

**Route TO long-term memory (rare):**
- Fundamental patterns that will apply for months/years
- Hard-won lessons from multi-hour debugging
- Safety-critical information (data loss, security)
- Non-obvious gotchas that would be costly to rediscover
- Architecture decisions with lasting impact

**Do NOT route (most things):**
- Routine implementation details
- Version-specific fixes
- One-time configuration steps
- Things easily re-discoverable via search
- Learnings that might not hold up over time

**Routing destinations:**
- Global (`[global/*]`) → `~/.claude/memory/global-long-term-memory.md`
- Project (`[project/*]`) → `~/.claude/memory/project-memory/{project}-long-term-memory.md`

**Format when routing:**
- Add date prefix: `(YYYY-MM-DD) [type] Description`
- Remove scope from tag (file is already scoped)
- Check for duplicates before adding

**Create missing project files** from template at `~/.claude/memory/templates/project-long-term-memory.md`

### Phase 3: Decay & Finalize

1. Run decay: `python3 $HOME/.claude/scripts/decay.py`
2. Update timestamp: `python3 -c "from datetime import datetime, timezone; from pathlib import Path; Path.home().joinpath('.claude/memory/.last-synthesis').write_text(datetime.now(timezone.utc).isoformat())"`

### Output

Return a summary: "Processed N days. Created/updated daily summaries for [dates]. Routed X items to long-term memory (list them). Archived Y old items."
```

---

## Reference: Tag Types

**Actions:** `implement`, `improve`, `document`, `analyze`
**Decisions:** `design`, `tradeoff`, `scope`
**Learnings:** `gotcha`, `pitfall`, `pattern`
**Lessons:** `insight`, `tip`, `workaround`

## Reference: Pinning Criteria

Items in long-term memory can be moved to `## Pinned` section (protected from decay):
- Fundamental architecture patterns
- Safety-critical information
- Cross-project patterns that proved valuable over time
