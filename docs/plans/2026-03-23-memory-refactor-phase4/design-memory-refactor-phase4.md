# Design: Memory System Refactor — Phase 4 (Retrieval, Consolidation & Health)

**Date:** 2026-03-23
**Parent issue:** #64, #90
**Depends on:** Phase 3 (DB-first architecture) — merged on `memory-system-refactor`

## Problem

Phase 3 established the DB as the sole store with a unified `data_points` schema, MCP server, and web frontend. Five operational gaps remain:

1. **Search is broken.** fastembed and sqlite_vec aren't installed in most environments; `search_memories` silently falls back to `ORDER BY salience DESC` with no semantic understanding. No FTS5 tables exist for keyword matching.

2. **No mid-session recall.** Memories are only loaded at SessionStart. Claude must manually call `search_memories` during a session, which it often doesn't think to do. As the corpus grows, relevant memories become effectively invisible.

3. **Memory redundancy.** Synthesis processes one session at a time and only sees a sample of existing memories via pre-retrieval. Over time, multiple sessions produce overlapping memories — slight variations of the same fact that waste token budget and make retrieval noisier.

4. **No system visibility.** The only diagnostic signal is the token budget warning at the end of `load_memory.py` output. No visibility into salience distribution, synthesis health, graph density, or memory staleness.

5. **No epistemic metadata.** All memories are treated equally regardless of confidence level. A speculative observation ranks the same as a long-standing established pattern.

6. **No secret sanitization.** API keys, passwords, and tokens can be stored and injected verbatim — a data safety concern.

7. **SimHash bug.** `hamming_distance` in `simhash.py` silently produces wrong results when passed negative (signed) integers from SQLite's signed 64-bit INTEGER type (#70).

## Goal

Deliver hybrid search (FTS5 + vector + RRF), proactive mid-session memory injection, daily memory consolidation, enhanced health monitoring, epistemic metadata, secret sanitization, and retrieval benchmarking — all within the existing SQLite-backed architecture.

## Success Criteria

1. **Hybrid search works.** Memory search returns semantically relevant results via RRF fusion of FTS5 BM25 + vector KNN, outperforming the current salience-ranked fallback. Graceful degradation when fastembed or sqlite-vec are unavailable.

2. **Proactive mid-session recall.** Relevant memories are surfaced during a session via UserPromptSubmit hook without Claude needing to call a tool. Latency < 800ms per prompt.

3. **Redundant memories are merged.** When two or more active memories have cosine similarity >= 0.80 and represent the same fact, the consolidation pipeline produces a single merged data_point that supersedes the originals.

4. **Evolving knowledge is preserved.** When similar memories represent an evolution of understanding (decisions, reversals, corrections), the LLM refuses to merge them (SKIP). The pipeline never destroys temporal context or decision reasoning.

5. **Dates in consolidation.** The LLM merge prompt includes full ISO datetime (`created_at`) for each cluster member, enabling it to preserve temporal ordering and identify which insight came last within the same day.

6. **Daily consolidation cadence.** Consolidation runs once per day as a post-step in `synthesis_cron.py`, gated by interval and minimum-memory thresholds. No separate timer required.

7. **Backfill on first run.** The first-ever consolidation run processes all unconsolidated memories (higher cluster cap) to clear the historical backlog.

8. **Epistemic metadata.** Memories have certainty scores (1-5) that affect decay rates and retrieval ranking.

9. **Secrets are redacted.** API keys, connection strings, JWTs, and private keys are sanitized before storage and injection.

10. **Health dashboard.** `health.py` reports: memories by scope/type, consolidation stats, synthesis stats, staleness indicators, and graph density. SessionStart surfaces alerts for degraded conditions.

11. **Retrieval benchmarking.** A benchmark harness measures precision@5, recall@5, NDCG@10, and MRR with regression detection.

12. **Hamming distance guard.** `hamming_distance()` raises `ValueError` on negative inputs.

13. **No regressions.** All new public functions tested. Test baseline: 1024 passed, 18 skipped.

## Non-Goals

