import json
from unittest.mock import patch

from helpers import make_jsonl_content
from memory_utils import DEFAULT_SETTINGS


class TestWriteRecallFile:
    """Tests for the main write_recall_file() function."""

    def test_basic_recall_file(self, tmp_path):
        """Writes recall file with frontmatter, first prompt blockquote, and assistant messages."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("user", "investigate the synthesis pipeline"),
            ("assistant", "I'll look into the synthesis pipeline architecture."),
            ("assistant", "The bottleneck is the LLM call in synthesis_cron.py."),
        ]))
        recall_dir = tmp_path / "pending-recall"

        from session_end_recall import write_recall_file
        write_recall_file(
            session_id="abc123",
            transcript_path=transcript,
            cwd="/some/project",
            recall_dir=recall_dir,
        )

        recall_file = recall_dir / "abc123.md"
        assert recall_file.exists()
        content = recall_file.read_text()
        assert "session_id: abc123" in content
        assert "cwd: /some/project" in content
        assert "> investigate the synthesis pipeline" in content
        lines = content.split("\n")
        assistant_start = None
        for i, line in enumerate(lines):
            if "I'll look into" in line:
                assistant_start = i
                break
        assert assistant_start is not None
        bottleneck_line = None
        for i, line in enumerate(lines):
            if "bottleneck" in line:
                bottleneck_line = i
                break
        assert bottleneck_line is not None
        assert bottleneck_line > assistant_start

    def test_token_budget_limits_messages(self, tmp_path):
        """Stops collecting messages when token budget is exceeded."""
        token_limit = DEFAULT_SETTINGS["previousSessionRecall"]["tokenLimit"]
        messages = [("user", "start")]
        for i in range(20):
            messages.append(("assistant", f"Message {i}: " + "x" * 400))
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content(messages))
        recall_dir = tmp_path / "pending-recall"

        from session_end_recall import write_recall_file
        write_recall_file(
            session_id="budget-test",
            transcript_path=transcript,
            cwd="/test",
            recall_dir=recall_dir,
        )

        content = (recall_dir / "budget-test.md").read_text()
        body_start = content.index("---", 4) + 4
        body = content[body_start:].strip()
        body_tokens = len(body) // 4
        assert body_tokens <= token_limit + 100

    def test_per_message_truncation(self, tmp_path):
        """Messages over MAX_MESSAGE_LINES get head/tail truncated."""
        from session_end_recall import MAX_MESSAGE_LINES, write_recall_file

        long_msg = "\n".join(f"line {i}" for i in range(MAX_MESSAGE_LINES * 2))
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("user", "start"),
            ("assistant", long_msg),
        ]))
        recall_dir = tmp_path / "pending-recall"

        write_recall_file(
            session_id="truncate-test",
            transcript_path=transcript,
            cwd="/test",
            recall_dir=recall_dir,
        )

        content = (recall_dir / "truncate-test.md").read_text()
        assert "lines truncated" in content

    def test_empty_transcript(self, tmp_path):
        """Empty transcript produces no recall file."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text("")
        recall_dir = tmp_path / "pending-recall"

        from session_end_recall import write_recall_file
        write_recall_file(
            session_id="empty-test",
            transcript_path=transcript,
            cwd="/test",
            recall_dir=recall_dir,
        )

        assert not (recall_dir / "empty-test.md").exists()

    def test_head_tail_split_with_omitted_marker(self, tmp_path):
        """Tight budget keeps oldest + newest messages and inserts omission marker."""
        msgs = [("user", "start")]
        msgs.extend(("assistant", f"MSG{i}: " + "x" * 200) for i in range(10))
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content(msgs))
        recall_dir = tmp_path / "pending-recall"

        from session_end_recall import write_recall_file
        write_recall_file(
            session_id="split-test",
            transcript_path=transcript,
            cwd="/test",
            recall_dir=recall_dir,
            token_limit=200,
        )

        content = (recall_dir / "split-test.md").read_text()
        assert "messages omitted" in content
        assert "MSG0" in content
        assert "MSG9" in content
        marker_pos = content.index("messages omitted")
        msg0_pos = content.index("MSG0")
        msg9_pos = content.index("MSG9")
        assert msg0_pos < marker_pos < msg9_pos

    def test_no_marker_when_everything_fits(self, tmp_path):
        """Generous budget includes all messages with no omission marker."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("user", "start"),
            ("assistant", "first"),
            ("assistant", "second"),
            ("assistant", "third"),
        ]))
        recall_dir = tmp_path / "pending-recall"

        from session_end_recall import write_recall_file
        write_recall_file(
            session_id="fits-test",
            transcript_path=transcript,
            cwd="/test",
            recall_dir=recall_dir,
            token_limit=500,
        )

        content = (recall_dir / "fits-test.md").read_text()
        assert "messages omitted" not in content
        assert all(s in content for s in ("first", "second", "third"))

    def test_latest_message_force_included(self, tmp_path):
        """Single message exceeding tail budget is still included."""
        big_msg = "BIGMSG: " + "y" * 4000
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("user", "start"),
            ("assistant", "earlier"),
            ("assistant", big_msg),
        ]))
        recall_dir = tmp_path / "pending-recall"

        from session_end_recall import write_recall_file
        write_recall_file(
            session_id="force-test",
            transcript_path=transcript,
            cwd="/test",
            recall_dir=recall_dir,
            token_limit=100,
        )

        content = (recall_dir / "force-test.md").read_text()
        assert "BIGMSG" in content

    def test_no_marker_when_head_meets_tail(self, tmp_path):
        """When head and tail together cover all messages with no gap, no marker."""
        msgs = [("user", "start")]
        msgs.extend(("assistant", f"M{i}") for i in range(4))
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content(msgs))
        recall_dir = tmp_path / "pending-recall"

        from session_end_recall import write_recall_file
        write_recall_file(
            session_id="nogap-test",
            transcript_path=transcript,
            cwd="/test",
            recall_dir=recall_dir,
            token_limit=50,
        )

        content = (recall_dir / "nogap-test.md").read_text()
        assert "messages omitted" not in content

    def test_chronological_output_order(self, tmp_path):
        """Assistant messages appear oldest-to-newest in output."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("user", "start"),
            ("assistant", "FIRST message"),
            ("assistant", "SECOND message"),
            ("assistant", "THIRD message"),
        ]))
        recall_dir = tmp_path / "pending-recall"

        from session_end_recall import write_recall_file
        write_recall_file(
            session_id="order-test",
            transcript_path=transcript,
            cwd="/test",
            recall_dir=recall_dir,
        )

        content = (recall_dir / "order-test.md").read_text()
        first_pos = content.index("FIRST")
        second_pos = content.index("SECOND")
        third_pos = content.index("THIRD")
        assert first_pos < second_pos < third_pos


