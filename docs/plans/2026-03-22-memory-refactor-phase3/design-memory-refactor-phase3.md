# Design: Memory System Refactor — Phase 3 (Workflow)

**Date:** 2026-03-22
**Parent issue:** #64
**Depends on:** Phase 1 (Foundation) + Phase 2 (Intelligence) — both merged on `memory-system-refactor`

## Problem

The memory system has dual storage paths that create complexity and waste:

1. **Firehose loading.** SessionStart dumps ~11K tokens of markdown content (4 file tiers) regardless of relevance. Stale LTM entries crowd out useful context. No awareness of what the user is actually working on.

2. **No session continuity.** Each session starts cold. The user must re-explain "I was working on X" because there's no mechanism to capture and restore task context between sessions.

3. **Dual write paths.** Phase 2 writes to both markdown (PROJECT blocks → daily/LTM files) and DB (MEMORY_OPS → chunks). This creates sync complexity, routing logic (keyword dedup, quality floors, route caps, scope injection), and fragile markdown-to-DB reconciliation.

4. **Friction in memory capture.** `/remember` requires explicit invocation. Important facts should enter the DB automatically when Claude detects them, not when the user remembers to type a command.

5. **Grep-based recall.** `/recall` does keyword search across daily files — no semantic understanding, no ranking, misses conceptually related memories.

6. **Disconnected data layers.** The `chunks` table (content) and `nodes` table (entities) are separate with only a loose JSON link (`entities` column). Memories cannot have formal relationships to each other (supersession, contradiction, causation).

7. **No memory provenance.** When a fact changes, the old memory is silently archived. There's no chain showing *why* something changed or *what* replaced it. Claude can't say "we used to do it this way, but after investigation we found this instead."

## Goal

Make the DB the sole store with a unified data model. Loading becomes SQL-ranked retrieval. Synthesis writes structured CRUD ops directly to the DB. Session context persists between sessions. Memory capture is frictionless via MCP tools. Memories can supersede, contradict, and build on each other with full provenance. A web frontend replaces markdown files for human inspection.

## Success Criteria

1. **Unified schema.** A single `data_points` table replaces both `chunks` and `nodes`. All relationships (memory↔memory, memory↔entity, entity↔entity) use one `edges` table. Migration from v2 schema is automated and lossless.

2. **DB is sole store.** No markdown files written or read as part of normal operation. Daily files, LTM files, scope tag injection, `[routed]` marking, and LTM routing pipeline are all eliminated.

3. **Semantic loading.** SessionStart loads context via SQL queries ranked by salience + recency + scope (~6K token budget, ~60ms latency — same as current markdown approach). No vector search at session start.

4. **Session continuity.** SessionEnd writes a context data_point. Next SessionStart in the same project surfaces "what you were working on" automatically, enabling task resumption without re-explanation.

5. **MCP server.** A persistent Python process exposes 4 tools via MCP protocol: `search_memories` (vector + graph hybrid), `write_memory` (atomic DB write), `delete_memory` (soft delete with provenance), `traverse_graph` (graph navigation). Claude calls these proactively during conversation — no `/recall` or `/remember` skills needed.

6. **Memory provenance.** Memories can have explicit relationships: `supersedes`, `contradicts`, `led_to`, `refines`, `supports`. When a fact changes, the system preserves the chain of reasoning. Claude can explain the evolution of understanding, not just the current truth.

7. **Unified synthesis.** Synthesis produces a single MEMORY_OPS output format with LLM-assigned salience scores (0.0–1.0). No dual PROJECT blocks + MEMORY_OPS paths. The LLM assigns salience directly, replacing the binary `[LTM]` flag and the routing pipeline.

8. **Three scopes.** `user` (permanent profile, always loaded), `global` (cross-project knowledge, ranked by salience), `{project}` (project-specific, loaded when CWD matches). User profile gets a guaranteed allocation that never decays.

9. **Semantic recall.** `/recall` and `/remember` skills deprecated. The MCP `search_memories` tool provides vector-ranked retrieval. Claude uses it proactively when conversation context suggests relevant history.