- Triplet embeddings (#51) — current hybrid search + traverse_graph is sufficient
- Coding rule extraction (#57) — consolidation and retrieval are higher priority
- Hyperparameter optimization framework
- Multi-user or multi-tenant support
- Obsidian/markdown file compatibility (we're SQL-first)
- HDBSCAN clustering for pattern discovery
- Cross-encoder reranking (RRF is sufficient for our corpus size)

## Architecture

### Search Foundation

#### FTS5 Virtual Table

New FTS5 table synced with `data_points`:

```sql
CREATE VIRTUAL TABLE IF NOT EXISTS fts_data USING fts5(
    content,
    data_point_id UNINDEXED,
    scope UNINDEXED,
    tokenize='porter unicode61'
);
```

- Porter stemming for English keyword matching
- Populated on write/update in `write_memory` and `apply_memory_ops_v3`
- Deleted on soft-delete via `soft_delete_data_point()` in `storage.py` (single implementation point for all deletions)
- Migration backfills existing data_points
- Lives in same `memory.db` — zero new dependencies

**FTS5 sync points:**

| Operation | Location | FTS5 action |
|---|---|---|
| Manual write | `memory_server.py:_write_memory` | INSERT |
| Synthesis ADD | `synthesis.py:_apply_add_v3` | INSERT |
| Synthesis UPDATE | `synthesis.py:_apply_update_v3` | DELETE + INSERT |
| Soft delete | `storage.py:soft_delete_data_point` | DELETE |
| Migration backfill | `storage.py` migration block | Bulk INSERT |

#### RRF Hybrid Search

New function `search_hybrid()` in `embeddings.py`:

1. Run FTS5 BM25 query -> ranked list with scores
2. Run vec_data KNN query -> ranked list with distances
3. Combine via Reciprocal Rank Fusion: `RRF(d) = 1/(k + rank_bm25(d)) + 1/(k + rank_vec(d))` where k=60
4. Apply existing recency + salience weights on the fused score
5. Return top_k results

**Fallback chain:**
- Both available -> RRF hybrid
- Vector only -> vector search (current behavior)
- FTS5 only -> BM25 keyword search
- Neither -> SQL ranked fallback (current `_sql_ranked_search`)

#### Secret Sanitization

New function `sanitize_secrets(text: str) -> str` in `memory_utils.py`.

Patterns:
- AWS keys: `AKIA[0-9A-Z]{16}`
- API keys: `sk-[a-zA-Z0-9]{20,}`, `sk_live_`, `sk_test_`
- Connection strings: `(postgres|mysql|mongodb)://[^\s]+`
- JWTs: `eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`
- Private keys: `-----BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY-----`
- Generic secrets: `(token|password|secret|apikey)\s*[=:]\s*['"][^\s'"]{8,}`

Applied at:
1. `write_memory` — before storing
2. `apply_memory_ops_v3` — before DB write
3. `prompt_recall.py` — before injecting into stdout
4. `load_memory.py` — before SessionStart injection

Replaces matches with `[REDACTED:<type>]`. Logs redaction events to stderr.

### Proactive Retrieval

#### Per-Prompt Recall Hook

New script: `scripts/prompt_recall.py`, registered as `UserPromptSubmit` hook.

**Flow:**
1. Parse stdin JSON, extract `prompt` field
2. **Relevance gate** — skip if:
   - Prompt < 15 chars
   - Matches confirmation pattern (yes, ok, go ahead, looks good, etc.)
   - Starts with `/` (skill invocation)
   - Prompt > 500 chars (long prompts typically contain their own context)
3. **Search** — `search_hybrid()` against `memory.db`
4. **Dedup** — session-scoped state file (`~/.claude/memory/.prompt-recall-state-{session_id}`) tracks last 5 injected memory IDs; skip if top result already injected within last 3 prompts. Stale state files (older than 24h) cleaned up at SessionStart.
5. **Output** — 1-3 memories as stdout text, ~500 token budget max
6. **Latency target** — <800ms (FTS5 <10ms, vector KNN <200ms with warm model)

**Output format:**
```
[memory] Related context:
- (certainty: 4, scope: project) Redis cache requires explicit TTL
- (certainty: 3, scope: global) Always use BEGIN IMMEDIATE for concurrent SQLite writes
```

#### Epistemic Metadata

Two new columns on `data_points`:

```sql
ALTER TABLE data_points ADD COLUMN certainty INTEGER DEFAULT NULL;
ALTER TABLE data_points ADD COLUMN validity_context TEXT DEFAULT NULL;
```

Certainty scale:
- 1 = speculative (unverified observation)
- 2 = observed (seen once, synthesis default)
- 3 = confirmed (manual write default, verified)
- 4 = shipped (deployed/in production)
- 5 = established (long-standing pattern)

**Integration:**

| System | Usage |
|---|---|
| `write_memory` | Accept optional `certainty` param (default: 3) |
| `synthesis` | LLM assigns certainty in MEMORY_OPS (default: 2) |
| `decay` | Certainty 4-5 immune. Certainty 1-2 decay at 2x rate |
| `scoring` | Modulates salience: `salience * (0.6 + 0.1 * certainty)` |
| `prompt_recall` | Displayed in output so Claude can weigh appropriately |
| `consolidation` | Merged result inherits max(member certainties) |

`validity_context` is informational — stored and displayed but not used in scoring.

#### Per-File-Read Context Injection

Piggybacks on the `prompt_recall.py` hook with a deferred lookup:

1. **PreToolUse hook** (existing): When tool is `Read` and path matches a code file, append the file path to a session state file (`~/.claude/memory/.recent-reads`).
2. **UserPromptSubmit hook** (`prompt_recall.py`): On each prompt, check `.recent-reads` for new file paths since last check. Extract keywords from file paths (split camelCase/snake_case, filter stop words), run FTS5 keyword search, merge results with prompt-based search, dedup, and inject.

**Filter:** only `.py`, `.js`, `.ts`, `.rs`, `.go`, `.java`, `.rb`, `.c`, `.cpp`, `.h` — skip configs, docs, images, vendor dirs.

**Session cap:** max 5 file-context injections per session (separate from prompt recall cap).

### Memory Consolidation Pipeline

Consolidation is **batch supersession** — it finds groups of redundant memories and merges them using the same `supersedes` edge infrastructure from Phase 3. No new edge types or data_point types.

#### Pipeline Steps

```
synthesis_cron.py (daily gate: interval + min memories)
  |
  v
1. QUERY: all active memories (type='memory', salience > 0.1)
  |
  v
2. EMBED: get embeddings from vec_data (or embed if missing)
  |
  v
3. CLUSTER: group by cosine similarity >= threshold
     - Exclude pairs with existing contradicts/supersedes edges
     - Min cluster size: 2 (singletons skip)
     - Max cluster size: 15 (split larger clusters)
  |
  v
4. SCORE: rank clusters by 0.6 * count + 0.3 * max_recency + 0.1 * avg_salience
     - Cap: 15 clusters/run (incremental), 30 clusters/run (backfill)
  |
  v
5. MERGE (per cluster): headless `claude -p`
     - Input: cluster members with full ISO datetime + content + entities
     - Output: MERGE {fact, entities, salience} or SKIP {reason}
  |
  v
6. WRITE (for MERGE results):
     - INSERT new data_point (source_type='consolidation')
     - salience = max(member saliences) + 0.05 (capped 1.0)
     - certainty = max(member certainties)
     - entities = union of member entities
     - Create supersedes edges -> each original
     - Soft-delete originals (salience = 0)
```

#### Edge-Aware Clustering

Before forming clusters, the pipeline checks for existing edges between candidate memories:

```sql
SELECT source, target FROM edges
WHERE type IN ('contradicts', 'supersedes')
AND valid_to IS NULL
AND source IN (candidate_ids) AND target IN (candidate_ids)
```

Memory pairs with `contradicts` or `supersedes` edges are excluded from the same cluster — these edges exist precisely because synthesis detected they represent evolution, not redundancy.

#### LLM Merge Prompt

```
You are consolidating memories from a knowledge graph. These memories
were grouped by text similarity.

For each cluster, decide:
- MERGE: if they are truly redundant (saying the same thing in different words).
  Produce a single merged fact that preserves the most complete and accurate
  version. Include dates when temporal sequence matters.
- SKIP: if they represent evolving understanding, decision reversals, or
  contain important nuance that would be lost by merging. Explain why.

Guidelines:
- If the cluster contains a decision that was later reversed or corrected,
  preserve the reasoning journey: include what was tried and why it was rejected.
- Preserve dates when the temporal sequence matters (decisions, reversals,
  migrations). Drop dates for purely redundant facts.
- When in doubt, SKIP. It is better to keep two memories than to lose context.

Cluster members:
{for each member: [created_at ISO datetime] "content" (entities: [...])}

Respond with JSON:
{"decision": "MERGE"|"SKIP", "fact": "...", "entities": [...], "reason": "..."}
```

#### Scheduling

Consolidation runs as a **post-step in `synthesis_cron.py`**, gated by:

1. `consolidation.intervalHours` (default: 24) has elapsed since last run
2. At least `consolidation.minMemories` (default: 5) active unconsolidated memories exist

Detection of first-ever run: no `last_consolidation` metadata key in the DB. First run uses backfill mode (higher cluster cap of 30).

No separate launchd/systemd timer needed — piggybacks on existing synthesis scheduling.

#### Steady-State Behavior

With daily consolidation, clusters are typically **2 members**: one existing memory + one new memory from the latest synthesis. The pipeline naturally handles the "new memory similar to a previous consolidation result" case — both are active data_points, they cluster together, and the merged result supersedes both.

```
Day 1: synthesis creates A about topic X       -> A (singleton, skip)
Day 3: synthesis creates B similar to A         -> {A, B} cluster -> MERGE -> C supersedes A, B
Day 7: synthesis creates D similar to C         -> {C, D} cluster -> MERGE -> E supersedes C, D
Day 7: synthesis creates F about topic Y        -> {F} singleton, skip
Day 9: synthesis creates G correcting topic X   -> {E, G} cluster -> LLM sees evolution -> SKIP
```

The supersession chain preserves full provenance: `E -> C -> A, B` and `E -> D`.

### Health Monitoring Enhancement

#### Extended HealthReport

New fields added to the `HealthReport` dataclass:

| Field | Source | Purpose |
|---|---|---|
| `memories_by_scope` | `GROUP BY scope` on data_points | Breakdown: user/global/project |
| `memories_by_type` | `GROUP BY type` on data_points | Breakdown: memory/entity/session_context/profile |
| `consolidated_count` | `source_type = 'consolidation'` | How many consolidation results exist |
| `last_consolidation` | Metadata query | When consolidation last ran |
| `last_synthesis` | `.last-synthesis` file | When synthesis last ran |
| `synthesis_errors_7d` | `.synthesis-errors.log` | Error count in last 7 days |
| `never_accessed_pct` | `access_count = 0` ratio | % memories never served |
| `oldest_memory_days` | `MIN(created_at)` | Age of oldest active memory |
| `edges_per_entity` | `COUNT(edges) / COUNT(entities)` | Graph density metric |

#### SessionStart Alerts

Lightweight checks added to `load_memory.py` after loading memories:

```python
alerts = []
if cold_ratio >= 0.8:
    alerts.append("80%+ memories are cold — consider running /consolidate")
if days_since_synthesis > 7:
    alerts.append("No synthesis in 7+ days — check synthesis_cron")
if total_memories == 0:
    alerts.append("Memory DB is empty — run install.py")
if days_since_new_edge > 7 and total_edges > 0:
    alerts.append("No new graph edges in 7+ days")
```

Alerts are appended to the SessionStart output as a `## Health Alerts` section, only when alerts exist.

#### Web Dashboard Enhancement

`/api/stats` response extended with:
- `memories_by_scope`, `memories_by_type` breakdowns
- `consolidated_count`, `last_consolidation`, `last_synthesis`
- `graph_density` (edges per entity)

Dashboard HTML updated with a health summary row showing key indicators.

### Hamming Distance Fix (#70)

```python
def hamming_distance(a: int, b: int) -> int:
    if a < 0 or b < 0:
        raise ValueError(
            f"Both arguments must be non-negative unsigned 64-bit integers "
            f"(got a={a}, b={b}). Cast SQLite values with: val & 0xFFFFFFFFFFFFFFFF"
        )
    return bin(a ^ b).count("1")
```

Plus test covering the guard.

## Key Decisions

| Decision | Choice | Why |
|---|---|---|
| FTS5 over external search | Same DB, zero new dependencies | In-process speed, single DB, no coordination overhead. |
| RRF over learned fusion | k=60 constant, proven | Simple, no training needed. Standard constant works well at our corpus size. |
| Push-based recall (hook) alongside pull-based (MCP) | UserPromptSubmit hook + MCP tools | Hook handles the common case (Claude doesn't think to search); MCP handles targeted searches. Complementary. |
| Certainty as integer 1-5, not float | Discrete scale | Easier for LLMs to assign consistently. Maps to established patterns (memento-vault). |
| File-read context via deferred UserPromptSubmit | PreToolUse records reads, next prompt surfaces context | PreToolUse hooks can only return permission decisions, not context text. Deferred lookup is the only mechanism. |
| No new edge types for consolidation | Use existing `supersedes` | Consolidation IS batch supersession. No structural distinction needed. |
| Daily not weekly consolidation | Piggybacked on synthesis_cron | User's work is day-structured. Daily catches redundancy before it accumulates. Eliminates need for separate timer. |
| LLM can refuse to merge | SKIP decision | Critical for preserving evolving knowledge, decision reversals, and reasoning journey. |
| Edge-aware clustering | Exclude contradicts/supersedes pairs | These edges mean synthesis already detected evolution. Don't cluster what's already marked as non-redundant. |
| Include ISO datetime in merge prompt | Full datetime per cluster member | Enables LLM to identify which insight came last within the same day. Temporal ordering prevents merging corrections with the thing being corrected. |
| Soft-delete originals on consolidation | salience=0 via existing supersession | Consistent with Phase 3 delete_memory behavior. Provenance chain preserved via edges. |
| Secret sanitization bundled here | Touches same write/inject paths | Orthogonal to search quality, but same code paths. Bundled for implementation convenience. |
| Custom benchmark over LongMemEval | LongMemEval assumes conversational memory | Our system stores factual data_points with graph edges. Custom benchmark tests scope filtering, certainty weighting, and RRF fusion specifically. |

## Research: How Other Systems Handle Evolving Knowledge

### Graphiti (Cognee's temporal engine)
- LLM-driven distinction between "duplicate" (same info, merge) and "contradicted" (superseding, preserve both with timestamps)
- Tri-temporal model: `valid_at`, `invalid_at`, `expired_at` — old edges are never deleted
- Progressive rolling entity summarization (pairwise LLM merge when content exceeds threshold)
- Our edge-aware clustering mirrors Graphiti's principle: never merge things with temporal/contradiction relationships

### Mem0
- Contradictions trigger DELETE old + ADD new — destructive, but `history` table preserves audit trail
- All decisions inline during `add()` (no batch consolidation)
- The "OpenMemory periodic reflection loop" referenced in #56 does not exist in their codebase
- Our batch approach fills a gap that Mem0's inline-only model cannot address

### Letta
- Sleep-time agent pattern: background processing every N steps, converts raw conversation to clean memory blocks
- No explicit contradiction detection — delegates to LLM judgment
- Core memory blocks are overwritten (last-write-wins), but recall memory preserves full history

### LangMem
- Multi-phase consolidation: initial extraction, then separate dedup pass with different LLM instructions
- Profile vs. collection distinction (single doc vs. individual facts)
- No first-class mechanism for decision reversals

### Key insight
No existing system handles "tried X, failed, went back to Y" as a first-class concept. Our SKIP decision + edge-aware clustering + datetime-in-prompt approach is a novel combination that preserves this context.

## Settings

New settings in `settings.json`:

**Consolidation:**

| Setting | Default | Description |
|---|---|---|
| `consolidation.intervalHours` | 24 | Minimum hours between consolidation runs |
| `consolidation.minMemories` | 5 | Minimum active unconsolidated memories to trigger |
| `consolidation.similarityThreshold` | 0.80 | Cosine similarity threshold for clustering |
| `consolidation.maxClusters` | 15 | Max clusters per incremental run |
| `consolidation.backfillMaxClusters` | 30 | Max clusters for first-ever (backfill) run |
| `consolidation.model` | sonnet | LLM model for merge decisions |

**Recall:**

| Setting | Default | Description |
|---|---|---|
| `recall.maxPromptLength` | 500 | Skip recall for prompts longer than this (chars) |
| `recall.minPromptLength` | 15 | Skip recall for prompts shorter than this (chars) |
| `recall.maxInjectionsPerPrompt` | 3 | Max memories injected per prompt |
| `recall.maxTokenBudget` | 500 | Max tokens per injection |
| `recall.fileContext.maxPerSession` | 5 | Max file-read context injections per session |

## Implementation Approach

### Four Sequential Phases

**Phase A: Search Foundation + Health** (no LLM dependency)
- A1: FTS5 virtual table + migration backfill in `storage.py`
- A2: RRF hybrid search (`search_hybrid()`) in `embeddings.py`
- A3: Wire hybrid search into `memory_server.py`
- A4: Secret sanitization in `memory_utils.py` + integration at write/inject points
- A5: Hamming distance fix in `simhash.py`
- A6: Health monitoring enhancement (`health.py` extended metrics, `load_memory.py` alerts, web dashboard stats)

**Phase B: Proactive Retrieval** (depends on A for hybrid search)
- B1: `prompt_recall.py` — UserPromptSubmit hook with relevance gate, hybrid search, dedup, output formatting
- B2: Epistemic metadata columns (`certainty`, `validity_context`) + migration
- B3: Certainty integration into decay, scoring, synthesis, write_memory
- B4: Hook registration in `install.py`

**Phase C: Consolidation** (depends on A for search, benefits from B for certainty)
- C1: Consolidation pipeline (`scripts/consolidation.py` — clustering, scoring, LLM merge/skip, DB writes)
- C2: Integration into `synthesis_cron.py` (daily gate, backfill detection)
- C3: `/consolidate` skill for manual trigger
- C4: Settings defaults in `memory_utils.py` + `install.py` updates

**Phase D: File Context + Benchmarking** (depends on B for prompt_recall)
- D1: File-read context injection (deferred lookup in `prompt_recall.py`, PreToolUse file path recording)
- D2: Retrieval benchmark harness (`tests/benchmark_retrieval.py`) with seed data, metrics, regression detection

### New Files

| File | Phase | Purpose |
|---|---|---|
| `scripts/prompt_recall.py` | B | UserPromptSubmit hook for proactive memory injection |
| `scripts/consolidation.py` | C | Clustering, scoring, LLM merge, supersession writes |
| `skills/consolidate/SKILL.md` | C | Manual trigger skill |
| `tests/test_prompt_recall.py` | B | Tests for relevance gate, search, dedup, output format |
| `tests/test_consolidation.py` | C | Tests for clustering, edge-awareness, merge/skip, DB writes |
| `tests/benchmark_retrieval.py` | D | Benchmark harness: seed, query, measure, compare |
| `tests/benchmark_baseline.json` | D | Baseline metrics for regression detection |

### Modified Files

| File | Phases | Changes |
|---|---|---|
| `scripts/storage.py` | A, B | FTS5 DDL, migration backfill, delete sync, certainty/validity_context columns |
| `scripts/embeddings.py` | A | `search_hybrid()`, `search_fts5()`, FTS5 insert/delete helpers |
| `scripts/memory_server.py` | A, B | Wire hybrid search, accept certainty in write_memory, sanitize writes |
| `scripts/memory_utils.py` | A, C | `sanitize_secrets()`, consolidation + recall defaults in DEFAULT_SETTINGS |
| `scripts/synthesis.py` | A, B | Sanitize in apply ops, FTS5 sync, parse certainty from MEMORY_OPS |
| `scripts/load_memory.py` | A | Sanitize output, SessionStart health alerts, stale state cleanup |
| `scripts/decay.py` | B | Certainty-aware decay rates |
| `scripts/synthesis_cron.py` | C | Consolidation post-step with daily gate |
| `scripts/health.py` | A | Extended HealthReport + new alerts |
| `scripts/web_app.py` | A | Extended `/api/stats` response |
| `scripts/simhash.py` | A | Hamming distance guard |
| `templates/web/index.html` | A | Dashboard health summary row |
| `install.py` | B, C | Register UserPromptSubmit hook, link prompt_recall.py + consolidation.py + consolidate skill |
| `tests/test_embeddings.py` | A | Tests for hybrid search, FTS5, fallback chain |
| `tests/test_storage.py` | A, B | Tests for FTS5 migration, sync, certainty column |
| `tests/test_memory_utils.py` | A | Tests for `sanitize_secrets()` |
| `tests/test_simhash.py` | A | Hamming distance guard test |
| `tests/test_health.py` | A | Extended health metrics tests |
| `tests/test_web_app.py` | A | Extended stats endpoint test |
| `tests/test_decay.py` | B | Certainty-aware decay tests |
| `tests/test_synthesis.py` | B | Certainty in MEMORY_OPS tests |
