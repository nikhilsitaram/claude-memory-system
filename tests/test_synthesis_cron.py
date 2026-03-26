"""Tests for synthesis_cron.py -- systemd-triggered deferred synthesis."""
import subprocess as real_subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from synthesis_cron import (
    MAX_TOPICS,
    _clear_eager_timestamp,
    _log_error,
    build_claude_command,
    extract_topics,
    retrieve_existing_memories,
    run_synthesis,
    should_run_deferred_synthesis,
)


class TestShouldRunDeferredSynthesis:
    """Tests for the scheduling check."""

    def test_returns_false_when_not_deferred(self):
        """If synthesis.deferred is False, should not run."""
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"deferred": False, "intervalHours": 2}
        }):
            assert should_run_deferred_synthesis() is False

    def test_returns_false_when_recently_synthesized(self, tmp_path):
        """If .last-synthesis is recent, should not run."""
        last_synth = tmp_path / ".last-synthesis"
        last_synth.write_text(datetime.now(timezone.utc).isoformat())
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"deferred": True, "intervalHours": 2}
        }), patch("load_memory.get_last_synthesis_file", return_value=last_synth):
            assert should_run_deferred_synthesis() is False

    def test_returns_true_when_deferred_and_due(self, tmp_path):
        """If deferred=True and enough time passed, should run."""
        last_synth = tmp_path / ".last-synthesis"
        # Don't create it -- never synthesized
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"deferred": True, "intervalHours": 2}
        }), patch("load_memory.get_last_synthesis_file", return_value=last_synth):
            assert should_run_deferred_synthesis() is True

    def test_returns_true_when_deferred_and_old_timestamp(self, tmp_path):
        """If deferred=True and timestamp is old enough, should run."""
        last_synth = tmp_path / ".last-synthesis"
        old_time = datetime.now(timezone.utc) - timedelta(hours=3)
        last_synth.write_text(old_time.isoformat())
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"deferred": True, "intervalHours": 2}
        }), patch("load_memory.get_last_synthesis_file", return_value=last_synth):
            assert should_run_deferred_synthesis() is True

    def test_returns_true_when_deferred_missing_defaults_to_setting(self):
        """If synthesis.deferred key is missing, defaults to DEFAULT_SETTINGS value."""
        from memory_utils import DEFAULT_SETTINGS

        expected = DEFAULT_SETTINGS["synthesis"]["deferred"]
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"intervalHours": 2}
        }), patch("synthesis_cron.should_synthesize", return_value=True):
            assert should_run_deferred_synthesis() is expected

    def test_returns_true_when_synthesis_section_missing(self):
        """If synthesis section is missing entirely, defaults to DEFAULT_SETTINGS deferred."""
        from memory_utils import DEFAULT_SETTINGS

        expected = DEFAULT_SETTINGS["synthesis"]["deferred"]
        with patch("synthesis_cron.load_settings", return_value={}), \
             patch("synthesis_cron.should_synthesize", return_value=True):
            assert should_run_deferred_synthesis() is expected


class TestBuildClaudeCommand:
    """Tests for building the claude -p command."""

    def test_includes_allowed_tools(self):
        cmd = build_claude_command(model="sonnet")
        assert "--allowedTools" in cmd
        tools_idx = cmd.index("--allowedTools") + 1
        tools_str = cmd[tools_idx]
        assert "Write" in tools_str
        assert "Bash" in tools_str
        assert "Read" in tools_str

    def test_includes_model(self):
        cmd = build_claude_command(model="haiku")
        assert "--model" in cmd
        model_idx = cmd.index("--model") + 1
        assert cmd[model_idx] == "haiku"

    def test_includes_print_flag(self):
        """Should use -p for non-interactive (print) mode."""
        cmd = build_claude_command(model="sonnet")
        assert "-p" in cmd

    def test_includes_no_session_persistence(self):
        cmd = build_claude_command(model="sonnet")
        assert "--no-session-persistence" in cmd

    def test_uses_bypass_permissions_mode(self):
        """Headless synthesis needs permission bypass to avoid interactive prompts."""
        cmd = build_claude_command(model="sonnet")
        assert "--permission-mode" in cmd
        assert "bypassPermissions" in cmd
        assert "--dangerously-skip-permissions" not in cmd

    def test_no_positional_prompt(self):
        """Prompt is piped via stdin, not as a positional arg."""
        cmd = build_claude_command(model="sonnet")
        # Every element should be a known flag or flag value, not a bare prompt
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert all(
            el.startswith("-") or el in ("claude", "sonnet", "bypassPermissions", "Write,Bash,Read")
            or el.startswith("{")  # --settings JSON
            for el in cmd
        )

    def test_disables_hooks_and_mcp(self):
        """Headless synthesis disables hooks, MCP, and skills to minimize overhead."""
        cmd = build_claude_command(model="sonnet")
        assert "--disable-slash-commands" in cmd
        assert "--settings" in cmd
        settings_idx = cmd.index("--settings") + 1
        import json
        settings = json.loads(cmd[settings_idx])
        assert settings["disableAllHooks"] is True
        assert settings["mcpServers"] == {}

    def test_returns_list(self):
        cmd = build_claude_command(model="sonnet")
        assert isinstance(cmd, list)
        assert cmd[0] == "claude"


