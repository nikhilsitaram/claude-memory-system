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

## Actions
<!-- What was done. Tag [scope/action]. -->
- [project-name/action] What was accomplished for this project
- [global/action] What was accomplished that applies broadly

## Decisions
<!-- Important choices and rationale. Tag [project/decision] or [global/decision]. -->
- [project-name/decision] Choice made and why it was made
- [global/decision] Project-agnostic choice and rationale

## Learnings
<!-- Patterns, gotchas, insights. Tag [scope/type]. -->
- [scope/type] Description of the pattern or insight

## Lessons
<!-- Actionable takeaways. Tag [scope/type]. -->
- [scope/type] Actionable takeaway or rule
```

### Content Guidance

**Actions** - "What was done":
- `[scope/implement]` - New features, setup, configuration
- `[scope/improve]` - Bug fixes, refactoring, optimization
- `[scope/document]` - Documentation work
- `[scope/analyze]` - Research, exploration

**Decisions** - "What was chosen and why":
- `[scope/design]` - Architecture, structure, technology choices
- `[scope/tradeoff]` - Choosing between competing options with rationale
- `[scope/scope]` - What to include/exclude/defer
- Always include the "why" - bare choices without rationale go in Actions

**Learnings** - "What was discovered" (observations):
- Format: `- [scope/type] Description` - NO date (date comes from filename)
- `[scope/gotcha]` - Unexpected behavior, edge cases, surprises
- `[scope/pitfall]` - Errors, bugs, mistakes to avoid
- `[scope/pattern]` - How-to approaches, proven methods

**Lessons** - "What to do about it" (takeaways):
- Format: `- [scope/type] Actionable takeaway`
- `[scope/insight]` - Why things work, mental models
- `[scope/tip]` - Shortcuts, commands, snippets
- `[scope/workaround]` - Temporary fixes, known limitations
- Learnings and Lessons don't need to be 1:1 paired

### Tag System

**Scopes:** `global` (project-agnostic) or `{project-name}` (project-specific)

**Action Subtypes:**
- `implement` - New features, setup, configuration
- `improve` - Bug fixes, refactoring, optimization
- `document` - Documentation work
- `analyze` - Research, exploration

**Decision Subtypes:**
- `design` - Architecture, structure, technology choices
- `tradeoff` - Choosing between competing options
- `scope` - What to include/exclude/defer

**Learning Subtypes (observations/discoveries):**
- `gotcha` - Unexpected behavior, edge cases, surprises
- `pitfall` - Errors, bugs, mistakes to avoid
- `pattern` - How-to approaches, proven methods

**Lesson Subtypes (takeaways/actions):**
- `insight` - Why things work, mental models
- `tip` - Shortcuts, commands, snippets
- `workaround` - Temporary fixes, known limitations

**IMPORTANT:** Do NOT include date in daily file learnings - date is derived from filename during long-term routing.

## Phase 2: Route to Long-Term Memory

Route daily content to long-term memory files:
- Global (`[global/*]`) → `~/.claude/memory/global-long-term-memory.md`
- Project (`[project/*]`) → `~/.claude/memory/project-memory/{project}-long-term-memory.md`

### Routing Table

| Daily Section | Daily Tag | Long-Term Section |
|---------------|-----------|-------------------|
| `## Actions` | `[scope/action]` | `## Key Actions` |
| `## Decisions` | `[scope/decision]` | `## Key Decisions` |
| `## Learnings` | `[scope/type]` | `## Key Learnings` |
| `## Lessons` | `[scope/type]` | `## Key Lessons` |

### Routing Rules

1. **Scope-stripping:** Remove scope from tag (long-term file is already scoped)
2. **Date-first format:** Prefix with `(YYYY-MM-DD)` from daily filename, then `[type]`
3. **Deduplication:** Check if concept already exists (skip duplicates)
4. **Selective routing:** Only route items worth preserving long-term (not every action/decision)

### Examples

```markdown
# In daily file (2026-02-02.md):
## Actions
- [claude-memory-system/implement] Built age-based decay with 30-day threshold

## Decisions
- [claude-memory-system/design] Use age-based decay instead of access tracking: simpler to implement, predictable behavior

## Learnings
- [claude-memory-system/gotcha] Path encoding is lossy - both / and . become -

## Lessons
- [claude-memory-system/insight] Always read sessions-index.json for authoritative path

# Routed to project-memory/claude-memory-system-long-term-memory.md:
## Key Actions
- (2026-02-02) [implement] Built age-based decay with 30-day threshold

## Key Decisions
- (2026-02-02) [design] Use age-based decay instead of access tracking: simpler to implement, predictable behavior

## Key Learnings
- (2026-02-02) [gotcha] Path encoding is lossy - both / and . become -

## Key Lessons
- (2026-02-02) [insight] Always read sessions-index.json for authoritative path
```

**Templates** (read for section structure):
- Global: `Read(~/.claude/memory/templates/global-long-term-memory.md)`
- Project: `Read(~/.claude/memory/templates/project-long-term-memory.md)`

## Filtering Specification

Daily files use mandatory tags (`[project]` or `[global]`) to enable project-specific filtering during memory loading.

**Tag precedence:** Tag determines scope, content is informational only.
- `- [global] Analyzed token usage for claude-memory-system` → Global (despite mentioning project)
- `- [claude-memory-system] Analyzed token usage` → Project-specific

**Untagged content:** Treated as global (fallback). Avoid by always tagging.

**Tag format (all use `[scope/type]`):**
- Actions: `[scope/implement]`, `[scope/improve]`, `[scope/document]`, `[scope/analyze]`
- Decisions: `[scope/design]`, `[scope/tradeoff]`, `[scope/scope]`
- Learnings: `[scope/gotcha]`, `[scope/pitfall]`, `[scope/pattern]`
- Lessons: `[scope/insight]`, `[scope/tip]`, `[scope/workaround]`

**Filtering behavior** (implemented in load_memory.py):
- When loading project memory, include items tagged with that project name
- Global items (`[global]`) always included
- Untagged items treated as global

## Phase 3: Decay

Run decay.py to archive learnings older than 30 days:

```bash
python3 $HOME/.claude/scripts/decay.py
```

**Protected sections** (never decay): `## About Me`, `## Current Projects`, `## Technical Environment`, `## Patterns & Preferences`, `## Pinned`

**Decay-eligible sections:** `## Key Actions`, `## Key Decisions`, `## Key Learnings`, `## Key Lessons`

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
