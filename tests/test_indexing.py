#!/usr/bin/env python3
"""
Unit tests for indexing.py

Run with: python -m pytest tests/test_indexing.py -v
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest
from helpers import make_jsonl_line, make_session_info  # noqa: I001
from indexing import (
    MIN_SESSION_SIZE_BYTES,
    _extract_from_jsonl,
    _parse_index_datetime,
    build_projects_index,
    get_session_date,
    has_assistant_message,
    list_all_sessions,
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
        # mtime is set to now in the helper; get_session_date converts to local tz
        today = datetime.now().strftime("%Y-%m-%d")
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

    def test_worktree_session_merges_into_parent_project(self, env):
        """A session whose CWD is a worktree should merge into the main repo project."""
        projects_dir, memory_dir, index_file = env

        # Main repo project folder
        main_folder = projects_dir / "main-repo"
        main_folder.mkdir(parents=True)
        sessions_index = {
            "originalPath": "/home/user/myproject",
            "entries": [{"created": "2026-01-15T10:00:00Z", "projectPath": "/home/user/myproject"}],
        }
        (main_folder / "sessions-index.json").write_text(json.dumps(sessions_index))

        # Worktree session folder (different encoded name, worktree CWD)
        wt_folder = projects_dir / "worktree-feature"
        wt_folder.mkdir()
        wt_sessions = {
            "originalPath": "/home/user/myproject/.worktrees/feature",
            "entries": [{"created": "2026-01-16T10:00:00Z", "projectPath": "/home/user/myproject/.worktrees/feature"}],
        }
        (wt_folder / "sessions-index.json").write_text(json.dumps(wt_sessions))

        with mock.patch("indexing.resolve_session_path") as mock_resolve:
            # Worktree path resolves to main repo
            def side_effect(p):
                if ".worktrees" in p:
                    return "/home/user/myproject"
                return p
            mock_resolve.side_effect = side_effect

            index = build_projects_index()

        projects = index["projects"]
        canonical = "/home/user/myproject"

        assert canonical in projects
        assert len(projects) == 1, f"Expected 1 project, got {len(projects)}: {list(projects.keys())}"
        assert "2026-01-15" in projects[canonical]["workDays"]
        assert "2026-01-16" in projects[canonical]["workDays"]


# =============================================================================
# _parse_index_datetime Tests
# =============================================================================


class TestParseIndexDatetime:
    def test_valid_iso_datetime_returns_datetime(self):
        """Valid ISO datetime string with Z suffix returns datetime object."""
        result = _parse_index_datetime("2026-01-25T21:48:21.826Z")
        assert isinstance(result, datetime)
        assert result.year == 2026
        assert result.month == 1
        assert result.day == 25
        assert result.hour == 21
        assert result.minute == 48
        assert result.tzinfo is not None

    def test_valid_iso_datetime_with_offset(self):
        """Valid ISO datetime string with +00:00 suffix returns datetime object."""
        result = _parse_index_datetime("2026-03-10T14:30:00+00:00")
        assert isinstance(result, datetime)
        assert result.year == 2026
        assert result.month == 3

    def test_empty_string_returns_none(self):
        """Empty string returns None."""
        assert _parse_index_datetime("") is None

    def test_invalid_string_returns_none(self):
        """Invalid/unparseable string returns None."""
        assert _parse_index_datetime("not-a-date") is None

    def test_partial_date_returns_none(self):
        """Partial date string that can't parse returns None."""
        assert _parse_index_datetime("2026-13-45") is None


# =============================================================================
# _extract_from_jsonl Tests
# =============================================================================