class TestRunSynthesis:
    """Tests for the full synthesis pipeline."""

    def test_skips_when_not_due_and_not_forced(self, capsys):
        """When should_run_deferred_synthesis returns False, exit cleanly."""
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=False):
            result = run_synthesis(force=False)
        assert result == 0
        output = capsys.readouterr().out
        assert "not due" in output.lower() or "deferred" in output.lower()

    def test_force_bypasses_schedule_check(self):
        """When force=True, should skip schedule check and proceed."""
        with patch("synthesis_cron.should_run_deferred_synthesis") as mock_check, \
             patch("synthesis_cron.write_synthesis_prompt") as mock_wsp:
            # write_synthesis_prompt prints "No pending" -- simulate via side_effect
            mock_wsp.side_effect = lambda **kw: print("No pending transcripts.")
            result = run_synthesis(force=True)
        # should_run_deferred_synthesis should NOT be called when force=True
        mock_check.assert_not_called()
        assert result == 0

    def test_skips_when_no_pending(self, capsys):
        """When write_synthesis_prompt prints 'No pending', should exit cleanly."""
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt") as mock_wsp:
            mock_wsp.side_effect = lambda **kw: print("No pending transcripts.")
            result = run_synthesis()
        assert result == 0
        output = capsys.readouterr().out
        assert "No pending" in output

    def test_calls_v3_synthesis(self, tmp_path):
        """When prompt is generated, should invoke _run_synthesis_v3."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        mock_conn = MagicMock()
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron._run_synthesis_v3", return_value=True) as mock_v3, \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron._run_consolidation_post_step"), \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("storage.get_db", return_value=mock_conn), \
             patch("storage.close_db"):
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            result = run_synthesis()

        assert result == 0
        mock_v3.assert_called_once()
        args = mock_v3.call_args[0]
        assert args[0] is mock_conn
        assert args[1] == "sonnet"

    def test_returns_1_on_claude_failure(self, tmp_path):
        """When _run_synthesis_v3 returns False, should return 1."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        mock_conn = MagicMock()
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron._run_synthesis_v3", return_value=False), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("storage.get_db", return_value=mock_conn), \
             patch("storage.close_db"):
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            result = run_synthesis()

        assert result == 1

    def test_returns_1_on_timeout(self, tmp_path):
        """When _run_synthesis_v3 raises, should return failure."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        mock_conn = MagicMock()
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron._run_synthesis_v3", return_value=False), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("storage.get_db", return_value=mock_conn), \
             patch("storage.close_db"):
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            result = run_synthesis()

        assert result == 1

    def test_writes_timestamp_before_running(self, tmp_path):
        """Should write .last-synthesis timestamp before calling v3 synthesis."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")
        last_synth = tmp_path / ".last-synthesis"

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        call_order = []

        def track_v3(*args, **kwargs):
            call_order.append(("v3", last_synth.exists()))
            return True

        mock_conn = MagicMock()
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron._run_synthesis_v3", side_effect=track_v3), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron._run_consolidation_post_step"), \
             patch("synthesis_cron.get_last_synthesis_file", return_value=last_synth), \
             patch("storage.get_db", return_value=mock_conn), \
             patch("storage.close_db"):
            result = run_synthesis()

        assert result == 0
        # Timestamp should have been written BEFORE _run_synthesis_v3 was called
        assert call_order == [("v3", True)]

    def test_returns_1_when_no_prompt_file_generated(self, capsys):
        """When write_synthesis_prompt outputs unexpected content, should return 1."""
        def fake_write_prompt(**kwargs):
            # Prints something but no model= or prompt_file= lines
            print("Something unexpected happened")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt):
            result = run_synthesis()

        assert result == 1

    def test_uses_model_from_prompt_output(self, tmp_path):
        """Should parse model from write_synthesis_prompt output and pass to v3."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=haiku")
            print(f"prompt_file={prompt_file}")

        mock_conn = MagicMock()
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron._run_synthesis_v3", return_value=True) as mock_v3, \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron._run_consolidation_post_step"), \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("storage.get_db", return_value=mock_conn), \
             patch("storage.close_db"):
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            result = run_synthesis()

        assert result == 0
        args = mock_v3.call_args[0]
        assert args[1] == "haiku"

    def test_clears_timestamp_on_failure(self, tmp_path):
        """When _run_synthesis_v3 returns False, eager timestamp cleared to allow retry."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")
        last_synth = tmp_path / ".last-synthesis"

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        mock_conn = MagicMock()
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron._run_synthesis_v3", return_value=False), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron.get_last_synthesis_file", return_value=last_synth), \
             patch("storage.get_db", return_value=mock_conn), \
             patch("storage.close_db"):
            result = run_synthesis()

        assert result == 1
        assert not last_synth.exists()

    def test_runs_decay_after_synthesis(self, tmp_path):
        """Should run decay after synthesis completes."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        mock_conn = MagicMock()
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron._run_synthesis_v3", return_value=True), \
             patch("synthesis_cron._run_decay_v3") as mock_decay, \
             patch("synthesis_cron._run_consolidation_post_step"), \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("storage.get_db", return_value=mock_conn), \
             patch("storage.close_db"):
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            run_synthesis()

        mock_decay.assert_called_once_with(mock_conn)

    def test_runs_consolidation_on_success(self, tmp_path):
        """Should run consolidation post-step after successful synthesis."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        mock_conn = MagicMock()
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron._run_synthesis_v3", return_value=True), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron._run_consolidation_post_step") as mock_consol, \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("storage.get_db", return_value=mock_conn), \
             patch("storage.close_db"):
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            run_synthesis()

        mock_consol.assert_called_once()

    def test_skips_consolidation_on_failure(self, tmp_path):
        """Should skip consolidation post-step when synthesis fails."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        mock_conn = MagicMock()
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron._run_synthesis_v3", return_value=False), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron._run_consolidation_post_step") as mock_consol, \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("storage.get_db", return_value=mock_conn), \
             patch("storage.close_db"):
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            run_synthesis()

        mock_consol.assert_not_called()

    def test_closes_db_on_success(self, tmp_path):
        """Should close DB connection after successful synthesis."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        mock_conn = MagicMock()
        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron._run_synthesis_v3", return_value=True), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron._run_consolidation_post_step"), \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("storage.get_db", return_value=mock_conn), \
             patch("storage.close_db") as mock_close:
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            run_synthesis()

        mock_close.assert_called_once_with(mock_conn)


class TestClearEagerTimestamp:
    """Tests for _clear_eager_timestamp."""

    def test_removes_existing_file(self, tmp_path):
        ts_file = tmp_path / ".last-synthesis"
        ts_file.write_text("2026-01-01T00:00:00Z")
        with patch("synthesis_cron.get_last_synthesis_file", return_value=ts_file):
            _clear_eager_timestamp()
        assert not ts_file.exists()

    def test_noop_when_file_missing(self, tmp_path):
        ts_file = tmp_path / ".last-synthesis"
        with patch("synthesis_cron.get_last_synthesis_file", return_value=ts_file):
            _clear_eager_timestamp()  # Should not raise


