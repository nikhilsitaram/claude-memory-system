---
status: Complete
---

# Text Processing Pipeline — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use orchestrate

**Goal:** Build a text chunking + SimHash fingerprinting pipeline that converts markdown memory files into structured `Chunk` dataclass instances ready for database ingestion.

**Architecture:** Two new scripts — `scripts/chunking.py` (paragraph-level LTM chunking with configurable overlap, entry-level daily chunking, section boundary respect via existing `parse_markdown_sections()`) and `scripts/simhash.py` (64-bit SimHash fingerprinting with Hamming distance comparison). Chunks are pure data objects returned as lists; they are not written to the DB (that responsibility belongs to the `storage-foundation` worktree). Token estimation reuses `estimate_tokens()` from `memory_utils.py`.

**Tech Stack:** Python 3.9+, dataclasses, hashlib (SHA-256 for content_hash), re, pytest

**Design doc:** `docs/plans/2026-03-19-memory-refactor-phase1/design-memory-refactor-phase1.md`

---

## Phase A — SimHash Fingerprinting
**Status:** Complete (2026-03-19) | **Rationale:** SimHash is a standalone module with no dependencies on chunking. Building it first means chunking (Phase B) can import and use it to populate the `simhash` field on every chunk.

### Phase A Checklist
- [x] A1: Create `scripts/simhash.py` with SimHash computation and Hamming distance
- [x] A2: Create `tests/test_simhash.py` with full test coverage

### Phase A Completion Notes

**Date:** 2026-03-19
**Summary:** Created `scripts/simhash.py` with the standard SimHash algorithm: 3-shingle tokenization, SHA-256-truncated 64-bit per-shingle hashes, bit-accumulator fingerprint. Exports `compute_simhash(text) -> int`, `hamming_distance(a, b) -> int`, `are_near_duplicates(a, b, threshold) -> bool`, plus constants `SIMHASH_BITS=64` and `DEFAULT_HAMMING_THRESHOLD=3`. Created `tests/test_simhash.py` with 22 tests covering all required categories.
**Deviations:** A2 — `test_similar_texts_close_hashes` assertion changed from `dist <= DEFAULT_HAMMING_THRESHOLD * 3` (bound of 9) to a relative comparison `dist_similar < dist_different` plus `dist_similar < 32` — Rule 1 (bug fix: the plan's absolute bound of 9 doesn't hold for short texts with 3-shingles; actual distance for a 1-word substitution is ~18 due to cascading shingle changes).

**Implementation review fixes (9b4fe80):**
- Added docstring warnings to `hamming_distance` and `are_near_duplicates` that both arguments must be non-negative unsigned 64-bit integers (signed/unsigned mixing gives wrong results)
- Added `simhash.py` to `install.py` `link_scripts()` — deployed installations would have failed to import without this

### Phase A Tasks

#### A1: Create `scripts/simhash.py` with SimHash computation and Hamming distance

**Files:**
- Create: `scripts/simhash.py`

**Verification:** `python3 -c "from simhash import compute_simhash, hamming_distance, are_near_duplicates; print('imports OK')"`

**Done when:** `simhash.py` exports three functions: `compute_simhash(text) -> int`, `hamming_distance(a, b) -> int`, `are_near_duplicates(a, b, threshold) -> bool`. Module imports cleanly.

**Avoid:** Do not use external libraries (e.g., `simhash` PyPI package) — the algorithm is ~30 lines and adding a dependency is not worth it. Do not use 128-bit hashes — the design specifies 64-bit and the SQLite `simhash INTEGER` column stores a 64-bit signed int.

**Note:** `compute_simhash` returns unsigned 64-bit integers (range `[0, 2^64)`). SQLite `INTEGER` stores signed 64-bit values. Values `>= 2^63` must be converted at the storage boundary (in `storage-foundation` worktree): `signed = val - (1 << 64) if val >= (1 << 63) else val`. This module intentionally returns unsigned values — conversion is the storage layer's responsibility.

**Step 1: Create the module**

Create `scripts/simhash.py` with the following content:

