---
name: consolidate
description: Run memory consolidation to merge redundant memories
user-invokable: true
---

# /consolidate

Run the memory consolidation pipeline to find and merge redundant memories.

## What it does

1. Finds clusters of semantically similar active memories via three strategies:
   - Cosine similarity from sqlite-vec embeddings (threshold >= 0.80)
   - Entity overlap (Jaccard >= 0.70) for memories sharing entity metadata
   - Token overlap coefficient (>= 0.45) for paraphrased content
2. For each cluster, asks an LLM whether to MERGE (redundant) or SKIP (evolving knowledge)
3. Merged clusters produce a single new memory that supersedes the originals

## Usage

**Important:** Requires sqlite-vec + fastembed. The script will exit with an
error if sqlite-vec is not available — run with the project venv Python.

Run consolidation now (bypasses the daily schedule):

```bash
.venv/bin/python3 scripts/consolidation.py --force
```

Preview what would be consolidated without making changes:

```bash
.venv/bin/python3 scripts/consolidation.py --dry-run
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
