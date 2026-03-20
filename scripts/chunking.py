#!/usr/bin/env python3
"""Text chunking pipeline for memory files.

Splits long-term memory (LTM) files into paragraph-level chunks with
configurable overlap, and daily files into entry-level chunks. Each chunk
is a Chunk dataclass instance with metadata.

Chunks are returned as dataclass instances — they are NOT written to the
database. The storage layer is responsible for persisting them.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from memory_utils import parse_markdown_sections
from simhash import compute_simhash

__all__ = [
    "Chunk",
    "DEFAULT_OVERLAP_RATIO",
    "MAX_OVERLAP_PARAGRAPHS",
    "chunk_ltm_file",
    "chunk_daily_file",
]

# Overlap ratio for paragraph-level chunking (10-20% as per design doc)
DEFAULT_OVERLAP_RATIO = 0.15

# Maximum number of preceding paragraphs to include as overlap context.
# Caps the ratio-based calculation to prevent unbounded growth in large sections.
MAX_OVERLAP_PARAGRAPHS = 2

# Comment line pattern (replicates memory_utils._COMMENT_LINE_RE locally
# to keep this module self-contained for its internal parsing)
_COMMENT_RE = re.compile(r"^\s*<!--.*-->\s*$")

# Footer patterns: structural metadata that should not become chunks
_FOOTER_RE = re.compile(r"^---\s*$")
_SYNTH_TIMESTAMP_RE = re.compile(r"^\*Last synthesized:.*\*\s*$")

# LTM entry pattern: - (YYYY-MM-DD) [type] Description
_LTM_ENTRY_RE = re.compile(
    r"^\s*-\s*\((\d{4}-\d{2}-\d{2})\)\s*\[([^\]]+)\]\s*(.+)"
)

# Daily entry pattern: - [scope/type] Description  or  - [scope1|scope2/type] Description
# Also handles [routed] prefix
_DAILY_ENTRY_RE = re.compile(
    r"^\s*-\s*(?:\[routed\])?\s*\[([^\]]+)\]\s*(.*)"
)

# Scope/type extractor from tag like "project/implement" or "global|project/design"
_SCOPE_TYPE_RE = re.compile(
    r"^([^\]/]+(?:\|[^\]/]+)*)/([^\]]+)$"
)

# Date header in daily files: # YYYY-MM-DD
_DATE_HEADER_RE = re.compile(r"^#\s+(\d{4}-\d{2}-\d{2})\s*$")


@dataclass(frozen=True)
class Chunk:
    """A chunk of memory content with metadata.

    Attributes:
        content: The text content of this chunk.
        source_file: Absolute path to the source markdown file.
        source_type: 'ltm' for long-term memory files, 'daily' for daily files.
        section: Section header (e.g., '## Key Actions') or '' for preamble.
        chunk_index: 0-based index within the (source_file, section) pair.
        created_at: ISO date string. For LTM: entry date. For daily: file date.
        content_hash: SHA-256 hex digest of content (for change detection).
        simhash: 64-bit SimHash fingerprint (for near-duplicate detection).
        scope: Project name, 'global', or None if not determinable.
        entry_type: Entry type (implement, design, gotcha, etc.) or None.
    """
    content: str
    source_file: str
    source_type: str  # 'ltm' or 'daily'
    section: str
    chunk_index: int
    created_at: str | None
    content_hash: str
    simhash: int
    scope: str | None
    entry_type: str | None


def _content_hash(text: str) -> str:
    """Compute SHA-256 hex digest for content deduplication."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_content_line(line: str) -> bool:
    """Check if a line has meaningful content (not blank, not structural metadata)."""
    stripped = line.strip()
    if not stripped:
        return False
    if _COMMENT_RE.match(line):
        return False
    if _FOOTER_RE.match(stripped):
        return False
    if _SYNTH_TIMESTAMP_RE.match(stripped):
        return False
    return True


def _extract_ltm_entry_metadata(
    line: str,
) -> tuple[str | None, str | None]:
    """Extract (created_at, entry_type) from an LTM entry line.

    Returns (None, None) if the line does not match LTM entry format.
    """
    match = _LTM_ENTRY_RE.match(line)
    if match:
        return match.group(1), match.group(2).lower()
    return None, None


def _split_paragraphs(lines: list[str]) -> list[list[str]]:
    """Split content lines into paragraphs separated by blank lines.

    Groups consecutive non-blank lines into paragraphs. Blank lines
    and HTML comments are treated as paragraph separators.
    """
    paragraphs: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if _is_content_line(line):
            current.append(line)
        else:
            if current:
                paragraphs.append(current)
                current = []

    if current:
        paragraphs.append(current)

    return paragraphs


