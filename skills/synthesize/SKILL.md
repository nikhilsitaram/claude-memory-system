---
name: synthesize
description: Process raw session transcripts into daily summaries and update long-term memory with patterns and insights. Run when prompted or weekly.
user-invocable: true
---

# Synthesize Skill

Process memory transcripts into compact daily summaries and route learnings to appropriate long-term memory files. This skill is designed to run autonomously as a subagent.

## Tool & Command Guidelines (IMPORTANT)

**Use the Read tool for ALL file reading** - no bash needed, no permissions required:
- Daily summaries: `Read(~/.claude/memory/daily/YYYY-MM-DD.md)`
- Long-term memory: `Read(~/.claude/memory/global-long-term-memory.md)`
- Project index: `Read(~/.claude/memory/projects-index.json)`

**Use Glob tool for listing files** - no bash needed:
- Daily files: `Glob(pattern="*.md", path="~/.claude/memory/daily")`
- Transcript dirs: `Glob(pattern="*", path="~/.claude/memory/transcripts")`

**Use indexing.py for all transcript operations** (cross-platform, no bash rm needed):
```bash
python3 $HOME/.claude/scripts/indexing.py list-pending
python3 $HOME/.claude/scripts/indexing.py extract YYYY-MM-DD
python3 $HOME/.claude/scripts/indexing.py delete YYYY-MM-DD
python3 $HOME/.claude/scripts/indexing.py build-index
```

**Bash restrictions (do NOT use):**
- `rm` commands - use `indexing.py delete` instead
- Tilde (`~`) in bash commands - use `$HOME` instead
- Pipes (`cmd | cmd`) or operators (`||`, `&&`)
- Redirects (`2>/dev/null`)

**Edit/Read/Write tools:** Use `~/.claude/memory/...` paths (NOT `$HOME`)

## Quick Start (for subagent execution)

1. Run extraction: `python3 $HOME/.claude/scripts/indexing.py extract`
2. Use Read/Edit tools to create/update `~/.claude/memory/daily/YYYY-MM-DD.md` (include `## Learnings` section)
3. Route learnings to long-term memory (see Phase 2)
4. Delete processed transcripts: `python3 $HOME/.claude/scripts/indexing.py delete YYYY-MM-DD`
5. Return summary: "Processed N transcripts into daily summaries for [dates]. Routed X learnings to global memory, Y learnings to project memory."

---

## Compactness Guidelines (CRITICAL)

Daily summaries should be **1-4 KB** (250-1000 tokens). Apply these rules:

### 1. Final Solutions Only
- **DO NOT** document debugging iterations, failed approaches, or troubleshooting steps
- **DO** document the final working solution and the key insight that led to it
- If debugging revealed an important lesson, distill it to one learning entry

**Bad** (too detailed):
```
First tried PermissionRequest hooks - patterns didn't match.
Then tried Python script for PermissionRequest - hooks not triggered.
Then tried agent override files - not picked up by subagents.
Finally PreToolUse hooks worked because they run before permission check.
```

**Good** (distilled):
```
PreToolUse hooks work for subagent permissions (unlike PermissionRequest hooks)
```

### 2. Significant Commits Only
- Include only the **final commit** for each feature/fix
- Omit intermediate commits, fixups, and iterations
- One commit hash per logical change is sufficient

**Bad**: `Commits: baaab70, 7c1d8cc, f3b1eb2, 6791763, ea83b7f`
**Good**: `Commit: ea83b7f - "fix: Simplify PreCompact hook"`

### 3. Consolidate Redundant Content
- If the same concept appears multiple times in transcripts, write it **once**
- Merge related topics into single sections
- Don't repeat information that's already in long-term memory

### 4. Learnings: One Per Concept
- Each learning should capture a **unique** insight
- If two learnings teach the same lesson, keep only the clearer one
- Learnings should be actionable, not narrative

### 5. Omit Routine Details
- Don't list every file modified (unless significant)
- Don't document standard workflows (git commands, tool usage)
- Don't include conversation back-and-forth

## Detailed Process (three phases):

## Phase 0: Update Project Index

Before processing transcripts, rebuild the project-to-work-days index:

```bash
python3 $HOME/.claude/scripts/indexing.py build-index
```

