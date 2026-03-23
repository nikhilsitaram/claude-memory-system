# Design: Memory System Refactor — Phase 4 (Consolidation & Health)

**Date:** 2026-03-23
**Parent issue:** #64
**Depends on:** Phase 3 (DB-first architecture) — merged on `memory-system-refactor`

## Problem

Phase 3 established the DB as the sole store with a unified `data_points` schema, MCP server, and web frontend. Two operational gaps remain:

1. **Memory redundancy.** Synthesis processes one session at a time and only sees a sample of existing memories via pre-retrieval. Over time, multiple sessions produce overlapping memories about the same topic — slight variations of the same fact that waste token budget at SessionStart and make retrieval noisier.

2. **No system visibility.** The only diagnostic signal is the token budget warning at the end of `load_memory.py` output. There's no visibility into salience distribution, synthesis health, graph density, or memory staleness. Problems (stalled synthesis, decayed-out knowledge, sparse graphs) go unnoticed until retrieval quality degrades.

3. **SimHash bug.** `hamming_distance` in `simhash.py` silently produces wrong results when passed negative (signed) integers from SQLite's signed 64-bit INTEGER type (#70).

## Goal

Add a daily consolidation pipeline that discovers and merges redundant memories (using existing `supersedes` infrastructure), enhance health monitoring with richer metrics and SessionStart alerts, and fix the SimHash bug.

## Success Criteria

1. **Redundant memories are merged.** When two or more active memories have cosine similarity >= 0.80 and represent the same fact, the consolidation pipeline produces a single merged data_point that supersedes the originals. Token budget waste from redundancy is reduced.

2. **Evolving knowledge is preserved.** When similar memories represent an evolution of understanding (decisions, reversals, corrections), the LLM refuses to merge them (SKIP). The pipeline never destroys temporal context or decision reasoning.

3. **Dates in consolidation.** The LLM merge prompt includes full ISO datetime (`created_at`) for each cluster member, enabling it to preserve temporal ordering and identify which insight came last when multiple entries were created on the same day.

4. **Daily cadence.** Consolidation runs once per day as a post-step in `synthesis_cron.py`, gated by interval and minimum-memory thresholds. No separate timer or plist required.

5. **Backfill on first run.** The first-ever consolidation run processes all unconsolidated memories (higher cluster cap) to clear the historical backlog. Subsequent runs are incremental.

6. **Health dashboard.** `health.py` reports: memories by scope, memories by type, consolidation stats, synthesis stats, staleness indicators, and graph density.

7. **SessionStart alerts.** `load_memory.py` surfaces lightweight alerts for: 80%+ cold memories, no synthesis in 7+ days, empty DB, graph stagnation.

8. **Web dashboard stats.** `/api/stats` endpoint returns extended metrics including consolidation and synthesis timestamps.

9. **Hamming distance guard.** `hamming_distance()` raises `ValueError` on negative inputs with a diagnostic message.

10. **No regressions.** All new public functions tested. Test baseline: 1024 passed, 18 skipped.

## Non-Goals

- Triplet embeddings (#51) — deferred, current hybrid search + traverse_graph is sufficient
- Coding rule extraction (#57) — deferred, consolidation and health are higher priority
- Vector-enhanced SessionStart (#58) — Phase 3 SQL-only loading is working well
- Hyperparameter optimization framework
- Multi-user or multi-tenant support

## Architecture

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
| No new edge types | Use existing `supersedes` | Consolidation IS batch supersession. No structural distinction between manually superseded and consolidated memories. |
| No new data_point types | Regular memory with `source_type='consolidation'` | Consolidated results are just memories. No special treatment needed. |
| Daily not weekly | Piggybacked on synthesis_cron | User's work is day-structured. Daily catches redundancy before it accumulates. Eliminates need for separate timer. |
| LLM can refuse to merge | SKIP decision | Critical for preserving evolving knowledge, decision reversals, and reasoning journey. "When in doubt, SKIP." |
| Edge-aware clustering | Exclude contradicts/supersedes pairs | These edges mean synthesis already detected evolution. Don't cluster what's already marked as non-redundant. |
| Include datetime in merge prompt | Full ISO datetime per member | Enables LLM to identify which insight came last within the same day. Temporal ordering prevents merging corrections with the thing being corrected. |
| Soft-delete originals | salience=0 via existing supersession | Consistent with Phase 3 delete_memory behavior. Provenance chain preserved via edges. |
| Backfill mode on first run | Higher cluster cap (30 vs 15) | Clears historical backlog in 1-2 runs instead of spreading over a week. |

## Research: How Other Systems Handle This

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

New settings under `consolidation.*` in `settings.json`:

| Setting | Default | Description |
|---|---|---|
| `consolidation.intervalHours` | 24 | Minimum hours between consolidation runs |
| `consolidation.minMemories` | 5 | Minimum active unconsolidated memories to trigger |
| `consolidation.similarityThreshold` | 0.80 | Cosine similarity threshold for clustering |
| `consolidation.maxClusters` | 15 | Max clusters per incremental run |
| `consolidation.backfillMaxClusters` | 30 | Max clusters for first-ever (backfill) run |
| `consolidation.model` | sonnet | LLM model for merge decisions |

## Implementation Approach

### Two Sequential Phases

**Phase A: Foundation** (no LLM dependency, no breaking changes)
- A1: Hamming distance fix in `simhash.py` + test
- A2: Health monitoring enhancement (`health.py` extended metrics, `load_memory.py` alerts, web dashboard)
- A3: Issue cleanup (close implemented issues #46-#55 with comments)

**Phase B: Consolidation** (depends on A2 for health stats integration)
- B1: Consolidation pipeline (`scripts/consolidation.py` — clustering, scoring, LLM merge, DB writes)
- B2: Integration into `synthesis_cron.py` (daily gate, backfill detection)
- B3: `/consolidate` skill for manual trigger
- B4: Settings defaults in `memory_utils.py` + `install.py` updates

### New Files
- `scripts/consolidation.py` — clustering, scoring, LLM merge, supersession writes
- `skills/consolidate/SKILL.md` — manual trigger skill
- `tests/test_consolidation.py` — unit tests

### Modified Files
- `scripts/simhash.py` — hamming_distance guard
- `scripts/health.py` — extended HealthReport + new alerts
- `scripts/load_memory.py` — SessionStart health alerts
- `scripts/web_app.py` — extended `/api/stats` response
- `scripts/memory_utils.py` — consolidation defaults in DEFAULT_SETTINGS
- `scripts/synthesis_cron.py` — consolidation post-step with daily gate
- `templates/web/index.html` — dashboard health summary
- `install.py` — link consolidation.py, consolidate skill
- `tests/test_simhash.py` — hamming_distance guard test
- `tests/test_health.py` — extended health metrics tests
- `tests/test_web_app.py` — extended stats endpoint test
