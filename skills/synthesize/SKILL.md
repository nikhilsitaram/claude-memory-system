---
name: synthesize
description: Process raw session transcripts into daily summaries and update long-term memory with patterns and insights. Run during first session of the day, when prompted, or according to a set schedule.
user-invocable: true
---

# Synthesize Skill

Process memory transcripts into compact daily summaries and route learnings to appropriate long-term memory files.

## Tool Guidelines

**File operations** - use tilde paths (`~/.claude/...`):
- `Read(~/.claude/memory/daily/YYYY-MM-DD.md)`
- `Read(~/.claude/memory/global-long-term-memory.md)`
- `Edit(~/.claude/memory/...)` for updates

**Transcript operations** - use `$HOME` in bash:
```bash
python3 $HOME/.claude/scripts/indexing.py list-pending
python3 $HOME/.claude/scripts/indexing.py extract YYYY-MM-DD
python3 $HOME/.claude/scripts/indexing.py delete YYYY-MM-DD
python3 $HOME/.claude/scripts/indexing.py build-index
```

**Decay** - run after routing learnings:
```bash
python3 $HOME/.claude/scripts/decay.py
python3 $HOME/.claude/scripts/decay.py --dry-run  # Preview only
```

## Quick Start

1. `indexing.py build-index` - Update project index
2. `indexing.py extract` - Get pending transcripts
3. Create/update `~/.claude/memory/daily/YYYY-MM-DD.md` with `## Learnings`
4. Route learnings to long-term memory (Phase 2)
5. `decay.py` - Archive old learnings
6. `indexing.py delete YYYY-MM-DD` - Clean up transcripts
7. Update `.last-synthesis`: `python3 -c "from datetime import datetime, timezone; from pathlib import Path; Path.home().joinpath('.claude/memory/.last-synthesis').write_text(datetime.now(timezone.utc).isoformat())"`

## Compactness Rules (Target: 1-4 KB per daily summary)

- **Final solutions only** - no debugging narratives or failed attempts
- **One commit per feature** - omit intermediate fixups
- **One learning per concept** - deduplicate similar insights
- **Omit routine details** - no file lists, standard workflows, conversation back-and-forth

## Phase 1: Daily Summaries

For each day with transcripts, create `~/.claude/memory/daily/YYYY-MM-DD.md`:

```markdown
# YYYY-MM-DD

<!-- projects: project-name-1, project-name-2 -->

## Sessions Summary
[1-2 sentences: what was accomplished]

## Topics
- [Topic 1]
- [Topic 2]

## Key Points
[Only decisions, outcomes, insights - not process details]

## Learnings
- **Title** [scope/type] (YYYY-MM-DD): Brief description
  - Lesson: Actionable takeaway
```

### Learning Tags

**Scopes:** `global` (project-agnostic) or `{project-name}` (project-specific)

**Types:**
- `error` - Bugs, failed commands, exceptions
- `best-practice` - Patterns that worked well
- `data-quirk` - Edge cases, gotchas
- `decision` - Important choices and rationale
- `command` - Useful queries, scripts

**IMPORTANT:** Always include creation date `(YYYY-MM-DD)` - enables decay.

## Phase 2: Route Learnings

### Global → `~/.claude/memory/global-long-term-memory.md`

| Tag | Section |
|-----|---------|
| `[global/error]` | `## Error Patterns to Avoid` |
| `[global/best-practice]` | `## Best Practices` |
| `[global/data-quirk]` | `## Key Learnings` |
| `[global/decision]` | `## Patterns & Preferences` |
| `[global/command]` | `## Key Learnings` |

### Project → `~/.claude/memory/project-memory/{project}-long-term-memory.md`

| Tag | Section |
|-----|---------|
| `[{project}/error]` | `## Error Patterns to Avoid` |
| `[{project}/best-practice]` | `## Best Practices` |
| `[{project}/data-quirk]` | `## Data Quirks` |
| `[{project}/decision]` | `## Key Decisions` |
| `[{project}/command]` | `## Useful Commands` |

**Deduplication:** Before adding, check if concept already exists (even with different wording). Skip duplicates.

**Templates** (read for section structure):
- Global: `Read(~/.claude/memory/templates/global-long-term-memory.md)`
- Project: `Read(~/.claude/memory/templates/project-long-term-memory.md)`

## Phase 3: Decay

Run decay.py to archive learnings older than 30 days:

```bash
python3 $HOME/.claude/scripts/decay.py
```

**Protected sections** (never decay): `## About Me`, `## Current Projects`, `## Technical Environment`, `## Patterns & Preferences`, `## Pinned`

**Decay-eligible sections:** `## Key Learnings`, `## Error Patterns to Avoid`, `## Best Practices`, `## Data Quirks`, `## Key Decisions`, `## Useful Commands`

Learnings without `(YYYY-MM-DD)` dates are protected from decay.

## Pinning Criteria

**Move to `## Pinned`:**
- Fundamental patterns (architecture decisions, core workflows)
- Hard-won lessons (multi-day debugging, complex investigations)
- Safety-critical info (data loss risks, security issues)
- Cross-project patterns

**Leave in normal sections:**
- Version-specific fixes
- Temporary workarounds
- Recently discovered patterns (let them prove value first)

## Output

Return: "Processed N transcripts into daily summaries for [dates]. Routed X learnings to global memory, Y learnings to project memory. Archived Z old learnings."