class TestProjectResolution:
    """Tests for project name resolution in recall files."""

    def test_registered_project(self, tmp_path):
        """Writes project name for registered project."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("user", "test"), ("assistant", "OK"),
        ]))
        recall_dir = tmp_path / "pending-recall"

        mock_index = {
            "projects": {
                "/resolved/path": {"name": "my-project", "originalPath": "/resolved/path"}
            }
        }
        from session_end_recall import write_recall_file
        with patch("session_end_recall.resolve_session_path", return_value="/resolved/path"), \
             patch("session_end_recall.load_json_file", return_value=mock_index), \
             patch("session_end_recall.find_current_project", return_value={"name": "my-project"}):
            write_recall_file(
                session_id="proj-test",
                transcript_path=transcript,
                cwd="/some/worktree",
                recall_dir=recall_dir,
            )

        content = (recall_dir / "proj-test.md").read_text()
        assert "project: my-project" in content

    def test_unregistered_project(self, tmp_path):
        """Writes empty project for unregistered directory."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("user", "test"), ("assistant", "OK"),
        ]))
        recall_dir = tmp_path / "pending-recall"

        from session_end_recall import write_recall_file
        with patch("session_end_recall.resolve_session_path", return_value="/unknown/path"), \
             patch("session_end_recall.load_json_file", return_value={"projects": {}}), \
             patch("session_end_recall.find_current_project", return_value=None):
            write_recall_file(
                session_id="unreg-test",
                transcript_path=transcript,
                cwd="/unknown/path",
                recall_dir=recall_dir,
            )

        content = (recall_dir / "unreg-test.md").read_text()
        assert content.split("project:", 1)[1].split("\n")[0].strip() == ""


class TestMainEntryPoint:
    """Tests for the main() stdin-reading entry point."""

    def test_reads_stdin_json(self, tmp_path):
        """Reads session_id, transcript_path, cwd from stdin JSON."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("user", "hello"), ("assistant", "hi there"),
        ]))
        recall_dir = tmp_path / "pending-recall"

        stdin_data = json.dumps({
            "session_id": "stdin-test",
            "transcript_path": str(transcript),
            "cwd": "/test/dir",
        })

        import io

        from session_end_recall import main as recall_main
        with patch("sys.stdin", io.StringIO(stdin_data)), \
             patch("session_end_recall.get_pending_recall_dir", return_value=recall_dir), \
             patch("session_end_recall.load_settings", return_value=DEFAULT_SETTINGS), \
             patch("session_end_recall.resolve_session_path", return_value="/test/dir"), \
             patch("session_end_recall.load_json_file", return_value={"projects": {}}), \
             patch("session_end_recall.find_current_project", return_value=None):
            recall_main()

        assert (recall_dir / "stdin-test.md").exists()
