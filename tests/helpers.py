"""Shared test factories and utilities."""

import json
from datetime import datetime, timezone
from pathlib import Path

from indexing import SessionInfo  # noqa: I001


def make_session_info(
    session_id="test-session",
    transcript_path=None,
    file_size=2000,
    project_path=None,
    created=None,
    file_mtime=None,
):
    """Factory for creating SessionInfo test objects."""
    return SessionInfo(
        session_id=session_id,
        transcript_path=transcript_path or Path("/tmp/test.jsonl"),
        project_hash="test-hash",
        file_mtime=file_mtime or datetime.now(timezone.utc),
        file_size=file_size,
        project_path=project_path,
        created=created,
    )


def make_jsonl_line(role, content):
    """Create a single JSONL transcript line."""
    return json.dumps({
        "type": role,
        "message": {"role": role, "content": content},
    })


def make_jsonl_content(messages):
    """Create JSONL content from list of (role, text) tuples."""
    lines = [
        json.dumps({"type": role, "message": {"role": role, "content": text}})
        for role, text in messages
    ]
    return "\n".join(lines) + "\n"
