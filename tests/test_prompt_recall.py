#!/usr/bin/env python3
"""
Unit tests for scripts/prompt_recall.py (UserPromptSubmit hook)

Run with: python3 -m pytest tests/test_prompt_recall.py -v
"""

import json
import time
from unittest.mock import MagicMock, patch


class TestRelevanceGate:
    """Tests for the relevance gate that filters prompts before search."""

    def test_short_prompt_skipped(self):
        from prompt_recall import should_search
        assert should_search("ok") is False

    def test_empty_prompt_skipped(self):
        from prompt_recall import should_search
        assert should_search("") is False

    def test_long_prompt_skipped(self):
        from prompt_recall import MAX_PROMPT_LENGTH, should_search
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
        from prompt_recall import DEDUP_WINDOW, is_recently_injected, record_injection
        state_file = tmp_path / ".prompt-recall-state-test"
        record_injection("dp-1", state_file, prompt_index=1)
        assert is_recently_injected("dp-1", state_file, current_prompt_index=1 + DEDUP_WINDOW + 1) is False

    def test_state_keeps_last_n(self, tmp_path):
        from prompt_recall import DEDUP_WINDOW, MAX_INJECTIONS, record_injection
        state_file = tmp_path / ".prompt-recall-state-test"
        cap = MAX_INJECTIONS * DEDUP_WINDOW
        for i in range(cap + 5):
            record_injection(f"dp-{i}", state_file, prompt_index=i)
        state = json.loads(state_file.read_text())
        assert len(state["injections"]) == cap

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
        from prompt_recall import MAX_INJECTIONS, format_injection
        memories = [{"content": f"fact {i}", "certainty": 3, "scope": "global"} for i in range(10)]
        output = format_injection(memories)
        assert output.count("- (") == MAX_INJECTIONS


class TestRelevanceFloor:
    """Tests for the minimum relevance score filter."""

    def test_low_score_filtered(self, tmp_path, capsys):
        """Memories below MIN_RELEVANCE_SCORE are filtered out."""
        from prompt_recall import MIN_RELEVANCE_SCORE, main

        mock_dp = MagicMock()
        mock_dp.id = "dp-low-1"
        mock_dp.content = "Some irrelevant memory"
        mock_dp.scope = "global"
        mock_dp.salience = 0.3
        mock_dp.certainty = 2
        mock_scored = MagicMock()
        mock_scored.data_point = mock_dp
        mock_scored.score = MIN_RELEVANCE_SCORE - 0.1

        mock_log = MagicMock()

        with patch("sys.stdin") as mock_stdin, \
             patch("memory_utils.get_memory_dir", return_value=tmp_path), \
             patch("embeddings.search_hybrid", return_value=[mock_scored]), \
             patch("storage.get_db", return_value=MagicMock()), \
             patch("storage.close_db"), \
             patch("memory_utils.sanitize_secrets", side_effect=lambda x: x), \
             patch.dict("sys.modules", {"injection_log": MagicMock(log_prompt_recall=mock_log)}):
            mock_stdin.read.return_value = json.dumps({
                "prompt": "How should I configure Redis caching for this project?",
                "sessionId": "test-relevance",
            })
            main()

        captured = capsys.readouterr()
        assert captured.out == ""
        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args[1]
        assert len(call_kwargs["filtered"]) == 1
        assert call_kwargs["filtered"][0]["reason"] == "low_relevance"

    def test_high_score_injected(self, tmp_path, capsys):
        """Memories above MIN_RELEVANCE_SCORE are injected normally."""
        from prompt_recall import MIN_RELEVANCE_SCORE, main

        mock_dp = MagicMock()
        mock_dp.id = "dp-high-1"
        mock_dp.content = "Redis requires explicit TTL settings"
        mock_dp.scope = "global"
        mock_dp.salience = 0.9
        mock_dp.certainty = 4
        mock_scored = MagicMock()
        mock_scored.data_point = mock_dp
        mock_scored.score = MIN_RELEVANCE_SCORE + 0.1

        with patch("sys.stdin") as mock_stdin, \
             patch("memory_utils.get_memory_dir", return_value=tmp_path), \
             patch("embeddings.search_hybrid", return_value=[mock_scored]), \
             patch("storage.get_db", return_value=MagicMock()), \
             patch("storage.close_db"), \
             patch("memory_utils.sanitize_secrets", side_effect=lambda x: x):
            mock_stdin.read.return_value = json.dumps({
                "prompt": "How should I configure Redis caching for this project?",
                "sessionId": "test-relevance-pass",
            })
            main()

        captured = capsys.readouterr()
        assert "[memory]" in captured.out
        assert "Redis" in captured.out


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


class TestMain:
    """Tests for the main() hook entry point."""

    def test_short_prompt_produces_no_output(self, capsys):
        """Short prompts that fail relevance gate produce no output."""
        from prompt_recall import main
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = json.dumps({"prompt": "ok", "sessionId": "test-1"})
            main()
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_relevant_prompt_searches_and_injects(self, tmp_path, capsys):
        """A relevant prompt triggers search and injects matching memories."""
        from prompt_recall import MIN_RELEVANCE_SCORE, main

        mock_dp = MagicMock()
        mock_dp.id = "dp-test-1"
        mock_dp.content = "Redis requires explicit TTL settings"
        mock_dp.scope = "global"
        mock_dp.salience = 0.8
        mock_dp.certainty = 4
        mock_scored = MagicMock()
        mock_scored.data_point = mock_dp
        mock_scored.score = MIN_RELEVANCE_SCORE + 0.1

        with patch("sys.stdin") as mock_stdin, \
             patch("memory_utils.get_memory_dir", return_value=tmp_path), \
             patch("embeddings.search_hybrid", return_value=[mock_scored]), \
             patch("storage.get_db", return_value=MagicMock()), \
             patch("storage.close_db"), \
             patch("memory_utils.sanitize_secrets", side_effect=lambda x: x):
            mock_stdin.read.return_value = json.dumps({
                "prompt": "How should I configure Redis caching for this project?",
                "sessionId": "test-session",
            })
            main()

        captured = capsys.readouterr()
        assert "[memory]" in captured.out
        assert "Redis" in captured.out

    def test_import_error_produces_no_output(self, capsys):
        """When storage/embeddings are not importable, main() silently returns."""
        from prompt_recall import main
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = json.dumps({
                "prompt": "How should I configure Redis caching for this project?",
                "sessionId": "test-2",
            })
            with patch.dict("sys.modules", {"embeddings": None}):
                main()
        captured = capsys.readouterr()
        assert captured.out == ""


