#!/usr/bin/env python3
"""One-time entity backfill for existing chunks.

Re-extracts entities for all chunks in memory.db where the entities
column is NULL. Uses Sonnet via 'claude -p' for high-quality extraction.

Idempotent: safe to re-run (skips chunks with entities already set).

Usage:
    python3 backfill.py              # Run entity extraction
    python3 backfill.py --dry-run    # Show what would be processed
    python3 backfill.py --batch-size 25  # Custom batch size
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from storage import ensure_db, close_db, query_chunk_by_id  # noqa: E402

BATCH_SIZE = 50
MODEL = "sonnet"


def get_chunks_needing_entities(conn) -> list[tuple[str, str]]:
    """Query chunks where entities IS NULL. Returns list of (id, content)."""
    rows = conn.execute(
        "SELECT id, content FROM chunks WHERE entities IS NULL"
    ).fetchall()
    return [(r[0], r[1]) for r in rows]


def build_extraction_prompt(chunks: list[tuple[str, str]]) -> str:
    """Build a prompt for entity extraction from a batch of chunks."""
    chunk_lines = []
    for cid, content in chunks:
        chunk_lines.append(f"[{cid}] {content}")
    chunks_block = "\n".join(chunk_lines)

    return (
        "Extract structured entities from each memory chunk below.\n"
        "For each chunk ID, return a JSON object with the chunk_id and an entities array.\n"
        "\n"
        "Entities to extract: project names, library/tool names, programming languages,\n"
        "concepts, people, URLs, dates, file paths.\n"
        "\n"
        "Output ONLY valid JSON, no prose:\n"
        '{"results": [\n'
        '  {"chunk_id": "abc123", "entities": ["Python", "pytest", "TDD"]},\n'
        "  ...\n"
        "]}\n"
        "\n"
        f"Chunks:\n{chunks_block}"
    )


def extract_entities_batch(chunks: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Call Sonnet to extract entities for a batch of chunks.

    Returns dict mapping chunk_id -> entities list.
    """
    prompt = build_extraction_prompt(chunks)
    try:
        result = subprocess.run(
            ["claude", "-p", "--model", MODEL, "--no-session-persistence"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            print(f"Warning: claude -p failed: {result.stderr[:200]}", file=sys.stderr)
            return {}

        output = result.stdout.strip()
        parsed = json.loads(output)
        return {
            r["chunk_id"]: r.get("entities", [])
            for r in parsed.get("results", [])
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as e:
        print(f"Warning: Entity extraction failed: {e}", file=sys.stderr)
        return {}


def run_backfill(dry_run: bool = False, batch_size: int = BATCH_SIZE) -> int:
    """Run entity backfill on all chunks missing entities."""
    conn = ensure_db()
    try:
        chunks = get_chunks_needing_entities(conn)
        total = len(chunks)

        if total == 0:
            print("No chunks need entity extraction.")
            return 0

        if dry_run:
            num_batches = (total + batch_size - 1) // batch_size
            print(f"DRY RUN: {total} chunks would be processed in "
                  f"{num_batches} batches")
            return 0

        processed = 0
        for i in range(0, total, batch_size):
            batch = chunks[i:i + batch_size]
            results = extract_entities_batch(batch)

            for cid, entities in results.items():
                entities_json = json.dumps(entities)
                conn.execute(
                    "UPDATE chunks SET entities = ? WHERE id = ?",
                    (entities_json, cid),
                )
                processed += 1

            conn.commit()
            print(f"Processed {min(i + batch_size, total)}/{total} chunks")

        print(f"Backfill complete: {processed}/{total} chunks updated")
        return 0

    finally:
        close_db(conn)


def main() -> int:
    """CLI entry point for entity backfill."""
    parser = argparse.ArgumentParser(
        description="Entity backfill for memory chunks"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be processed without changes",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=BATCH_SIZE,
        help=f"Chunks per LLM call (default {BATCH_SIZE})",
    )
    args = parser.parse_args()
    return run_backfill(dry_run=args.dry_run, batch_size=args.batch_size)


if __name__ == "__main__":
    sys.exit(main())
