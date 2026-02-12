#!/usr/bin/env python3
"""
Unit tests for indexing.py

Run with: python -m pytest tests/test_indexing.py -v
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest

# Add scripts directory to path
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from indexing import (
    MIN_SESSION_SIZE_BYTES,
    SessionInfo,
    build_projects_index,
    get_session_date,
    has_assistant_message,
    list_pending_sessions,
)
from transcript_ops import (
    extract_text_content,
    format_transcripts_for_output,
    parse_jsonl_file,
    should_skip_message,
)


# =============================================================================
# Helper to create JSONL content
# =============================================================================


def make_jsonl_line(role: str, content: str) -> str:
    return json.dumps({
        "type": role,
        "message": {"role": role, "content": content},
    })


def make_session_info(
    session_id: str = "test-session",
    transcript_path: Path = Path("/tmp/test.jsonl"),
    file_size: int = 2000,
    project_path: str | None = None,
    created: datetime | None = None,
) -> SessionInfo:
    return SessionInfo(
        session_id=session_id,
        transcript_path=transcript_path,
        project_hash="test-hash",
        file_mtime=datetime.now(timezone.utc),
        file_size=file_size,
        project_path=project_path,
        created=created,
    )


# =============================================================================
# Content Extraction Tests
# =============================================================================


class TestExtractTextContent:
    def test_string_content(self):
        assert extract_text_content("hello world") == "hello world"

    def test_list_content(self):
        content = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]
        result = extract_text_content(content)
        assert "first" in result
        assert "second" in result

    def test_list_with_non_text(self):
        content = [
            {"type": "text", "text": "keep"},
            {"type": "image", "url": "http://example.com"},
        ]
        result = extract_text_content(content)
        assert "keep" in result
        assert "example.com" not in result

    def test_none_content(self):
        assert extract_text_content(None) == ""

    def test_int_content(self):
        assert extract_text_content(42) == ""

    def test_empty_list(self):
        assert extract_text_content([]) == ""


# =============================================================================
# Message Filter Tests
# =============================================================================


class TestShouldSkipMessage:
    def test_skill_injection(self):
        assert should_skip_message("Base directory for this skill: /home/user/.claude/skills/test")

    def test_command_name_tag(self):
        assert should_skip_message("<command-name>/synthesize</command-name> some content")

    def test_system_reminder(self):
        assert should_skip_message("Some text with <system-reminder> embedded")

    def test_user_interruption(self):
        assert should_skip_message("[Request interrupted by user]")

    def test_normal_content(self):
        assert not should_skip_message("I've analyzed the codebase and found the following patterns")

    def test_empty_content(self):
        assert not should_skip_message("")


# =============================================================================
# has_assistant_message Tests
# =============================================================================


class TestHasAssistantMessage:
    def test_with_assistant_message(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(make_jsonl_line("user", "hello") + "\n")
            f.write(make_jsonl_line("assistant", "hi there") + "\n")
            f.flush()
            try:
                assert has_assistant_message(Path(f.name)) is True
            finally:
                os.unlink(f.name)

    def test_without_assistant_message(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(make_jsonl_line("user", "hello") + "\n")
            f.flush()
            try:
                assert has_assistant_message(Path(f.name)) is False
            finally:
                os.unlink(f.name)

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write("")
            f.flush()
            try:
                assert has_assistant_message(Path(f.name)) is False
            finally:
                os.unlink(f.name)

    def test_metadata_only(self):
        """Sessions with only metadata (file-history-snapshot, progress) should return False."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(json.dumps({"type": "progress", "data": {}}) + "\n")
            f.write(json.dumps({"type": "file-history-snapshot", "files": []}) + "\n")
            f.flush()
            try:
                assert has_assistant_message(Path(f.name)) is False
            finally:
                os.unlink(f.name)

    def test_nonexistent_file(self):
        assert has_assistant_message(Path("/nonexistent/file.jsonl")) is False

    def test_invalid_json_lines(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write("not json\n")
            f.write(make_jsonl_line("assistant", "valid") + "\n")
            f.flush()
            try:
                assert has_assistant_message(Path(f.name)) is True
            finally:
                os.unlink(f.name)


# =============================================================================
# parse_jsonl_file Tests
# =============================================================================


class TestParseJsonlFile:
    def test_parses_assistant_messages(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(make_jsonl_line("assistant", "I found the bug") + "\n")
            f.flush()
            try:
                messages = parse_jsonl_file(Path(f.name))
                assert len(messages) == 1
                assert messages[0]["role"] == "assistant"
                assert "bug" in messages[0]["content"]
            finally:
                os.unlink(f.name)

    def test_skips_user_messages(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(make_jsonl_line("user", "Fix the bug") + "\n")
            f.write(make_jsonl_line("assistant", "I found the issue") + "\n")
            f.flush()
            try:
                messages = parse_jsonl_file(Path(f.name))
                assert len(messages) == 1
                assert messages[0]["role"] == "assistant"
            finally:
                os.unlink(f.name)

    def test_skips_system_reminders(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.write(make_jsonl_line("assistant", "Normal response") + "\n")
            f.write(make_jsonl_line("assistant", "Response with <system-reminder> injected") + "\n")
            f.flush()
            try:
                messages = parse_jsonl_file(Path(f.name))
                assert len(messages) == 1
                assert "Normal response" in messages[0]["content"]
            finally:
                os.unlink(f.name)

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False
        ) as f:
            f.flush()
            try:
                messages = parse_jsonl_file(Path(f.name))
                assert messages == []
            finally:
                os.unlink(f.name)

    def test_nonexistent_file(self):
        messages = parse_jsonl_file(Path("/nonexistent/file.jsonl"))
        assert messages == []


# =============================================================================
# list_pending_sessions Tests
# =============================================================================


class TestListPendingSessions:
    def _make_sessions(self) -> list[SessionInfo]:
        return [
            make_session_info("captured-1", file_size=2000),
            make_session_info("pending-1", file_size=2000),
            make_session_info("small-1", file_size=500),
            make_session_info("pending-2", file_size=3000),
        ]

    def test_filters_captured(self):
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_pending_sessions(captured={"captured-1"})
            ids = {s.session_id for s in result}
            assert "captured-1" not in ids
            assert "pending-1" in ids

    def test_filters_small_sessions(self):
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_pending_sessions(captured=set())
            ids = {s.session_id for s in result}
            assert "small-1" not in ids

    def test_excludes_session_id(self):
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_pending_sessions(
                captured=set(), exclude_session_id="pending-1"
            )
            ids = {s.session_id for s in result}
            assert "pending-1" not in ids
            assert "pending-2" in ids

    def test_min_session_size_constant(self):
        assert MIN_SESSION_SIZE_BYTES == 1000


# =============================================================================
# get_session_date Tests
# =============================================================================


class TestGetSessionDate:
    def test_prefers_created(self):
        session = make_session_info(
            created=datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
        )
        assert get_session_date(session) == "2026-01-15"

    def test_falls_back_to_mtime(self):
        session = make_session_info(created=None)
        # mtime is set to now in the helper
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert get_session_date(session) == today


# =============================================================================
# Format Output Tests
# =============================================================================


class TestFormatTranscriptsForOutput:
    def test_basic_formatting(self):
        daily_data = {
            "2026-02-01": [
                {
                    "session_id": "abc123",
                    "filepath": "/tmp/test.jsonl",
                    "project_path": None,
                    "message_count": 2,
                    "messages": [
                        {"role": "assistant", "content": "Hello world"},
                        {"role": "assistant", "content": "Goodbye"},
                    ],
                }
            ]
        }
        output = format_transcripts_for_output(daily_data)
        assert "2026-02-01" in output
        assert "abc123" in output
        assert "Hello world" in output
        assert "CLAUDE" in output

    def test_empty_data(self):
        assert format_transcripts_for_output({}) == ""

    def test_multiple_days_sorted(self):
        daily_data = {
            "2026-02-03": [
                {
                    "session_id": "s1",
                    "filepath": "/tmp/a.jsonl",
                    "project_path": None,
                    "message_count": 1,
                    "messages": [{"role": "assistant", "content": "Day 3"}],
                }
            ],
            "2026-02-01": [
                {
                    "session_id": "s2",
                    "filepath": "/tmp/b.jsonl",
                    "project_path": None,
                    "message_count": 1,
                    "messages": [{"role": "assistant", "content": "Day 1"}],
                }
            ],
        }
        output = format_transcripts_for_output(daily_data)
        # Day 1 should appear before Day 3
        assert output.index("2026-02-01") < output.index("2026-02-03")


# =============================================================================
# build_projects_index Tests
# =============================================================================


def _make_sessions_index(original_path, entries):
    """Helper to create a sessions-index.json dict."""
    return {
        "version": 1,
        "originalPath": original_path,
        "entries": entries,
    }


def _make_session_entry(session_id, created, project_path=None):
    """Helper to create a session entry."""
    entry = {
        "sessionId": session_id,
        "fullPath": f"/tmp/{session_id}.jsonl",
        "fileMtime": 1770000000000,
        "firstPrompt": "test",
        "summary": "test",
        "messageCount": 5,
        "created": created,
        "modified": created,
        "gitBranch": "",
        "isSidechain": False,
    }
    if project_path:
        entry["projectPath"] = project_path
    return entry


class TestBuildProjectsIndex:
    def _setup_project(self, projects_dir, folder_name, original_path, entries):
        """Create a project folder with sessions-index.json."""
        folder = projects_dir / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        index_data = _make_sessions_index(original_path, entries)
        (folder / "sessions-index.json").write_text(
            json.dumps(index_data), encoding="utf-8"
        )

    def test_basic_project_discovery(self):
        """Smoke test: function runs and finds a project."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            self._setup_project(
                projects_dir,
                "-home-user-myproject",
                "/home/user/myproject",
                [_make_session_entry("s1", "2026-02-01T10:00:00Z")],
            )

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                result = build_projects_index()

            projects = result["projects"]
            assert len(projects) == 1
            data = list(projects.values())[0]
            assert data["name"] == "myproject"
            assert "2026-02-01" in data["workDays"]

    def test_extracts_project_name_from_path(self):
        """Project name is the last component of originalPath."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            self._setup_project(
                projects_dir,
                "-home-user-swyfft-projects-tableau-agency-overview",
                "/home/user/swyfft/projects/tableau/agency-overview",
                [_make_session_entry("s1", "2026-02-06T10:00:00Z")],
            )

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                result = build_projects_index()

            projects = result["projects"]
            data = list(projects.values())[0]
            assert data["name"] == "agency-overview"

    def test_multiple_work_days(self):
        """Sessions on different days produce multiple work days."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            self._setup_project(
                projects_dir,
                "-home-user-proj",
                "/home/user/proj",
                [
                    _make_session_entry("s1", "2026-02-01T10:00:00Z"),
                    _make_session_entry("s2", "2026-02-01T14:00:00Z"),
                    _make_session_entry("s3", "2026-02-03T09:00:00Z"),
                ],
            )

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                result = build_projects_index()

            data = list(result["projects"].values())[0]
            assert data["workDays"] == ["2026-02-01", "2026-02-03"]

    def test_multiple_projects(self):
        """Discovers multiple projects from separate folders."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            self._setup_project(
                projects_dir, "-proj-a", "/home/user/proj-a",
                [_make_session_entry("s1", "2026-01-15T10:00:00Z")],
            )
            self._setup_project(
                projects_dir, "-proj-b", "/home/user/proj-b",
                [_make_session_entry("s2", "2026-01-20T10:00:00Z")],
            )

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                result = build_projects_index()

            assert len(result["projects"]) == 2
            names = {d["name"] for d in result["projects"].values()}
            assert names == {"proj-a", "proj-b"}

    def test_skips_folder_without_sessions_index(self):
        """Folders without sessions-index.json are ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            # Folder with no sessions-index.json
            (projects_dir / "empty-folder").mkdir(parents=True)
            # Folder with sessions-index.json
            self._setup_project(
                projects_dir, "-real-proj", "/home/user/real-proj",
                [_make_session_entry("s1", "2026-02-01T10:00:00Z")],
            )

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                result = build_projects_index()

            assert len(result["projects"]) == 1

    def test_skips_entries_without_created(self):
        """Entries missing created timestamp are skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            entry_no_created = {
                "sessionId": "s1",
                "fullPath": "/tmp/s1.jsonl",
                "fileMtime": 1770000000000,
            }
            self._setup_project(
                projects_dir, "-proj", "/home/user/proj",
                [entry_no_created],
            )

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                result = build_projects_index()

            # No work days extracted -> project skipped
            assert len(result["projects"]) == 0

    def test_empty_projects_dir(self):
        """Empty projects directory returns no projects."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            projects_dir.mkdir()
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                result = build_projects_index()

            assert len(result["projects"]) == 0

    def test_nonexistent_projects_dir(self):
        """Missing projects directory returns empty result."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "nonexistent"
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                result = build_projects_index()

            assert result == {"projects": {}}

    def test_case_insensitive_path_merging(self):
        """Same project path with different cases merges work days."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            self._setup_project(
                projects_dir, "-folder-a", "/home/user/MyProject",
                [_make_session_entry("s1", "2026-02-01T10:00:00Z")],
            )
            self._setup_project(
                projects_dir, "-folder-b", "/home/user/myproject",
                [_make_session_entry("s2", "2026-02-03T10:00:00Z")],
            )

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                result = build_projects_index()

            assert len(result["projects"]) == 1
            data = list(result["projects"].values())[0]
            assert len(data["workDays"]) == 2
            assert len(data["encodedPaths"]) == 2

    def test_writes_index_file(self):
        """Result is written to projects-index.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            self._setup_project(
                projects_dir, "-proj", "/home/user/proj",
                [_make_session_entry("s1", "2026-02-01T10:00:00Z")],
            )

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                build_projects_index()

            assert index_file.exists()
            saved = json.loads(index_file.read_text(encoding="utf-8"))
            assert saved["version"] == 1
            assert "lastUpdated" in saved
            assert len(saved["projects"]) == 1

    def test_fallback_to_entry_project_path(self):
        """Uses entries[0].projectPath when root originalPath is missing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            memory_dir = Path(tmpdir) / "memory"
            index_file = memory_dir / "projects-index.json"

            folder = projects_dir / "-proj"
            folder.mkdir(parents=True)
            index_data = {
                "version": 1,
                "entries": [
                    {
                        "sessionId": "s1",
                        "fullPath": "/tmp/s1.jsonl",
                        "fileMtime": 1770000000000,
                        "created": "2026-02-01T10:00:00Z",
                        "projectPath": "/home/user/fallback-proj",
                    }
                ],
            }
            (folder / "sessions-index.json").write_text(
                json.dumps(index_data), encoding="utf-8"
            )

            with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
                 mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
                 mock.patch("indexing.get_projects_index_file", return_value=index_file):
                result = build_projects_index()

            data = list(result["projects"].values())[0]
            assert data["name"] == "fallback-proj"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