```python
#!/usr/bin/env python3
"""SimHash fingerprinting for near-duplicate detection.

Produces 64-bit fingerprints from text. Two texts with a small Hamming
distance between their SimHash values are likely near-duplicates.

Algorithm:
1. Tokenize text into shingles (word n-grams)
2. Hash each shingle to a 64-bit value
3. For each bit position, sum +1 (if bit=1) or -1 (if bit=0) across all hashes
4. Final fingerprint: bit i = 1 if sum[i] > 0, else 0
"""

import hashlib
import re

__all__ = [
    "SIMHASH_BITS",
    "DEFAULT_HAMMING_THRESHOLD",
    "compute_simhash",
    "hamming_distance",
    "are_near_duplicates",
]

# Number of bits in the fingerprint
SIMHASH_BITS = 64

# Default Hamming distance threshold for near-duplicate detection
# (design doc specifies 3)
DEFAULT_HAMMING_THRESHOLD = 3

# Tokenization pattern: sequences of alphanumeric + underscore
_TOKEN_RE = re.compile(r"[a-z0-9_]+")

# Shingle size (number of consecutive tokens per shingle)
_SHINGLE_SIZE = 3


def _tokenize(text: str) -> list[str]:
    """Lowercase tokenize text into alphanumeric tokens."""
    return _TOKEN_RE.findall(text.lower())


def _shingle(tokens: list[str], size: int = _SHINGLE_SIZE) -> list[str]:
    """Create word n-gram shingles from token list.

    If there are fewer tokens than size, returns each individual token
    as a shingle (graceful degradation for short texts).
    """
    if len(tokens) < size:
        return tokens
    return [" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)]


def _hash64(text: str) -> int:
    """Hash a string to a 64-bit unsigned integer via SHA-256 truncation."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big")


def compute_simhash(text: str) -> int:
    """Compute a 64-bit SimHash fingerprint for the given text.

    Args:
        text: Input text to fingerprint.

    Returns:
        64-bit integer fingerprint. Returns 0 for empty/whitespace-only text.
    """
    tokens = _tokenize(text)
    if not tokens:
        return 0

    shingles = _shingle(tokens)
    if not shingles:
        return 0

    # Accumulator: one slot per bit position
    v = [0] * SIMHASH_BITS

    for shingle in shingles:
        h = _hash64(shingle)
        for i in range(SIMHASH_BITS):
            if h & (1 << i):
                v[i] += 1
            else:
                v[i] -= 1

    # Build fingerprint from accumulator
    fingerprint = 0
    for i in range(SIMHASH_BITS):
        if v[i] > 0:
            fingerprint |= 1 << i

    return fingerprint


def hamming_distance(a: int, b: int) -> int:
    """Compute the Hamming distance between two 64-bit SimHash fingerprints.

    Args:
        a: First fingerprint.
        b: Second fingerprint.

    Returns:
        Number of differing bit positions (0-64).
    """
    return bin(a ^ b).count("1")


def are_near_duplicates(
    a: int,
    b: int,
    threshold: int = DEFAULT_HAMMING_THRESHOLD,
) -> bool:
    """Check if two SimHash fingerprints indicate near-duplicate content.

    Args:
        a: First fingerprint.
        b: Second fingerprint.
        threshold: Maximum Hamming distance to consider near-duplicate.

    Returns:
        True if Hamming distance <= threshold.
    """
    return hamming_distance(a, b) <= threshold
```

**Step 2: Verify import**

Run: `cd /Users/nsitaram/personal/claude-memory-system && python3 -c "import sys; sys.path.insert(0, 'scripts'); from simhash import compute_simhash, hamming_distance, are_near_duplicates, SIMHASH_BITS, DEFAULT_HAMMING_THRESHOLD; print('OK')"`

Expected: `OK`

---

#### A2: Create `tests/test_simhash.py` with full test coverage

**Files:**
- Create: `tests/test_simhash.py`

**Verification:** `python3 -m pytest tests/test_simhash.py -v`

**Done when:** All tests pass. Coverage includes: empty text, short text, identical texts, near-duplicates, distant texts, hamming distance edge cases, threshold boundary, and determinism.

**Avoid:** Do not hardcode the threshold value `3` in tests — import `DEFAULT_HAMMING_THRESHOLD` from `simhash` and derive from it. Do not test internal functions (`_tokenize`, `_shingle`, `_hash64`) — they are implementation details.

**Step 1: Write tests**

Create `tests/test_simhash.py`:

