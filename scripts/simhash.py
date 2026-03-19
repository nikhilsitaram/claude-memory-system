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

    Warning:
        Both arguments must be non-negative (unsigned) 64-bit integers.
        Mixing a signed value (e.g., read back from SQLite as a negative int)
        with an unsigned value produces silently wrong results because
        bin(a ^ b).count("1") counts extra sign-extension bits.
        Cast SQLite values with: value & 0xFFFFFFFFFFFFFFFF before comparing.
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

    Warning:
        Both arguments must be non-negative (unsigned) 64-bit integers.
        See hamming_distance() for details on the signed/unsigned footgun.
    """
    return hamming_distance(a, b) <= threshold