def _chunk_paragraphs_with_overlap(
    paragraphs: list[list[str]],
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
) -> list[str]:
    """Group paragraphs into chunks with overlap at boundaries.

    Each paragraph becomes one chunk. When overlap_ratio > 0, trailing
    paragraphs from the previous chunk are prepended to the next chunk
    to provide context continuity.

    For LTM files, each "paragraph" is typically a single entry line
    (e.g., "- (2026-01-15) [pattern] Description"). These are kept as
    individual chunks — overlap adds the preceding entry(ies) as context.

    The overlap count is capped at MAX_OVERLAP_PARAGRAPHS to prevent
    unbounded growth in large sections.

    Args:
        paragraphs: List of paragraph line-groups.
        overlap_ratio: Fraction of preceding paragraphs to include as overlap.
            0.0 = no overlap, 0.15 = ~15% overlap (1 paragraph lookback per ~7).
            The overlap count is at least 1 when ratio > 0 and there are
            preceding paragraphs, and at most MAX_OVERLAP_PARAGRAPHS.

    Returns:
        List of chunk text strings.
    """
    if not paragraphs:
        return []

    # Calculate overlap count: at least 1 if ratio > 0, capped at MAX_OVERLAP_PARAGRAPHS
    overlap_count = (
        min(MAX_OVERLAP_PARAGRAPHS, max(1, round(len(paragraphs) * overlap_ratio)))
        if overlap_ratio > 0
        else 0
    )

    chunks: list[str] = []
    for i, para in enumerate(paragraphs):
        parts: list[str] = []

        # Add overlap from preceding paragraphs
        if overlap_count > 0 and i > 0:
            start = max(0, i - overlap_count)
            for j in range(start, i):
                parts.extend(paragraphs[j])

        # Add the current paragraph
        parts.extend(para)
        chunks.append("\n".join(parts))

    return chunks


def chunk_ltm_file(
    content: str,
    source_file: str | Path,
    scope: str | None = None,
    overlap_ratio: float = DEFAULT_OVERLAP_RATIO,
) -> list[Chunk]:
    """Chunk a long-term memory file into paragraph-level chunks.

    Respects ``## Section`` boundaries — paragraphs never cross section
    headers. Each section is chunked independently.

    Pinned sections and profile sections (About Me, Current Projects,
    Technical Environment, Patterns & Preferences) are chunked the same
    way as other sections — the caller can filter by section name if needed.

    Args:
        content: Raw markdown content of the LTM file.
        source_file: Path to the source file (stored in chunk metadata).
        scope: Project name or 'global'. If None, inferred as 'global'
            for the global LTM file, or from filename for project files.
        overlap_ratio: Fraction of preceding paragraphs to overlap.

    Returns:
        List of Chunk instances, ordered by section then chunk_index.
    """
    source_str = str(source_file)
    sections = parse_markdown_sections(content)
    chunks: list[Chunk] = []

    for header, lines in sections:
        paragraphs = _split_paragraphs(lines)
        if not paragraphs:
            continue

        chunk_texts = _chunk_paragraphs_with_overlap(paragraphs, overlap_ratio)

        for idx, text in enumerate(chunk_texts):
            # Try to extract date and type from the last LTM entry in this
            # chunk (walk all lines so that for overlapping chunks the
            # "current" paragraph's metadata wins over overlap lines).
            created_at = None
            entry_type = None

            for line in text.split("\n"):
                date, etype = _extract_ltm_entry_metadata(line)
                if date:
                    created_at = date
                    entry_type = etype

            chunks.append(Chunk(
                content=text,
                source_file=source_str,
                source_type="ltm",
                section=header,
                chunk_index=idx,
                created_at=created_at,
                content_hash=_content_hash(text),
                simhash=compute_simhash(text),
                scope=scope,
                entry_type=entry_type,
            ))

    return chunks


def chunk_daily_file(
    content: str,
    source_file: str | Path,
    file_date: str | None = None,
) -> list[Chunk]:
    """Chunk a daily file into entry-level chunks.

    Each ``- [scope/type] Description`` line becomes one chunk. This is
    the natural boundary for daily files — entries are atomic observations.

    Args:
        content: Raw markdown content of the daily file.
        source_file: Path to the source file (stored in chunk metadata).
        file_date: Date string (YYYY-MM-DD) for the file. If None,
            extracted from the ``# YYYY-MM-DD`` header or filename.

    Returns:
        List of Chunk instances, one per entry line.
    """
    source_str = str(source_file)
    sections = parse_markdown_sections(content)
    chunks: list[Chunk] = []
    chunk_index = 0

    # Try to extract date from content header if not provided
    if file_date is None:
        for line in content.split("\n"):
            m = _DATE_HEADER_RE.match(line)
            if m:
                file_date = m.group(1)
                break

    # Fallback: extract from filename (YYYY-MM-DD.md)
    if file_date is None:
        stem = Path(source_file).stem
        if re.match(r"\d{4}-\d{2}-\d{2}$", stem):
            file_date = stem

    for header, lines in sections:
        for line in lines:
            # Skip blank lines, comments, and non-entry lines
            if not line.strip():
                continue
            if _COMMENT_RE.match(line):
                continue
            if not line.strip().startswith("-"):
                continue

            # Extract scope and type from tag
            scope = None
            entry_type = None
            tag_match = _DAILY_ENTRY_RE.match(line)
            if tag_match:
                tag_content = tag_match.group(1)
                scope_type_match = _SCOPE_TYPE_RE.match(tag_content)
                if scope_type_match:
                    scope = scope_type_match.group(1)
                    entry_type = scope_type_match.group(2).lower()
                else:
                    # Tag without scope (e.g., [implement])
                    entry_type = tag_content.lower()

            text = line.strip()
            chunks.append(Chunk(
                content=text,
                source_file=source_str,
                source_type="daily",
                section=header,
                chunk_index=chunk_index,
                created_at=file_date,
                content_hash=_content_hash(text),
                simhash=compute_simhash(text),
                scope=scope,
                entry_type=entry_type,
            ))
            chunk_index += 1

    return chunks