class TestLogError:
    """Tests for _log_error."""

    def test_creates_log_file(self, tmp_path):
        error_log = tmp_path / ".synthesis-errors.log"
        with patch("synthesis_cron.SYNTHESIS_ERROR_LOG", error_log):
            _log_error("test error message")
        assert error_log.exists()
        content = error_log.read_text()
        assert "test error message" in content

    def test_appends_to_existing_log(self, tmp_path):
        error_log = tmp_path / ".synthesis-errors.log"
        error_log.write_text("[2026-01-01T00:00:00Z] old error\n")
        with patch("synthesis_cron.SYNTHESIS_ERROR_LOG", error_log):
            _log_error("new error")
        lines = error_log.read_text().strip().splitlines()
        assert len(lines) == 2
        assert "old error" in lines[0]
        assert "new error" in lines[1]

    def test_includes_timestamp(self, tmp_path):
        error_log = tmp_path / ".synthesis-errors.log"
        with patch("synthesis_cron.SYNTHESIS_ERROR_LOG", error_log):
            _log_error("test")
        content = error_log.read_text()
        assert content.startswith("[2026-")


# ============================================================================
# C2: Topic extraction and pre-retrieval
# ============================================================================


class TestTopicExtraction:
    """Tests for algorithmic topic extraction from transcripts."""

    def test_extracts_key_terms_from_transcript(self):
        """Extracts meaningful keywords/phrases from transcript text."""
        transcript = "We migrated the API from REST to gRPC for performance and latency"
        topics = extract_topics(transcript)
        lower_topics = [t.lower() for t in topics]
        assert "grpc" in lower_topics or "gRPC" in topics
        assert "rest" in lower_topics or "REST" in topics

    def test_filters_stopwords(self):
        """Common words (the, is, a, we, etc.) are excluded."""
        topics = extract_topics("The user is a developer who writes code")
        lower_topics = [t.lower() for t in topics]
        assert "the" not in lower_topics
        assert "is" not in lower_topics

    def test_empty_transcript_returns_empty(self):
        """Empty or whitespace input returns empty list."""
        assert extract_topics("") == []
        assert extract_topics("   ") == []

    def test_limits_topic_count(self):
        """Returns at most MAX_TOPICS topics."""
        long_text = " ".join(f"unique_word_{i}" for i in range(100))
        topics = extract_topics(long_text)
        assert len(topics) <= MAX_TOPICS

    def test_custom_max_topics(self):
        """Respects custom max_topics parameter."""
        long_text = " ".join(f"word_{i}" for i in range(50))
        topics = extract_topics(long_text, max_topics=5)
        assert len(topics) <= 5

    def test_short_words_filtered(self):
        """Words of length <= 2 are excluded."""
        topics = extract_topics("go to it in my at")
        assert all(len(t) > 2 for t in topics)


class TestPreRetrievalContext:
    """Tests for vector-retrieved memory context in synthesis prompts."""

    def test_retrieve_existing_memories_fallback_when_import_error(self):
        """When embeddings module unavailable, returns empty list."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "embeddings":
                raise ImportError("no module")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = retrieve_existing_memories("some transcript text")
        assert result == []

    def test_retrieve_existing_memories_empty_transcript(self):
        """Empty transcript returns empty list without calling vector search."""
        result = retrieve_existing_memories("")
        assert result == []

    def test_retrieve_existing_memories_whitespace_transcript(self):
        """Whitespace-only transcript returns empty list."""
        result = retrieve_existing_memories("   ")
        assert result == []


# =============================================================================
# TestSynthesisCronV3 — C5: schema version detection + v3 path dispatch
# =============================================================================


def _make_v3_db_for_cron(tmp_path):
    """Create a minimal v3 schema DB for cron tests."""
    import sqlite3
    conn = sqlite3.connect(str(tmp_path / "test.db"))
    conn.execute('PRAGMA foreign_keys=ON')
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS data_points (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT,
            content TEXT,
            scope TEXT,
            entry_type TEXT,
            source_type TEXT,
            source_sessions TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            salience REAL DEFAULT 1.0,
            access_count INTEGER DEFAULT 0,
            last_accessed TEXT,
            evidence_count INTEGER DEFAULT 1,
            consolidated INTEGER DEFAULT 0,
            content_hash TEXT,
            simhash INTEGER,
            entities TEXT,
            properties TEXT,
            certainty INTEGER DEFAULT NULL,
            validity_context TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS edges (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL REFERENCES data_points(id),
            target TEXT NOT NULL REFERENCES data_points(id),
            type TEXT NOT NULL,
            reason TEXT,
            fact TEXT,
            properties TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            valid_from TEXT,
            valid_to TEXT,
            expired_at TEXT,
            weight REAL DEFAULT 1.0,
            source_sessions TEXT
        );
        PRAGMA user_version = 3;
    """)
    conn.commit()
    return conn


class TestSynthesisCronV3:
    def test_pre_retrieval_queries_data_points(self, tmp_path):
        """retrieve_existing_memories falls back gracefully when DB has no data."""
        from synthesis_cron import retrieve_existing_memories
        result = retrieve_existing_memories("pytest sqlite storage test")
        assert isinstance(result, list)


# =============================================================================
# TestSessionContext — C6: session_context data_points in synthesis
# =============================================================================