class TestExtractFromJsonl:
    def test_valid_jsonl_with_cwd(self, tmp_path):
        """Valid .jsonl file with cwd in first line returns path and work days."""
        folder = tmp_path / "project"
        folder.mkdir()
        line = json.dumps({
            "cwd": "/home/user/myproject",
            "timestamp": "2026-02-15T10:30:00Z",
            "type": "user",
        })
        (folder / "session1.jsonl").write_text(line + "\n", encoding="utf-8")

        original_path, work_days = _extract_from_jsonl(folder)

        assert original_path == "/home/user/myproject"
        assert "2026-02-15" in work_days

    def test_multiple_jsonl_files_first_cwd_wins(self, tmp_path):
        """First valid cwd found across sorted files is used as original_path."""
        folder = tmp_path / "project"
        folder.mkdir()

        line_a = json.dumps({
            "cwd": "/home/user/project-a",
            "timestamp": "2026-02-10T10:00:00Z",
        })
        (folder / "aaa.jsonl").write_text(line_a + "\n", encoding="utf-8")

        line_b = json.dumps({
            "cwd": "/home/user/project-b",
            "timestamp": "2026-02-11T10:00:00Z",
        })
        (folder / "bbb.jsonl").write_text(line_b + "\n", encoding="utf-8")

        original_path, work_days = _extract_from_jsonl(folder)

        assert original_path == "/home/user/project-a"
        assert "2026-02-10" in work_days
        assert "2026-02-11" in work_days

    def test_missing_cwd_field(self, tmp_path):
        """File with no cwd field returns empty path."""
        folder = tmp_path / "project"
        folder.mkdir()
        line = json.dumps({
            "type": "user",
            "timestamp": "2026-02-15T10:00:00Z",
        })
        (folder / "session.jsonl").write_text(line + "\n", encoding="utf-8")

        original_path, work_days = _extract_from_jsonl(folder)

        assert original_path == ""
        assert "2026-02-15" in work_days

    def test_empty_file(self, tmp_path):
        """Empty file is skipped gracefully."""
        folder = tmp_path / "project"
        folder.mkdir()
        (folder / "empty.jsonl").write_text("", encoding="utf-8")

        original_path, work_days = _extract_from_jsonl(folder)

        assert original_path == ""
        assert len(work_days) == 0

    def test_malformed_json(self, tmp_path):
        """Malformed JSON line is skipped gracefully."""
        folder = tmp_path / "project"
        folder.mkdir()
        (folder / "bad.jsonl").write_text("not valid json\n", encoding="utf-8")

        original_path, work_days = _extract_from_jsonl(folder)

        assert original_path == ""
        assert len(work_days) == 0

    def test_no_jsonl_files(self, tmp_path):
        """Folder with no .jsonl files returns empty results."""
        folder = tmp_path / "project"
        folder.mkdir()
        (folder / "readme.txt").write_text("hello", encoding="utf-8")

        original_path, work_days = _extract_from_jsonl(folder)

        assert original_path == ""
        assert len(work_days) == 0

    def test_missing_timestamp_still_extracts_cwd(self, tmp_path):
        """File with cwd but no timestamp still extracts the path."""
        folder = tmp_path / "project"
        folder.mkdir()
        line = json.dumps({"cwd": "/home/user/proj"})
        (folder / "session.jsonl").write_text(line + "\n", encoding="utf-8")

        original_path, work_days = _extract_from_jsonl(folder)

        assert original_path == "/home/user/proj"
        assert len(work_days) == 0

    def test_cwd_on_later_line(self, tmp_path):
        """cwd on a non-first line is found (e.g., file-history-snapshot first)."""
        folder = tmp_path / "project"
        folder.mkdir()
        lines = [
            json.dumps({"type": "file-history-snapshot", "snapshot": {}}),
            json.dumps({
                "cwd": "/home/user/myproject",
                "timestamp": "2026-02-12T17:35:00Z",
                "type": "progress",
            }),
        ]
        (folder / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        original_path, work_days = _extract_from_jsonl(folder)

        assert original_path == "/home/user/myproject"
        assert "2026-02-12" in work_days

    def test_timestamp_on_first_line_cwd_on_later(self, tmp_path):
        """Extracts timestamp from line 1 and cwd from a later line."""
        folder = tmp_path / "project"
        folder.mkdir()
        lines = [
            json.dumps({"type": "snapshot", "timestamp": "2026-03-01T10:00:00Z"}),
            json.dumps({"type": "other"}),
            json.dumps({"cwd": "/home/user/proj", "type": "user"}),
        ]
        (folder / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

        original_path, work_days = _extract_from_jsonl(folder)

        assert original_path == "/home/user/proj"
        assert "2026-03-01" in work_days


# =============================================================================
# list_all_sessions Tests
# =============================================================================


class TestListAllSessions:
    def test_finds_sessions_from_jsonl_files(self, tmp_path):
        """Discovers sessions from .jsonl files in project directories."""
        projects_dir = tmp_path / "projects"
        project_folder = projects_dir / "-home-user-myproject"
        project_folder.mkdir(parents=True)

        line = json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}})
        (project_folder / "sess-abc.jsonl").write_text(line + "\n", encoding="utf-8")
        (project_folder / "sess-def.jsonl").write_text(line + "\n", encoding="utf-8")

        with mock.patch("indexing.get_projects_dir", return_value=projects_dir):
            sessions = list_all_sessions()

        assert len(sessions) == 2
        session_ids = {s.session_id for s in sessions}
        assert "sess-abc" in session_ids
        assert "sess-def" in session_ids

    def test_skips_subagent_files(self, tmp_path):
        """Files with 'subagent' in the name are excluded."""
        projects_dir = tmp_path / "projects"
        project_folder = projects_dir / "-home-user-proj"
        project_folder.mkdir(parents=True)

        line = json.dumps({"type": "user"})
        (project_folder / "normal-session.jsonl").write_text(line + "\n", encoding="utf-8")
        (project_folder / "subagent-123.jsonl").write_text(line + "\n", encoding="utf-8")
        (project_folder / "task-subagent-456.jsonl").write_text(line + "\n", encoding="utf-8")

        with mock.patch("indexing.get_projects_dir", return_value=projects_dir):
            sessions = list_all_sessions()

        assert len(sessions) == 1
        assert sessions[0].session_id == "normal-session"

    def test_sorted_by_mtime_descending(self, tmp_path):
        """Sessions are sorted by file modification time, newest first."""
        import time

        projects_dir = tmp_path / "projects"
        project_folder = projects_dir / "-home-user-proj"
        project_folder.mkdir(parents=True)

        line = json.dumps({"type": "user"})

        older_file = project_folder / "older.jsonl"
        older_file.write_text(line + "\n", encoding="utf-8")
        old_mtime = time.time() - 100
        os.utime(older_file, (old_mtime, old_mtime))

        newer_file = project_folder / "newer.jsonl"
        newer_file.write_text(line + "\n", encoding="utf-8")

        with mock.patch("indexing.get_projects_dir", return_value=projects_dir):
            sessions = list_all_sessions()

        assert len(sessions) == 2
        assert sessions[0].session_id == "newer"
        assert sessions[1].session_id == "older"

    def test_returns_empty_for_nonexistent_dir(self, tmp_path):
        """Returns empty list when projects directory doesn't exist."""
        nonexistent = tmp_path / "does-not-exist"
        with mock.patch("indexing.get_projects_dir", return_value=nonexistent):
            sessions = list_all_sessions()
        assert sessions == []

    def test_populates_session_info_fields(self, tmp_path):
        """SessionInfo objects have correct fields populated."""
        projects_dir = tmp_path / "projects"
        project_folder = projects_dir / "-home-user-proj"
        project_folder.mkdir(parents=True)

        line = json.dumps({"type": "user"})
        (project_folder / "test-sess.jsonl").write_text(line + "\n", encoding="utf-8")

        with mock.patch("indexing.get_projects_dir", return_value=projects_dir):
            sessions = list_all_sessions()

        assert len(sessions) == 1
        s = sessions[0]
        assert s.session_id == "test-sess"
        assert s.project_hash == "-home-user-proj"
        assert s.file_size > 0
        assert s.file_mtime is not None
        assert s.transcript_path == project_folder / "test-sess.jsonl"

    def test_enriches_from_sessions_index(self, tmp_path):
        """Session metadata is enriched from sessions-index.json when available."""
        projects_dir = tmp_path / "projects"
        project_folder = projects_dir / "-home-user-proj"
        project_folder.mkdir(parents=True)

        line = json.dumps({"type": "user"})
        (project_folder / "sess-enriched.jsonl").write_text(line + "\n", encoding="utf-8")

        sessions_index = {
            "version": 1,
            "originalPath": "/home/user/proj",
            "entries": [
                {
                    "sessionId": "sess-enriched",
                    "created": "2026-02-20T14:00:00Z",
                    "summary": "Worked on feature X",
                }
            ],
        }
        (project_folder / "sessions-index.json").write_text(
            json.dumps(sessions_index), encoding="utf-8"
        )

        with mock.patch("indexing.get_projects_dir", return_value=projects_dir):
            sessions = list_all_sessions()

        assert len(sessions) == 1
        s = sessions[0]
        assert s.project_path == "/home/user/proj"
        assert s.summary == "Worked on feature X"
        assert s.created is not None
        assert s.created.year == 2026
        assert s.created.month == 2
        assert s.created.day == 20

    def test_multiple_project_folders(self, tmp_path):
        """Sessions from multiple project folders are all discovered."""
        projects_dir = tmp_path / "projects"

        for name in ["-proj-a", "-proj-b"]:
            folder = projects_dir / name
            folder.mkdir(parents=True)
            line = json.dumps({"type": "user"})
            (folder / f"sess-{name}.jsonl").write_text(line + "\n", encoding="utf-8")

        with mock.patch("indexing.get_projects_dir", return_value=projects_dir):
            sessions = list_all_sessions()

        assert len(sessions) == 2

    def test_skips_non_directory_entries(self, tmp_path):
        """Files directly under projects_dir (not directories) are skipped."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir(parents=True)
        (projects_dir / "stray-file.txt").write_text("hello", encoding="utf-8")

        project_folder = projects_dir / "-home-user-proj"
        project_folder.mkdir()
        line = json.dumps({"type": "user"})
        (project_folder / "sess.jsonl").write_text(line + "\n", encoding="utf-8")

        with mock.patch("indexing.get_projects_dir", return_value=projects_dir):
            sessions = list_all_sessions()

        assert len(sessions) == 1


# =============================================================================
# Working-Day Session Filtering Tests (B1)
# =============================================================================


class TestListRecentSessionsWorkingDays:
    """Test working-day mode in list_recent_sessions."""

    def test_working_day_filtering(self):
        """Sessions on 7 active dates across 20 calendar days all included."""
        from indexing import list_recent_sessions
        from memory_utils import DEFAULT_SETTINGS

        n_days = DEFAULT_SETTINGS["synthesis"]["recentWorkingDays"]
        dates = ["2026-03-25", "2026-03-23", "2026-03-20", "2026-03-18",
                 "2026-03-15", "2026-03-10", "2026-03-05"]
        sessions = [make_session_info(f"s{i}") for i in range(len(dates))]

        with mock.patch("indexing.list_all_sessions", return_value=sessions), \
             mock.patch("indexing.get_global_working_days", return_value=dates[:n_days]) as mock_gwd, \
             mock.patch("indexing.load_settings", return_value=DEFAULT_SETTINGS), \
             mock.patch("indexing.get_session_date", side_effect=dates):
            result = list_recent_sessions()

        mock_gwd.assert_called_once_with(n_days)
        assert len(result) == n_days

    def test_max_age_days_none_returns_all(self):
        """max_age_days=None returns all sessions (backfill mode)."""
        from indexing import list_recent_sessions

        sessions = [make_session_info(f"s{i}") for i in range(10)]
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(max_age_days=None)
        assert len(result) == 10

    def test_max_age_days_int_uses_calendar(self):
        """Explicit int uses calendar-day cutoff, not working days."""
        from indexing import list_recent_sessions

        sessions = [make_session_info("s1")]
        with mock.patch("indexing.list_all_sessions", return_value=sessions), \
             mock.patch("indexing.get_global_working_days") as mock_gwd:
            list_recent_sessions(max_age_days=10)

        mock_gwd.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
