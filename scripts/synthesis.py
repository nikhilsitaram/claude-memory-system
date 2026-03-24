#!/usr/bin/env python3
"""
Synthesis output parser and DB apply pipeline for Claude Code Memory System.

Parses MEMORY_OPS JSON from the synthesis subagent and applies results:
- Writes data_points rows via apply_memory_ops_v3 (ADD/UPDATE/DELETE/NOOP)
- Creates provenance edges between superseded and new data_points
- Runs post-processing (state pruning, decay, validation, timestamp)

Usage:
    python3 synthesis.py apply <output_file> --extracts <path1> [<path2>...]

Requirements: Python 3.9+
"""

import argparse
import contextlib
import io
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (  # noqa: E402
    get_global_memory_file,
    get_memory_dir,
    get_project_memory_dir,
    get_projects_dir,
    prune_stale_state_entries,
    rebuild_projects_index_quiet,
)

__all__ = [
    "MemoryOp",
    "SynthesisResult",
    "MEMORY_OPS_HEADER",
    "parse_synthesis_output",
    "compute_offsets_from_extracts",
    "run_post_processing",
    "run_validate_ltm",
    "apply_memory_ops_v3",
    "_apply_add_v3",
    "_apply_update_v3",
    "_apply_delete_v3",
    "_apply_noop_v3",
    "_get_or_create_entity",
]

# Delimiter patterns (legacy format headers retained for parse_synthesis_output backward compat)
DAILY_HEADER = re.compile(r"^===DAILY:(\d{4}-\d{2}-\d{2})===$")  # Legacy format
ROUTE_HEADER = re.compile(r"^===ROUTE:([^:]+):(.+)===$")  # Legacy format
PROJECT_HEADER = re.compile(r"^===PROJECT:([^=]+)===$")
MEMORY_OPS_HEADER = re.compile(r"^===MEMORY_OPS===$")
END_MARKER = "===END==="


@dataclass
class MemoryOp:
    """A single memory CRUD operation from LLM output."""

    action: str
    fact: str = ""
    id: str | None = None
    scope: str | None = None
    section: str | None = None
    type: str | None = None
    entities: list | None = None
    reason: str | None = None
    salience: float | None = None      # v3: LLM-assigned salience (0.0-1.0)
    supersedes: str | None = None       # v3: ID of data_point this replaces


@dataclass
class SynthesisResult:
    """Complete parsed synthesis output (v3: MEMORY_OPS only)."""

    memory_ops: list[MemoryOp] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _is_delimiter(line: str) -> bool:
    """Check if a line is any known delimiter (daily, route, project, memory_ops, or end)."""
    return bool(
        DAILY_HEADER.match(line)
        or ROUTE_HEADER.match(line)
        or PROJECT_HEADER.match(line)
        or MEMORY_OPS_HEADER.match(line)
        or line == END_MARKER
    )


def parse_synthesis_output(text: str) -> SynthesisResult:
    """Parse structured synthesis output into MEMORY_OPS operations.

    Supported format (v3):
        ===MEMORY_OPS===  followed by JSON block

    Legacy format headers (===DAILY===, ===ROUTE===, ===PROJECT===, ===END===)
    are recognised but skipped — only MEMORY_OPS content is extracted.

    Text before the first delimiter is ignored. Missing ===END=== produces
    a warning but content is still parsed.
    """
    result = SynthesisResult()
    lines = text.split("\n")
    has_end = END_MARKER in text

    has_daily = any(DAILY_HEADER.match(line.strip()) for line in lines)
    has_project = any(PROJECT_HEADER.match(line.strip()) for line in lines)
    if not has_end and (has_daily or has_project):
        result.warnings.append("Missing ===END=== marker; processing available content")

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # Check for MEMORY_OPS block
        if MEMORY_OPS_HEADER.match(line):
            json_lines = []
            i += 1
            while i < len(lines):
                stripped = lines[i].strip()
                if _is_delimiter(stripped):
                    break
                json_lines.append(lines[i])
                i += 1
            json_text = "\n".join(json_lines).strip()
            if json_text:
                try:
                    parsed = json.loads(json_text)
                    raw_ops = parsed.get("ops", [])
                    result.memory_ops = [
                        MemoryOp(
                            action=op.get("action", ""),
                            fact=op.get("fact", ""),
                            id=op.get("id"),
                            scope=op.get("scope"),
                            section=op.get("section"),
                            type=op.get("type"),
                            entities=op.get("entities"),
                            reason=op.get("reason"),
                            salience=op.get("salience"),
                            supersedes=op.get("supersedes"),
                        )
                        for op in raw_ops
                    ]
                except json.JSONDecodeError as e:
                    result.warnings.append(
                        f"Failed to parse MEMORY_OPS JSON: {e}"
                    )
            continue

        # Skip legacy ===PROJECT:X===, ===DAILY:...===, ===ROUTE:...=== blocks
        if PROJECT_HEADER.match(line) or DAILY_HEADER.match(line) or ROUTE_HEADER.match(line):
            i += 1
            while i < len(lines):
                if _is_delimiter(lines[i].strip()):
                    break
                i += 1
            continue

        i += 1

    return result


