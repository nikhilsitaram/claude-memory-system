#!/usr/bin/env python3
"""Memory consolidation pipeline for Claude Code Memory System.

Finds groups of redundant memories via vector similarity clustering,
uses headless LLM to merge or skip each cluster, writes merged results
as new data_points with supersedes edges to originals.

Usage:
    python3 consolidation.py                # Run with settings gate
    python3 consolidation.py --force        # Skip interval/count gates
    python3 consolidation.py --dry-run      # Show what would be consolidated
"""
import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import load_settings, sanitize_secrets
from storage import (
    DataPointRow,
    EdgeRow,
    ensure_db,
    fts_insert,
    insert_data_point,
    insert_edge,
    soft_delete_data_point,
)


def find_clusters(conn, similarity_threshold=0.80, max_clusters=15):
    """Find clusters of similar active memories for potential consolidation."""
    rows = conn.execute(
        "SELECT id FROM data_points WHERE type = 'memory' AND salience > 0.1"
    ).fetchall()
    active_ids = [r[0] for r in rows]

    if len(active_ids) < 2:
        return []

    sim_pairs = _get_similarity_pairs(conn, active_ids, similarity_threshold)

    excluded = _get_excluded_pairs(conn, active_ids)
    sim_pairs = [(a, b, s) for a, b, s in sim_pairs if (a, b) not in excluded and (b, a) not in excluded]

    clusters = _connected_components(sim_pairs, active_ids)

    result = []
    for members, edges in clusters:
        if len(members) < 2:
            continue
        if len(members) > 15:
            sub_clusters = _split_large_cluster(members, edges, max_size=15)
            for sub_members in sub_clusters:
                if len(sub_members) >= 2:
                    result.append({"members": sub_members, "similarities": edges})
        else:
            result.append({"members": members, "similarities": edges})

    # Enrich clusters with metadata for scoring, then sort by importance
    for cluster in result:
        _enrich_cluster_metadata(conn, cluster)
    result.sort(key=score_cluster, reverse=True)

    return result[:max_clusters]


def _get_similarity_pairs(conn, active_ids, threshold):
    """Query vec_data for pairwise similarities above threshold."""
    try:
        from embeddings import HAS_FASTEMBED, HAS_SQLITE_VEC
        if not HAS_FASTEMBED or not HAS_SQLITE_VEC:
            return []
    except ImportError:
        return []

    pairs = []
    id_set = set(active_ids)

    for dp_id in active_ids:
        try:
            row = conn.execute(
                "SELECT embedding FROM vec_data WHERE data_point_id = ?", (dp_id,)
            ).fetchone()
            if not row:
                continue

            neighbors = conn.execute(
                "SELECT data_point_id, distance FROM vec_data "
                "WHERE embedding MATCH ? AND k = 11 "
                "ORDER BY distance",
                (row[0],)
            ).fetchall()

            for neighbor_id, distance in neighbors:
                if neighbor_id == dp_id or neighbor_id not in id_set:
                    continue
                similarity = 1.0 - distance
                if similarity >= threshold:
                    pair = tuple(sorted([dp_id, neighbor_id]))
                    pairs.append((pair[0], pair[1], similarity))
        except Exception:
            continue

    seen = set()
    unique_pairs = []
    for a, b, s in pairs:
        key = (a, b)
        if key not in seen:
            seen.add(key)
            unique_pairs.append((a, b, s))

    return unique_pairs


