"""Tests for synthesis_cron.py -- systemd-triggered deferred synthesis."""
import subprocess as real_subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from synthesis_cron import (
    _clear_eager_timestamp,
    _log_error,
    build_claude_command,
    run_synthesis,
    should_run_deferred_synthesis,
)


class TestShouldRunDeferredSynthesis:
    """Tests for the scheduling check."""

    def test_returns_false_when_recently_synthesized(self, tmp_path):
        """If .last-synthesis is recent, should not run."""
        last_synth = tmp_path / ".last-synthesis"
        last_synth.write_text(datetime.now(timezone.utc).isoformat())
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"intervalHours": 2}
        }), patch("load_memory.get_last_synthesis_file", return_value=last_synth):
            assert should_run_deferred_synthesis() is False

    def test_returns_true_when_due(self, tmp_path):
        """If enough time passed, should run."""
        last_synth = tmp_path / ".last-synthesis"
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"intervalHours": 2}
        }), patch("load_memory.get_last_synthesis_file", return_value=last_synth):
            assert should_run_deferred_synthesis() is True

    def test_returns_true_when_old_timestamp(self, tmp_path):
        """If timestamp is old enough, should run."""
        last_synth = tmp_path / ".last-synthesis"
        old_time = datetime.now(timezone.utc) - timedelta(hours=3)
        last_synth.write_text(old_time.isoformat())
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"intervalHours": 2}
        }), patch("load_memory.get_last_synthesis_file", return_value=last_synth):
            assert should_run_deferred_synthesis() is True


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
        # Last element should be a flag value, not a prompt string
        assert cmd[-1] == "Write,Bash,Read"

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

    def test_calls_claude_p_with_prompt(self, tmp_path):
        """When prompt is generated, should invoke claude -p."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf:
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_synthesis()

        assert result == 0
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "-p" in cmd

    def test_returns_1_on_claude_failure(self, tmp_path):
        """When claude -p fails, should return 1."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("synthesis_cron.SYNTHESIS_ERROR_LOG", tmp_path / ".synthesis-errors.log"):
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="some error")
            result = run_synthesis()

        assert result == 1

    def test_returns_1_on_timeout(self, tmp_path):
        """When claude -p times out, should return 1."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("synthesis_cron.SYNTHESIS_ERROR_LOG", tmp_path / ".synthesis-errors.log"):
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            mock_run.side_effect = real_subprocess.TimeoutExpired(cmd="claude", timeout=300)
            result = run_synthesis()

        assert result == 1

    def test_writes_timestamp_before_running(self, tmp_path):
        """Should write .last-synthesis timestamp before calling claude."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")
        last_synth = tmp_path / ".last-synthesis"

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        call_order = []

        def track_subprocess(*args, **kwargs):
            # Record whether timestamp was written before subprocess.run
            call_order.append(("subprocess", last_synth.exists()))
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run", side_effect=track_subprocess), \
             patch("synthesis_cron.get_last_synthesis_file", return_value=last_synth):
            result = run_synthesis()

        assert result == 0
        # Timestamp should have been written BEFORE subprocess.run was called
        assert call_order == [("subprocess", True)]

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
        """Should parse model from write_synthesis_prompt output."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=haiku")
            print(f"prompt_file={prompt_file}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf:
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_synthesis()

        assert result == 0
        cmd = mock_run.call_args[0][0]
        model_idx = cmd.index("--model") + 1
        assert cmd[model_idx] == "haiku"

    def test_unsets_claudecode_env(self, tmp_path):
        """Should unset CLAUDECODE env var to avoid nesting guard."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf:
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            run_synthesis()

        env = mock_run.call_args[1].get("env", {})
        assert env.get("CLAUDECODE") == ""

    def test_keeps_timestamp_on_timeout(self, tmp_path):
        """When claude -p times out, timestamp preserved (partial success possible)."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")
        last_synth = tmp_path / ".last-synthesis"

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file", return_value=last_synth), \
             patch("synthesis_cron._log_error"):
            mock_run.side_effect = real_subprocess.TimeoutExpired(cmd="claude", timeout=300)
            result = run_synthesis()

        assert result == 1
        # Timestamp kept — next run will re-extract only still-pending dates
        assert last_synth.exists()

    def test_keeps_timestamp_on_nonzero_exit(self, tmp_path):
        """When claude -p exits non-zero, timestamp preserved."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")
        last_synth = tmp_path / ".last-synthesis"

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file", return_value=last_synth), \
             patch("synthesis_cron._log_error"):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = run_synthesis()

        assert result == 1
        assert last_synth.exists()

    def test_keeps_timestamp_on_file_not_found(self, tmp_path):
        """When claude binary is missing, timestamp preserved."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")
        last_synth = tmp_path / ".last-synthesis"

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file", return_value=last_synth), \
             patch("synthesis_cron._log_error"):
            mock_run.side_effect = FileNotFoundError("No such file or directory: 'claude'")
            result = run_synthesis()

        assert result == 1
        assert last_synth.exists()

    def test_logs_error_on_failure(self, tmp_path):
        """When claude -p fails, should write to error log."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")
        error_log = tmp_path / ".synthesis-errors.log"

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file", return_value=tmp_path / ".last-synthesis"), \
             patch("synthesis_cron.SYNTHESIS_ERROR_LOG", error_log):
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="some error")
            run_synthesis()

        assert error_log.exists()
        content = error_log.read_text()
        assert "claude -p exited 1" in content
        assert "some error" in content

    def test_logs_error_on_file_not_found(self, tmp_path):
        """When claude binary is missing, should write to error log."""
        prompt_file = tmp_path / "synthesis-prompt-12345.txt"
        prompt_file.write_text("test prompt content")
        error_log = tmp_path / ".synthesis-errors.log"

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_file}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file", return_value=tmp_path / ".last-synthesis"), \
             patch("synthesis_cron.SYNTHESIS_ERROR_LOG", error_log):
            mock_run.side_effect = FileNotFoundError("No such file or directory: 'claude'")
            run_synthesis()

        assert error_log.exists()
        content = error_log.read_text()
        assert "claude" in content


    def test_runs_claude_once_per_date(self, tmp_path):
        """With multiple prompt files, should call claude -p for each."""
        prompt_a = tmp_path / "synthesis-prompt-2026-02-26-1234.txt"
        prompt_b = tmp_path / "synthesis-prompt-2026-02-27-1234.txt"
        prompt_a.write_text("date A content")
        prompt_b.write_text("date B content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_a}")
            print(f"prompt_file={prompt_b}")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf:
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_synthesis()

        assert result == 0
        assert mock_run.call_count == 2

    def test_continues_on_partial_failure(self, tmp_path, capsys):
        """If one date fails, should continue with remaining dates."""
        prompt_a = tmp_path / "synthesis-prompt-2026-02-26-1234.txt"
        prompt_b = tmp_path / "synthesis-prompt-2026-02-27-1234.txt"
        prompt_a.write_text("date A content")
        prompt_b.write_text("date B content")

        def fake_write_prompt(**kwargs):
            print("model=sonnet")
            print(f"prompt_file={prompt_a}")
            print(f"prompt_file={prompt_b}")

        call_count = 0

        def alternating_results(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return MagicMock(returncode=1, stdout="", stderr="fail first")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("synthesis_cron.should_run_deferred_synthesis", return_value=True), \
             patch("synthesis_cron.write_synthesis_prompt", side_effect=fake_write_prompt), \
             patch("synthesis_cron.subprocess.run", side_effect=alternating_results), \
             patch("synthesis_cron.get_last_synthesis_file") as mock_lsf, \
             patch("synthesis_cron._log_error"):
            mock_lsf.return_value = tmp_path / ".last-synthesis"
            result = run_synthesis()

        assert result == 1  # Partial failure
        assert call_count == 2  # Both dates attempted
        output = capsys.readouterr().out
        assert "complete" in output.lower()  # Second date succeeded


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