10. **Web frontend.** Local web app for browsing, searching, and managing the knowledge graph. Interactive graph visualization. Browse by scope/type/date/salience. Edit and delete memories.

11. **No regressions.** All new public functions tested. Existing tests updated for DB-only paths. Test baseline: 949 passed, 12 skipped.

## Non-Goals

- Triplet embeddings (Phase 4 — #51)
- Weekly memory consolidation/clustering (Phase 4 — #56)
- Implicit coding rule extraction (Phase 4 — #57)
- Full DataPoint unification where chunks ARE graph nodes with entity embeddings (partial: entities get data_points but not all get embeddings in Phase 3)
- Hyperparameter optimization framework (Cognee's Dreamify approach — future work)
- Multi-user or multi-tenant support

## Architecture

### Unified `data_points` Model

Everything — memories, entities, session contexts, profile entries — is a `data_point`. This follows the pattern from Cognee's DataPoint abstraction, adapted for our simpler SQLite-based system.

```sql
CREATE TABLE data_points (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,           -- 'memory', 'entity', 'session_context', 'profile'
    name TEXT,                    -- for entities: "JWT", "gRPC". NULL for memories.
    content TEXT,                 -- full text for memories. Description for entities.
    scope TEXT,                   -- 'user', 'global', or project name
    entry_type TEXT,              -- implement, design, gotcha, tip, etc.
    source_type TEXT,             -- 'synthesis', 'manual', 'migration', 'session_end'
    source_sessions TEXT,         -- JSON array of session IDs
    created_at TEXT NOT NULL,
    salience REAL DEFAULT 1.0,
    access_count INTEGER DEFAULT 0,
    last_accessed TEXT,
    evidence_count INTEGER DEFAULT 1,
    consolidated INTEGER DEFAULT 0,
    content_hash TEXT,
    simhash INTEGER,
    entities TEXT,                -- JSON: extracted entities (for memories)
    properties TEXT               -- JSON: flexible extra fields
);

CREATE TABLE edges (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL REFERENCES data_points(id),
    target TEXT NOT NULL REFERENCES data_points(id),
    type TEXT NOT NULL,           -- relationship type (see below)
    reason TEXT,                  -- why this relationship exists
    fact TEXT,                    -- for entity relationships
    properties TEXT,              -- JSON blob
    created_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    expired_at TEXT,
    weight REAL DEFAULT 1.0,
    source_sessions TEXT
);

CREATE VIRTUAL TABLE vec_data USING vec0(
    embedding float[384],
    +data_point_id TEXT,
    +type TEXT
);
```

### Edge Types

| Type | Connects | Meaning |
|---|---|---|
| `supersedes` | memory → memory | New memory replaces old one |
| `contradicts` | memory → memory | New evidence disproves old belief |
| `led_to` | memory → memory | Decision caused an implementation |
| `refines` | memory → memory | Enriches without replacing |
| `supports` | memory → memory | Corroborates existing memory |
| `mentions` | memory → entity | Memory references this entity |
| `uses` | entity → entity | Technology/tool usage |
| `depends_on` | entity → entity | Dependency relationship |
| `part_of` | entity → entity | Containment/membership |
| `context_for` | session_context → entity | Session relates to entity |

### Three Scopes

| Scope | Loaded | Decays? | Purpose |
|---|---|---|---|
| `user` | Every session, always | No (salience=1.0, permanent) | Claude knows who you are from the first message. `write_memory` with `scope='user'` auto-sets `salience=1.0` and `consolidated=1`. |
| `global` | Every session, ranked by salience | Yes (normal decay) | Cross-project technical knowledge surfaces by relevance |
| `{project}` | Only when CWD matches, ranked | Yes (normal decay) | Project context surfaces when you're in that project |

### Salience Spectrum (replaces daily/LTM binary)

| Old System | New System |
|---|---|
| Daily entry (transient, 2-5 day window) | data_point with salience 0.3–0.5 (decays naturally) |
| LTM entry (curated, routed) | data_point with salience 0.7–0.9 (LLM-assigned, persists) |
| Pinned entry (permanent) | data_point with salience 1.0, consolidated=1 (never decays) |
| Profile section (auto-pinned) | data_point with type='profile', salience 1.0 |

### MCP Server

A persistent Python process giving Claude native tool access to the memory DB. Uses the `mcp` Python SDK for protocol handling. Configured in Claude Code's settings via `install.py`.

**Tools:**

1. **`search_memories(query, scope?, top_k?)`** — Embed query (warm FastEmbed model), sqlite-vec KNN search, composite scoring via existing `score_memory()` from `embeddings.py` (saturating exponential on vec_sim: `1 - exp(-3.0 * vec_sim)`, 0.50 weight + 0.25 recency + 0.25 salience). Graph boost: for each result with edges to other results in the top-K set, add +0.05 per connection (capped at +0.15). Returns ranked results with IDs, provenance chains, and entities.

2. **`write_memory(fact, scope, salience?, entities?, supersedes?, relation_type?, relation_reason?)`** — Atomic: INSERT data_point + generate embedding + create entity data_points + create `mentions` edges + optional provenance edge (supersedes/refines/etc).

3. **`delete_memory(id, reason?)`** — Soft delete: set salience=0, invalidate related edges. **Pure deletion** (no replacement): creates a synthetic "deletion marker" data_point with type='memory', content=reason, salience=0, and a `supersedes` edge from the marker to the deleted data_point — preserving provenance ("memory X was deleted because [reason]"). **Supersession** (via `write_memory` with `supersedes` parameter): the new memory is the edge source, no deletion marker needed.

4. **`traverse_graph(entity, depth?, relationship_type?)`** — Walk the knowledge graph from a data_point. Returns connected entities, memories, and their relationships. Follows both entity edges AND memory provenance chains. Uses recursive CTE for multi-hop traversal.

**Lifecycle & Failure Modes:**
- **Registration:** `install.py` adds `mcpServers` entry to `~/.claude/settings.json`: `{"memory": {"command": "python3", "args": ["~/.claude/scripts/memory_server.py"], "env": {}}}`. Claude Code manages the process lifecycle (start on session open, stop on session close, auto-restart on crash).
- **Startup:** Server must respond within 10 seconds. FastEmbed model loading happens in a background thread; `search_memories` returns SQL-only results until the model is warm.
- **Fallback:** If the MCP server is unavailable, SessionStart SQL loading continues to work (it does not depend on MCP). The 4 MCP tools become unavailable — Claude cannot search/write memories mid-conversation but still receives context at session start. This is graceful degradation, not a hard failure.
- **Graceful degradation:** If FastEmbed is unavailable, `search_memories` falls back to SQL-only ranking (salience + recency). If sqlite-vec is unavailable, vector search is skipped.

**Latency:**
- SessionStart (SQL loading): ~60ms (no MCP, no vectors)
- MCP search_memories: ~100-300ms (warm model, acceptable for mid-conversation)
- MCP write_memory: ~50ms (DB insert + embed)
- MCP traverse_graph: ~10ms (SQL recursive CTE)

### Smart Loading (SessionStart)

Pure SQL queries against `data_points`, no vector search:

```sql
-- 1. User profile (permanent, ~500 tokens)
SELECT content FROM data_points WHERE scope = 'user' AND salience > 0.5
ORDER BY salience DESC;

-- 2. Session continuity (~500 tokens)
SELECT content, properties FROM data_points
WHERE type = 'session_context' AND scope = ?
ORDER BY created_at DESC LIMIT 1;

-- 3. Project memories (~2K tokens)
SELECT content FROM data_points
WHERE scope = ? AND type = 'memory' AND salience > 0.4
ORDER BY salience DESC, last_accessed DESC LIMIT 20;

-- 4. Global knowledge (~1K tokens)
SELECT content FROM data_points
WHERE scope = 'global' AND type = 'memory' AND salience > 0.6
ORDER BY salience DESC, last_accessed DESC LIMIT 10;

-- 5. Recent activity (~2K tokens)
SELECT content FROM data_points
WHERE scope IN ('global', ?) AND type = 'memory'
AND created_at > date('now', '-3 days')
ORDER BY created_at DESC LIMIT 15;
```

Target budget: ~6K tokens. **Deduplication:** Queries execute in priority order (1-5). Each subsequent query excludes data_point IDs already selected by earlier queries using a running `seen_ids` set. Access tracking on served data_points (existing Phase 2 logic, adapted).

### Session Continuity

**SessionEnd hook** writes a data_point:
```python
{
    "type": "session_context",
    "content": "Working on auth middleware rewrite. Completed JWT integration. Pending: rate limiting tests.",
    "scope": "myproject",
    "salience": 0.8,
    "entities": ["auth middleware", "JWT", "rate limiting"],
    "properties": {"files_touched": [...], "session_id": "abc123", "status": "in_progress"}
}
```

Plus `context_for` edges to entity data_points and optionally a `continues` edge to previous session context.

**SessionStart hook** retrieves the most recent session context for the current project scope.

**Session context creation mechanism:** The synthesis pipeline (`synthesis_cron.py`) writes `session_context` data_points as part of deferred synthesis, since it already reads transcripts and extracts session metadata. This is the least intrusive change — synthesis already runs after sessions end and has full transcript access for summarization. The session context is available by the next session (synthesis runs every 30 min or on next session start). For immediate context (same-day rapid sessions), the MCP server's `write_memory` tool can also create session context data_points if Claude detects session-end patterns.

### Unified Synthesis

Single output format (MEMORY_OPS only). No PROJECT blocks, no daily files, no LTM routing.

**Before (Phase 2):**
```
transcripts → LLM → PROJECT blocks + MEMORY_OPS → daily/*.md + memory.db
```

**After (Phase 3):**
```
transcripts → LLM → MEMORY_OPS only → memory.db (data_points + edges)
```

Enhanced MEMORY_OPS format with salience and provenance:
```json
{"ops": [
  {"action": "ADD", "fact": "Switched to gRPC for internal services",
   "scope": "myproject", "type": "design", "salience": 0.8,
   "entities": ["gRPC", "internal services"],
   "supersedes": "dp_abc123",
   "reason": "Latency investigation showed 3x improvement"},
  {"action": "UPDATE", "id": "dp_def456",
   "fact": "JWT with 15-min expiry and refresh tokens",
   "entities": ["JWT", "refresh tokens"]},
  {"action": "DELETE", "id": "dp_ghi789",
   "reason": "Contradicted: rotation is 90 days not 30"},
  {"action": "NOOP", "id": "dp_jkl012", "reason": "Confirmed correct"}
]}
```

**Eliminated code paths:** `build_dailies_from_project_blocks()`, `extract_routes_from_project_blocks()`, `inject_scopes()`, `write_daily_files()`, `append_to_ltm()`, `mark_routed_entries()`, `filter_daily_content()`, `_build_embedded_files()`, all markdown parsing/writing in synthesis.

### Web Frontend

Local web app for browsing and managing the knowledge graph.

**Tech stack:** Python backend (http.server or Flask-lite) + single HTML file with embedded CSS + vanilla JS + vis.js for graph visualization. No build step, no npm, no React.

**Views:**
1. **Dashboard** — memory stats, salience distribution, recent activity
2. **Browse** — filter data_points by scope, type, salience range, date
3. **Search** — keyword + semantic search with ranked results
4. **Graph** — interactive vis.js graph showing entities, memories, and edges
5. **Memory Detail** — view a single data_point, its edges, provenance chain
6. **Edit/Delete** — modify or remove memories

**API endpoints:** `/api/stats`, `/api/data_points`, `/api/search`, `/api/graph`, `/api/data_point/:id`

**How to run:** `python3 ~/.claude/scripts/web_app.py` → opens `http://localhost:8742`

**Security:** Server binds to `127.0.0.1` only (not `0.0.0.0`). No authentication — acceptable for single-user localhost tool. Port conflict: if 8742 is in use, try 8743-8749, print actual port. CORS: no CORS headers (same-origin only). API write operations (edit/delete) require a simple CSRF token generated at server start and embedded in the HTML page.

### Migration Strategy (v2 → v3)

1. Phase 1 already has `migrate_markdown_to_db()` — extend for unified schema
2. Existing chunks → `data_points` with `type='memory'`. **IDs preserved** (same primary key values).
3. Existing nodes → `data_points` with `type='entity'`. **IDs preserved** (same primary key values).
4. Profile sections (About Me, etc.) → `data_points` with `type='profile'`, `scope='user'`, `salience=1.0`, `consolidated=1`
5. Existing edges: keep rows as-is. Node IDs are preserved (step 3), chunk IDs are preserved (step 2), so existing edge `source`/`target` references remain valid. `ALTER TABLE edges ADD COLUMN reason TEXT` for the new column (using version-gated migration pattern from v1→v2). **Post-migration integrity check:** `SELECT COUNT(*) FROM edges WHERE source NOT IN (SELECT id FROM data_points) OR target NOT IN (SELECT id FROM data_points)` must return 0.
6. Existing `vec_chunks` → create `vec_data` virtual table, `INSERT INTO vec_data SELECT embedding, chunk_id AS data_point_id, source_type AS type FROM vec_chunks`, then `DROP TABLE vec_chunks`. Chunk IDs preserved (step 2), so `data_point_id` values remain valid.
7. Drop old `chunks` and `nodes` tables after data migration
8. Archive old markdown files to `~/.claude/memory/.archive/`
9. Bump SCHEMA_VERSION to 3

## Key Decisions

| Decision | Choice | Why |
|---|---|---|
| Unified data_points table | Replace separate chunks + nodes | One node table, one edge table, one vector table. Any-to-any relationships. No sync issues. Cognee's DataPoint pattern validated at scale. |
| Memory provenance chains | First-class edge types (supersedes, contradicts, etc.) | User requirement: Claude must explain *why* facts changed, not just know the current truth. |
| MCP server (not CLI scripts) | Persistent Python process with warm FastEmbed | Sub-100ms search vs 300-500ms cold start per invocation. Standard pattern across Mem0, Zep, Cognee. |
| SQL-based SessionStart (not vector) | Pure SQL queries: salience + recency + scope | No query to embed at session start (no user message yet). Same ~60ms latency as current markdown approach. |
| Deprecate /recall and /remember | MCP tools + proactive Claude behavior | Claude should search/write automatically. Skills add friction. Every major memory system (Mem0, Zep, Cognee, LangMem) uses tool calls, not explicit commands. |
| Three scopes (user/global/project) | Separate user profile from global knowledge | User profile always loads with guaranteed budget. Global knowledge ranked by salience. Prevents competition for tokens. |
| Cognee as reference (not fork) | Adapt patterns, don't adopt platform | Our system solves a different problem (Claude Code session memory vs general-purpose knowledge platform). Integration cost of forking exceeds building on our Phase 1-2 foundation. |
| Complete markdown deprecation | DB is sole store, web frontend for inspection | Eliminates dual write paths. One source of truth. Web UI is better for inspection than flat files. |
| Salience replaces LTM flag | LLM assigns 0.0-1.0 at synthesis time | Continuous spectrum is more expressive than binary. High-salience entries naturally persist; low-salience entries naturally decay. |
| vis.js for graph visualization | Lightweight, no build step | Works with vanilla JS. Good for knowledge graphs. No npm/React overhead. |

## Dead Code Inventory

Functions, constants, and modules that become dead in Phase 3, organized by file:

**`scripts/load_memory.py`:**
- `load_global_memory()` — reads markdown LTM file
- `load_project_memory()` — reads markdown project LTM
- `load_daily_summaries()` — reads daily markdown files
- `load_project_history()` — reads daily markdown files
- `_strip_profile_sections()` — strips profile from LTM for synthesis prompt
- `_build_embedded_files()` — builds file-based context for synthesis
- `_build_synthesis_prompt()` — replaced by simplified MEMORY_OPS prompt
- `_build_preextracted_prompt()` — PROJECT block format eliminated
- `_build_synthesis_instructions()` — replaced by MEMORY_OPS-only instructions
- `_get_project_names_str()` — PROJECT block format eliminated
- `TRANSCRIPT_LINE_BUDGET` — may change with new synthesis prompt
- `write_synthesis_prompt()` — rewritten for MEMORY_OPS-only

**`scripts/synthesis.py`:**
- `build_dailies_from_project_blocks()` — no daily files
- `extract_routes_from_project_blocks()` — no LTM routing
- `inject_scopes()` — scope goes in MEMORY_OPS directly
- `write_daily_files()` — no markdown writing
- `append_to_ltm()` — no LTM file management
- `mark_routed_entries()` — no `[routed]` system
- `parse_daily_sections()` / `merge_daily_sections()` — no daily file merging
- `DailyFile` / `RouteEntry` / `ProjectBlock` dataclasses — PROJECT format eliminated
- `MIN_ROUTE_KEYWORDS`, `ROUTE_CAP`, `TYPE_TO_SECTION` constants — routing eliminated

**`scripts/memory_utils.py`:**
- `filter_daily_content()` — no tag-based filtering
- `get_working_days()` — no longer relevant with salience-based loading
- `get_daily_dir()` — no daily files (keep for migration only)
- `get_global_memory_file()` — no markdown LTM (keep for migration only)
- `get_project_memory_dir()` — no markdown LTM (keep for migration only)
- `project_name_to_filename()` — no project LTM files

**`scripts/storage.py`:**
- `chunks` table DDL — replaced by `data_points`
- `nodes` table DDL — replaced by `data_points`
- `VEC_CHUNKS_DDL` — replaced by `vec_data`
- `insert_chunk()` / `insert_node()` — replaced by `insert_data_point()`
- `query_chunks_by_scope()` / `query_chunks_by_source()` — replaced by data_point queries
- `update_chunk_salience()` / `update_node_salience()` — unified to `update_data_point_salience()`

**`skills/`:**
- `skills/recall/SKILL.md` — deprecated (MCP `search_memories`)
- `skills/remember/SKILL.md` — deprecated (MCP `write_memory`)

**`scripts/chunking.py`:**
- `chunk_ltm_file()` — no LTM files to chunk (keep for migration only)
- `chunk_daily_file()` — no daily files to chunk (keep for migration only)

## Implementation Approach

### Three Parallel Worktrees + Integration

The three worktrees are independent and can develop in parallel. Integration is sequential after all three merge.

```
[mcp-server]  ||  [unified-schema + synthesis]  ||  [web-frontend]
       \                    |                          /
        \                   |                         /
         [integration: smart loading + session continuity + deprecation]
```

**Worktree 1: MCP Server**
- `scripts/memory_server.py` — MCP server with 4 tools
- Uses `mcp` Python SDK for protocol
- Warm FastEmbed in background thread
- References Cognee's MCP server code as template
- Tests: tool invocation, search ranking, write/delete operations, graph traversal

**Worktree 2: Unified Schema + DB-Primary Synthesis** (core)
- Schema v3: `data_points` + unified `edges` + `vec_data`
- Migration from v2 (automated, lossless)
- Synthesis rewrite: MEMORY_OPS-only output, no markdown
- Provenance chain creation (supersedes, contradicts, led_to, refines, supports)
- Eliminate: daily file writing, LTM routing, scope injection, routed marking
- Tests: migration, CRUD apply, provenance chains, edge types

**Worktree 3: Web Frontend**
- `scripts/web_app.py` — HTTP server + API
- `templates/web/` — HTML/CSS/JS with vis.js
- Graph visualization, browse, search, edit, delete
- Tests: API endpoints

**Integration (sequential, after all three merge):**
- Smart loading in `load_memory.py` — SQL queries against `data_points`
- Session continuity — SessionEnd writes context, SessionStart reads it
- Update `install.py` for MCP server config + schema v3
- Deprecate `/recall` and `/remember` skills
- Update CLAUDE.md instructions for proactive memory behavior
- Archive markdown file templates
- End-to-end testing

### Cognee as Reference

Cognee's open-source codebase (github.com/topoteretes/cognee) is used as a reference implementation throughout Phase 3:
- DataPoint model → our `data_points` table design
- MCP server code → our `memory_server.py` structure
- Graph completion retrieval → our `search_memories` hybrid search
- `save_interaction` pattern → our session context writing
- Tool API design → consistent parameters, IDs, scope filters
- QA comparison: benchmark retrieval quality against Cognee on equivalent queries