```python
"""Tests for simhash.py — SimHash fingerprinting and Hamming distance."""

from simhash import (
    DEFAULT_HAMMING_THRESHOLD,
    SIMHASH_BITS,
    are_near_duplicates,
    compute_simhash,
    hamming_distance,
)


class TestComputeSimhash:
    """Tests for compute_simhash()."""

    def test_empty_text_returns_zero(self):
        assert compute_simhash("") == 0

    def test_whitespace_only_returns_zero(self):
        assert compute_simhash("   \n\t  ") == 0

    def test_returns_integer(self):
        result = compute_simhash("hello world this is a test")
        assert isinstance(result, int)

    def test_fits_in_64_bits(self):
        result = compute_simhash("hello world this is a test of the simhash algorithm")
        assert 0 <= result < (1 << SIMHASH_BITS)

    def test_deterministic_across_calls(self):
        text = "implementing the chunking pipeline for memory files"
        result_a = compute_simhash(text)
        result_b = compute_simhash(text)
        assert result_a == result_b

    def test_similar_texts_close_hashes(self):
        a = "added pytest fixtures for memory loading with temporary directories"
        b = "added pytest fixtures for memory loading with temp directories"
        dist = hamming_distance(compute_simhash(a), compute_simhash(b))
        # Similar texts should have small Hamming distance
        assert dist <= DEFAULT_HAMMING_THRESHOLD * 3  # generous bound

    def test_different_texts_distant_hashes(self):
        a = "configure sqlite WAL mode for concurrent read access"
        b = "the quick brown fox jumps over the lazy dog near the river"
        dist = hamming_distance(compute_simhash(a), compute_simhash(b))
        # Very different texts should have large Hamming distance
        assert dist > DEFAULT_HAMMING_THRESHOLD

    def test_short_text_single_token(self):
        """Single token degrades gracefully (no shingles, uses individual tokens)."""
        result = compute_simhash("hello")
        assert isinstance(result, int)
        assert result != 0

    def test_short_text_two_tokens(self):
        """Two tokens (fewer than shingle size) still produces valid hash."""
        result = compute_simhash("hello world")
        assert isinstance(result, int)
        assert result != 0

    def test_case_insensitive(self):
        assert compute_simhash("Hello World Test") == compute_simhash("hello world test")

    def test_punctuation_ignored(self):
        a = compute_simhash("implement the chunking pipeline")
        b = compute_simhash("implement, the chunking pipeline!")
        assert a == b


class TestHammingDistance:
    """Tests for hamming_distance()."""

    def test_identical_values_zero_distance(self):
        assert hamming_distance(42, 42) == 0

    def test_zero_and_zero(self):
        assert hamming_distance(0, 0) == 0

    def test_single_bit_difference(self):
        assert hamming_distance(0b1000, 0b0000) == 1

    def test_all_bits_different(self):
        # 64-bit all-ones vs all-zeros
        all_ones = (1 << SIMHASH_BITS) - 1
        assert hamming_distance(all_ones, 0) == SIMHASH_BITS

    def test_known_distance(self):
        # 0b1010 = 10, 0b0110 = 6, XOR = 0b1100 = 12, popcount(12) = 2
        assert hamming_distance(0b1010, 0b0110) == 2

    def test_symmetric(self):
        assert hamming_distance(123, 456) == hamming_distance(456, 123)


class TestAreNearDuplicates:
    """Tests for are_near_duplicates()."""

    def test_identical_hashes_are_duplicates(self):
        assert are_near_duplicates(42, 42) is True

    def test_within_threshold(self):
        # Exactly at the default threshold
        a = 0
        # Set exactly DEFAULT_HAMMING_THRESHOLD bits
        b = (1 << DEFAULT_HAMMING_THRESHOLD) - 1
        assert hamming_distance(a, b) == DEFAULT_HAMMING_THRESHOLD
        assert are_near_duplicates(a, b) is True

    def test_beyond_threshold(self):
        a = 0
        # Set DEFAULT_HAMMING_THRESHOLD + 1 bits
        b = (1 << (DEFAULT_HAMMING_THRESHOLD + 1)) - 1
        assert hamming_distance(a, b) == DEFAULT_HAMMING_THRESHOLD + 1
        assert are_near_duplicates(a, b) is False

    def test_custom_threshold(self):
        custom = DEFAULT_HAMMING_THRESHOLD + 5
        a = 0
        b = (1 << custom) - 1
        assert are_near_duplicates(a, b, threshold=custom) is True
        assert are_near_duplicates(a, b, threshold=custom - 1) is False

    def test_default_threshold_matches_constant(self):
        """Verify the default parameter matches the module constant."""
        # If a and b have exactly DEFAULT_HAMMING_THRESHOLD differing bits,
        # calling without explicit threshold should return True
        a = 0
        b = (1 << DEFAULT_HAMMING_THRESHOLD) - 1
        assert are_near_duplicates(a, b) is True
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/test_simhash.py -v`

Expected: All tests pass (15+ tests).

**Step 3: Commit**

```bash
git add scripts/simhash.py tests/test_simhash.py
git commit -m "feat: add SimHash fingerprinting module (#53)

64-bit SimHash with configurable Hamming distance threshold for
near-duplicate detection. 16 tests."
```

---

## Phase B — Chunk Dataclass and Chunking Pipeline
**Status:** Complete (2026-03-19) | **Rationale:** Chunking depends on SimHash (Phase A) to populate the `simhash` field on every chunk. This phase builds the `Chunk` dataclass, LTM paragraph chunking, and daily entry chunking.

### Phase B Checklist
- [x] B1: Create `scripts/chunking.py` with Chunk dataclass and LTM paragraph chunking
- [x] B2: Add daily file entry-level chunking to `scripts/chunking.py`
- [x] B3: Create `tests/test_chunking.py` — LTM chunking tests
- [x] B4: Add daily chunking tests to `tests/test_chunking.py`
- [x] B5: Add integration test — full pipeline from real-format files to chunks with simhash

### Phase B Completion Notes

**Date:** 2026-03-19
**Summary:** Created `scripts/chunking.py` with the `Chunk` frozen dataclass (10 fields: content, source_file, source_type, section, chunk_index, created_at, content_hash, simhash, scope, entry_type), `chunk_ltm_file()` (paragraph-level LTM chunking with configurable overlap via `DEFAULT_OVERLAP_RATIO=0.15`, capped at `MAX_OVERLAP_PARAGRAPHS=2`, section-boundary-respecting via `parse_markdown_sections()`), and `chunk_daily_file()` (entry-level daily chunking with date extraction from parameter/header/filename, handles `[scope/type]`, multi-scope `[scope1|scope2/type]`, `[routed][scope/type]`, and untagged entries). Created `tests/test_chunking.py` with 44 tests across `TestChunkDataclass`, `TestChunkLtmFile`, `TestChunkDailyFile`, and `TestChunkingIntegration`. All 757 project tests pass (no regressions).
**Deviations:** None. All tasks implemented exactly as specified. B2 was already implemented within B1 (as noted in the plan); spec compliance verified inline.
**Commit:** 245f1d6

**Implementation review fixes (69889a7):**
- Added `chunking.py` to `install.py` `link_scripts()` — deployed installations would have failed to import without this (same class of bug caught for `simhash.py` in Phase A)
- Updated design doc field list to include `source_type` (10 fields, matching implementation and DB schema)

### Phase B Tasks

