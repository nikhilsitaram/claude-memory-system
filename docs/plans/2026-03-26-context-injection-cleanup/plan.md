---
status: In Development
---

# Recover ~1,500 wasted tokens from SessionStart injection by cleaning stale data, preventing systemic duplication in synthesis, fixing the false health alert, and stopping passive-load reinforcement from counteracting decay. Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use orchestrate

**Goal:** Recover ~1,500 wasted tokens from SessionStart injection by cleaning stale data, preventing systemic duplication in synthesis, fixing the false health alert, and stopping passive-load reinforcement from counteracting decay.
**Architecture:** Single-phase, 6 parallel tasks touching health.py, storage.py, synthesis.py, synthesis_cron.py, and load_memory.py. SimHash dedup gate in synthesis prevents future duplicates. Passive vs active reinforcement flag in load_memory stops SessionStart auto-loading from counteracting decay. Legacy table DROP and one-time cleanup function in storage removes existing waste. Health check priority fix eliminates false alerts.
**Tech Stack:** Python 3.9+, SQLite3, SimHash (scripts/simhash.py), pytest

---

## Phase A — Context Injection Cleanup
**Status:** Not Started | **Rationale:** All 6 changes are independent (different functions in different files). Single phase allows full parallelism. A2 combines legacy table DROP and cleanup function since both modify storage.py.

- [ ] A1: Fix false health alert for v3 DBs with legacy tables — *health_report() skips chunks/nodes branches when user_version >= 3; health_alerts() returns no 'DB empty' alert for a v3 DB with data_points but empty chunks table. 2 new tests pass.*
- [ ] A2: Legacy table cleanup and one-time data cleanup in storage — *ensure_db() drops legacy chunks/nodes tables. New cleanup_stale_data(conn) function: (1) deletes profile waste (HTML comments, bare tags), (2) soft-deletes near-duplicate clusters keeping highest evidence_count, (3) soft-deletes stale project memories. Returns dict with counts. Callable via CLI. 6 new tests pass.*
- [ ] A3: SimHash dedup gate in synthesis _apply_add_v3 — *_apply_add_v3 computes SimHash, queries candidates in same scope with salience > 0 and created_at within 90 days, and returns status='deduped' (bumping evidence_count, merging source_sessions, boosting salience) when Hamming distance <= 3. Normal insert proceeds when no near-duplicate found. Content replacement occurs when new content is >2x longer. 4 new tests pass.*
- [ ] A4: Pre-retrieval enhancement for synthesis prompt — *_run_synthesis_v3 injects top-10 existing memories (by salience) for the target scope into the synthesis prompt before sending to claude -p. Memories are formatted as '[id] content' lines under an 'Existing Memories' header. 2 new tests pass.*
- [ ] A5: Passive vs active reinforcement in load_memory — *_batch_update_data_point_access accepts passive: bool = False parameter. When passive=True, it updates access_count and last_accessed but skips salience reinforcement and neighbor boosting. _load_from_db calls it with passive=True. 3 new tests pass.*
- [ ] A6: Integration test validating end-to-end token recovery — *Integration test creates a DB with profile waste, near-duplicate memories, and stale project memories, runs cleanup_stale_data, then verifies: (1) profile items with HTML comments or bare tags are gone, (2) duplicate clusters reduced to 1 survivor, (3) stale project memories soft-deleted. 3 tests pass.*
