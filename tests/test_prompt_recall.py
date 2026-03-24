#!/usr/bin/env python3
"""
Unit tests for scripts/prompt_recall.py (UserPromptSubmit hook)

Run with: python3 -m pytest tests/test_prompt_recall.py -v
"""

import json
import time

import pytest
from unittest.mock import patch, MagicMock


class TestRelevanceGate:
    """Tests for the relevance gate that filters prompts before search."""

    def test_short_prompt_skipped(self):
        from prompt_recall import should_search
        assert should_search("ok") is False

    def test_empty_prompt_skipped(self):
        from prompt_recall import should_search
        assert should_search("") is False

    def test_long_prompt_skipped(self):
        from prompt_recall import should_search, MAX_PROMPT_LENGTH
        assert should_search("x" * (MAX_PROMPT_LENGTH + 100)) is False

    def test_confirmation_pattern_skipped(self):
        from prompt_recall import should_search
        assert should_search("yes") is False
        assert should_search("looks good") is False
        assert should_search("go ahead") is False
        assert should_search("Ok") is False
        assert should_search("LGTM") is False
        assert should_search("sure") is False

    def test_skill_invocation_skipped(self):
        from prompt_recall import should_search
        assert should_search("/synthesize") is False
        assert should_search("/settings view") is False

    def test_normal_prompt_passes(self):
        from prompt_recall import should_search
        assert should_search("How should I configure Redis caching?") is True

    def test_medium_length_prompt_passes(self):
        from prompt_recall import should_search
        assert should_search("Fix the authentication bug in the login flow") is True


class TestSessionDedup:
    """Tests for session-scoped deduplication of injected memories."""

    def test_new_memory_not_deduped(self, tmp_path):
        from prompt_recall import is_recently_injected
        state_file = tmp_path / ".prompt-recall-state-test"
        assert is_recently_injected("dp-1", state_file) is False

    def test_recently_injected_memory_deduped(self, tmp_path):
        from prompt_recall import is_recently_injected, record_injection
        state_file = tmp_path / ".prompt-recall-state-test"
        record_injection("dp-1", state_file, prompt_index=1)
        assert is_recently_injected("dp-1", state_file, current_prompt_index=2) is True

    def test_old_injection_not_deduped(self, tmp_path):
        from prompt_recall import is_recently_injected, record_injection, DEDUP_WINDOW
        state_file = tmp_path / ".prompt-recall-state-test"
        record_injection("dp-1", state_file, prompt_index=1)
        assert is_recently_injected("dp-1", state_file, current_prompt_index=1 + DEDUP_WINDOW + 1) is False

    def test_state_keeps_last_5(self, tmp_path):
        from prompt_recall import record_injection
        state_file = tmp_path / ".prompt-recall-state-test"
        for i in range(10):
            record_injection(f"dp-{i}", state_file, prompt_index=i)
        state = json.loads(state_file.read_text())
        assert len(state["injections"]) == 5

    def test_corrupt_state_treated_as_empty(self, tmp_path):
        from prompt_recall import is_recently_injected
        state_file = tmp_path / ".prompt-recall-state-test"
        state_file.write_text("not json")
        assert is_recently_injected("dp-1", state_file) is False


class TestOutputFormat:
    """Tests for the output formatting of injected memories."""

    def test_format_single_memory(self):
        from prompt_recall import format_injection
        memories = [{"content": "Redis cache requires explicit TTL", "certainty": 4, "scope": "project"}]
        output = format_injection(memories)
        assert "[memory]" in output
        assert "Redis cache" in output
        assert "certainty: 4" in output

    def test_format_multiple_memories(self):
        from prompt_recall import format_injection
        memories = [
            {"content": "fact one", "certainty": 3, "scope": "global"},
            {"content": "fact two", "certainty": 2, "scope": "project"},
        ]
        output = format_injection(memories)
        assert output.count("- (") == 2

    def test_empty_memories_returns_empty(self):
        from prompt_recall import format_injection
        assert format_injection([]) == ""

    def test_max_injections_limit(self):
        from prompt_recall import format_injection, MAX_INJECTIONS
        memories = [{"content": f"fact {i}", "certainty": 3, "scope": "global"} for i in range(10)]
        output = format_injection(memories)
        assert output.count("- (") == MAX_INJECTIONS


class TestStaleStateCleanup:
    """Tests for stale state file cleanup."""

    def test_cleanup_removes_stale_files(self, tmp_path):
        from prompt_recall import cleanup_stale_state_files
        stale = tmp_path / ".prompt-recall-state-old"
        stale.write_text("{}")
        import os
        os.utime(str(stale), (time.time() - 90000, time.time() - 90000))
        fresh = tmp_path / ".prompt-recall-state-new"
        fresh.write_text("{}")
        cleanup_stale_state_files(tmp_path)
        assert not stale.exists()
        assert fresh.exists()