This enables project-aware memory loading by scanning `sessions-index.json` files
and mapping projects to their work days.

## Phase 1: Transcripts → Daily Summaries

**Important**: Transcript files are JSONL format (one JSON object per line).
Each line is a JSON object with conversation data.

1. List pending days: `python3 $HOME/.claude/scripts/indexing.py list-pending`
2. For each day with transcript files (.jsonl):
   - Extract transcripts: `python3 $HOME/.claude/scripts/indexing.py extract YYYY-MM-DD`
   - Use Edit tool to create summary in `~/.claude/memory/daily/YYYY-MM-DD.md`
   - Include: topics discussed, decisions made, problems solved, key learnings
   - Preserve any existing user notes (## HH:MM - User Note sections)
   - **Add project tags** (see below)
   - **Extract learnings** (see Learning Extraction below)
3. After summarizing each day, delete: `python3 $HOME/.claude/scripts/indexing.py delete YYYY-MM-DD`

### Project Tags

Add an HTML comment after the H1 heading to indicate which projects had work that day:

```markdown
# 2026-01-25

<!-- projects: personal-shopper, granada -->

## Sessions Summary
...
```

To determine which projects to tag:
- Check `~/.claude/memory/projects-index.json`
- Look for projects that have this date in their `workDays` array
- List project names (comma-separated) in the HTML comment

This enables future filtering/searching by project.

### Learning Extraction

While creating daily summaries, extract learnings and tag them with `[scope/type]`:

**Scopes:**
- `global` - Project-agnostic patterns (applies everywhere)
- `{project-name}` - Project-specific knowledge (e.g., `granada`, `cartwheel`)

**Types:**
- `error` - Exceptions, permission errors, failed commands, SQL errors, bugs fixed
- `best-practice` - Patterns that worked well, efficient approaches discovered
- `data-quirk` - Unexpected data issues, edge cases found
- `decision` - Important choices made and why
- `command` - Useful queries, scripts, commands worth remembering

**How to determine scope:**
- Use project tags from `<!-- projects: ... -->` header
- If learning is specific to that project's data/code, use project scope
- If learning applies broadly (Claude Code, bash, general SQL, etc.), use `global`

Add a `## Learnings` section to each daily summary:

```markdown
## Learnings
- **Permission glob syntax** [global/error]: Used `:*` instead of `/*`
  - Lesson: Use `/` for path separators in permission globs

- **Missing Accident_Key check** [granada/error]: Joined without filtering CWOP
  - Lesson: Always filter Accident_Key != '99' before joins

- **Dataiku heap scans** [global/best-practice]: No indexes on Dataiku tables
  - Lesson: Use row count knowledge for query planning

- **PolicyholderType=1 filter** [granada/data-quirk]: Most rows are renewals
  - Lesson: Filter to PolicyholderType=1 for new business analysis
```

## Phase 2: Route Learnings to Long-Term Memory

After creating/updating daily summaries, route learnings to appropriate files:

### Global Learnings → `~/.claude/memory/global-long-term-memory.md`

1. Read daily summaries from the last 30 days
2. Extract all learnings tagged with `[global/*]`
3. Route to appropriate section based on type:
   - `[global/error]` → `## Error Patterns to Avoid`
   - `[global/best-practice]` → `## Best Practices`
   - `[global/data-quirk]` → `## Key Learnings` (general data insights)
   - `[global/decision]` → `## Patterns & Preferences`
   - `[global/command]` → `## Key Learnings`
4. Apply deduplication and pruning (see Size Management below)
5. Update "Last synthesized" date

### Project Learnings → `~/.claude/memory/project-memory/{project}-long-term-memory.md`

1. Extract learnings tagged with `[{project}/*]`
2. Create file if doesn't exist (use template structure):
   ```markdown
   # {Project Name}

   ## Error Patterns to Avoid
   <!-- Mistakes encountered and how to prevent them -->

   ## Best Practices
   <!-- Patterns and approaches that worked well -->

   ## Data Quirks
   <!-- Gotchas, edge cases, data quality issues -->

   ## Key Decisions
   <!-- Important choices made and their rationale -->

   ## Useful Commands
   <!-- Queries, scripts, commands that helped -->

   ---
   *Last updated: YYYY-MM-DD*
   ```
3. Route to appropriate section based on type:
   - `[{project}/error]` → `## Error Patterns to Avoid`
   - `[{project}/best-practice]` → `## Best Practices`
   - `[{project}/data-quirk]` → `## Data Quirks`
   - `[{project}/decision]` → `## Key Decisions`
   - `[{project}/command]` → `## Useful Commands`
4. Apply deduplication and pruning (see Size Management below)
5. Update "Last updated" date

### Size Management (~10k tokens max per file)

**Deduplication:**
- Before adding a learning, check if a similar entry already exists
- Skip if the concept is already captured (even if wording differs)
- Look for matching keywords, patterns, or lessons

**Update existing entries:**
- If a new learning contradicts or updates an existing one, modify the old entry
- Example: "X was fixed" should remove or update the entry about X being broken

**Pruning (if file exceeds ~10k tokens):**
- Remove oldest entries first (bottom of each section)
- Prioritize keeping:
  - Recent entries (last 30 days)
  - Frequently-referenced patterns
  - High-impact learnings (errors that cost time, crucial decisions)
- Token estimation: bytes / 4 ≈ tokens

### Also Update User Profile Sections

In addition to routing tagged learnings, continue updating global-long-term-memory.md sections:
- `## About Me` - User preferences, communication style
- `## Current Projects` - Active work with context
- `## Technical Environment` - Tools, systems, workflows
- `## Patterns & Preferences` - Coding style, decision patterns

## Output Format for Daily Summary

**Target size: 1-4 KB** (250-1000 tokens). Most days should be under 2 KB.

```markdown
# YYYY-MM-DD

<!-- projects: project-name-1, project-name-2 -->

## Sessions Summary
[1-2 sentences: what was accomplished]

## Topics
- [Topic 1]
- [Topic 2]

## Key Points
[Only decisions, outcomes, and insights - not process details]

## Learnings
- **[Title]** [scope/type]: [Brief description]
  - Lesson: [Actionable takeaway]
```

**Optional sections** (include only if relevant):
- `## Commits` - Only significant commits (1-3 max per topic)
- `## Files Created` - Only if notable new files were created

**Omit these sections** (they add bulk without value):
- Detailed technical steps (belongs in docs, not daily summary)
- Debugging narratives
- File modification lists
- Conversation summaries

## Extraction Commands

Use the indexing script to parse transcripts (use `$HOME`, not `~`):

```bash
# List days with pending transcripts
python3 $HOME/.claude/scripts/indexing.py list-pending

# Extract all pending days
python3 $HOME/.claude/scripts/indexing.py extract

# Extract specific day
python3 $HOME/.claude/scripts/indexing.py extract 2026-01-22

# Save to file for review
python3 $HOME/.claude/scripts/indexing.py extract --output /tmp/transcripts.txt

# Output as JSON (for programmatic use)
python3 $HOME/.claude/scripts/indexing.py extract --json
```

The script handles:
- JSONL format parsing (one JSON object per line)
- Both user and assistant messages (for full context)
- Top-level type field ("user" or "assistant", not "message")
- Nested content arrays with text blocks

## JSONL Structure

Each line is a JSON object with:
- `type`: "user" or "assistant"
- `message.role`: "user" or "assistant"
- `message.content`: string or array of `{type: "text", text: "..."}` objects

## Manual Project Memory Loading

To load project history for any project (regardless of current directory):

```bash
# List all known projects
python3 $HOME/.claude/scripts/load-project-memory.py --list

# Load memory for a specific project
python3 $HOME/.claude/scripts/load-project-memory.py $HOME/personal/cartwheel

# Load more/fewer work days (default is from settings)
python3 $HOME/.claude/scripts/load-project-memory.py $HOME/projects/granada --days 7
```

## Directory Structure

After synthesis, the memory directory should look like:

```
~/.claude/memory/
├── global-long-term-memory.md    # Global patterns (always loaded)
├── settings.json
├── projects-index.json
├── daily/
│   └── YYYY-MM-DD.md             # Daily summaries with ## Learnings
└── project-memory/
    ├── granada-long-term-memory.md
    ├── cartwheel-long-term-memory.md
    └── claude-memory-system-long-term-memory.md
```
