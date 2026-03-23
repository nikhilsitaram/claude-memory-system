#!/usr/bin/env python3
"""
MCP server for Claude Code Memory System.

Exposes 4 tools via the Model Context Protocol:
  - search_memories: vector + graph hybrid search
  - write_memory: atomic DB write with embedding
  - delete_memory: soft delete with provenance
  - traverse_graph: knowledge graph navigation

Runs as a persistent process managed by Claude Code (stdio transport).
"""
import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool
    HAS_MCP = True
except ImportError:
    HAS_MCP = False

from embeddings import _serialize_vector, embed_text, search_similar
from storage import (
    DataPointRow,
    EdgeRow,
    ensure_db,
    insert_data_point,
    insert_edge,
    invalidate_edge,
    query_data_point_by_id,
    query_edges_for_data_point,
    soft_delete_data_point,
)

TOOL_NAMES = ["search_memories", "write_memory", "delete_memory", "traverse_graph"]

_db_conn = None
_model_ready = threading.Event()


def init_db():
    """Open a DB connection at startup and return it."""
    global _db_conn
    _db_conn = ensure_db()
    return _db_conn


def _warm_model_async():
    """Load FastEmbed model in a background thread so server startup is not blocked."""
    def _warm():
        try:
            result = embed_text("warmup")
            if result and len(result) > 0:
                _model_ready.set()
        except Exception as exc:
            print(f"[memory_server] Model warmup failed: {exc}", file=sys.stderr)

    t = threading.Thread(target=_warm, daemon=True)
    t.start()
    return t


if HAS_MCP:
    server = Server("memory")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name="search_memories",
                description="Search memories by semantic similarity with graph-boosted ranking",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search query text"},
                        "scope": {"type": "string", "description": "Limit results to this scope"},
                        "top_k": {"type": "integer", "default": 10, "description": "Max results"},
                    },
                    "required": ["query"],
                },
            ),
            Tool(
                name="write_memory",
                description="Write a new memory to the knowledge base with optional entity linking",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "fact": {"type": "string", "description": "The memory content to store"},
                        "scope": {"type": "string", "description": "Scope for the memory (e.g. project name)"},
                        "salience": {"type": "number", "description": "Importance score 0.0-1.0"},
                        "entities": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Entity names mentioned in this memory",
                        },
                        "supersedes": {"type": "string", "description": "ID of data_point this replaces"},
                        "relation_type": {"type": "string", "description": "Edge type for supersedes relation"},
                        "relation_reason": {"type": "string", "description": "Reason for supersedes relation"},
                    },
                    "required": ["fact", "scope"],
                },
            ),
            Tool(
                name="delete_memory",
                description="Soft-delete a memory with provenance audit trail",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "ID of the data_point to delete"},
                        "reason": {"type": "string", "description": "Reason for deletion"},
                    },
                    "required": ["id"],
                },
            ),
            Tool(
                name="traverse_graph",
                description="Walk the knowledge graph from a data point using recursive CTE",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "entity": {"type": "string", "description": "ID of the starting data_point"},
                        "depth": {"type": "integer", "default": 2, "description": "Max traversal depth"},
                        "relationship_type": {
                            "type": "string",
                            "description": "Filter by edge type (e.g. 'mentions', 'supersedes')",
                        },
                    },
                    "required": ["entity"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name, arguments):
        if name == "search_memories":
            results = await _search_memories(
                arguments["query"],
                arguments.get("scope"),
                arguments.get("top_k", 10),
            )
            return [TextContent(type="text", text=json.dumps(results, indent=2))]

        if name == "write_memory":
            result = await _write_memory(
                fact=arguments["fact"],
                scope=arguments["scope"],
                salience=arguments.get("salience"),
                entities=arguments.get("entities"),
                supersedes=arguments.get("supersedes"),
                relation_type=arguments.get("relation_type"),
                relation_reason=arguments.get("relation_reason"),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == "delete_memory":
            result = await _delete_memory(
                id=arguments["id"],
                reason=arguments.get("reason"),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == "traverse_graph":
            result = await _traverse_graph(
                entity=arguments["entity"],
                depth=arguments.get("depth", 2),
                relationship_type=arguments.get("relationship_type"),
            )
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]


# =============================================================================
# Tool implementations
# =============================================================================

def _sql_ranked_search(conn, scope, top_k):
    """SQL fallback when FastEmbed model is not ready: rank by salience + recency."""
    from embeddings import ScoredDataPoint

    conditions = ["salience > 0"]
    params = []
    if scope:
        conditions.append("scope = ?")
        params.append(scope)

    where = "WHERE " + " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT id, type, name, content, scope, entry_type, source_type, source_sessions, "
        f"created_at, salience, access_count, last_accessed, evidence_count, "
        f"consolidated, content_hash, simhash, entities, properties "
        f"FROM data_points {where} ORDER BY salience DESC, created_at DESC LIMIT ?",
        params + [top_k],
    ).fetchall()

    results = []
    for row in rows:
        dp = DataPointRow(
            id=row[0], type=row[1], name=row[2], content=row[3],
            scope=row[4], entry_type=row[5], source_type=row[6],
            source_sessions=row[7], created_at=row[8], salience=row[9],
            access_count=row[10], last_accessed=row[11], evidence_count=row[12],
            consolidated=row[13], content_hash=row[14], simhash=row[15],
            entities=row[16], properties=row[17],
        )
        results.append(ScoredDataPoint(data_point=dp, score=dp.salience, vec_similarity=0.0))
    return results


