---
name: synthesize
description: Process raw session transcripts into daily summaries and update long-term memory with patterns and insights. Run when prompted or weekly.
user-invocable: true
---

# Synthesize Skill

Process memory transcripts into compact daily summaries. This skill is designed to run autonomously as a subagent.

## Quick Start (for subagent execution)

1. Run extraction: `python3 ~/.claude/skills/synthesize/extract_transcripts.py`
2. For each day in output, create/update `~/.claude/memory/daily/YYYY-MM-DD.md`
3. Delete processed transcripts: `rm -rf $HOME/.claude/memory/transcripts/YYYY-MM-DD/` (pre-approved permission - must use $HOME, not ~)
4. Return summary: "Processed N transcripts into daily summaries for [dates]"

---

## Detailed Process (three phases):

## Phase 0: Update Project Index

Before processing transcripts, rebuild the project-to-work-days index:

```bash
python3 ~/.claude/skills/synthesize/build_projects_index.py
```

This enables project-aware memory loading by scanning `sessions-index.json` files
and mapping projects to their work days.

## Phase 1: Transcripts → Daily Summaries

**Important**: Transcript files are JSONL format (one JSON object per line).
Each line is a JSON object with conversation data.

1. List all directories in `~/.claude/memory/transcripts/`
2. For each day with transcript files (.jsonl):
   - Read and parse the JSONL files (each line is a JSON object)
   - Extract the conversation content from the JSON
   - Create a summary in `~/.claude/memory/daily/YYYY-MM-DD.md`
   - Include: topics discussed, decisions made, problems solved, key learnings
   - Preserve any existing user notes (## HH:MM - User Note sections)
   - **Add project tags** (see below)
3. After summarizing each day, delete using: `rm -rf $HOME/.claude/memory/transcripts/YYYY-MM-DD/` (must use $HOME, not ~ - this exact format is pre-approved)

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

## Phase 2: Daily Summaries → Long-Term Memory

1. Read `~/.claude/memory/LONG_TERM.md`
2. Read daily summaries from the last 30 days
3. Identify:
   - Recurring themes or projects
   - User preferences and patterns
   - Important learnings to preserve
4. Update LONG_TERM.md sections intelligently (don't duplicate, merge/update)
5. Update the "Last synthesized" date at the bottom

## Output Format for Daily Summary

```markdown
# YYYY-MM-DD

<!-- projects: project-name-1, project-name-2 -->

## Sessions Summary
[Brief overview of what was discussed across sessions]

## Topics
- [Topic 1]
- [Topic 2]

## Key Points
- [Important insights]

## Decisions Made
- [Any decisions or conclusions]
```

## Extraction Script

Use the included extraction script to parse transcripts:

```bash
# Extract all days
python3 ~/.claude/skills/synthesize/extract_transcripts.py

# Extract specific day
python3 ~/.claude/skills/synthesize/extract_transcripts.py 2026-01-22

# Save to file for review
python3 ~/.claude/skills/synthesize/extract_transcripts.py --output /tmp/transcripts.txt
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
python3 ~/.claude/scripts/load-project-memory.py --list

# Load memory for a specific project
python3 ~/.claude/scripts/load-project-memory.py ~/personal/personal-shopper

# Load more/fewer work days (default is 14)
python3 ~/.claude/scripts/load-project-memory.py ~/claude-code/projects/granada --days 7
```