def run_validate_ltm() -> None:
    """Run LTM validation as function call (no subprocess)."""
    try:
        from devtools import cmd_validate_ltm

        with contextlib.redirect_stdout(io.StringIO()):
            cmd_validate_ltm(argparse.Namespace())
    except Exception:
        pass  # Non-critical


def run_decay() -> None:
    """Run decay as function call (no subprocess).

    .. deprecated:: Phase 4
        Use ``decay.decay_data_points()`` for v3 data_points decay.
        This function is kept for backward compatibility with ``run_post_processing``.
    """
    import warnings
    warnings.warn(
        "run_decay() is deprecated for markdown decay; use decay.decay_data_points() for v3",
        DeprecationWarning, stacklevel=2,
    )
    try:
        from decay import run as decay_run

        with contextlib.redirect_stdout(io.StringIO()):
            decay_run(dry_run=False)
    except Exception:
        pass  # Non-critical


def _reindex_after_synthesis() -> None:
    """Re-index all chunk vectors after synthesis completes.

    Opens its own DB connection, calls reindex_all(), then closes.
    Non-critical: failures are silently ignored.
    """
    try:
        from embeddings import reindex_all
        from storage import close_db, get_db

        conn = get_db()
        try:
            reindex_all(conn)
        finally:
            close_db(conn)
    except Exception:
        pass


def run_post_processing(
    extract_paths: list[str],
    offsets_json: str | None = None,
) -> None:
    """Run state pruning, cleanup, decay, validation, and timestamp update."""
    from datetime import datetime, timezone

    # Prune stale state entries
    try:
        prune_stale_state_entries()
    except Exception:
        pass  # Non-critical

    # Cleanup temp files
    paths_to_clean = list(extract_paths)
    if offsets_json:
        paths_to_clean.append(offsets_json)
    for path in paths_to_clean:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    # Rebuild projects index so decay sees current work days
    rebuild_projects_index_quiet()

    # Direct function calls instead of subprocesses
    run_validate_ltm()
    run_decay()

    _reindex_after_synthesis()

    # Update timestamp
    ts_file = get_memory_dir() / ".last-synthesis"
    ts_file.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")


_SESSION_HEADER_RE = re.compile(r"^Session:\s+(\S+)")


def _count_nonblank_lines(filepath: Path) -> int:
    """Count non-blank lines in a file."""
    count = 0
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def compute_offsets_from_extracts(extract_paths: list[str]) -> dict[str, dict]:
    """Compute session offsets from extract files and JSONL sources on disk.

    Parses session IDs from extract file headers, locates the corresponding
    JSONL transcript files in ~/.claude/projects/, and computes current file
    size and line count.

    This eliminates the dependency on --offsets-json being passed via CLI,
    which required the LLM to faithfully reproduce the argument.

    Returns:
        Dict mapping session_id -> {"offset": file_size, "lines": line_count}
    """
    # Parse session IDs from extract files
    session_ids: set[str] = set()
    for path in extract_paths:
        try:
            for line in Path(path).open(encoding="utf-8"):
                match = _SESSION_HEADER_RE.match(line)
                if match:
                    session_ids.add(match.group(1))
        except IOError:
            continue

    if not session_ids:
        return {}

    # Find JSONL files and compute offsets
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return {}

    offsets: dict[str, dict] = {}
    for sid in session_ids:
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            session_file = proj_dir / f"{sid}.jsonl"
            if session_file.exists():
                offsets[sid] = {
                    "offset": session_file.stat().st_size,
                    "lines": _count_nonblank_lines(session_file),
                }
                break

    return offsets


