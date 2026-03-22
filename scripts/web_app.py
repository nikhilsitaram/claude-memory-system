#!/usr/bin/env python3
"""
Web frontend for Claude Code Memory System.

Local web app for browsing, searching, and managing the knowledge graph.
Uses Python's built-in http.server + single HTML file with vis.js.

Usage: python3 web_app.py
Opens: http://localhost:8742
"""
import json
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

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
    update_data_point,
)

HOST = "127.0.0.1"
DEFAULT_PORT = 8742
MAX_PORT = 8749

_db_conn = None
_csrf_token = None


def generate_csrf_token() -> str:
    """Generate a cryptographically secure CSRF token."""
    return secrets.token_hex(32)


# ---------------------------------------------------------------------------
# Read-only query helpers
# ---------------------------------------------------------------------------

def _get_stats(conn: sqlite3.Connection) -> dict:
    """Return memory statistics: total count, salience distribution, recent activity."""
    total = conn.execute("SELECT COUNT(*) FROM data_points WHERE salience > 0").fetchone()[0]
    by_type = {}
    rows = conn.execute(
        "SELECT type, COUNT(*) FROM data_points WHERE salience > 0 GROUP BY type"
    ).fetchall()
    for row in rows:
        by_type[row[0]] = row[1]

    # Salience buckets: 0-0.2, 0.2-0.4, 0.4-0.6, 0.6-0.8, 0.8-1.0
    buckets = {"0.0-0.2": 0, "0.2-0.4": 0, "0.4-0.6": 0, "0.6-0.8": 0, "0.8-1.0": 0}
    sal_rows = conn.execute(
        "SELECT salience FROM data_points WHERE salience > 0"
    ).fetchall()
    for (sal,) in sal_rows:
        if sal < 0.2:
            buckets["0.0-0.2"] += 1
        elif sal < 0.4:
            buckets["0.2-0.4"] += 1
        elif sal < 0.6:
            buckets["0.4-0.6"] += 1
        elif sal < 0.8:
            buckets["0.6-0.8"] += 1
        else:
            buckets["0.8-1.0"] += 1

    recent_rows = conn.execute(
        "SELECT id, type, name, content, scope, salience, created_at "
        "FROM data_points WHERE salience > 0 ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    recent = [
        {
            "id": r[0], "type": r[1], "name": r[2],
            "content": (r[3] or "")[:100], "scope": r[4],
            "salience": r[5], "created_at": r[6],
        }
        for r in recent_rows
    ]

    return {
        "total_memories": total,
        "by_type": by_type,
        "salience_distribution": buckets,
        "recent_activity": recent,
    }


def _get_data_points(
    conn: sqlite3.Connection,
    scope: str = None,
    dp_type: str = None,
    limit: int = 50,
    offset: int = 0,
) -> list:
    """Return a list of data_points as dicts with optional filters and pagination."""
    conditions = ["salience > 0"]
    params = []

    if scope:
        conditions.append("scope = ?")
        params.append(scope)
    if dp_type:
        conditions.append("type = ?")
        params.append(dp_type)

    where = "WHERE " + " AND ".join(conditions)
    params.extend([limit, offset])
    rows = conn.execute(
        f"SELECT id, type, name, content, scope, entry_type, salience, created_at "
        f"FROM data_points {where} ORDER BY salience DESC, created_at DESC LIMIT ? OFFSET ?",
        params,
    ).fetchall()

    return [
        {
            "id": r[0], "type": r[1], "name": r[2],
            "content": r[3], "scope": r[4], "entry_type": r[5],
            "salience": r[6], "created_at": r[7],
        }
        for r in rows
    ]


def _search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list:
    """Full-text search over data_points content and name fields."""
    if not query:
        return []
    like = f"%{query}%"
    rows = conn.execute(
        "SELECT id, type, name, content, scope, salience, created_at "
        "FROM data_points WHERE salience > 0 AND (content LIKE ? OR name LIKE ?) "
        "ORDER BY salience DESC LIMIT ?",
        (like, like, limit),
    ).fetchall()
    return [
        {
            "id": r[0], "type": r[1], "name": r[2],
            "content": r[3], "scope": r[4],
            "salience": r[5], "created_at": r[6],
        }
        for r in rows
    ]


