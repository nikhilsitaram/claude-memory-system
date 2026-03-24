---
name: consolidate
description: Run memory consolidation to merge redundant memories
user-invokable: true
---

# /consolidate

Run the memory consolidation pipeline to find and merge redundant memories.

## What it does

1. Finds clusters of semantically similar active memories (cosine similarity >= 0.80)
2. For each cluster, asks an LLM whether to MERGE (redundant) or SKIP (evolving knowledge)
3. Merged clusters produce a single new memory that supersedes the originals

## Usage

Run consolidation now (bypasses the daily schedule):

```bash
python3 ~/.claude/scripts/consolidation.py --force
```

Preview what would be consolidated without making changes:

```bash
python3 ~/.claude/scripts/consolidation.py --dry-run
```

## When to use

- When you see many similar memories in the web dashboard
- After a batch of synthesis runs that may have produced overlaps
- When the health check reports high memory redundancy

## Notes

- Consolidation normally runs daily as a post-step after synthesis
- The LLM will refuse to merge memories that represent evolving understanding
- Merged results preserve provenance via supersedes edges
- Original memories are soft-deleted (salience=0) but not removed from the DB
