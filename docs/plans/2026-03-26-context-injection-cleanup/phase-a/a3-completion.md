# A3: SimHash Dedup Gate in synthesis _apply_add_v3

## Status: Complete

## What was implemented

Added a SimHash-based near-duplicate detection gate to _apply_add_v3 in scripts/synthesis.py. Before inserting a new memory data_point, the function now:

1. Computes the SimHash fingerprint of the incoming fact
2. Queries candidate data_points in the same scope with salience > 0 and created_at within 90 days
3. Compares Hamming distance between the new fingerprint and each candidate
4. When distance <= DEFAULT_HAMMING_THRESHOLD (3), returns status=deduped and updates the existing entry:
   - Increments evidence_count by 1
   - Boosts salience by 0.05 (capped at 1.0)
   - Preserves existing source_sessions
   - Replaces content (plus content_hash and simhash) when new text is >2x longer
5. When no near-duplicate is found, proceeds with normal INSERT (now storing simhash on every new data_point)

## Additional changes

- _simhash_to_sqlite() helper: Added to convert unsigned 64-bit SimHash values to signed two s-complement for SQLite INTEGER storage. Without this, compute_simhash() values >= 2^63 cause OverflowError on INSERT. Blocker fix (Deviation Rule 3).
- _simhash_to_sqlite added to __all__: Exported for use in tests and other modules.

## Files changed

- scripts/synthesis.py: Added _simhash_to_sqlite(), SimHash dedup gate in _apply_add_v3, simhash storage on new inserts (+65 lines)
- tests/test_synthesis.py: Added TestApplyAddV3SimhashDedup class with 4 tests (+133 lines)

## Tests added

1. test_near_duplicate_deduped -- Near-duplicate returns status=deduped, increments evidence_count, no new row
2. test_no_duplicate_inserts_normally -- Non-duplicate inserts normally with status=inserted
3. test_dedup_preserves_source_sessions -- Existing source_sessions preserved during dedup
4. test_dedup_replaces_content_when_new_is_longer -- Content replaced when new text is >2x longer

## Test results

- All 4 new tests pass
- All 62 tests in test_synthesis.py pass
- Full suite: 1036 passed, 8 skipped, 0 failures

## Deviations

- Deviation Rule 3 (Auto-fix blocker): Added _simhash_to_sqlite() for unsigned-to-signed 64-bit conversion. compute_simhash() returns values in [0, 2^64) but SQLite INTEGER is signed, causing OverflowError for values >= 2^63.
- Test string selection: Task-suggested strings had Hamming distance 14 (above threshold 3). Used strings with verified Hamming distance of exactly 3.
- Content replacement test: Uses pre-set simhash matching the long fact (d=0) since natural text pairs cannot be both >2x length difference AND d<=3 with 3-gram SimHash shingles.