def _get_graph(conn: sqlite3.Connection, scope: str = None, limit: int = 200) -> dict:
    """Return nodes and edges for vis.js network rendering."""
    conditions = ["salience > 0"]
    params = []
    if scope:
        conditions.append("scope = ?")
        params.append(scope)
    where = "WHERE " + " AND ".join(conditions)
    params.append(limit)

    dp_rows = conn.execute(
        f"SELECT id, type, name, content, scope, salience "
        f"FROM data_points {where} ORDER BY salience DESC LIMIT ?",
        params,
    ).fetchall()

    node_ids = set()
    nodes = []
    for r in dp_rows:
        dp_id, dp_type, name, content, dp_scope, salience = r
        node_ids.add(dp_id)
        label = name or (content or "")[:40]
        nodes.append({
            "id": dp_id,
            "label": label,
            "type": dp_type,
            "scope": dp_scope,
            "salience": salience,
        })

    # Fetch edges between visible nodes
    edge_rows = conn.execute(
        "SELECT id, source, target, type, fact, weight "
        "FROM edges WHERE valid_to IS NULL"
    ).fetchall()

    edges = []
    for r in edge_rows:
        eid, source, target, etype, fact, weight = r
        if source in node_ids and target in node_ids:
            edges.append({
                "id": eid,
                "from": source,
                "to": target,
                "label": etype or "",
                "title": fact or "",
                "weight": weight,
            })

    return {"nodes": nodes, "edges": edges}


def _get_data_point_detail(conn: sqlite3.Connection, dp_id: str) -> dict:
    """Return a single data_point with all fields plus connected edges."""
    dp = query_data_point_by_id(conn, dp_id)
    if dp is None:
        return {"error": f"Data point {dp_id} not found"}

    edges_raw = query_edges_for_data_point(conn, dp_id)
    edges = [
        {
            "id": e.id, "source": e.source, "target": e.target,
            "type": e.type, "fact": e.fact, "reason": e.fact, "weight": e.weight,
            "valid_from": e.valid_from, "valid_to": e.valid_to,
        }
        for e in edges_raw
    ]

    return {
        "id": dp.id,
        "type": dp.type,
        "name": dp.name,
        "content": dp.content,
        "scope": dp.scope,
        "entry_type": dp.entry_type,
        "source_type": dp.source_type,
        "source_sessions": dp.source_sessions,
        "created_at": dp.created_at,
        "salience": dp.salience,
        "access_count": dp.access_count,
        "last_accessed": dp.last_accessed,
        "evidence_count": dp.evidence_count,
        "consolidated": dp.consolidated,
        "entities": dp.entities,
        "properties": dp.properties,
        "edges": edges,
    }


# ---------------------------------------------------------------------------
# Write helpers (D2)
# ---------------------------------------------------------------------------

def _edit_data_point(conn: sqlite3.Connection, dp_id: str, updates: dict) -> dict:
    """Update content and/or salience of a data_point, then re-embed if content changed."""
    dp = query_data_point_by_id(conn, dp_id)
    if not dp:
        return {"error": f"Data point {dp_id} not found"}

    kwargs = {}
    if "content" in updates:
        kwargs["content"] = updates["content"]
    if "salience" in updates:
        kwargs["salience"] = max(0.0, min(1.0, float(updates["salience"])))

    if kwargs:
        update_data_point(conn, dp_id, **kwargs)
        conn.commit()

    # Re-embed if content changed (optional; silently skip if unavailable)
    if "content" in updates:
        try:
            from embeddings import ensure_vec_table, index_data_points
            ensure_vec_table(conn)
            index_data_points(conn, [dp_id])
        except Exception:
            pass

    return {"status": "updated", "id": dp_id}