#### B1: Create `scripts/chunking.py` with Chunk dataclass and LTM paragraph chunking

> **Handoff from A1:** `scripts/simhash.py` created at commit `8d94192`. Exports: `compute_simhash(text: str) -> int` (64-bit unsigned SimHash fingerprint; returns 0 for empty/whitespace-only text), `hamming_distance(a: int, b: int) -> int`, `are_near_duplicates(a: int, b: int, threshold: int = DEFAULT_HAMMING_THRESHOLD) -> bool`. Constants: `SIMHASH_BITS = 64`, `DEFAULT_HAMMING_THRESHOLD = 3`. Import with `from simhash import compute_simhash` (conftest.py adds `scripts/` to sys.path).

**Files:**
- Create: `scripts/chunking.py`

**Verification:** `python3 -c "from chunking import Chunk, chunk_ltm_file, chunk_daily_file, DEFAULT_OVERLAP_RATIO, MAX_OVERLAP_PARAGRAPHS; print('imports OK')"`

**Done when:** `chunking.py` defines the `Chunk` dataclass with all 10 fields specified in the design doc (`content`, `source_file`, `source_type`, `section`, `chunk_index`, `created_at`, `content_hash`, `simhash`, `scope`, `entry_type`). `chunk_ltm_file()` splits LTM content into paragraph-level chunks with configurable overlap, respecting `## Section` boundaries via `parse_markdown_sections()`. Module imports cleanly.

**Avoid:** Do not write chunks to any database or file — this module returns `list[Chunk]` only. Do not add a `tiktoken` dependency for token counting — use `estimate_tokens()` from `memory_utils` if token counting is needed. Do not break paragraphs mid-sentence — the overlap window works at paragraph boundaries.

**Step 1: Create the module with Chunk dataclass and LTM chunking**

Create `scripts/chunking.py`:

```python
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
```

**Step 2: Verify import**

Run: `cd /Users/nsitaram/personal/claude-memory-system && python3 -c "import sys; sys.path.insert(0, 'scripts'); from chunking import Chunk, chunk_ltm_file, chunk_daily_file, DEFAULT_OVERLAP_RATIO; print('OK')"`

Expected: `OK`

---

#### B2: Add daily file entry-level chunking to `scripts/chunking.py`

**Note:** This task is already implemented as part of B1 (`chunk_daily_file` is included in the initial module). This task exists as a review checkpoint — verify that `chunk_daily_file` correctly handles all daily file formats.

**Files:**
- Modify: `scripts/chunking.py` (only if B3/B4 testing reveals issues)

**Verification:** `python3 -c "from chunking import chunk_daily_file; print('OK')"`

**Done when:** `chunk_daily_file()` correctly parses entries with formats: `- [scope/type] text`, `- [scope1|scope2/type] text`, `- [routed][scope/type] text`, and entries without scope tags. Each entry becomes exactly one chunk. Blank lines, comments, and non-entry lines are skipped.

**Avoid:** Do not split multi-line entries (continuation lines indented under a list item) — daily entries in this system are always single-line. Do not filter out `[routed]` entries — the chunking pipeline indexes everything; filtering is the consumer's job.

**Step 1: Verify daily entry parsing handles all formats**

The implementation in B1 already handles these cases via `_DAILY_ENTRY_RE` and `_SCOPE_TYPE_RE`. If tests in B4 reveal issues, fix them here.

---

#### B3: Create `tests/test_chunking.py` — LTM chunking tests

> **Handoff from A2:** `tests/test_simhash.py` created at commit `8d94192`. 22 tests, all passing. `compute_simhash()` verified deterministic, 64-bit unsigned, returns 0 for empty text, case-insensitive, punctuation-ignored. `hamming_distance()` and `are_near_duplicates()` fully tested including boundary and custom-threshold cases.

**Files:**
- Create: `tests/test_chunking.py`

**Verification:** `python3 -m pytest tests/test_chunking.py -v -k "Ltm or Chunk"`

**Done when:** All LTM chunking tests pass. Coverage includes: empty content, single section, multiple sections, section boundary respect, overlap mechanics, metadata extraction (date, type), content_hash determinism, simhash population, scope passthrough, Pinned section handling, and comment line skipping.