def _extract_date_from_extracts(extract_paths: list[str]) -> str:
    """Extract date from extract file names (format: *YYYY-MM-DD*).

    Scans file names for a YYYY-MM-DD pattern and returns the first match.
    Falls back to today's date if no date is found.
    """
    for path in extract_paths:
        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", Path(path).name)
        if date_match:
            return date_match.group(1)
    # Fallback: today
    from datetime import date

    return date.today().isoformat()


def main() -> int:
    """CLI entry point (v3: no-op, synthesis handled by synthesis_cron.py)."""
    parser = argparse.ArgumentParser(description="Synthesis output processor (v3)")
    parser.parse_args()
    print("synthesis.py v3: use synthesis_cron.py for synthesis.")
    return 0


# =============================================================================
# V3 Apply Pipeline — DB-only writes (no markdown)
# =============================================================================


def _get_or_create_entity(conn, entity_name: str, scope: str | None) -> str:
    """Return the ID of an entity data_point, creating it if absent.

    Uses content_hash to avoid duplicates across scopes.
    """
    from storage import DataPointRow, _content_hash, insert_data_point  # noqa: F401 (private but stable)

    content_hash = _content_hash(f"entity:{entity_name}")
    row = conn.execute(
        "SELECT id FROM data_points WHERE type='entity' AND content_hash=?",
        (content_hash,),
    ).fetchone()
    if row:
        return row[0]
    return insert_data_point(conn, DataPointRow(
        type="entity", name=entity_name, scope=scope,
        content=entity_name, source_type="synthesis_v3",
        salience=0.5,
    ))


def _apply_add_v3(conn, op: "MemoryOp") -> dict:
    """Apply an ADD operation — inserts a new data_point."""
    from datetime import datetime, timezone

    from memory_utils import sanitize_secrets
    from storage import DataPointRow, EdgeRow, create_provenance_edge, insert_data_point, insert_edge

    fact = sanitize_secrets(op.fact)
    salience = op.salience if op.salience is not None else 0.5
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    dp = DataPointRow(
        type="memory",
        content=fact,
        scope=op.scope,
        entry_type=op.type,
        source_type="synthesis_v3",
        salience=salience,
    )
    dp_id = insert_data_point(conn, dp)

    try:
        from storage import fts_insert
        fts_insert(conn, dp_id, fact, op.scope)
    except Exception as e:
        print(f"Warning: FTS5 sync failed for ADD: {e}", file=sys.stderr)

    # Create entity data_points and link them
    if op.entities:
        for entity_name in op.entities:
            entity_id = _get_or_create_entity(conn, entity_name, op.scope)
            insert_edge(conn, EdgeRow(
                source=dp_id, target=entity_id, type="mentions",
                created_at=now,
            ))

    # Create provenance edge if this supersedes an existing data_point
    if op.supersedes:
        try:
            create_provenance_edge(conn, dp_id, op.supersedes, "supersedes", op.reason)
        except (ValueError, sqlite3.IntegrityError):
            pass  # Self-reference, invalid type, or FK violation — skip

    # Attempt to generate embedding (best-effort; requires sqlite-vec)
    try:
        from embeddings import index_data_points
        index_data_points(conn, [dp_id])
    except Exception:
        pass

    return {"action": "ADD", "status": "inserted", "id": dp_id}


def _apply_update_v3(conn, op: "MemoryOp") -> dict:
    """Apply an UPDATE operation — modifies content/salience of an existing data_point."""
    from storage import update_data_point

    if not op.id:
        return {"action": "UPDATE", "status": "skipped", "reason": "missing id"}

    kwargs = {}
    if op.fact:
        kwargs["content"] = op.fact
    if op.salience is not None:
        kwargs["salience"] = op.salience
    if op.entities:
        import json as _json
        kwargs["entities"] = _json.dumps(op.entities)

    rows_affected = update_data_point(conn, op.id, **kwargs) if kwargs else 0

    if rows_affected > 0 and op.fact:
        try:
            from storage import fts_delete, fts_insert
            fts_delete(conn, op.id)
            fts_insert(conn, op.id, op.fact, op.scope)
        except Exception as e:
            print(f"Warning: FTS5 sync failed for UPDATE: {e}", file=sys.stderr)

    # Attempt to re-embed updated content
    if rows_affected > 0 and op.fact:
        try:
            from embeddings import index_data_points
            index_data_points(conn, [op.id])
        except Exception:
            pass

    return {"action": "UPDATE", "status": "updated" if rows_affected > 0 else "not_found", "id": op.id}