def _delete_data_point(conn: sqlite3.Connection, dp_id: str, reason: str = None) -> dict:
    """Soft-delete a data_point (set salience=0), invalidate its edges, and record provenance."""
    dp = query_data_point_by_id(conn, dp_id)
    if not dp:
        return {"error": f"Data point {dp_id} not found"}

    now = datetime.now(timezone.utc).isoformat()
    soft_delete_data_point(conn, dp_id)

    # Invalidate edges connected to this data_point
    edges = query_edges_for_data_point(conn, dp_id)
    for edge in edges:
        if edge.valid_to is None:
            invalidate_edge(conn, edge.id, now, now)

    # Create a tombstone data_point + supersedes edge to record deletion reason
    marker_content = reason or f"Deleted: {(dp.content or '')[:100]}"
    marker = DataPointRow(
        type="memory",
        content=marker_content,
        scope=dp.scope,
        salience=0.0,
        source_type="deletion",
        created_at=now,
    )
    marker_id = insert_data_point(conn, marker)
    insert_edge(conn, EdgeRow(
        source=marker_id,
        target=dp_id,
        type="supersedes",
        fact=reason,
        created_at=now,
        valid_from=now,
    ))

    conn.commit()
    return {"status": "deleted", "id": dp_id}


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------

class MemoryAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the memory web app."""

    def log_message(self, fmt, *args):
        pass  # Suppress default access log noise

    def _send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _validate_csrf(self) -> bool:
        token = self.headers.get("X-CSRF-Token", "")
        return bool(_csrf_token) and token == _csrf_token

    def _serve_html(self) -> None:
        html_path = Path(__file__).parent.parent / "templates" / "web" / "index.html"
        if not html_path.exists():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"index.html not found")
            return
        content = html_path.read_text(encoding="utf-8")
        content = content.replace("{{CSRF_TOKEN}}", _csrf_token or "")
        body = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        def qp(key, default=None):
            vals = qs.get(key)
            return vals[0] if vals else default

        if path == "" or path == "/":
            self._serve_html()

        elif path == "/api/stats":
            self._send_json(_get_stats(_db_conn))

        elif path == "/api/data_points":
            scope = qp("scope")
            dp_type = qp("type")
            limit = int(qp("limit", "50"))
            offset = int(qp("offset", "0"))
            self._send_json(_get_data_points(_db_conn, scope=scope, dp_type=dp_type, limit=limit, offset=offset))

        elif path == "/api/search":
            q = qp("q", "")
            limit = int(qp("limit", "20"))
            self._send_json(_search(_db_conn, q, limit=limit))

        elif path == "/api/graph":
            scope = qp("scope")
            limit = int(qp("limit", "200"))
            self._send_json(_get_graph(_db_conn, scope=scope, limit=limit))

        elif path.startswith("/api/data_point/"):
            dp_id = path[len("/api/data_point/"):]
            result = _get_data_point_detail(_db_conn, dp_id)
            status = 404 if "error" in result else 200
            self._send_json(result, status)

        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self) -> None:
        if not self._validate_csrf():
            self._send_json({"error": "CSRF token required"}, 403)
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/data_point/"):
            dp_id = path[len("/api/data_point/"):]
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                updates = json.loads(body)
            except json.JSONDecodeError:
                self._send_json({"error": "Invalid JSON"}, 400)
                return
            result = _edit_data_point(_db_conn, dp_id, updates)
            status = 404 if "error" in result else 200
            self._send_json(result, status)
        else:
            self._send_json({"error": "Not found"}, 404)

    def do_DELETE(self) -> None:
        if not self._validate_csrf():
            self._send_json({"error": "CSRF token required"}, 403)
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        if path.startswith("/api/data_point/"):
            dp_id = path[len("/api/data_point/"):]
            reason = qs.get("reason", [None])[0]
            result = _delete_data_point(_db_conn, dp_id, reason)
            status = 404 if "error" in result else 200
            self._send_json(result, status)
        else:
            self._send_json({"error": "Not found"}, 404)


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def _find_port() -> int:
    """Try ports DEFAULT_PORT through MAX_PORT; return first available."""
    import socket
    for port in range(DEFAULT_PORT, MAX_PORT + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((HOST, port))
                return port
            except OSError:
                continue
    raise OSError(f"No available port in range {DEFAULT_PORT}-{MAX_PORT}")


def run_server() -> None:
    """Start the web server."""
    global _db_conn, _csrf_token

    _db_conn = ensure_db()
    _csrf_token = generate_csrf_token()

    port = _find_port()
    server = HTTPServer((HOST, port), MemoryAPIHandler)
    print(f"Claude Memory Web UI: http://{HOST}:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if _db_conn:
            _db_conn.close()


if __name__ == "__main__":
    run_server()