async def _search_memories(query, scope=None, top_k=10):
    """Search memories by vector similarity or SQL fallback, with graph boost."""
    conn = _db_conn

    if _model_ready.is_set():
        results = search_similar(conn, query, top_k=top_k, scope=scope)
    else:
        results = _sql_ranked_search(conn, scope, top_k)

    result_ids = {r.data_point.id for r in results}
    for r in results:
        edges = query_edges_for_data_point(conn, r.data_point.id, direction="both")
        connected = sum(
            1 for e in edges
            if e.valid_to is None
            and (
                (e.source == r.data_point.id and e.target in result_ids)
                or (e.target == r.data_point.id and e.source in result_ids)
            )
        )
        boost = min(0.15, connected * 0.05)
        r.score += boost

    results.sort(key=lambda x: x.score, reverse=True)

    formatted = []
    for r in results[:top_k]:
        prov_edges = conn.execute(
            "SELECT id, type, fact FROM edges WHERE source=? "
            "AND type IN ('supersedes','contradicts','refines','led_to','supports') "
            "AND valid_to IS NULL",
            (r.data_point.id,),
        ).fetchall()
        formatted.append({
            "id": r.data_point.id,
            "content": r.data_point.content,
            "score": round(r.score, 3),
            "scope": r.data_point.scope,
            "entities": json.loads(r.data_point.entities) if r.data_point.entities else [],
            "provenance": [{"id": e[0], "type": e[1], "reason": e[2]} for e in prov_edges],
        })
    return formatted


def _get_or_create_entity(conn, entity_name, scope):
    """Return existing entity data_point ID, or create and return a new one."""
    row = conn.execute(
        "SELECT id FROM data_points WHERE LOWER(name) = LOWER(?) AND type = 'entity' AND scope = ?",
        (entity_name, scope),
    ).fetchone()
    if row:
        return row[0]

    now = datetime.now(timezone.utc).isoformat()
    entity_dp = DataPointRow(
        type="entity",
        name=entity_name,
        scope=scope,
        salience=0.5,
        source_type="manual",
        created_at=now,
    )
    return insert_data_point(conn, entity_dp)


async def _write_memory(fact, scope, salience=None, entities=None,
                        supersedes=None, relation_type=None, relation_reason=None):
    """Write a new memory data_point with embedding, entity links, and provenance."""
    conn = _db_conn
    try:
        conn.execute("BEGIN IMMEDIATE")

        if scope == "user":
            eff_salience = 1.0
            consolidated = 1
        else:
            eff_salience = salience if salience is not None else 0.7
            consolidated = 0

        now = datetime.now(timezone.utc).isoformat()
        dp = DataPointRow(
            type="memory",
            content=fact,
            scope=scope,
            salience=eff_salience,
            consolidated=consolidated,
            source_type="manual",
            created_at=now,
        )
        dp_id = insert_data_point(conn, dp)

        vec = embed_text(fact)
        if vec:
            conn.execute(
                "INSERT INTO vec_data (embedding, data_point_id, type) VALUES (?, ?, ?)",
                (_serialize_vector(vec), dp_id, "memory"),
            )

        if entities:
            for entity_name in entities:
                entity_id = _get_or_create_entity(conn, entity_name, scope)
                insert_edge(conn, EdgeRow(
                    source=dp_id,
                    target=entity_id,
                    type="mentions",
                    created_at=now,
                ))

        if supersedes:
            edge_type = relation_type or "supersedes"
            insert_edge(conn, EdgeRow(
                source=dp_id,
                target=supersedes,
                type=edge_type,
                fact=relation_reason,
                created_at=now,
                valid_from=now,
            ))
            soft_delete_data_point(conn, supersedes)

        conn.commit()
        return {"id": dp_id, "status": "created"}
    except Exception:
        conn.rollback()
        raise


