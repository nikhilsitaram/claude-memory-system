#!/usr/bin/env python3
"""
Unit tests for indexing.py

Run with: python -m pytest tests/test_indexing.py -v
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
from helpers import make_jsonl_line, make_session_info  # noqa: I001
from indexing import (
    MIN_SESSION_SIZE_BYTES,
    build_projects_index,
    get_session_date,
    has_assistant_message,
)
from transcript_ops import (
    extract_text_content,
    format_transcripts_for_output,
    parse_jsonl_file,
    should_skip_message,
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


@pytest.mark.parametrize("content", [
    "Base directory for this skill: /home/user/.claude/skills/test",
    "<command-name>/synthesize</command-name> some content",
    "Some text with <system-reminder> embedded",
    "[Request interrupted by user]",
    "===DAILY:2026-02-20===\n# 2026-02-20\n## Actions\n- [global/implement] something",
    "python3 $HOME/.claude/scripts/synthesis.py apply /tmp/output.txt --sidecars /tmp/s.sessions",
    "## AUTO-SYNTHESIZE REQUIRED\nThere are 2 pending date(s)",
])
def test_should_skip(content):
    assert should_skip_message(content)


@pytest.mark.parametrize("content", [
    "I've analyzed the codebase and found the following patterns",
    "",
])
def test_should_not_skip(content):
    assert not should_skip_message(content)


# =============================================================================
# has_assistant_message Tests
# =============================================================================


class TestHasAssistantMessage:
    def test_with_assistant_message(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text(
            make_jsonl_line("user", "hello") + "\n"
            + make_jsonl_line("assistant", "hi there") + "\n"
        )
        assert has_assistant_message(f) is True

    def test_without_assistant_message(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text(make_jsonl_line("user", "hello") + "\n")
        assert has_assistant_message(f) is False

    def test_empty_file(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("")
        assert has_assistant_message(f) is False

    def test_metadata_only(self, tmp_path):
        """Sessions with only metadata (file-history-snapshot, progress) should return False."""
        f = tmp_path / "test.jsonl"
        f.write_text(
            json.dumps({"type": "progress", "data": {}}) + "\n"
            + json.dumps({"type": "file-history-snapshot", "files": []}) + "\n"
        )
        assert has_assistant_message(f) is False

    def test_nonexistent_file(self):
        assert has_assistant_message(Path("/nonexistent/file.jsonl")) is False

    def test_invalid_json_lines(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text(
            "not json\n"
            + make_jsonl_line("assistant", "valid") + "\n"
        )
        assert has_assistant_message(f) is True


# =============================================================================
# parse_jsonl_file Tests
# =============================================================================


class TestParseJsonlFile:
    def test_parses_assistant_messages(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text(make_jsonl_line("assistant", "I found the bug") + "\n")
        messages = parse_jsonl_file(f)
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"
        assert "bug" in messages[0]["content"]

    def test_skips_user_messages(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text(
            make_jsonl_line("user", "Fix the bug") + "\n"
            + make_jsonl_line("assistant", "I found the issue") + "\n"
        )
        messages = parse_jsonl_file(f)
        assert len(messages) == 1
        assert messages[0]["role"] == "assistant"

    def test_skips_system_reminders(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text(
            make_jsonl_line("assistant", "Normal response") + "\n"
            + make_jsonl_line("assistant", "Response with <system-reminder> injected") + "\n"
        )
        messages = parse_jsonl_file(f)
        assert len(messages) == 1
        assert "Normal response" in messages[0]["content"]

    def test_empty_file(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text("")
        messages = parse_jsonl_file(f)
        assert messages == []

    def test_nonexistent_file(self):
        messages = parse_jsonl_file(Path("/nonexistent/file.jsonl"))
        assert messages == []


# =============================================================================
# list_recent_sessions Tests
# =============================================================================


class TestListRecentSessions:
    def _make_sessions(self):
        """Build sessions with controlled mtimes."""
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        return [
            make_session_info("recent-1", file_size=2000,
                              file_mtime=now - timedelta(days=1)),
            make_session_info("recent-2", file_size=3000,
                              file_mtime=now - timedelta(days=3)),
            make_session_info("old-1", file_size=2000,
                              file_mtime=now - timedelta(days=10)),
            make_session_info("small-1", file_size=500,
                              file_mtime=now - timedelta(days=1)),
        ]

    def test_filters_old_sessions(self):
        from indexing import list_recent_sessions
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(max_age_days=7)
            ids = {s.session_id for s in result}
            assert "recent-1" in ids
            assert "recent-2" in ids
            assert "old-1" not in ids

    def test_filters_small_sessions(self):
        from indexing import list_recent_sessions
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(max_age_days=7)
            ids = {s.session_id for s in result}
            assert "small-1" not in ids

    def test_excludes_session_id(self):
        from indexing import list_recent_sessions
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(
                max_age_days=7, exclude_session_id="recent-1"
            )
            ids = {s.session_id for s in result}
            assert "recent-1" not in ids
            assert "recent-2" in ids

    def test_default_window(self):
        from indexing import DEFAULT_RECENCY_WINDOW_DAYS
        assert DEFAULT_RECENCY_WINDOW_DAYS == 7

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
    @pytest.fixture()
    def env(self, tmp_path):
        projects_dir = tmp_path / "projects"
        memory_dir = tmp_path / "memory"
        index_file = memory_dir / "projects-index.json"
        with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
             mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
             mock.patch("indexing.get_projects_index_file", return_value=index_file):
            yield projects_dir, memory_dir, index_file

    def _setup_project(self, projects_dir, folder_name, original_path, entries):
        """Create a project folder with sessions-index.json."""
        folder = projects_dir / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        index_data = _make_sessions_index(original_path, entries)
        (folder / "sessions-index.json").write_text(
            json.dumps(index_data), encoding="utf-8"
        )

    def test_basic_project_discovery(self, env):
        """Smoke test: function runs and finds a project."""
        projects_dir, memory_dir, index_file = env

        self._setup_project(
            projects_dir,
            "-home-user-myproject",
            "/home/user/myproject",
            [_make_session_entry("s1", "2026-02-01T10:00:00Z")],
        )

        result = build_projects_index()

        projects = result["projects"]
        assert len(projects) == 1
        data = list(projects.values())[0]
        assert data["name"] == "myproject"
        assert "2026-02-01" in data["workDays"]

    def test_extracts_project_name_from_path(self, env):
        """Project name is the last component of originalPath."""
        projects_dir, memory_dir, index_file = env

        self._setup_project(
            projects_dir,
            "-home-user-swyfft-projects-tableau-agency-overview",
            "/home/user/swyfft/projects/tableau/agency-overview",
            [_make_session_entry("s1", "2026-02-06T10:00:00Z")],
        )

        result = build_projects_index()

        projects = result["projects"]
        data = list(projects.values())[0]
        assert data["name"] == "agency-overview"

    def test_multiple_work_days(self, env):
        """Sessions on different days produce multiple work days."""
        projects_dir, memory_dir, index_file = env

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

        result = build_projects_index()

        data = list(result["projects"].values())[0]
        assert data["workDays"] == ["2026-02-01", "2026-02-03"]

    def test_multiple_projects(self, env):
        """Discovers multiple projects from separate folders."""
        projects_dir, memory_dir, index_file = env

        self._setup_project(
            projects_dir, "-proj-a", "/home/user/proj-a",
            [_make_session_entry("s1", "2026-01-15T10:00:00Z")],
        )
        self._setup_project(
            projects_dir, "-proj-b", "/home/user/proj-b",
            [_make_session_entry("s2", "2026-01-20T10:00:00Z")],
        )

        result = build_projects_index()

        assert len(result["projects"]) == 2
        names = {d["name"] for d in result["projects"].values()}
        assert names == {"proj-a", "proj-b"}

    def test_skips_folder_without_sessions_index(self, env):
        """Folders without sessions-index.json are ignored."""
        projects_dir, memory_dir, index_file = env

        # Folder with no sessions-index.json
        (projects_dir / "empty-folder").mkdir(parents=True)
        # Folder with sessions-index.json
        self._setup_project(
            projects_dir, "-real-proj", "/home/user/real-proj",
            [_make_session_entry("s1", "2026-02-01T10:00:00Z")],
        )

        result = build_projects_index()

        assert len(result["projects"]) == 1

    def test_skips_entries_without_created(self, env):
        """Entries missing created timestamp are skipped."""
        projects_dir, memory_dir, index_file = env

        entry_no_created = {
            "sessionId": "s1",
            "fullPath": "/tmp/s1.jsonl",
            "fileMtime": 1770000000000,
        }
        self._setup_project(
            projects_dir, "-proj", "/home/user/proj",
            [entry_no_created],
        )

        result = build_projects_index()

        # No work days extracted -> project skipped
        assert len(result["projects"]) == 0

    def test_empty_projects_dir(self, env):
        """Empty projects directory returns no projects."""
        projects_dir, memory_dir, index_file = env
        projects_dir.mkdir()

        result = build_projects_index()

        assert len(result["projects"]) == 0

    def test_nonexistent_projects_dir(self, env):
        """Missing projects directory returns empty result."""
        result = build_projects_index()

        assert result == {"projects": {}}

    def test_case_insensitive_path_merging(self, env):
        """Same project path with different cases merges work days."""
        projects_dir, memory_dir, index_file = env

        self._setup_project(
            projects_dir, "-folder-a", "/home/user/MyProject",
            [_make_session_entry("s1", "2026-02-01T10:00:00Z")],
        )
        self._setup_project(
            projects_dir, "-folder-b", "/home/user/myproject",
            [_make_session_entry("s2", "2026-02-03T10:00:00Z")],
        )

        result = build_projects_index()

        assert len(result["projects"]) == 1
        data = list(result["projects"].values())[0]
        assert len(data["workDays"]) == 2
        assert len(data["encodedPaths"]) == 2

    def test_writes_index_file(self, env):
        """Result is written to projects-index.json."""
        projects_dir, memory_dir, index_file = env

        self._setup_project(
            projects_dir, "-proj", "/home/user/proj",
            [_make_session_entry("s1", "2026-02-01T10:00:00Z")],
        )

        build_projects_index()

        assert index_file.exists()
        saved = json.loads(index_file.read_text(encoding="utf-8"))
        assert saved["version"] == 1
        assert "lastUpdated" in saved
        assert len(saved["projects"]) == 1

    def test_fallback_to_entry_project_path(self, env):
        """Uses entries[0].projectPath when root originalPath is missing."""
        projects_dir, memory_dir, index_file = env

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

        result = build_projects_index()

        data = list(result["projects"].values())[0]
        assert data["name"] == "fallback-proj"

    # --- JSONL fallback tests ---

    def _make_jsonl_file(self, folder, session_id, cwd, timestamp):
        """Create a JSONL file with cwd and timestamp in first message."""
        line = json.dumps({
            "cwd": cwd,
            "timestamp": timestamp,
            "type": "user",
            "sessionId": session_id,
            "message": {"role": "user", "content": "hello"},
        })
        jsonl_path = folder / f"{session_id}.jsonl"
        jsonl_path.write_text(line + "\n", encoding="utf-8")
        return jsonl_path

    def test_jsonl_fallback_discovers_project_without_sessions_index(self, env):
        """Folders with JSONL files but no sessions-index.json are discovered."""
        projects_dir, memory_dir, index_file = env

        # Folder with JSONL but NO sessions-index.json
        folder = projects_dir / "-home-user-swyfft-projects-tableau"
        folder.mkdir(parents=True)
        self._make_jsonl_file(
            folder, "sess-1",
            "/home/user/swyfft/projects/tableau",
            "2026-02-12T10:00:00Z",
        )

        result = build_projects_index()

        assert len(result["projects"]) == 1
        data = list(result["projects"].values())[0]
        assert data["name"] == "tableau"
        assert data["originalPath"] == "/home/user/swyfft/projects/tableau"
        assert "2026-02-12" in data["workDays"]

    def test_jsonl_fallback_extracts_multiple_work_days(self, env):
        """Multiple JSONL files produce multiple work days."""
        projects_dir, memory_dir, index_file = env

        folder = projects_dir / "-home-user-proj"
        folder.mkdir(parents=True)
        self._make_jsonl_file(folder, "s1", "/home/user/proj", "2026-02-10T10:00:00Z")
        self._make_jsonl_file(folder, "s2", "/home/user/proj", "2026-02-11T14:00:00Z")
        self._make_jsonl_file(folder, "s3", "/home/user/proj", "2026-02-12T09:00:00Z")

        result = build_projects_index()

        data = list(result["projects"].values())[0]
        assert data["workDays"] == ["2026-02-10", "2026-02-11", "2026-02-12"]

    def test_jsonl_supplements_sessions_index_work_days(self, env):
        """JSONL files add work days that sessions-index.json missed."""
        projects_dir, memory_dir, index_file = env

        # Sessions-index with only one day
        self._setup_project(
            projects_dir,
            "-home-user-proj",
            "/home/user/proj",
            [_make_session_entry("s1", "2026-02-01T10:00:00Z")],
        )

        # Additional JSONL files for days not in the index
        folder = projects_dir / "-home-user-proj"
        self._make_jsonl_file(folder, "s2", "/home/user/proj", "2026-02-05T10:00:00Z")
        self._make_jsonl_file(folder, "s3", "/home/user/proj", "2026-02-10T10:00:00Z")

        result = build_projects_index()

        data = list(result["projects"].values())[0]
        assert "2026-02-01" in data["workDays"]  # from sessions-index
        assert "2026-02-05" in data["workDays"]  # from JSONL
        assert "2026-02-10" in data["workDays"]  # from JSONL

    def test_jsonl_fallback_skips_empty_files(self, env):
        """Empty or tiny JSONL files are skipped."""
        projects_dir, memory_dir, index_file = env

        folder = projects_dir / "-home-user-proj"
        folder.mkdir(parents=True)
        # Empty file
        (folder / "empty.jsonl").write_text("", encoding="utf-8")
        # Valid file
        self._make_jsonl_file(folder, "valid", "/home/user/proj", "2026-02-12T10:00:00Z")

        result = build_projects_index()

        assert len(result["projects"]) == 1
        data = list(result["projects"].values())[0]
        assert data["workDays"] == ["2026-02-12"]

    def test_jsonl_fallback_handles_malformed_json(self, env):
        """Malformed JSONL lines are skipped gracefully."""
        projects_dir, memory_dir, index_file = env

        folder = projects_dir / "-home-user-proj"
        folder.mkdir(parents=True)
        (folder / "bad.jsonl").write_text("not valid json\n", encoding="utf-8")
        self._make_jsonl_file(folder, "good", "/home/user/proj", "2026-02-12T10:00:00Z")

        result = build_projects_index()

        assert len(result["projects"]) == 1

    def test_jsonl_only_folder_no_cwd_is_skipped(self, env):
        """JSONL files without cwd field cannot determine project path."""
        projects_dir, memory_dir, index_file = env

        folder = projects_dir / "-home-user-proj"
        folder.mkdir(parents=True)
        # JSONL with no cwd
        line = json.dumps({"type": "user", "timestamp": "2026-02-12T10:00:00Z"})
        (folder / "no-cwd.jsonl").write_text(line + "\n", encoding="utf-8")

        result = build_projects_index()

        assert len(result["projects"]) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