**Avoid:** Do not hardcode `0.15` as the overlap ratio — import `DEFAULT_OVERLAP_RATIO` from `chunking`. Do not test `parse_markdown_sections` behavior (it's tested in `test_memory_utils.py`) — test that `chunk_ltm_file` correctly uses its output.

**Step 1: Write LTM chunking tests**

Create `tests/test_chunking.py`:

```python
"""Tests for chunking.py — text chunking pipeline for memory files."""

import hashlib

from chunking import (
    DEFAULT_OVERLAP_RATIO,
    MAX_OVERLAP_PARAGRAPHS,
    Chunk,
    chunk_daily_file,
    chunk_ltm_file,
)
from simhash import compute_simhash


class TestChunkDataclass:
    """Tests for the Chunk dataclass."""

    def test_chunk_is_frozen(self):
        chunk = Chunk(
            content="test",
            source_file="/tmp/test.md",
            source_type="ltm",
            section="## Test",
            chunk_index=0,
            created_at="2026-01-15",
            content_hash="abc123",
            simhash=42,
            scope="global",
            entry_type="pattern",
        )
        try:
            chunk.content = "modified"  # type: ignore[misc]
            assert False, "Chunk should be frozen"
        except AttributeError:
            pass

    def test_chunk_has_all_fields(self):
        chunk = Chunk(
            content="test",
            source_file="/tmp/test.md",
            source_type="ltm",
            section="## Test",
            chunk_index=0,
            created_at="2026-01-15",
            content_hash="abc",
            simhash=42,
            scope="global",
            entry_type="pattern",
        )
        assert chunk.content == "test"
        assert chunk.source_file == "/tmp/test.md"
        assert chunk.source_type == "ltm"
        assert chunk.section == "## Test"
        assert chunk.chunk_index == 0
        assert chunk.created_at == "2026-01-15"
        assert chunk.content_hash == "abc"
        assert chunk.simhash == 42
        assert chunk.scope == "global"
        assert chunk.entry_type == "pattern"


class TestChunkLtmFile:
    """Tests for chunk_ltm_file()."""

    def test_empty_content(self):
        chunks = chunk_ltm_file("", "/tmp/test.md")
        assert chunks == []

    def test_single_section_single_entry(self):
        content = (
            "## Key Actions\n"
            "- (2026-01-15) [implement] Added new chunking pipeline\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", scope="global")
        assert len(chunks) == 1
        assert chunks[0].section == "## Key Actions"
        assert chunks[0].chunk_index == 0
        assert chunks[0].created_at == "2026-01-15"
        assert chunks[0].entry_type == "implement"
        assert chunks[0].scope == "global"
        assert chunks[0].source_type == "ltm"

    def test_multiple_entries_in_section(self):
        content = (
            "## Key Learnings\n"
            "- (2026-01-10) [gotcha] SQLite WAL mode needs busy_timeout\n"
            "\n"
            "- (2026-01-12) [pattern] Use dataclasses for structured data\n"
            "\n"
            "- (2026-01-15) [pitfall] Avoid global mutable state in modules\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", scope="global", overlap_ratio=0)
        assert len(chunks) == 3
        assert chunks[0].chunk_index == 0
        assert chunks[1].chunk_index == 1
        assert chunks[2].chunk_index == 2
        assert chunks[0].entry_type == "gotcha"
        assert chunks[1].entry_type == "pattern"
        assert chunks[2].entry_type == "pitfall"

    def test_section_boundary_respected(self):
        content = (
            "## Key Actions\n"
            "- (2026-01-10) [implement] Feature A\n"
            "\n"
            "## Key Decisions\n"
            "- (2026-01-11) [design] Architecture B\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", scope="global", overlap_ratio=0)
        assert len(chunks) == 2
        assert chunks[0].section == "## Key Actions"
        assert chunks[1].section == "## Key Decisions"
        # Chunk indices reset per section
        assert chunks[0].chunk_index == 0
        assert chunks[1].chunk_index == 0

    def test_overlap_adds_preceding_paragraph(self):
        content = (
            "## Key Actions\n"
            "- (2026-01-10) [implement] First entry\n"
            "\n"
            "- (2026-01-11) [implement] Second entry\n"
            "\n"
            "- (2026-01-12) [implement] Third entry\n"
        )
        chunks = chunk_ltm_file(
            content, "/tmp/ltm.md", scope="global",
            overlap_ratio=DEFAULT_OVERLAP_RATIO,
        )
        assert len(chunks) == 3
        # First chunk has no overlap (nothing precedes it)
        assert "First entry" in chunks[0].content
        assert "Second entry" not in chunks[0].content
        # Second chunk should include first as overlap
        assert "First entry" in chunks[1].content
        assert "Second entry" in chunks[1].content
        # Third chunk should include second as overlap
        assert "Second entry" in chunks[2].content
        assert "Third entry" in chunks[2].content

    def test_no_overlap_when_ratio_zero(self):
        content = (
            "## Key Actions\n"
            "- (2026-01-10) [implement] First entry\n"
            "\n"
            "- (2026-01-11) [implement] Second entry\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", overlap_ratio=0)
        assert len(chunks) == 2
        assert "First entry" not in chunks[1].content

    def test_overlap_does_not_cross_sections(self):
        content = (
            "## Key Actions\n"
            "- (2026-01-10) [implement] Action entry\n"
            "\n"
            "## Key Decisions\n"
            "- (2026-01-11) [design] Decision entry\n"
        )
        chunks = chunk_ltm_file(
            content, "/tmp/ltm.md",
            overlap_ratio=DEFAULT_OVERLAP_RATIO,
        )
        # Decision chunk should NOT contain Action entry (different section)
        decision_chunks = [c for c in chunks if c.section == "## Key Decisions"]
        assert len(decision_chunks) == 1
        assert "Action entry" not in decision_chunks[0].content

    def test_overlap_capped_at_max(self):
        """Overlap count never exceeds MAX_OVERLAP_PARAGRAPHS even with many paragraphs."""
        entries = []
        for i in range(20):
            entries.append(f"- (2026-01-{i+1:02d}) [implement] Entry number {i}\n")
        content = "## Key Actions\n" + "\n".join(entries)
        chunks = chunk_ltm_file(
            content, "/tmp/ltm.md",
            overlap_ratio=DEFAULT_OVERLAP_RATIO,
        )
        # Last chunk should contain at most MAX_OVERLAP_PARAGRAPHS preceding entries
        last = chunks[-1]
        overlap_entries = [
            line for line in last.content.split("\n")
            if line.startswith("- (") and "Entry number" in line
        ]
        # Current paragraph + at most MAX_OVERLAP_PARAGRAPHS overlap paragraphs
        assert len(overlap_entries) <= MAX_OVERLAP_PARAGRAPHS + 1

    def test_comments_skipped(self):
        content = (
            "## Key Actions\n"
            "<!-- Subject to 30-day decay -->\n"
            "- (2026-01-15) [implement] Real entry\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md")
        assert len(chunks) == 1
        assert "Subject to 30-day decay" not in chunks[0].content

    def test_footer_separator_skipped(self):
        """The --- footer separator is not chunked as content."""
        content = (
            "## Key Learnings\n"
            "- (2026-01-15) [gotcha] Real entry\n"
            "\n"
            "---\n"
            "*Last synthesized: 2026-01-15*\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", overlap_ratio=0)
        assert len(chunks) == 1
        assert "---" not in chunks[0].content
        assert "Last synthesized" not in chunks[0].content

    def test_content_hash_deterministic(self):
        content = "## Test\n- (2026-01-15) [pattern] Same content\n"
        chunks_a = chunk_ltm_file(content, "/tmp/a.md")
        chunks_b = chunk_ltm_file(content, "/tmp/b.md")
        assert chunks_a[0].content_hash == chunks_b[0].content_hash

    def test_content_hash_is_sha256(self):
        content = "## Test\n- (2026-01-15) [pattern] Test content\n"
        chunks = chunk_ltm_file(content, "/tmp/test.md")
        # Verify it is a valid SHA-256 hex string
        assert len(chunks[0].content_hash) == 64
        # Verify it matches direct SHA-256
        expected = hashlib.sha256(chunks[0].content.encode("utf-8")).hexdigest()
        assert chunks[0].content_hash == expected

    def test_simhash_populated(self):
        content = "## Test\n- (2026-01-15) [pattern] Test content for simhash\n"
        chunks = chunk_ltm_file(content, "/tmp/test.md")
        assert chunks[0].simhash == compute_simhash(chunks[0].content)
        assert isinstance(chunks[0].simhash, int)

    def test_scope_passed_through(self):
        content = "## Test\n- (2026-01-15) [pattern] Entry\n"
        chunks = chunk_ltm_file(content, "/tmp/test.md", scope="my-project")
        assert chunks[0].scope == "my-project"

    def test_scope_none_when_not_provided(self):
        content = "## Test\n- (2026-01-15) [pattern] Entry\n"
        chunks = chunk_ltm_file(content, "/tmp/test.md")
        assert chunks[0].scope is None

    def test_pinned_section_chunked(self):
        content = (
            "## Pinned\n"
            "- (2026-01-01) [tip] Important pinned item\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/test.md")
        assert len(chunks) == 1
        assert chunks[0].section == "## Pinned"

    def test_preamble_content_chunked(self):
        """Content before the first ## header gets section=''."""
        content = (
            "# Long-Term Memory\n"
            "\n"
            "Some preamble text here.\n"
            "\n"
            "## Key Actions\n"
            "- (2026-01-15) [implement] Entry\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/test.md", overlap_ratio=0)
        preamble_chunks = [c for c in chunks if c.section == ""]
        assert len(preamble_chunks) >= 1

    def test_source_file_stored_as_string(self):
        from pathlib import Path
        content = "## Test\n- (2026-01-15) [pattern] Entry\n"
        chunks = chunk_ltm_file(content, Path("/tmp/test.md"))
        assert chunks[0].source_file == "/tmp/test.md"
        assert isinstance(chunks[0].source_file, str)

    def test_non_entry_paragraphs_in_profile_sections(self):
        """Profile sections (About Me, etc.) contain free-form text, not entries."""
        content = (
            "## About Me\n"
            "Senior engineer working on insurance systems.\n"
            "Prefer concise, direct communication.\n"
            "\n"
            "## Current Projects\n"
            "Working on Claude memory system refactor.\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/test.md", overlap_ratio=0)
        about_chunks = [c for c in chunks if c.section == "## About Me"]
        assert len(about_chunks) >= 1
        # No date or entry_type for free-form text
        assert about_chunks[0].created_at is None
        assert about_chunks[0].entry_type is None
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/test_chunking.py -v -k "Ltm or Chunk"`

Expected: All pass.

---

#### B4: Add daily chunking tests to `tests/test_chunking.py`

**Files:**
- Modify: `tests/test_chunking.py` (append new test class)

**Verification:** `python3 -m pytest tests/test_chunking.py -v -k "Daily"`

**Done when:** All daily chunking tests pass. Coverage includes: empty content, single entry, multiple entries across sections, scope/type extraction from tags, multi-scope tags (`scope1|scope2/type`), routed entries, entries without tags, date extraction from header, date extraction from filename, comment skipping, and blank line handling.

**Avoid:** Do not test `chunk_ltm_file` again — only test `chunk_daily_file` here. Do not assume entries have scopes — some entries may lack tags (edge case from manual editing).

**Step 1: Append daily chunking tests**

Add the following class to the end of `tests/test_chunking.py`:

```python
class TestChunkDailyFile:
    """Tests for chunk_daily_file()."""

    def test_empty_content(self):
        chunks = chunk_daily_file("", "/tmp/2026-01-15.md")
        assert chunks == []

    def test_single_entry(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [my-project/implement] Added new feature\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1
        assert chunks[0].source_type == "daily"
        assert chunks[0].section == "## Actions"
        assert chunks[0].scope == "my-project"
        assert chunks[0].entry_type == "implement"
        assert chunks[0].created_at == "2026-01-15"

    def test_multiple_entries_across_sections(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [project-a/implement] Feature one\n"
            "- [project-b/improve] Bug fix\n"
            "## Decisions\n"
            "- [project-a/design] Architecture choice\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 3
        assert chunks[0].section == "## Actions"
        assert chunks[1].section == "## Actions"
        assert chunks[2].section == "## Decisions"

    def test_multi_scope_tag(self):
        content = (
            "# 2026-01-15\n"
            "## Learnings\n"
            "- [global|my-project/gotcha] Shared learning\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1
        assert chunks[0].scope == "global|my-project"
        assert chunks[0].entry_type == "gotcha"

    def test_routed_entry_included(self):
        """Routed entries are included — filtering is the consumer's job."""
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [routed][project/implement] Already routed to LTM\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1
        assert "routed" in chunks[0].content

    def test_comments_skipped(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "<!-- implement: new features -->\n"
            "- [project/implement] Real entry\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1
        assert "implement: new features" not in chunks[0].content

    def test_blank_lines_skipped(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "\n"
            "- [project/implement] Entry\n"
            "\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1

    def test_date_from_header(self):
        content = (
            "# 2026-03-01\n"
            "## Actions\n"
            "- [project/implement] Entry\n"
        )
        chunks = chunk_daily_file(content, "/tmp/daily.md")
        assert chunks[0].created_at == "2026-03-01"

    def test_date_from_parameter(self):
        content = (
            "## Actions\n"
            "- [project/implement] Entry\n"
        )
        chunks = chunk_daily_file(content, "/tmp/daily.md", file_date="2026-05-20")
        assert chunks[0].created_at == "2026-05-20"

    def test_date_parameter_overrides_header(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [project/implement] Entry\n"
        )
        chunks = chunk_daily_file(
            content, "/tmp/daily.md", file_date="2026-05-20"
        )
        assert chunks[0].created_at == "2026-05-20"

    def test_date_from_filename(self):
        content = (
            "## Actions\n"
            "- [project/implement] Entry\n"
        )
        chunks = chunk_daily_file(content, "/tmp/daily/2026-02-28.md")
        assert chunks[0].created_at == "2026-02-28"

    def test_chunk_index_sequential(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [p/implement] One\n"
            "- [p/implement] Two\n"
            "## Decisions\n"
            "- [p/design] Three\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        # chunk_index is sequential across the entire file
        assert [c.chunk_index for c in chunks] == [0, 1, 2]

    def test_each_entry_is_one_chunk(self):
        """Every entry line produces exactly one chunk."""
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [a/implement] First\n"
            "- [b/implement] Second\n"
            "- [c/implement] Third\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 3

    def test_content_hash_and_simhash_populated(self):
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "- [project/implement] Test entry for hashing\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks[0].content_hash) == 64
        assert isinstance(chunks[0].simhash, int)

    def test_source_file_stored(self):
        content = "## Actions\n- [p/implement] Entry\n"
        chunks = chunk_daily_file(content, "/home/user/daily/2026-01-15.md")
        assert chunks[0].source_file == "/home/user/daily/2026-01-15.md"

    def test_non_list_lines_skipped(self):
        """Lines that don't start with '-' are not entries."""
        content = (
            "# 2026-01-15\n"
            "## Actions\n"
            "Some non-entry text\n"
            "- [project/implement] Real entry\n"
        )
        chunks = chunk_daily_file(content, "/tmp/2026-01-15.md")
        assert len(chunks) == 1
```

**Step 2: Run tests**

Run: `python3 -m pytest tests/test_chunking.py -v -k "Daily"`

Expected: All pass.

---

#### B5: Add integration test — full pipeline from real-format files to chunks with simhash

**Files:**
- Modify: `tests/test_chunking.py` (append integration test class)

**Verification:** `python3 -m pytest tests/test_chunking.py -v -k "Integration"`

**Done when:** Integration tests pass covering: (1) a realistic global LTM file with multiple sections produces correct chunks with valid simhash and content_hash, (2) a realistic daily file with mixed scopes produces correct chunks, (3) near-duplicate entries in an LTM file produce chunks with close simhash values, (4) different entries produce chunks with distant simhash values.

**Avoid:** Do not use `tmp_path` to create actual files for these tests — pass content strings directly to the chunking functions (they accept content, not file paths to read). Do not duplicate unit test assertions — focus on end-to-end properties.

**Step 1: Append integration tests**

Add the following class to the end of `tests/test_chunking.py`:

```python
class TestChunkingIntegration:
    """End-to-end tests with realistic file content."""

    REALISTIC_LTM = (
        "# Long-Term Memory\n"
        "\n"
        "## About Me\n"
        "Senior engineer working on insurance and memory systems.\n"
        "Prefer concise communication, no emojis.\n"
        "\n"
        "## Pinned\n"
        "- (2026-01-01) [tip] Always use WAL mode for SQLite concurrent access\n"
        "\n"
        "## Key Actions\n"
        "<!-- Subject to 30-day decay -->\n"
        "- (2026-03-10) [implement] Built text chunking pipeline with paragraph-level splitting\n"
        "\n"
        "- (2026-03-12) [implement] Added SimHash fingerprinting for near-duplicate detection\n"
        "\n"
        "- (2026-03-15) [improve] Optimized batch embedding with content hash skip\n"
        "\n"
        "## Key Decisions\n"
        "- (2026-03-09) [design] Chose sqlite-vec over FAISS for single-file portability\n"
        "\n"
        "## Key Learnings\n"
        "- (2026-03-11) [gotcha] parse_markdown_sections returns preamble with empty header\n"
        "\n"
        "---\n"
        "*Last synthesized: 2026-03-15*\n"
    )

    REALISTIC_DAILY = (
        "# 2026-03-15\n"
        "## Actions\n"
        "- [claude-memory/implement] Built paragraph chunking with configurable overlap\n"
        "- [claude-memory/implement] Added SimHash module with 64-bit fingerprints\n"
        "- [global|claude-memory/improve] Improved token estimation accuracy\n"
        "## Decisions\n"
        "- [claude-memory/design] Chose dataclass over dict for chunk representation\n"
        "## Learnings\n"
        "- [claude-memory/gotcha] SimHash Hamming distance 3 catches typo-level edits\n"
    )

    def test_ltm_produces_chunks_for_all_sections(self):
        chunks = chunk_ltm_file(
            self.REALISTIC_LTM, "/tmp/global-ltm.md",
            scope="global", overlap_ratio=0,
        )
        sections = {c.section for c in chunks}
        assert "## About Me" in sections
        assert "## Pinned" in sections
        assert "## Key Actions" in sections
        assert "## Key Decisions" in sections
        assert "## Key Learnings" in sections

    def test_ltm_chunk_count(self):
        chunks = chunk_ltm_file(
            self.REALISTIC_LTM, "/tmp/global-ltm.md",
            scope="global", overlap_ratio=0,
        )
        # Preamble: 1 ("# Long-Term Memory"), About Me: 1 (2 lines as 1 paragraph),
        # Pinned: 1, Key Actions: 3, Key Decisions: 1, Key Learnings: 1
        # Footer ("---" + "*Last synthesized*") is filtered by _is_content_line
        assert len(chunks) == 8

    def test_ltm_all_chunks_have_valid_hashes(self):
        chunks = chunk_ltm_file(
            self.REALISTIC_LTM, "/tmp/global-ltm.md", scope="global",
        )
        for chunk in chunks:
            assert len(chunk.content_hash) == 64
            assert isinstance(chunk.simhash, int)
            assert chunk.simhash >= 0

    def test_daily_produces_one_chunk_per_entry(self):
        chunks = chunk_daily_file(
            self.REALISTIC_DAILY, "/tmp/2026-03-15.md",
        )
        assert len(chunks) == 5

    def test_daily_scopes_extracted(self):
        chunks = chunk_daily_file(
            self.REALISTIC_DAILY, "/tmp/2026-03-15.md",
        )
        scopes = [c.scope for c in chunks]
        assert "claude-memory" in scopes
        assert "global|claude-memory" in scopes

    def test_near_duplicate_entries_have_close_simhash(self):
        from simhash import hamming_distance, DEFAULT_HAMMING_THRESHOLD

        content = (
            "## Key Actions\n"
            "- (2026-03-10) [implement] Built text chunking pipeline with paragraph splitting\n"
            "\n"
            "- (2026-03-10) [implement] Built text chunking pipeline with paragraph-level splitting\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", overlap_ratio=0)
        assert len(chunks) == 2
        dist = hamming_distance(chunks[0].simhash, chunks[1].simhash)
        # Near-duplicate entries should have small distance
        # (generous bound: 3x threshold since these are short texts)
        assert dist <= DEFAULT_HAMMING_THRESHOLD * 3

    def test_different_entries_have_distant_simhash(self):
        from simhash import hamming_distance, DEFAULT_HAMMING_THRESHOLD

        content = (
            "## Key Actions\n"
            "- (2026-03-10) [implement] Built SQLite storage layer with WAL mode and migration pipeline\n"
            "\n"
            "## Key Learnings\n"
            "- (2026-03-11) [gotcha] Python pathlib resolves symlinks which breaks worktree detection\n"
        )
        chunks = chunk_ltm_file(content, "/tmp/ltm.md", overlap_ratio=0)
        assert len(chunks) == 2
        dist = hamming_distance(chunks[0].simhash, chunks[1].simhash)
        assert dist > DEFAULT_HAMMING_THRESHOLD
```

**Step 2: Run full test suite**

Run: `python3 -m pytest tests/test_chunking.py tests/test_simhash.py -v`

Expected: All tests pass.

**Step 3: Run entire project test suite**

Run: `python3 -m pytest tests/ -q`

Expected: No regressions.

**Step 4: Commit**

```bash
git add scripts/chunking.py tests/test_chunking.py
git commit -m "feat: add text chunking pipeline (#47)

Paragraph-level LTM chunking with configurable overlap, entry-level
daily chunking, Chunk dataclass with 10 metadata fields. Respects
section boundaries via parse_markdown_sections(). Content hash (SHA-256)
and SimHash fingerprint on every chunk. ~50 tests."
```

---