async def _delete_memory(id, reason=None):
    """Soft-delete a data_point: salience=0, invalidate edges, create deletion marker."""
    conn = _db_conn
    target = query_data_point_by_id(conn, id)
    if not target:
        return {"error": f"Data point {id} not found"}

    try:
        conn.execute("BEGIN IMMEDIATE")
        now = datetime.now(timezone.utc).isoformat()

        soft_delete_data_point(conn, id)

        edges = query_edges_for_data_point(conn, id, direction="both")
        for edge in edges:
            if edge.valid_to is None:
                invalidate_edge(conn, edge.id, now, now)

        marker_content = reason or f"Deleted: {(target.content or '')[:100]}"
        marker = DataPointRow(
            type="memory",
            content=marker_content,
            scope=target.scope,
            salience=0.0,
            source_type="deletion",
            created_at=now,
        )
        marker_id = insert_data_point(conn, marker)

        insert_edge(conn, EdgeRow(
            source=marker_id,
            target=id,
            type="supersedes",
            fact=reason,
            created_at=now,
            valid_from=now,
        ))

        conn.commit()
        return {
            "status": "deleted",
            "deleted_content": target.content,
            "marker_id": marker_id,
        }
    except Exception:
        conn.rollback()
        raise


async def _traverse_graph(entity, depth=2, relationship_type=None):
    """Walk the knowledge graph using a recursive CTE. Returns nodes within depth hops."""
    conn = _db_conn

    type_clause = "AND e.type = ?" if relationship_type else ""

    query = f"""
    WITH RECURSIVE graph_walk(dp_id, depth, edge_type, edge_reason) AS (
        SELECT
            CASE WHEN e.source = ? THEN e.target ELSE e.source END,
            1,
            e.type,
            e.fact
        FROM edges e
        WHERE (e.source = ? OR e.target = ?)
          AND e.valid_to IS NULL
          {type_clause}

        UNION

        SELECT
            CASE WHEN e.source = gw.dp_id THEN e.target ELSE e.source END,
            gw.depth + 1,
            e.type,
            e.fact
        FROM graph_walk gw
        JOIN edges e ON (e.source = gw.dp_id OR e.target = gw.dp_id)
        WHERE gw.depth < ?
          AND e.valid_to IS NULL
          {type_clause}
          AND CASE WHEN e.source = gw.dp_id THEN e.target ELSE e.source END != ?
    )
    SELECT gw.dp_id, MIN(gw.depth) AS min_depth, MIN(gw.edge_type) AS edge_type,
           MIN(gw.edge_reason) AS edge_reason,
           dp.content, dp.type, dp.name, dp.scope
    FROM graph_walk gw
    JOIN data_points dp ON dp.id = gw.dp_id
    WHERE gw.dp_id != ?
    GROUP BY gw.dp_id, dp.content, dp.type, dp.name, dp.scope
    ORDER BY min_depth, dp.salience DESC
    """

    params = [entity, entity, entity]
    if relationship_type:
        params.append(relationship_type)
    params.append(depth)
    if relationship_type:
        params.append(relationship_type)
    params.append(entity)
    params.append(entity)

    rows = conn.execute(query, params).fetchall()
    return [
        {
            "id": r[0],
            "depth": r[1],
            "relationship": r[2],
            "reason": r[3],
            "content": r[4],
            "type": r[5],
            "name": r[6],
            "scope": r[7],
        }
        for r in rows
    ]


async def main():
    if not HAS_MCP:
        print("Error: mcp SDK not installed. Run: pip install mcp", file=sys.stderr)
        sys.exit(1)

    init_db()
    _warm_model_async()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
