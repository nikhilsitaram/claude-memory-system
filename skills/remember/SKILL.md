---
name: remember
description: Save specific notes or highlights to today's daily memory log. Use when user wants to explicitly remember something from the conversation.
user-invocable: true
---

# Remember Skill

Save a note to today's daily memory log at `~/.claude/memory/daily/YYYY-MM-DD.md`.

## Instructions

1. If the user provided text after `/remember`, use that as the note
2. If no text provided, ask what they'd like to remember
3. Create the daily file if it doesn't exist
4. Append a timestamped entry:

```markdown
## HH:MM - User Note

[The note content]
```

5. Confirm the note was saved

## Example

User: `/remember The API rate limit is 100 requests per minute`

Result: Appends to `~/.claude/memory/daily/2025-01-29.md`:

```markdown
## 14:32 - User Note

The API rate limit is 100 requests per minute
```
