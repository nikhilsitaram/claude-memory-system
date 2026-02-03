---
name: recall
description: Search through all historical daily memory files to find information from past sessions. Use when user asks about past work, decisions, discussions, or topics that might have relevant history.
user-invocable: true
---

# Recall Skill

## Proactive Use (IMPORTANT)

Use this skill PROACTIVELY without being asked when:
- User asks about past decisions, discussions, or work ("what did we decide about X?")
- User references something from more than a week ago
- User asks "when did we...", "did we ever...", "have we talked about..."
- A topic comes up that likely has relevant history (projects, patterns, bugs fixed)
- User seems to expect you to remember something not in the loaded context

Don't wait to be prompted - search first, then answer with full context.

## Instructions

1. Get the search query from the user
2. List all files in `~/.claude/memory/daily/`
3. Read through each file searching for relevant content
4. Return matching excerpts with their dates
5. Summarize findings if there are many matches

## Example Usage

- `/recall authentication` → Search all daily files for authentication mentions
- `/recall what was I working on in December?` → Read December files, summarize work

## Search Strategy

1. For specific topics: search files for keywords
2. For time-based queries: read files from the specified time period
3. Always include date context when returning results

## Implementation

Use grep to find matches across all daily files:

```bash
# Search for a term across all daily files
grep -r -l "search_term" ~/.claude/memory/daily/

# Search with context
grep -r -B2 -A2 "search_term" ~/.claude/memory/daily/
```

For time-based queries, list and read files from the relevant date range:

```bash
# List all daily files from January
ls ~/.claude/memory/daily/2026-01-*.md
```

Always provide the date with each result so the user knows when something happened.
