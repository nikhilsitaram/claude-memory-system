# Design: Memory System Refactor — Phase 2 (Intelligence)

**Date:** 2026-03-21
**Parent issue:** #64
**Depends on:** Phase 1 (Foundation) — storage (#49/#55/#63), text processing (#47/#53), vector search (#46)

## Problem

The memory system's write path has three weaknesses:

1. **Dedup is brittle.** `is_routed_match()` in `memory_utils.py` uses keyword overlap at 0.6 threshold. It misses semantic duplicates ("likes Python" vs "prefers Python for scripting"), creates false positives on common words, cannot detect contradictions ("uses PostgreSQL" vs "migrated to MySQL"), and cannot do partial updates (enriching an existing memory with new details).

2. **Decay is binary.** `decay.py` uses flat age thresholds (30 calendar days global, 20 working days project). All memories decay at the same rate regardless of access frequency. Frequently-accessed memories are treated identically to never-accessed ones. No mechanism to detect factually outdated memories.

3. **No structured metadata.** The `entities` JSON column on chunks exists but is never populated. No way to filter memories by URL, date reference, project name, library, or concept without full-text search.

## Goal

Make the write path intelligent: the synthesis LLM makes informed ADD/UPDATE/DELETE decisions against existing memories (replacing keyword dedup), salience scoring replaces binary age-based decay, and LLM-extracted entity metadata enables filtered retrieval.

## Success Criteria

1. Synthesis produces explicit CRUD decisions (ADD/UPDATE/DELETE/NOOP) for each extracted fact, using vector-retrieved existing memories as context — all within the existing single synthesis LLM call (no additional LLM calls)
2. Contradicted memories get bi-temporal invalidation (`valid_to` set) rather than silent deletion, preserving historical context
3. Frequently-accessed memories decay slower (hot tier, λ=0.005) than untouched ones (cold tier, λ=0.05)
4. `load_memory.py` tracks access counts and timestamps on served chunks/nodes in `memory.db`
5. Chunks have populated `entities` JSON with LLM-extracted structured data (project names, libraries, concepts, people, URLs, dates) — extracted as part of the synthesis CRUD output
6. A one-time backfill command (`scripts/backfill.py`) re-extracts entities for all existing chunks using Sonnet, idempotent and safe to re-run
7. All existing 816 tests pass; new tests cover each worktree's functionality

## Non-Goals

- Replacing markdown as source of truth (Phase 3)
- Smart context loading / semantic retrieval at SessionStart (Phase 3)
- Triplet embeddings, consolidation, rule extraction (Phase 4)
- Changes to existing skills (`/remember`, `/recall`, `/synthesize`, `/settings`, `/projects`)
- Regex-based entity extraction (LLM extraction is more valuable and comes free with the synthesis call)

## Architecture Approach

### Two Parallel Worktrees

Phase 2 has two independent worktrees that develop in parallel, plus a backfill script that ships with both but runs after merge:

```
[salience-decay]  ||  [intelligent-synthesis]    (parallel)
          \                    /
           [backfill.py]                         (runs once after merge, before Phase 3)
```

### Pre-Requisite: Branch Reconciliation

Before starting Phase 2 worktrees, the `memory-system-refactor` branch must incorporate the vector search work (currently on `main` via squash merge `80c9444`). Vector search (`scripts/embeddings.py`) is a direct dependency for the intelligent-synthesis worktree's pre-retrieval step. The vector search commits should be brought onto `memory-system-refactor` and rolled back from `main`.

## Worktree 1: `salience-decay` (#50)

**What it delivers:**
- Access tracking in `load_memory.py` (increment `access_count`, update `last_accessed`, reinforce salience on served chunks/nodes)
- Tiered salience decay in `decay.py` (hot/warm/cold λ rates replacing binary age threshold)
- Associative reinforcement propagation via graph neighbors

### Access Tracking (in `load_memory.py`)

When chunks/nodes are served at SessionStart, batch-write to `memory.db`:
- Increment `access_count` on the chunk/node row
- Update `last_accessed` to current UTC timestamp
- Reinforce salience with diminishing returns:
  ```python
  new_salience = min(1.0, salience + 0.18 * (1.0 - salience))
  ```
- Associative reinforcement for graph neighbors:
  ```python
  for linked_node in graph.neighbors(accessed_node):
      boost = 0.18 * edge_weight * accessed_node.salience
      linked_node.salience = min(1.0, linked_node.salience + boost)
  ```
- Uses `BEGIN IMMEDIATE` with retry on `SQLITE_BUSY` for concurrency safety

### Tiered Decay (replaces `decay.py` logic)

```python
def pick_tier(chunk):
    dt = days_since(chunk.last_accessed)
    if dt < 6 and (chunk.access_count > 5 or chunk.salience > 0.7):
        return "hot",  0.005
    if dt < 6 or chunk.salience > 0.4:
        return "warm", 0.02
    return "cold", 0.05

def decay_salience(chunk):
    tier, lam = pick_tier(chunk)
    dt = days_since(chunk.last_accessed)
    factor = exp(-lam * (dt / (chunk.salience + 0.1)))
    chunk.salience = clamp(chunk.salience * factor, 0.0, 1.0)
```

- Archive threshold: `salience < 0.05` (replaces the current 30-day / 20-working-day cutoff)
- Auto-pinned sections (`## About Me`, `## Pinned`, etc.) remain protected — unchanged
- Decay runs on DB metadata, then syncs changes back to markdown files (markdown remains source of truth in Phase 2)

### Migration for Existing Data

- All existing chunks: `salience=1.0` (already the schema default), `access_count=0`, `last_accessed=created_at`
- Conservative approach — real salience scores accumulate naturally via access tracking
- No data loss: the old `decay.py` binary thresholds are replaced, not run alongside

### Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Reinforcement eta | 0.18 | OpenMemory's tuned value; diminishing returns prevents runaway salience |
| Archive threshold | 0.05 | Low enough that only truly forgotten memories are archived |
| Hot tier boundary | 6 days, access_count>5 or salience>0.7 | Matches typical work-week access patterns |
| Concurrency model | BEGIN IMMEDIATE + retry | WAL mode handles concurrent reads; IMMEDIATE prevents write conflicts |

### Files Modified

- `scripts/decay.py` — replace `should_decay_entry()` with salience threshold, add tiered decay math
- `scripts/load_memory.py` — add access tracking batch writes after memory loading
- `scripts/storage.py` — add `update_chunk_access()`, `update_node_salience()`, `batch_update_access()` helpers
- `tests/test_decay.py` — new tests for tiered decay math, tier classification, archive threshold
- `tests/test_load_memory.py` — new tests for access tracking writes
- `tests/test_storage.py` — new tests for access/salience update helpers

## Worktree 2: `intelligent-synthesis` (#52 + #54 + #48)

**What it delivers:**
- LLM-driven CRUD decisions (ADD/UPDATE/DELETE/NOOP) integrated into the existing synthesis call
- Bi-temporal edge tracking for contradiction detection
- LLM-extracted entity metadata on every CRUD operation
- Replaces `is_routed_match()` keyword overlap as the primary dedup strategy

### Modified Synthesis Pipeline

```
[Before]
transcripts → LLM → daily summaries + route entries → keyword dedup (is_routed_match) → write

[After]
transcripts + vector-retrieved existing memories → LLM → daily summaries + CRUD decisions (with entities) → apply ops → write
```

### Step-by-Step Flow

**1. Pre-retrieval** (new step in `synthesis_cron.py`):
Before the synthesis LLM call, retrieve existing memories relevant to the transcript content:
- Extract key topics/terms from transcripts (algorithmic, not LLM)
- Vector-search `vec_chunks` for top-K similar existing memories per topic
- Format retrieved memories with their chunk IDs as context for the LLM

**2. Enhanced synthesis prompt**:
The LLM receives:
- Session transcripts (existing, unchanged)
- Existing relevant memories with their IDs (new context section)
- Instructions to produce daily summaries AND memory CRUD decisions with entity extraction

**3. New output format** (extends existing `===PROJECT:X===`):
```
===PROJECT:my-project===
- [implement] Added retry logic to API client
- [LTM][design] Switched from REST to gRPC for internal services
===MEMORY_OPS===
{"ops": [
  {"action": "ADD", "fact": "project uses gRPC for internal services", "scope": "my-project", "section": "Key Decisions", "entities": ["gRPC", "my-project"]},
  {"action": "UPDATE", "id": "chunk_abc123", "fact": "API client has retry logic with exponential backoff", "entities": ["API client"]},
  {"action": "DELETE", "id": "chunk_def456", "reason": "Contradicted: project no longer uses REST internally"},
  {"action": "NOOP", "id": "chunk_ghi789", "reason": "Already captured"}
]}
===END===
```

**4. Apply pipeline** (modified `apply_results()` in `synthesis.py`):
- **ADD**: Insert new chunk + node into DB, append entry to LTM markdown file
- **UPDATE**: Modify existing chunk content + metadata in DB, update corresponding line in markdown
- **DELETE**: Set `valid_to` on related edges (bi-temporal invalidation), reduce salience to 0, archive entry from markdown (move to `## Archived` section)
- **NOOP**: Skip; optionally increment `evidence_count` to track corroboration

**5. Bi-temporal edges** (#54):
When the LLM issues a DELETE (contradiction detected):
- Old edge: `valid_to = session_date`, `expired_at = now()`
- New fact: creates fresh edge with `valid_from = session_date`
- Both remain in DB — queryable by validity window:
  ```sql
  -- Current truth
  SELECT * FROM edges WHERE valid_to IS NULL;
  -- What was true on a given date?
  SELECT * FROM edges WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?);
  ```

**6. Entity extraction** (#48):
Each CRUD operation includes an `entities` array — LLM-extracted (not regex). Captures:
- Project names, library names, tool names
- People, mentions
- URLs, dates, concepts
- Stored in the `entities` JSON column on the chunk

### Key Decisions

| Decision | Choice | Why |
|----------|--------|-----|
| Entity extraction | LLM (not regex) | Free with existing synthesis call; captures high-value entities (projects, concepts) regex can't |
| Dedup integration | Inside synthesis call | One LLM call instead of two; synthesis already reads transcripts |
| #54 coupling | With #52 | Fact extraction creates edges; contradiction detection invalidates them — one pipeline |
| Pre-retrieval method | Vector search | SimHash only catches near-dupes; vector search finds semantic equivalents |
| Output format | JSON in `===MEMORY_OPS===` block | Structured, parseable, coexists with existing `===PROJECT===` format |
| DELETE behavior | Bi-temporal invalidation | Preserves history; enables "what changed?" queries |

### Files Modified

- `scripts/synthesis_cron.py` — add pre-retrieval step (vector search existing memories before LLM call)
- `scripts/synthesis.py` — parse `===MEMORY_OPS===` block, implement CRUD apply logic, bi-temporal edge handling
- `scripts/storage.py` — add `invalidate_edge()`, `update_chunk_content()`, `query_chunks_for_retrieval()` helpers
- `scripts/load_memory.py` — update `write_synthesis_prompt()` to include retrieved existing memories as context
- `tests/test_synthesis.py` — new tests for CRUD parsing, apply logic, bi-temporal edges
- `tests/test_storage.py` — new tests for new CRUD helpers

## Backfill Script (`scripts/backfill.py`)

Ships with Phase 2, runs once after both worktrees merge (before Phase 3 starts):

- Scans all chunks in `memory.db` where `entities IS NULL`
- Batches chunks and sends to Sonnet for entity extraction
- Populates `entities` JSON column
- Idempotent: skips chunks where `entities IS NOT NULL`
- Uses `claude -p` with Sonnet model
- Progress reporting: prints chunks processed / total

### Why Sonnet for All Chunks

Quality matters more than speed for the backfill. Entity extraction accuracy directly affects Phase 3's smart loading retrieval quality. The backfill runs once and the cost is bounded by existing chunk count.

## Implementation Approach

**Phasing rationale:** Salience-decay and intelligent-synthesis have no dependency on each other — develop in parallel. Both depend on Phase 1 infrastructure (DB, chunks, vectors) which is complete.

```
Week 1-2:  [salience-decay]  ||  [intelligent-synthesis]    (parallel)
                    \                    /
Week 3:              [merge + backfill]                      (sequential)
```

Each worktree gets its own branch, PR, and test suite. No worktree depends on uncommitted work from the other.

**Pre-requisite:** Before creating worktree branches, reconcile `memory-system-refactor` with the vector search work currently on `main` (squash merge `80c9444`). Cherry-pick vector search onto this branch and reset `main` to before that merge.

## Testing Strategy

- `tests/test_decay.py` — tiered decay math, tier classification (hot/warm/cold), archive threshold, salience reinforcement
- `tests/test_load_memory.py` — access tracking writes (access_count, last_accessed, salience reinforcement)
- `tests/test_storage.py` — new CRUD helpers (invalidate_edge, update_chunk_content, batch_update_access)
- `tests/test_synthesis.py` — MEMORY_OPS parsing, CRUD apply logic (ADD/UPDATE/DELETE/NOOP), bi-temporal edge creation/invalidation, entity extraction from LLM output
- `tests/test_backfill.py` — idempotent re-extraction, Sonnet model selection, progress tracking

All tests use `tmp_path` fixture for filesystem isolation. No hardcoded values — derive from constants. Mock LLM calls in tests (no real API calls).

## Future Phases (unchanged from Phase 1 design)

### Phase 3: Workflow
- `smart-loading` (#58 + #61 + #62): Replace firehose `load_memory.py` with semantic retrieval
- `semantic-recall` (#59 + #60): Upgrade `/recall` to sqlite-vec + triplet ranking

### Phase 4: Enrichment
- `triplet-embeddings` (#51): Embed graph edges as natural language triplets
- `consolidation` (#56): Weekly clustering of similar memories
- `rule-extraction` (#57): LLM extracts implicit coding rules