def _apply_delete_v3(conn, op: "MemoryOp") -> dict:
    """Apply a DELETE operation — soft-deletes a data_point (salience=0) and records provenance."""
    from datetime import datetime, timezone

    from storage import EdgeRow, insert_edge, soft_delete_data_point

    if not op.id:
        return {"action": "DELETE", "status": "skipped", "reason": "missing id"}

    rows_affected = soft_delete_data_point(conn, op.id)
    if rows_affected > 0:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        # Record a self-referencing-safe provenance: use a synthetic tombstone edge
        # from the (now deleted) dp pointing to itself is invalid; instead we record
        # a 'supersedes' edge from op.id → op.id would be self-ref. Use the op.id
        # as target of a separate tombstone node is overkill. Instead we just write
        # the reason into properties or use a 'deleted' edge from op.id → op.id would
        # violate the no-self-ref rule. So we store reason in the data_point properties.
        import json as _json
        existing = conn.execute(
            "SELECT properties FROM data_points WHERE id=?", (op.id,)
        ).fetchone()
        props = {}
        if existing and existing[0]:
            try:
                props = _json.loads(existing[0])
            except Exception:
                pass
        props["deleted_reason"] = op.reason or "Removed by synthesis"
        props["deleted_at"] = now
        conn.execute(
            "UPDATE data_points SET properties=? WHERE id=?",
            (_json.dumps(props), op.id),
        )
        # Create a self-referential provenance marker via a synthetic node
        # The C3 task says DELETE should create provenance edge with type="supersedes" targeting op.id
        # To avoid self-reference, we insert a tombstone data_point and point to it
        from storage import DataPointRow, insert_data_point
        tombstone_id = insert_data_point(conn, DataPointRow(
            type="tombstone",
            content=f"Deleted: {op.id}",
            scope=None,
            salience=0.0,
            source_type="synthesis_v3",
        ))
        insert_edge(conn, EdgeRow(
            source=tombstone_id, target=op.id, type="supersedes",
            reason=op.reason or "Deleted by synthesis",
            created_at=now,
        ))

    return {"action": "DELETE", "status": "deleted" if rows_affected > 0 else "not_found", "id": op.id}


def _apply_noop_v3(conn, op: "MemoryOp") -> dict:
    """Apply a NOOP — increments evidence_count confirming the fact is still correct."""
    from storage import query_data_point_by_id, update_data_point

    if not op.id:
        return {"action": "NOOP", "status": "skipped", "reason": "missing id"}

    dp = query_data_point_by_id(conn, op.id)
    if not dp:
        return {"action": "NOOP", "status": "not_found", "id": op.id}

    update_data_point(conn, op.id, evidence_count=dp.evidence_count + 1)
    return {"action": "NOOP", "status": "confirmed", "id": op.id}


def apply_memory_ops_v3(conn, ops: list) -> list:
    """Apply CRUD operations from MEMORY_OPS to the database only (no markdown writes).

    This is the v3 replacement for apply_memory_ops. It writes exclusively to
    the data_points and edges tables.

    Args:
        conn: Open SQLite connection to a v3 schema database.
        ops: List of MemoryOp instances.

    Returns:
        List of result dicts, one per op: {action, status, id?}.
    """
    results = []
    for op in ops:
        if op.action == "ADD":
            result = _apply_add_v3(conn, op)
        elif op.action == "UPDATE":
            result = _apply_update_v3(conn, op)
        elif op.action == "DELETE":
            result = _apply_delete_v3(conn, op)
        elif op.action == "NOOP":
            result = _apply_noop_v3(conn, op)
        else:
            result = {"action": op.action, "status": "unknown_action"}
        results.append(result)
    conn.commit()
    return results


if __name__ == "__main__":
    sys.exit(main())
