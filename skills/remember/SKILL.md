---
name: remember
description: Save important notes directly to long-term memory's Pinned section. Use when user wants to explicitly preserve something permanently.
user-invocable: true
---

# Remember Skill

Save a note directly to long-term memory's `## Pinned` section. User-invoked `/remember` indicates importance worth preserving permanently.

## Instructions

1. If the user provided text after `/remember`, use that as the note
2. If no text provided, ask what they'd like to remember
3. Determine scope:
   - If note is project-specific, use project long-term memory
   - If note is general/cross-project, use global long-term memory
   - When in doubt, ask the user
4. Format the note with date:

```markdown
- **[Title]** (YYYY-MM-DD): [The note content]
```

5. Append to the `## Pinned` section of the appropriate long-term memory file
6. Confirm where the note was saved

## File Locations

- Global: `~/.claude/memory/global-long-term-memory.md`
- Project: `~/.claude/memory/project-memory/{project}-long-term-memory.md`

## Example

User: `/remember The API rate limit is 100 requests per minute`

Result: Appends to global `## Pinned` section:

```markdown
- **API rate limit** (2026-01-29): 100 requests per minute
```

User: `/remember Granada uses CoverageTypeId for claim-level coverage`

Result: Appends to `granada-long-term-memory.md` `## Pinned` section:

```markdown
- **CoverageTypeId for claim-level coverage** (2026-01-29): Granada uses CoverageTypeId to identify which coverage a loss is filed under
```
