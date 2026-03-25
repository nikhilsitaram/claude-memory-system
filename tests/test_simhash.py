"""Tests for simhash.py -- SimHash fingerprinting and Hamming distance."""

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
        dist_similar = hamming_distance(compute_simhash(a), compute_simhash(b))
        # Similar texts should produce a smaller distance than clearly different texts
        c = "the quick brown fox jumps over the lazy dog near the river"
        d = "configure sqlite WAL mode for concurrent read access in production"
        dist_different = hamming_distance(compute_simhash(c), compute_simhash(d))
        # Similar pair should be closer than a randomly dissimilar pair
        assert dist_similar < dist_different
        # And should be well below the random expectation of ~32 bits different
        assert dist_similar < 32

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


class TestHammingDistanceSignedGuard:
    """Tests for hamming_distance rejection of signed (negative) integers."""

    def test_negative_a_raises_value_error(self):
        import pytest
        with pytest.raises(ValueError, match="non-negative"):
            hamming_distance(-1, 42)

    def test_negative_b_raises_value_error(self):
        import pytest
        with pytest.raises(ValueError, match="non-negative"):
            hamming_distance(42, -1)

    def test_both_negative_raises_value_error(self):
        import pytest
        with pytest.raises(ValueError, match="non-negative"):
            hamming_distance(-100, -200)

    def test_error_message_includes_cast_hint(self):
        import pytest
        with pytest.raises(ValueError, match="0xFFFFFFFFFFFFFFFF"):
            hamming_distance(-1, 0)

    def test_zero_values_still_work(self):
        assert hamming_distance(0, 0) == 0

    def test_large_unsigned_values_still_work(self):
        a = (1 << 63) + 42
        b = (1 << 63) + 43
        result = hamming_distance(a, b)
        assert isinstance(result, int)
        assert result >= 0

    def test_are_near_duplicates_also_guarded(self):
        """are_near_duplicates delegates to hamming_distance, so it inherits the guard."""
        import pytest
        with pytest.raises(ValueError, match="non-negative"):
            are_near_duplicates(-1, 42)
