---
name: synthesize
description: Process raw session transcripts into daily summaries and update long-term memory with patterns and insights. Run when prompted or weekly.
user-invocable: true
---

# Synthesize Skill

Process memory in two phases:

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
3. After summarizing each day, delete the transcript files for that day

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

## JSONL Parsing Notes

Each line in a transcript file is a JSON object. Use tools like `jq` or read line-by-line:

```bash
# Count messages in a transcript
wc -l < transcript.jsonl

# Extract all messages
while IFS= read -r line; do
    echo "$line" | jq -r '.content // empty'
done < transcript.jsonl
```

The JSON structure typically contains conversation turns with role and content fields.