def _get_excluded_pairs(conn, active_ids):
    """Get pairs with contradicts or supersedes edges (should not cluster)."""
    if not active_ids:
        return set()
    placeholders = ",".join("?" for _ in active_ids)
    rows = conn.execute(
        f"SELECT source, target FROM edges "
        f"WHERE type IN ('contradicts', 'supersedes') "
        f"AND valid_to IS NULL "
        f"AND source IN ({placeholders}) AND target IN ({placeholders})",
        active_ids + active_ids,
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def _connected_components(pairs, all_ids):
    """Extract connected components from similarity pairs."""
    parent = {}

    def find(x):
        if x not in parent:
            parent[x] = x
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        px, py = find(x), find(y)
        if px != py:
            parent[px] = py

    for a, b, _ in pairs:
        union(a, b)

    groups = {}
    for a, b, s in pairs:
        root = find(a)
        if root not in groups:
            groups[root] = {"members": set(), "edges": []}
        groups[root]["members"].add(a)
        groups[root]["members"].add(b)
        groups[root]["edges"].append((a, b, s))

    return [(list(g["members"]), g["edges"]) for g in groups.values()]


def _split_large_cluster(members, edges, max_size=15):
    """Split a cluster larger than max_size by removing weakest edges."""
    sorted_edges = sorted(edges, key=lambda x: x[2])
    removed = set()
    while True:
        active_edges = [e for e in sorted_edges if e not in removed]
        components = _connected_components(active_edges, members)
        if not components:
            return [members[:max_size]]
        if all(len(c[0]) <= max_size for c in components):
            result = [c[0] for c in components if len(c[0]) >= 2]
            if not result:
                return [members[:max_size]]
            return result
        for e in sorted_edges:
            if e not in removed:
                removed.add(e)
                break
        else:
            break
    return [members[:max_size]]


def _enrich_cluster_metadata(conn, cluster):
    """Add max_recency and avg_salience to a cluster dict for scoring."""
    saliences = []
    max_ts = None
    now = datetime.now(timezone.utc)
    for dp_id in cluster["members"]:
        row = conn.execute(
            "SELECT salience, created_at FROM data_points WHERE id = ?", (dp_id,)
        ).fetchone()
        if row:
            saliences.append(row[0] or 0.0)
            if row[1]:
                try:
                    ts = datetime.fromisoformat(row[1].replace("Z", "+00:00"))
                    if max_ts is None or ts > max_ts:
                        max_ts = ts
                except (ValueError, AttributeError):
                    pass

    cluster["avg_salience"] = sum(saliences) / len(saliences) if saliences else 0.0
    if max_ts:
        age_days = (now - max_ts).total_seconds() / 86400
        cluster["max_recency"] = max(0.0, 1.0 - age_days / 365.0)
    else:
        cluster["max_recency"] = 0.0


def score_cluster(cluster):
    """Score a cluster for priority ranking.

    Formula: 0.6 * member_count + 0.3 * max_recency + 0.1 * avg_salience
    """
    count = len(cluster["members"])
    max_recency = cluster.get("max_recency", 0.0)
    avg_salience = cluster.get("avg_salience", 0.0)
    return 0.6 * count + 0.3 * max_recency + 0.1 * avg_salience


def merge_cluster(conn, member_ids, model="sonnet"):
    """Call headless LLM to merge or skip a cluster.

    Returns dict: {"decision": "MERGE"|"SKIP", "fact": "...", "entities": [...], "reason": "..."}
    """
    members = []
    for dp_id in member_ids:
        row = conn.execute(
            "SELECT content, created_at, entities FROM data_points WHERE id = ?", (dp_id,)
        ).fetchone()
        if row:
            members.append({
                "id": dp_id,
                "content": sanitize_secrets(row[0]) if row[0] else row[0],
                "created_at": row[1],
                "entities": row[2],
            })

    prompt = _build_merge_prompt(members)

    try:
        result = subprocess.run(
            ["claude", "-p", "--no-session-persistence", "--model", model],
            input=prompt,
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return {"decision": "SKIP", "reason": f"LLM error: {result.stderr[:200]}"}

        return _parse_merge_response(result.stdout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"decision": "SKIP", "reason": f"LLM call failed: {e}"}


def _build_merge_prompt(members):
    """Build the LLM merge/skip prompt for a cluster."""
    lines = [
        "You are consolidating memories from a knowledge graph. These memories",
        "were grouped by text similarity.",
        "",
        "For each cluster, decide:",
        "- MERGE: if they are truly redundant (saying the same thing in different words).",
        "  Produce a single merged fact that preserves the most complete and accurate",
        "  version. Include dates when temporal sequence matters.",
        "- SKIP: if they represent evolving understanding, decision reversals, or",
        "  contain important nuance that would be lost by merging. Explain why.",
        "",
        "Guidelines:",
        "- If the cluster contains a decision that was later reversed or corrected,",
        "  preserve the reasoning journey.",
        "- Preserve dates when the temporal sequence matters.",
        "- When in doubt, SKIP.",
        "",
        "Cluster members:",
    ]
    for m in members:
        entities_str = m.get("entities", "[]") or "[]"
        lines.append(f'  [{m["created_at"]}] "{m["content"]}" (entities: {entities_str})')

    lines.append("")
    lines.append('Respond with JSON only: {"decision": "MERGE"|"SKIP", "fact": "...", "entities": [...], "reason": "..."}')
    return "\n".join(lines)


def _parse_merge_response(text):
    """Parse LLM merge/skip response JSON."""
    import re
    # Try direct parse first (clean JSON response)
    try:
        return json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        pass
    # Fall back to extracting JSON object from surrounding text
    json_match = re.search(r'\{.*\}', text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass
    return {"decision": "SKIP", "reason": "Could not parse LLM response"}


def write_merge_result(conn, merged_fact, original_ids, entities=None, certainty=None, scope=None):
    """Write a merged data_point and create supersedes edges to originals."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    max_sal = 0.0
    for oid in original_ids:
        row = conn.execute("SELECT salience FROM data_points WHERE id = ?", (oid,)).fetchone()
        if row and row[0]:
            max_sal = max(max_sal, row[0])
    new_salience = min(1.0, max_sal + 0.05)

    safe_fact = sanitize_secrets(merged_fact)
    entities_json = json.dumps(entities) if entities else None
    dp = DataPointRow(
        type="memory",
        content=safe_fact,
        scope=scope,
        source_type="consolidation",
        salience=new_salience,
        certainty=certainty,
        entities=entities_json,
        created_at=now,
        consolidated=1,
    )
    new_id = insert_data_point(conn, dp)

    for oid in original_ids:
        insert_edge(conn, EdgeRow(source=new_id, target=oid, type="supersedes", created_at=now))
        soft_delete_data_point(conn, oid)

    try:
        fts_insert(conn, new_id, safe_fact, scope)
    except Exception:
        pass

    try:
        from embeddings import index_data_points
        index_data_points(conn, [new_id])
    except Exception:
        pass

    return new_id


def run_consolidation(conn, settings=None, backfill=False, dry_run=False):
    """Run the full consolidation pipeline.

    Uses a directory-based lock to prevent concurrent runs from creating
    duplicate consolidated memories.
    """
    from memory_utils import FileLock, get_memory_dir

    lock = FileLock(str(get_memory_dir() / ".consolidation-lock"), timeout=5)
    if not lock.acquire():
        print("Consolidation skipped: another instance is running", file=sys.stderr)
        return {"clusters_found": 0, "clusters_merged": 0, "clusters_skipped": 0, "memories_consolidated": 0, "skipped_reason": "lock"}

    try:
        return _run_consolidation_locked(conn, settings, backfill, dry_run)
    finally:
        lock.release()


def _run_consolidation_locked(conn, settings, backfill, dry_run):
    """Inner consolidation logic, called while holding the lock."""
    if settings is None:
        settings = load_settings()

    consol = settings.get("consolidation", {})
    threshold = consol.get("similarityThreshold", 0.80)
    max_clusters = consol.get("backfillMaxClusters", 30) if backfill else consol.get("maxClusters", 15)
    model = consol.get("model", "sonnet")

    clusters = find_clusters(conn, similarity_threshold=threshold, max_clusters=max_clusters)

    stats = {"clusters_found": len(clusters), "clusters_merged": 0, "clusters_skipped": 0, "memories_consolidated": 0}

    if dry_run:
        print(f"Consolidation dry run: {len(clusters)} clusters found", file=sys.stderr)
        for i, cluster in enumerate(clusters):
            print(f"  Cluster {i+1}: {len(cluster['members'])} members", file=sys.stderr)
        return stats

    for cluster in clusters:
        result = merge_cluster(conn, cluster["members"], model=model)
        if result["decision"] == "MERGE":
            merged_fact = result.get("fact", "")
            if not merged_fact or not merged_fact.strip():
                stats["clusters_skipped"] += 1
                print("Consolidation SKIP: LLM returned empty merged fact", file=sys.stderr)
                continue
            scopes = []
            for mid in cluster["members"]:
                row = conn.execute("SELECT scope FROM data_points WHERE id = ?", (mid,)).fetchone()
                if row:
                    scopes.append(row[0])
            scope = max(set(scopes), key=scopes.count) if scopes else "global"

            cert_rows = conn.execute(
                f"SELECT MAX(certainty) FROM data_points WHERE id IN ({','.join('?' for _ in cluster['members'])})",
                cluster["members"]
            ).fetchone()
            max_cert = cert_rows[0] if cert_rows and cert_rows[0] else None

            write_merge_result(
                conn,
                merged_fact=merged_fact,
                original_ids=cluster["members"],
                entities=result.get("entities", []),
                certainty=max_cert,
                scope=scope,
            )
            conn.commit()
            stats["clusters_merged"] += 1
            stats["memories_consolidated"] += len(cluster["members"])
        else:
            stats["clusters_skipped"] += 1
            print(f"Consolidation SKIP: {result.get('reason', 'unknown')}", file=sys.stderr)

    return stats


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Memory consolidation pipeline")
    parser.add_argument("--force", action="store_true", help="Skip interval/count gates")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be consolidated")
    args = parser.parse_args()

    conn = ensure_db()
    try:
        stats = run_consolidation(conn, backfill=args.force, dry_run=args.dry_run)
        print(json.dumps(stats, indent=2))
    finally:
        conn.close()