class TestSessionContext:
    def test_session_context_created(self, tmp_path):
        """_write_session_context creates a data_point with type='session_context'."""
        from synthesis_cron import _write_session_context
        conn = _make_v3_db_for_cron(tmp_path)
        dp_id = _write_session_context(conn, "myproject", ["sqlite", "test"], "session-001")
        row = conn.execute(
            "SELECT type, scope, content FROM data_points WHERE id=?", (dp_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "session_context"
        assert row[1] == "myproject"
        assert "myproject" in row[2]

    def test_context_content_summarizes_work(self, tmp_path):
        """session_context content includes topics."""
        from synthesis_cron import _write_session_context
        conn = _make_v3_db_for_cron(tmp_path)
        dp_id = _write_session_context(conn, "proj", ["grpc", "auth", "jwt"], "sess-002")
        row = conn.execute("SELECT content FROM data_points WHERE id=?", (dp_id,)).fetchone()
        assert row is not None
        assert len(row[0]) > 0

    def test_continues_edge_to_prior_context(self, tmp_path):
        """If prior session_context exists for same project, continues edge created."""
        from synthesis_cron import _write_session_context
        conn = _make_v3_db_for_cron(tmp_path)
        first_id = _write_session_context(conn, "proj", ["topic1"], "sess-001")
        second_id = _write_session_context(conn, "proj", ["topic2"], "sess-002")
        edges = conn.execute(
            "SELECT type, target FROM edges WHERE source=?", (second_id,)
        ).fetchall()
        continues_edges = [e for e in edges if e[0] == "continues"]
        assert len(continues_edges) == 1
        assert continues_edges[0][1] == first_id

    def test_properties_include_session_metadata(self, tmp_path):
        """session_context properties JSON has session_id."""
        import json

        from synthesis_cron import _write_session_context
        conn = _make_v3_db_for_cron(tmp_path)
        dp_id = _write_session_context(conn, "proj", ["topic"], "sess-unique-123")
        row = conn.execute("SELECT properties FROM data_points WHERE id=?", (dp_id,)).fetchone()
        assert row is not None
        props = json.loads(row[0])
        assert props.get("session_id") == "sess-unique-123"

    def test_idempotent_no_duplicate_context(self, tmp_path):
        """Running synthesis twice for same session_id doesn't create duplicate contexts."""
        from synthesis_cron import _write_session_context
        conn = _make_v3_db_for_cron(tmp_path)
        id1 = _write_session_context(conn, "proj", ["topic"], "sess-idem")
        id2 = _write_session_context(conn, "proj", ["topic"], "sess-idem")
        assert id1 == id2
        count = conn.execute(
            "SELECT COUNT(*) FROM data_points WHERE type='session_context'"
        ).fetchone()[0]
        assert count == 1

    def test_context_for_edges_to_entities(self, tmp_path):
        """context_for edges connect session_context to entity data_points."""
        from synthesis_cron import _write_session_context
        conn = _make_v3_db_for_cron(tmp_path)
        dp_id = _write_session_context(conn, "proj", ["topic"], "sess-ent", entities=["gRPC", "pytest"])
        edges = conn.execute(
            "SELECT type FROM edges WHERE source=?", (dp_id,)
        ).fetchall()
        edge_types = {e[0] for e in edges}
        assert "context_for" in edge_types


# =============================================================================
# TestSessionContextIdempotencyWildcard — Issue 7: LIKE wildcard injection
# =============================================================================


class TestSessionContextIdempotencyWildcard:
    """Tests that session_id wildcards are escaped in the idempotency LIKE query."""

    def test_session_id_with_percent_is_idempotent(self, tmp_path):
        """A session_id containing '%' still produces only one context row."""
        from synthesis_cron import _write_session_context
        conn = _make_v3_db_for_cron(tmp_path)

        sid = "session%with%percent"
        id1 = _write_session_context(conn, "proj", ["topic"], sid)
        id2 = _write_session_context(conn, "proj", ["topic"], sid)
        assert id1 == id2
        count = conn.execute(
            "SELECT COUNT(*) FROM data_points WHERE type='session_context'"
        ).fetchone()[0]
        assert count == 1

    def test_session_id_with_underscore_is_idempotent(self, tmp_path):
        """A session_id containing '_' still produces only one context row."""
        from synthesis_cron import _write_session_context
        conn = _make_v3_db_for_cron(tmp_path)

        sid = "session_with_underscores"
        id1 = _write_session_context(conn, "proj", ["topic"], sid)
        id2 = _write_session_context(conn, "proj", ["topic"], sid)
        assert id1 == id2

    def test_wildcard_session_id_does_not_match_other_sessions(self, tmp_path):
        """A session_id with '%' does not match unrelated session contexts."""
        from synthesis_cron import _write_session_context
        conn = _make_v3_db_for_cron(tmp_path)

        id_real = _write_session_context(conn, "proj", ["a"], "session-2026-01-01")
        id_wild = _write_session_context(conn, "proj", ["b"], "session%")

        assert id_real != id_wild
        count = conn.execute(
            "SELECT COUNT(*) FROM data_points WHERE type='session_context'"
        ).fetchone()[0]
        assert count == 2


# =============================================================================
# TestV3PromptCleanup — HIGH-1: v3 prompt strips v2-only sections
# =============================================================================


class TestV3PromptCleanup:
    """Tests that _run_synthesis_v3 removes v2-only sections before calling LLM."""

    def _make_v2_prompt(self, tmp_path, date_label="synthesis-prompt-2026-03-22-12345"):
        """Create a realistic v2-style prompt file with all three v2 sections."""
        content = (
            "You are a structured data extractor.\n\n"
            "## Output Format\n\n"
            "===PROJECT:myproject===\n"
            "- [implement] Built something\n"
            "===END===\n\n"
            "## Delivery\n\n"
            "Only use the Write and Bash tools — no other tools.\n\n"
            "1. Write(`/tmp/synthesis-output-1234.txt`, <your structured output>)\n"
            "2. Bash: `python3 ~/.claude/scripts/synthesis.py apply ...`\n\n"
            "## Synthesis Instructions\n\n"
            "Old v2 instructions here.\n\n"
            "## Existing Long-Term Memory (for dedup)\n\n"
            "(no existing LTM content)\n\n"
            "## Session Transcripts\n\n"
            "**Pending dates:** 2026-03-22\n\n"
            "### Transcript: 2026-03-22\nSome transcript content.\n\n"
            "## Reminder\n\n"
            "Output only the structured format shown above. Start with ===PROJECT:...===.\n"
        )
        prompt_file = tmp_path / f"{date_label}.txt"
        prompt_file.write_text(content)
        return prompt_file

    def test_output_format_section_removed(self, tmp_path):
        """v3 prompt must not contain ## Output Format (v2 PROJECT block instructions)."""
        from synthesis_cron import _run_synthesis_v3
        prompt_file = self._make_v2_prompt(tmp_path)
        captured_prompts = []

        def fake_run(cmd, stdin, **kwargs):
            captured_prompts.append(stdin.read())
            return MagicMock(returncode=0, stdout="", stderr="")

        conn = _make_v3_db_for_cron(tmp_path)
        with patch("synthesis_cron.subprocess.run", side_effect=fake_run), \
             patch("synthesis_cron._log_error"):
            _run_synthesis_v3(conn, "sonnet", [str(prompt_file)])

        assert len(captured_prompts) == 1
        assert "## Output Format" not in captured_prompts[0]

    def test_delivery_section_removed(self, tmp_path):
        """v3 prompt must not contain ## Delivery (Write/Bash tool instructions)."""
        from synthesis_cron import _run_synthesis_v3
        prompt_file = self._make_v2_prompt(tmp_path)
        captured_prompts = []

        def fake_run(cmd, stdin, **kwargs):
            captured_prompts.append(stdin.read())
            return MagicMock(returncode=0, stdout="", stderr="")

        conn = _make_v3_db_for_cron(tmp_path)
        with patch("synthesis_cron.subprocess.run", side_effect=fake_run), \
             patch("synthesis_cron._log_error"):
            _run_synthesis_v3(conn, "sonnet", [str(prompt_file)])

        assert len(captured_prompts) == 1
        assert "## Delivery" not in captured_prompts[0]

    def test_reminder_section_removed(self, tmp_path):
        """v3 prompt must not contain ## Reminder (tells LLM to use PROJECT blocks)."""
        from synthesis_cron import _run_synthesis_v3
        prompt_file = self._make_v2_prompt(tmp_path)
        captured_prompts = []

        def fake_run(cmd, stdin, **kwargs):
            captured_prompts.append(stdin.read())
            return MagicMock(returncode=0, stdout="", stderr="")

        conn = _make_v3_db_for_cron(tmp_path)
        with patch("synthesis_cron.subprocess.run", side_effect=fake_run), \
             patch("synthesis_cron._log_error"):
            _run_synthesis_v3(conn, "sonnet", [str(prompt_file)])

        assert len(captured_prompts) == 1
        assert "## Reminder" not in captured_prompts[0]

    def test_synthesis_instructions_and_transcripts_retained(self, tmp_path):
        """After stripping v2 sections, Synthesis Instructions and Transcripts must remain."""
        from synthesis_cron import _run_synthesis_v3
        prompt_file = self._make_v2_prompt(tmp_path)
        captured_prompts = []

        def fake_run(cmd, stdin, **kwargs):
            captured_prompts.append(stdin.read())
            return MagicMock(returncode=0, stdout="", stderr="")

        conn = _make_v3_db_for_cron(tmp_path)
        with patch("synthesis_cron.subprocess.run", side_effect=fake_run), \
             patch("synthesis_cron._log_error"):
            _run_synthesis_v3(conn, "sonnet", [str(prompt_file)])

        assert len(captured_prompts) == 1
        prompt = captured_prompts[0]
        assert "## Synthesis Instructions" in prompt
        assert "## Session Transcripts" in prompt
        assert "MEMORY_OPS" in prompt

    def test_no_project_block_example_in_v3_prompt(self, tmp_path):
        """v3 prompt must not instruct LLM to produce ===PROJECT:name=== blocks."""
        from synthesis_cron import _run_synthesis_v3
        prompt_file = self._make_v2_prompt(tmp_path)
        captured_prompts = []

        def fake_run(cmd, stdin, **kwargs):
            captured_prompts.append(stdin.read())
            return MagicMock(returncode=0, stdout="", stderr="")

        conn = _make_v3_db_for_cron(tmp_path)
        with patch("synthesis_cron.subprocess.run", side_effect=fake_run), \
             patch("synthesis_cron._log_error"):
            _run_synthesis_v3(conn, "sonnet", [str(prompt_file)])

        assert len(captured_prompts) == 1
        assert "===PROJECT:" not in captured_prompts[0]


# =============================================================================
# TestV3SessionContextScope — HIGH-2: session_context scope from MEMORY_OPS
# =============================================================================


class TestV3SessionContextScope:
    """Tests that _run_synthesis_v3 uses the actual project scope, not date_label."""

    def _make_prompt_file(self, tmp_path, date_label="synthesis-prompt-2026-03-22-99999"):
        content = (
            "## Synthesis Instructions\n\nv3 instructions\n\n"
            "## Session Transcripts\n\n**Pending dates:** 2026-03-22\n\n"
            "### Transcript: 2026-03-22\nSome work on myproject.\n"
        )
        prompt_file = tmp_path / f"{date_label}.txt"
        prompt_file.write_text(content)
        return prompt_file

    def _make_memory_op(self, scope, action="ADD"):
        from synthesis import MemoryOp
        return MemoryOp(action=action, fact="some fact", scope=scope, entities=[])

    def test_scope_taken_from_most_common_op_scope(self, tmp_path):
        """session_context.scope should be the most frequent non-global op scope."""
        from synthesis import SynthesisResult
        from synthesis_cron import _run_synthesis_v3

        prompt_file = self._make_prompt_file(tmp_path)
        ops = [
            self._make_memory_op("myproject"),
            self._make_memory_op("myproject"),
            self._make_memory_op("otherproject"),
        ]
        synth_result = SynthesisResult(memory_ops=ops)

        written_scopes = []

        def fake_write_session_context(conn, project_name, topics, session_id, entities=None):
            written_scopes.append(project_name)
            return "fake-dp-id"

        conn = _make_v3_db_for_cron(tmp_path)
        with patch("synthesis_cron.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("synthesis.parse_synthesis_output", return_value=synth_result), \
             patch("synthesis.apply_memory_ops_v3", return_value=[]), \
             patch("synthesis_cron._write_session_context", side_effect=fake_write_session_context):
            _run_synthesis_v3(conn, "sonnet", [str(prompt_file)])

        assert written_scopes == ["myproject"]

    def test_scope_falls_back_to_global_when_no_project_ops(self, tmp_path):
        """When all ops have scope='global' or None, session_context uses 'global'."""
        from synthesis import SynthesisResult
        from synthesis_cron import _run_synthesis_v3

        prompt_file = self._make_prompt_file(tmp_path)
        ops = [
            self._make_memory_op("global"),
            self._make_memory_op(None),
        ]
        synth_result = SynthesisResult(memory_ops=ops)

        written_scopes = []

        def fake_write_session_context(conn, project_name, topics, session_id, entities=None):
            written_scopes.append(project_name)
            return "fake-dp-id"

        conn = _make_v3_db_for_cron(tmp_path)
        with patch("synthesis_cron.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("synthesis.parse_synthesis_output", return_value=synth_result), \
             patch("synthesis.apply_memory_ops_v3", return_value=[]), \
             patch("synthesis_cron._write_session_context", side_effect=fake_write_session_context):
            _run_synthesis_v3(conn, "sonnet", [str(prompt_file)])

        assert written_scopes == ["global"]

    def test_scope_not_prompt_filename_stem(self, tmp_path):
        """session_context scope must never be the prompt file stem (date_label)."""
        from synthesis import SynthesisResult
        from synthesis_cron import _run_synthesis_v3

        date_label = "synthesis-prompt-2026-03-22-99999"
        prompt_file = self._make_prompt_file(tmp_path, date_label)
        ops = [self._make_memory_op("myproject")]
        synth_result = SynthesisResult(memory_ops=ops)

        written_scopes = []

        def fake_write_session_context(conn, project_name, topics, session_id, entities=None):
            written_scopes.append(project_name)
            return "fake-dp-id"

        conn = _make_v3_db_for_cron(tmp_path)
        with patch("synthesis_cron.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), \
             patch("synthesis.parse_synthesis_output", return_value=synth_result), \
             patch("synthesis.apply_memory_ops_v3", return_value=[]), \
             patch("synthesis_cron._write_session_context", side_effect=fake_write_session_context):
            _run_synthesis_v3(conn, "sonnet", [str(prompt_file)])

        assert written_scopes[0] != date_label
        assert written_scopes[0] == "myproject"


# ---------------------------------------------------------------------------
# Consolidation gate tests
# ---------------------------------------------------------------------------


def _make_db_with_metadata(tmp_path):
    """Create a test DB with metadata table for consolidation tests."""
    from unittest.mock import patch as p

    from storage import ensure_db
    db_path = tmp_path / "memory.db"
    with p("storage.get_db_path", return_value=db_path), \
         p("storage.get_memory_dir", return_value=tmp_path):
        conn = ensure_db()
    return conn


class TestConsolidationGate:
    """Tests for consolidation scheduling in synthesis_cron."""

    def test_skips_when_interval_not_met(self, tmp_path):
        """Consolidation skips if less than intervalHours since last run."""
        from memory_utils import DEFAULT_SETTINGS
        from synthesis_cron import _should_consolidate

        conn = _make_db_with_metadata(tmp_path)
        recent = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
        conn.execute("INSERT INTO metadata (key, value) VALUES ('last_consolidation', ?)", (recent,))
        conn.commit()

        settings = dict(DEFAULT_SETTINGS)
        settings["consolidation"] = dict(DEFAULT_SETTINGS.get("consolidation", {}))
        settings["consolidation"]["intervalHours"] = DEFAULT_SETTINGS["consolidation"]["intervalHours"]

        assert _should_consolidate(conn, settings) is False
        conn.close()

    def test_runs_when_interval_met(self, tmp_path):
        """Consolidation runs when interval has elapsed and enough memories exist."""
        from memory_utils import DEFAULT_SETTINGS
        from storage import DataPointRow, insert_data_point
        from synthesis_cron import _should_consolidate

        conn = _make_db_with_metadata(tmp_path)
        interval = DEFAULT_SETTINGS["consolidation"]["intervalHours"]
        old = (datetime.now(timezone.utc) - timedelta(hours=interval + 1)).isoformat().replace("+00:00", "Z")
        conn.execute("INSERT INTO metadata (key, value) VALUES ('last_consolidation', ?)", (old,))

        min_mem = DEFAULT_SETTINGS["consolidation"]["minMemories"]
        for i in range(min_mem + 5):
            insert_data_point(conn, DataPointRow(type="memory", content=f"fact {i}", scope="global", salience=0.5))
        conn.commit()

        assert _should_consolidate(conn, DEFAULT_SETTINGS) is True
        conn.close()

    def test_skips_when_too_few_memories(self, tmp_path):
        """Consolidation skips if fewer than minMemories active memories."""
        from memory_utils import DEFAULT_SETTINGS
        from storage import DataPointRow, insert_data_point
        from synthesis_cron import _should_consolidate

        conn = _make_db_with_metadata(tmp_path)
        interval = DEFAULT_SETTINGS["consolidation"]["intervalHours"]
        old = (datetime.now(timezone.utc) - timedelta(hours=interval + 1)).isoformat().replace("+00:00", "Z")
        conn.execute("INSERT INTO metadata (key, value) VALUES ('last_consolidation', ?)", (old,))

        min_mem = DEFAULT_SETTINGS["consolidation"]["minMemories"]
        for i in range(min_mem - 2):
            insert_data_point(conn, DataPointRow(type="memory", content=f"fact {i}", scope="global", salience=0.5))
        conn.commit()

        assert _should_consolidate(conn, DEFAULT_SETTINGS) is False
        conn.close()

    def test_first_run_detected_as_backfill(self, tmp_path):
        """Missing last_consolidation metadata triggers backfill mode."""
        from memory_utils import DEFAULT_SETTINGS
        from storage import DataPointRow, insert_data_point
        from synthesis_cron import _is_backfill, _should_consolidate

        conn = _make_db_with_metadata(tmp_path)
        min_mem = DEFAULT_SETTINGS["consolidation"]["minMemories"]
        for i in range(min_mem + 5):
            insert_data_point(conn, DataPointRow(type="memory", content=f"fact {i}", scope="global", salience=0.5))
        conn.commit()

        assert _should_consolidate(conn, DEFAULT_SETTINGS) is True
        assert _is_backfill(conn) is True
        conn.close()

    def test_updates_timestamp_after_run(self, tmp_path):
        """last_consolidation metadata is updated after successful run."""
        from synthesis_cron import _update_consolidation_timestamp

        conn = _make_db_with_metadata(tmp_path)
        _update_consolidation_timestamp(conn)

        row = conn.execute("SELECT value FROM metadata WHERE key = 'last_consolidation'").fetchone()
        assert row is not None
        assert row[0] is not None
        ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
        assert (datetime.now(timezone.utc) - ts).total_seconds() < 5
        conn.close()

    def test_not_backfill_after_timestamp_set(self, tmp_path):
        """After timestamp is set, _is_backfill returns False."""
        from synthesis_cron import _is_backfill, _update_consolidation_timestamp

        conn = _make_db_with_metadata(tmp_path)
        _update_consolidation_timestamp(conn)
        assert _is_backfill(conn) is False
        conn.close()


# =============================================================================
# Backfill Tests (C1)
# =============================================================================


class TestRunBackfill:
    """Tests for the backfill command."""

    def test_groups_sessions_by_project(self):
        """Sessions are grouped by resolved project name."""
        from helpers import make_session_info
        from synthesis_cron import _group_sessions_by_project

        sessions = [
            make_session_info("s1", project_path="/home/user/projectA"),
            make_session_info("s2", project_path="/home/user/projectA"),
            make_session_info("s3", project_path="/home/user/projectB"),
        ]
        with patch("memory_utils.resolve_project_path_to_name",
                    side_effect=["projA", "projA", "projB"]):
            groups = _group_sessions_by_project(sessions)

        assert "projA" in groups
        assert len(groups["projA"]) == 2
        assert "projB" in groups
        assert len(groups["projB"]) == 1

    def test_model_selection_per_project(self):
        """Sessions within project working days get sonnet, older get haiku."""
        from helpers import make_session_info
        from memory_utils import DEFAULT_SETTINGS
        from synthesis_cron import run_backfill

        sessions = [make_session_info(f"s{i}") for i in range(3)]
        backfill_wd = DEFAULT_SETTINGS["synthesis"]["backfill"]["recentWorkingDays"]

        with patch("indexing.list_recent_sessions", return_value=sessions), \
             patch("synthesis_cron._group_sessions_by_project",
                    return_value={"proj": sessions}), \
             patch("memory_utils.get_project_working_days",
                    return_value=["2026-03-25"]) as mock_pwd, \
             patch("indexing.get_session_date",
                    side_effect=["2026-03-25", "2026-03-20", "2026-03-10"]), \
             patch("memory_utils.load_settings", return_value=DEFAULT_SETTINGS), \
             patch("builtins.input", return_value="y"), \
             patch("storage.ensure_db") as mock_db, \
             patch("memory_utils.load_synthesis_state", return_value={"sessions": {}}), \
             patch("transcript_ops.parse_jsonl_file_from_line", return_value=([], 0)), \
             patch("memory_utils.update_synthesis_state"), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("load_memory._build_synthesis_instructions_v3", return_value=""):
            mock_db.return_value = MagicMock()
            run_backfill()

        mock_pwd.assert_called_once_with("proj", backfill_wd)

    def test_days_filter_passed_to_list_recent(self):
        """--days N passes max_age_days=N to list_recent_sessions."""
        from synthesis_cron import run_backfill

        with patch("indexing.list_recent_sessions", return_value=[]) as mock_lrs, \
             patch("memory_utils.load_settings"):
            run_backfill(days=10)

        mock_lrs.assert_called_once_with(max_age_days=10)

    def test_import_from_calls_import_sessions(self):
        """--import-from calls import_sessions before discovery."""
        from session_import import ImportResult
        from synthesis_cron import run_backfill

        mock_result = ImportResult(copied=5, skipped=2, projects=3)
        with patch("session_import.import_sessions", return_value=mock_result) as mock_imp, \
             patch("indexing.list_recent_sessions", return_value=[]), \
             patch("memory_utils.load_settings"):
            run_backfill(import_from="/backup/projects")

        mock_imp.assert_called_once_with("/backup/projects")

    def test_cli_backfill_flag(self):
        """CLI --backfill routes to run_backfill."""
        from synthesis_cron import main

        with patch("sys.argv", ["synthesis_cron.py", "--backfill"]), \
             patch("synthesis_cron.run_backfill", return_value=0) as mock_bf:
            result = main()

        mock_bf.assert_called_once_with(days=None, import_from=None)
        assert result == 0

    def test_state_flushed_per_project_for_resumability(self):
        """Session state is saved after each project so interrupted runs can resume."""
        from helpers import make_session_info
        from memory_utils import DEFAULT_SETTINGS
        from synthesis_cron import run_backfill

        sessions_a = [make_session_info("a1", file_size=100)]
        sessions_b = [make_session_info("b1", file_size=200)]

        flush_calls = []

        def track_update(updates):
            flush_calls.append(dict(updates))

        with patch("indexing.list_recent_sessions",
                    return_value=sessions_a + sessions_b), \
             patch("synthesis_cron._group_sessions_by_project",
                    return_value={"projA": sessions_a, "projB": sessions_b}), \
             patch("memory_utils.get_project_working_days",
                    return_value=["2026-03-25"]), \
             patch("indexing.get_session_date", return_value="2026-03-25"), \
             patch("memory_utils.load_settings", return_value=DEFAULT_SETTINGS), \
             patch("builtins.input", return_value="y"), \
             patch("storage.ensure_db") as mock_db, \
             patch("memory_utils.load_synthesis_state",
                    return_value={"sessions": {}}), \
             patch("transcript_ops.parse_jsonl_file_from_line",
                    return_value=([{"role": "user", "content": "hi"}], 10)), \
             patch("memory_utils.update_synthesis_state",
                    side_effect=track_update), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron._run_claude_backfill",
                    return_value="MEMORY_OPS: []"), \
             patch("synthesis.parse_synthesis_output") as mock_parse, \
             patch("load_memory._build_synthesis_instructions_v3", return_value=""):
            mock_parse.return_value = MagicMock(memory_ops=[])
            mock_db.return_value = MagicMock()
            run_backfill()

        assert len(flush_calls) == 2, (
            f"Expected 2 flush calls (one per project), got {len(flush_calls)}"
        )
        assert "a1" in flush_calls[0]
        assert "b1" in flush_calls[1]

    def test_failed_claude_call_does_not_mark_sessions_processed(self):
        """Sessions where Claude fails (returns None) are not saved to state."""
        from helpers import make_session_info
        from memory_utils import DEFAULT_SETTINGS
        from synthesis_cron import run_backfill

        sessions = [make_session_info("s1", file_size=100)]
        flush_calls = []

        def track_update(updates):
            flush_calls.append(dict(updates))

        with patch("indexing.list_recent_sessions", return_value=sessions), \
             patch("synthesis_cron._group_sessions_by_project",
                    return_value={"proj": sessions}), \
             patch("memory_utils.get_project_working_days",
                    return_value=["2026-03-25"]), \
             patch("indexing.get_session_date", return_value="2026-03-25"), \
             patch("memory_utils.load_settings", return_value=DEFAULT_SETTINGS), \
             patch("builtins.input", return_value="y"), \
             patch("storage.ensure_db") as mock_db, \
             patch("memory_utils.load_synthesis_state",
                    return_value={"sessions": {}}), \
             patch("transcript_ops.parse_jsonl_file_from_line",
                    return_value=([{"role": "user", "content": "hi"}], 10)), \
             patch("memory_utils.update_synthesis_state",
                    side_effect=track_update), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron._run_claude_backfill", return_value=None), \
             patch("load_memory._build_synthesis_instructions_v3", return_value=""):
            mock_db.return_value = MagicMock()
            run_backfill()

        assert len(flush_calls) == 0, (
            f"Expected 0 flush calls (Claude failed), got {len(flush_calls)}"
        )

    def test_exception_mid_project_flushes_completed_sessions(self):
        """If an exception occurs mid-project, already-completed sessions are flushed."""
        from helpers import make_session_info
        from memory_utils import DEFAULT_SETTINGS
        from synthesis_cron import run_backfill

        s1 = make_session_info("s1", file_size=100)
        s2 = make_session_info("s2", file_size=200)
        flush_calls = []

        def track_update(updates):
            flush_calls.append(dict(updates))

        call_count = [0]

        def claude_with_error(prompt, model):
            call_count[0] += 1
            if call_count[0] == 1:
                return "MEMORY_OPS: []"
            raise RuntimeError("API timeout")

        with patch("indexing.list_recent_sessions", return_value=[s1, s2]), \
             patch("synthesis_cron._group_sessions_by_project",
                    return_value={"proj": [s1, s2]}), \
             patch("memory_utils.get_project_working_days",
                    return_value=["2026-03-25", "2026-03-24"]), \
             patch("indexing.get_session_date",
                    side_effect=["2026-03-25", "2026-03-24",
                                 "2026-03-25", "2026-03-24"]), \
             patch("memory_utils.load_settings", return_value=DEFAULT_SETTINGS), \
             patch("builtins.input", return_value="y"), \
             patch("storage.ensure_db") as mock_db, \
             patch("memory_utils.load_synthesis_state",
                    return_value={"sessions": {}}), \
             patch("transcript_ops.parse_jsonl_file_from_line",
                    return_value=([{"role": "user", "content": "hi"}], 10)), \
             patch("memory_utils.update_synthesis_state",
                    side_effect=track_update), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron._run_claude_backfill",
                    side_effect=claude_with_error), \
             patch("synthesis.parse_synthesis_output") as mock_parse, \
             patch("load_memory._build_synthesis_instructions_v3", return_value=""):
            mock_parse.return_value = MagicMock(memory_ops=[])
            mock_db.return_value = MagicMock()
            result = run_backfill()

        assert result == 1
        assert len(flush_calls) == 1, (
            f"Expected 1 flush call (partial progress), got {len(flush_calls)}"
        )
        assert "s2" in flush_calls[0], "s2 (earlier date, succeeded) should be flushed"
        assert "s1" not in flush_calls[0], "s1 (later date, failed) should not be flushed"

    def test_resumed_backfill_skips_already_processed_sessions(self):
        """Sessions already in synthesis state are skipped on re-run."""
        from helpers import make_session_info
        from memory_utils import DEFAULT_SETTINGS
        from synthesis_cron import run_backfill

        s1 = make_session_info("done-session", file_size=500)
        s2 = make_session_info("new-session", file_size=300)

        claude_calls = []

        def track_claude(prompt, model):
            claude_calls.append(prompt)
            return "MEMORY_OPS: []"

        with patch("indexing.list_recent_sessions", return_value=[s1, s2]), \
             patch("synthesis_cron._group_sessions_by_project",
                    return_value={"proj": [s1, s2]}), \
             patch("memory_utils.get_project_working_days",
                    return_value=["2026-03-25"]), \
             patch("indexing.get_session_date", return_value="2026-03-25"), \
             patch("memory_utils.load_settings", return_value=DEFAULT_SETTINGS), \
             patch("builtins.input", return_value="y"), \
             patch("storage.ensure_db") as mock_db, \
             patch("memory_utils.load_synthesis_state", return_value={
                    "sessions": {"done-session": {"offset": 500, "lines": 50}}}), \
             patch("transcript_ops.parse_jsonl_file_from_line",
                    return_value=([{"role": "user", "content": "hi"}], 10)), \
             patch("memory_utils.update_synthesis_state"), \
             patch("synthesis_cron._run_decay_v3"), \
             patch("synthesis_cron._run_claude_backfill",
                    side_effect=track_claude), \
             patch("synthesis.parse_synthesis_output") as mock_parse, \
             patch("load_memory._build_synthesis_instructions_v3", return_value=""):
            mock_parse.return_value = MagicMock(memory_ops=[])
            mock_db.return_value = MagicMock()
            run_backfill()

        assert len(claude_calls) == 1, (
            f"Expected 1 claude call (skipping done-session), got {len(claude_calls)}"
        )
        assert "new-session" in claude_calls[0]
        assert "done-session" not in claude_calls[0]