class TestInjectionLogging:
    """Tests for injection logging integration in main()."""

    def _make_mock_dp(self, dp_id="dp-test-1", content="Redis requires explicit TTL settings", scope="global", score=None):
        from prompt_recall import MIN_RELEVANCE_SCORE
        mock_dp = MagicMock()
        mock_dp.id = dp_id
        mock_dp.content = content
        mock_dp.scope = scope
        mock_dp.salience = 0.8
        mock_dp.certainty = 4
        mock_scored = MagicMock()
        mock_scored.data_point = mock_dp
        mock_scored.score = score if score is not None else MIN_RELEVANCE_SCORE + 0.1
        return mock_scored

    def test_main_calls_log_prompt_recall(self, tmp_path, capsys):
        """main() calls log_prompt_recall with correct arguments after search loop."""
        from prompt_recall import main

        mock_scored = self._make_mock_dp()
        mock_log = MagicMock()

        with patch("sys.stdin") as mock_stdin, \
             patch("memory_utils.get_memory_dir", return_value=tmp_path), \
             patch("embeddings.search_hybrid", return_value=[mock_scored]), \
             patch("storage.get_db", return_value=MagicMock()), \
             patch("storage.close_db"), \
             patch("memory_utils.sanitize_secrets", side_effect=lambda x: x), \
             patch.dict("sys.modules", {"injection_log": MagicMock(log_prompt_recall=mock_log)}):
            mock_stdin.read.return_value = json.dumps({
                "prompt": "How should I configure Redis caching for this project?",
                "sessionId": "test-log-session",
            })
            main()

        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args
        assert call_kwargs[1]["session_id"] == "test-log-session"
        assert call_kwargs[1]["prompt_preview"] == "How should I configure Redis caching for this project?"[:80]
        assert call_kwargs[1]["candidates"] == 1
        assert len(call_kwargs[1]["injected"]) == 1
        assert call_kwargs[1]["injected"][0]["id"] == "dp-test-1"
        assert call_kwargs[1]["injected"][0]["scope"] == "global"
        assert isinstance(call_kwargs[1]["filtered"], list)
        assert call_kwargs[1]["latency_ms"] >= 0

    def test_main_tracks_filtered_deduped_candidates(self, tmp_path, capsys):
        """Deduped memories appear in the filtered list with reason='deduped'."""
        from prompt_recall import main, record_injection

        state_file = tmp_path / ".prompt-recall-state-test-dedup"
        record_injection("dp-dedup-1", state_file, prompt_index=0)

        mock_fresh = self._make_mock_dp(dp_id="dp-fresh-1", content="Fresh memory content", scope="project")
        mock_dedup = self._make_mock_dp(dp_id="dp-dedup-1", content="Already injected memory", scope="global")
        mock_log = MagicMock()

        with patch("sys.stdin") as mock_stdin, \
             patch("memory_utils.get_memory_dir", return_value=tmp_path), \
             patch("embeddings.search_hybrid", return_value=[mock_dedup, mock_fresh]), \
             patch("storage.get_db", return_value=MagicMock()), \
             patch("storage.close_db"), \
             patch("memory_utils.sanitize_secrets", side_effect=lambda x: x), \
             patch.dict("sys.modules", {"injection_log": MagicMock(log_prompt_recall=mock_log)}):
            mock_stdin.read.return_value = json.dumps({
                "prompt": "How should I configure Redis caching for this project?",
                "sessionId": "test-dedup",
            })
            main()

        mock_log.assert_called_once()
        call_kwargs = mock_log.call_args[1]
        assert len(call_kwargs["filtered"]) == 1
        assert call_kwargs["filtered"][0]["id"] == "dp-dedup-1"
        assert call_kwargs["filtered"][0]["reason"] == "deduped"
        assert call_kwargs["filtered"][0]["content_preview"] == "Already injected memory"[:80]
        assert len(call_kwargs["injected"]) == 1
        assert call_kwargs["injected"][0]["id"] == "dp-fresh-1"

    def test_main_still_works_when_injection_log_missing(self, tmp_path, capsys):
        """main() still injects memories even when injection_log is not importable."""
        from prompt_recall import main

        mock_scored = self._make_mock_dp()

        with patch("sys.stdin") as mock_stdin, \
             patch("memory_utils.get_memory_dir", return_value=tmp_path), \
             patch("embeddings.search_hybrid", return_value=[mock_scored]), \
             patch("storage.get_db", return_value=MagicMock()), \
             patch("storage.close_db"), \
             patch("memory_utils.sanitize_secrets", side_effect=lambda x: x), \
             patch.dict("sys.modules", {"injection_log": None}):
            mock_stdin.read.return_value = json.dumps({
                "prompt": "How should I configure Redis caching for this project?",
                "sessionId": "test-no-log",
            })
            main()

        captured = capsys.readouterr()
        assert "[memory]" in captured.out
        assert "Redis" in captured.out
