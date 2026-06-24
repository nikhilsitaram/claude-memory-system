#!/usr/bin/env python3
"""
Unit tests for indexing.py

Run with: python -m pytest tests/test_indexing.py -v
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
from helpers import make_jsonl_line, make_session_info  # noqa: I001
from indexing import (
    DEFAULT_RECENCY_WINDOW_DAYS,
    MIN_SESSION_SIZE_BYTES,
    _extract_session_metadata,
    _suggest_path_correction,
    build_projects_index,
    get_session_date,
    has_assistant_message,
    list_all_sessions,
    list_recent_sessions,
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
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(max_age_days=7)
            ids = {s.session_id for s in result}
            assert "recent-1" in ids
            assert "recent-2" in ids
            assert "old-1" not in ids

    def test_filters_small_sessions(self):
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(max_age_days=7)
            ids = {s.session_id for s in result}
            assert "small-1" not in ids

    def test_excludes_session_id(self):
        sessions = self._make_sessions()
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(
                max_age_days=7, exclude_session_id="recent-1"
            )
            ids = {s.session_id for s in result}
            assert "recent-1" not in ids
            assert "recent-2" in ids

    def test_prefers_created_over_mtime(self):
        """Migrated sessions with stale mtime are included if created is recent."""
        now = datetime.now(timezone.utc)
        sessions = [
            # Old mtime (migrated), but recent created (real session date)
            make_session_info("migrated-1", file_size=2000,
                              file_mtime=now - timedelta(days=20),
                              created=now - timedelta(days=3)),
            # Old mtime AND old created — should be excluded
            make_session_info("truly-old", file_size=2000,
                              file_mtime=now - timedelta(days=20),
                              created=now - timedelta(days=20)),
        ]
        with mock.patch("indexing.list_all_sessions", return_value=sessions):
            result = list_recent_sessions(max_age_days=7)
            ids = {s.session_id for s in result}
            assert "migrated-1" in ids
            assert "truly-old" not in ids

    def test_default_window(self):
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
# _extract_session_metadata Tests
# =============================================================================


class TestExtractSessionMetadata:
    """Recover (created, summary) from a transcript without sessions-index.json."""

    def _write(self, tmp_path, *records):
        f = tmp_path / "sess.jsonl"
        f.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        return f

    def test_ai_title_preferred_as_summary(self, tmp_path):
        f = self._write(
            tmp_path,
            {"type": "user", "timestamp": "2026-03-01T09:00:00Z",
             "message": {"role": "user", "content": "first prompt text"}},
            {"type": "ai-title", "aiTitle": "Refactor the indexer"},
        )
        created, summary = _extract_session_metadata(f)
        assert created == datetime(2026, 3, 1, 9, 0, tzinfo=timezone.utc)
        assert summary == "Refactor the indexer"

    def test_falls_back_to_first_user_prompt(self, tmp_path):
        f = self._write(
            tmp_path,
            {"type": "user", "timestamp": "2026-03-02T08:30:00Z",
             "message": {"role": "user", "content": "investigate gh issue 146"}},
        )
        created, summary = _extract_session_metadata(f)
        assert created == datetime(2026, 3, 2, 8, 30, tzinfo=timezone.utc)
        assert summary == "investigate gh issue 146"

    def test_skips_meta_and_wrapper_prompts(self, tmp_path):
        f = self._write(
            tmp_path,
            {"type": "user", "timestamp": "2026-03-03T08:00:00Z", "isMeta": True,
             "message": {"role": "user", "content": "meta noise"}},
            {"type": "user",
             "message": {"role": "user", "content": "<command-name>/plugin</command-name>"}},
            {"type": "user",
             "message": {"role": "user", "content": "<local-command-caveat>x</local-command-caveat>"}},
            {"type": "user",
             "message": {"role": "user", "content": "the real question"}},
        )
        _created, summary = _extract_session_metadata(f)
        assert summary == "the real question"

    def test_list_content_blocks_flattened(self, tmp_path):
        f = self._write(
            tmp_path,
            {"type": "user", "timestamp": "2026-03-04T08:00:00Z",
             "message": {"role": "user",
                         "content": [{"type": "text", "text": "block one"},
                                     {"type": "text", "text": "block two"}]}},
        )
        _created, summary = _extract_session_metadata(f)
        assert summary == "block one block two"

    def test_empty_file_returns_none(self, tmp_path):
        f = tmp_path / "sess.jsonl"
        f.write_text("", encoding="utf-8")
        assert _extract_session_metadata(f) == (None, None)


# =============================================================================
# list_all_sessions Tests
# =============================================================================


class TestListAllSessions:
    """list_all_sessions recovers created/summary from transcripts (no index)."""

    def test_recovers_created_and_summary_without_index(self, tmp_path):
        projects_dir = tmp_path / "projects"
        folder = projects_dir / "-home-user-proj"
        folder.mkdir(parents=True)
        (folder / "abc.jsonl").write_text(
            json.dumps({"type": "user", "timestamp": "2026-04-01T12:00:00Z",
                        "cwd": "/home/user/proj",
                        "message": {"role": "user", "content": "do the thing"}}) + "\n"
            + json.dumps({"type": "ai-title", "aiTitle": "Do the thing"}) + "\n",
            encoding="utf-8",
        )

        with mock.patch("indexing.get_projects_dir", return_value=projects_dir):
            sessions = list_all_sessions()

        assert len(sessions) == 1
        s = sessions[0]
        assert s.session_id == "abc"
        assert s.created == datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
        assert s.summary == "Do the thing"

    def test_index_overrides_transcript_when_present(self, tmp_path):
        projects_dir = tmp_path / "projects"
        folder = projects_dir / "-home-user-proj"
        folder.mkdir(parents=True)
        (folder / "abc.jsonl").write_text(
            json.dumps({"type": "user", "timestamp": "2026-04-01T12:00:00Z",
                        "cwd": "/home/user/proj",
                        "message": {"role": "user", "content": "transcript prompt"}}) + "\n",
            encoding="utf-8",
        )
        (folder / "sessions-index.json").write_text(json.dumps({
            "originalPath": "/home/user/proj",
            "entries": [{"sessionId": "abc", "created": "2026-04-01T08:00:00Z",
                         "summary": "Index summary"}],
        }), encoding="utf-8")

        with mock.patch("indexing.get_projects_dir", return_value=projects_dir):
            sessions = list_all_sessions()

        s = sessions[0]
        assert s.created == datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)
        assert s.summary == "Index summary"
        assert s.project_path == "/home/user/proj"


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

    def test_transcript_cwd_wins_over_stale_index_path(self, env):
        """When both exist, the path comes from the transcript cwd, not the
        (possibly stale) sessions-index.json originalPath; index work days are
        still unioned in."""
        projects_dir, memory_dir, index_file = env

        # sessions-index.json records a STALE original path + one work day.
        self._setup_project(
            projects_dir,
            "-home-user-proj",
            "/home/user/STALE-old-path",
            [_make_session_entry("s1", "2026-02-01T10:00:00Z")],
        )
        # The transcript carries the real current cwd + a different work day.
        folder = projects_dir / "-home-user-proj"
        self._make_jsonl_file(folder, "s2", "/home/user/proj", "2026-02-05T10:00:00Z")

        result = build_projects_index()

        assert len(result["projects"]) == 1
        data = list(result["projects"].values())[0]
        assert data["originalPath"] == "/home/user/proj"  # cwd wins
        assert data["name"] == "proj"
        assert "2026-02-01" in data["workDays"]  # index day still unioned
        assert "2026-02-05" in data["workDays"]  # transcript day

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


    def test_stale_path_merges_into_live_entry_with_same_name(self, env):
        """Stale WSL path merges workDays into live macOS entry with same project name."""
        projects_dir, memory_dir, index_file = env

        # Stale entry (WSL path, doesn't exist on filesystem)
        self._setup_project(
            projects_dir, "-wsl-home-user-myproject", "/home/user/myproject",
            [
                _make_session_entry("s1", "2026-01-10T10:00:00Z"),
                _make_session_entry("s2", "2026-01-12T10:00:00Z"),
            ],
        )

        # Live entry (macOS path, exists on filesystem)
        live_path = str(env[0].parent / "myproject")
        Path(live_path).mkdir(parents=True, exist_ok=True)
        self._setup_project(
            projects_dir, "-mac-user-myproject", live_path,
            [_make_session_entry("s3", "2026-02-01T10:00:00Z")],
        )

        result = build_projects_index()

        assert len(result["projects"]) == 1
        data = list(result["projects"].values())[0]
        assert data["name"] == "myproject"
        assert data["originalPath"] == live_path
        assert len(data["workDays"]) == 3
        assert "2026-01-10" in data["workDays"]
        assert "2026-02-01" in data["workDays"]
        assert len(data["encodedPaths"]) == 2

    def test_stale_path_not_merged_when_multiple_live_entries_share_name(self, env):
        """Stale entry is NOT merged when two live repos share the same basename."""
        projects_dir, memory_dir, index_file = env

        # Two distinct live projects both named "api"
        live_path_a = str(env[0].parent / "team-a" / "api")
        Path(live_path_a).mkdir(parents=True, exist_ok=True)
        self._setup_project(
            projects_dir, "-team-a-api", live_path_a,
            [_make_session_entry("s1", "2026-01-10T10:00:00Z")],
        )

        live_path_b = str(env[0].parent / "team-b" / "api")
        Path(live_path_b).mkdir(parents=True, exist_ok=True)
        self._setup_project(
            projects_dir, "-team-b-api", live_path_b,
            [_make_session_entry("s2", "2026-02-01T10:00:00Z")],
        )

        # Stale entry also named "api" (WSL path)
        self._setup_project(
            projects_dir, "-wsl-api", "/home/user/api",
            [_make_session_entry("s3", "2026-03-01T10:00:00Z")],
        )

        result = build_projects_index()

        # All three should remain separate — no merge into either live entry
        assert len(result["projects"]) == 3
        for data in result["projects"].values():
            if data["originalPath"] in (live_path_a, live_path_b):
                assert "2026-03-01" not in data["workDays"]

    def test_stale_path_kept_when_no_live_entry_matches(self, env):
        """Stale entry without a live counterpart is preserved (not silently dropped)."""
        projects_dir, memory_dir, index_file = env

        self._setup_project(
            projects_dir, "-wsl-proj", "/home/user/unique-project",
            [_make_session_entry("s1", "2026-01-15T10:00:00Z")],
        )

        result = build_projects_index()

        assert len(result["projects"]) == 1
        data = list(result["projects"].values())[0]
        assert data["name"] == "unique-project"


class TestSuggestPathCorrection:
    """Tests for _suggest_path_correction fuzzy matching."""

    def test_jsonl_cwd_strategy(self, tmp_path):
        """Strategy 1: JSONL cwd from encoded folder matches."""
        live_dir = tmp_path / "myproject"
        live_dir.mkdir()
        project_data = {"encodedPaths": ["enc-a"], "name": "myproject"}
        jsonl_paths = {"enc-a": str(live_dir)}

        suggested, strategy = _suggest_path_correction(
            "/home/old/myproject", project_data, jsonl_paths,
        )
        assert suggested == str(live_dir)
        assert strategy == "JSONL cwd"

    def test_jsonl_cwd_nonexistent_skipped(self, tmp_path):
        """JSONL cwd that doesn't exist on disk is skipped."""
        project_data = {"encodedPaths": ["enc-a"], "name": "myproject"}
        jsonl_paths = {"enc-a": "/nonexistent/path/myproject"}

        suggested, strategy = _suggest_path_correction(
            "/home/old/myproject", project_data, jsonl_paths,
        )
        # Should fall through to other strategies (which also won't match)
        assert suggested is None

    def test_home_substitution_strategy(self, tmp_path):
        """Strategy 2: /home/<user>/ replaced with $HOME/."""
        live_dir = tmp_path / "myproject"
        live_dir.mkdir()
        project_data = {"encodedPaths": [], "name": "myproject"}

        with mock.patch("indexing.Path.home", return_value=tmp_path):
            suggested, strategy = _suggest_path_correction(
                "/home/olduser/myproject", project_data, {},
            )
        assert suggested == str(live_dir)
        assert strategy == "home directory match"

    def test_basename_scan_strategy_direct_child(self, tmp_path):
        """Strategy 3: basename found as direct child of $HOME."""
        live_dir = tmp_path / "myproject"
        live_dir.mkdir()
        project_data = {"encodedPaths": [], "name": "myproject"}

        with mock.patch("indexing.Path.home", return_value=tmp_path):
            suggested, strategy = _suggest_path_correction(
                "/some/other/path/myproject", project_data, {},
            )
        assert suggested == str(live_dir)
        assert strategy is not None and "basename match" in strategy

    def test_basename_scan_strategy_grandchild(self, tmp_path):
        """Strategy 3: basename found as grandchild of $HOME."""
        parent = tmp_path / "personal"
        parent.mkdir()
        live_dir = parent / "myproject"
        live_dir.mkdir()
        project_data = {"encodedPaths": [], "name": "myproject"}

        with mock.patch("indexing.Path.home", return_value=tmp_path):
            suggested, strategy = _suggest_path_correction(
                "/some/other/path/myproject", project_data, {},
            )
        assert suggested == str(live_dir)
        assert strategy is not None and "basename match" in strategy

    def test_no_match_returns_none(self, tmp_path):
        """No strategies match -> (None, None)."""
        project_data = {"encodedPaths": [], "name": "nonexistent-proj"}
        with mock.patch("indexing.Path.home", return_value=tmp_path):
            suggested, strategy = _suggest_path_correction(
                "/home/old/nonexistent-proj", project_data, {},
            )
        assert suggested is None
        assert strategy is None

    def test_priority_order_jsonl_over_home_sub(self, tmp_path):
        """JSONL cwd is preferred over home substitution when both match."""
        jsonl_dir = tmp_path / "jsonl-location" / "myproject"
        jsonl_dir.mkdir(parents=True)
        home_dir = tmp_path / "home-location"
        home_dir.mkdir()
        (home_dir / "myproject").mkdir()

        project_data = {"encodedPaths": ["enc-a"], "name": "myproject"}
        jsonl_paths = {"enc-a": str(jsonl_dir)}

        with mock.patch("indexing.Path.home", return_value=home_dir):
            suggested, strategy = _suggest_path_correction(
                "/home/old/myproject", project_data, jsonl_paths,
            )
        assert suggested == str(jsonl_dir)
        assert strategy == "JSONL cwd"


class TestExtractFromJsonlMultilineScan:
    """Tests that _extract_from_jsonl scans beyond the first line for cwd/timestamp."""

    def test_cwd_on_second_line(self, tmp_path):
        """cwd found on line 2 when line 1 is a preamble record without cwd."""
        from indexing import _extract_from_jsonl

        folder = tmp_path / "project"
        folder.mkdir()

        lines = [
            json.dumps({"type": "file-history-snapshot", "data": "..."}),
            json.dumps({"type": "attachment", "cwd": "/Users/me/myproject", "timestamp": "2026-04-25T12:00:00Z"}),
        ]
        (folder / "session.jsonl").write_text("\n".join(lines) + "\n")

        original_path, work_days = _extract_from_jsonl(folder)
        assert original_path == "/Users/me/myproject"
        assert "2026-04-25" in work_days

    def test_timestamp_on_first_line_cwd_on_later_line(self, tmp_path):
        """timestamp captured from first line even when cwd is on a later line."""
        from indexing import _extract_from_jsonl

        folder = tmp_path / "project"
        folder.mkdir()

        lines = [
            json.dumps({"type": "pr-link", "timestamp": "2026-04-21T18:00:00Z"}),
            json.dumps({"type": "pr-link", "timestamp": "2026-04-21T19:00:00Z"}),
            json.dumps({"type": "system", "cwd": "/Users/me/repo", "timestamp": "2026-04-21T20:00:00Z"}),
        ]
        (folder / "session.jsonl").write_text("\n".join(lines) + "\n")

        original_path, work_days = _extract_from_jsonl(folder)
        assert original_path == "/Users/me/repo"
        assert "2026-04-21" in work_days

    def test_no_cwd_in_scan_limit_returns_empty_path_but_captures_timestamp(self, tmp_path):
        """When cwd not found within scan limit, path is empty but timestamp is captured."""
        from indexing import _JSONL_SCAN_LIMIT, _extract_from_jsonl

        folder = tmp_path / "project"
        folder.mkdir()

        lines = []
        for i in range(_JSONL_SCAN_LIMIT):
            lines.append(json.dumps({"type": "pr-link", "timestamp": f"2026-04-25T{10 + i}:00:00Z"}))
        lines.append(json.dumps({"type": "system", "cwd": "/Users/me/repo", "timestamp": "2026-04-25T23:00:00Z"}))

        (folder / "session.jsonl").write_text("\n".join(lines) + "\n")

        original_path, work_days = _extract_from_jsonl(folder)
        assert original_path == ""
        assert "2026-04-25" in work_days

    def test_all_lines_no_cwd_no_timestamp(self, tmp_path):
        """File with no cwd or timestamp returns empty results."""
        from indexing import _extract_from_jsonl

        folder = tmp_path / "project"
        folder.mkdir()

        (folder / "session.jsonl").write_text(json.dumps({"type": "unknown"}) + "\n")

        original_path, work_days = _extract_from_jsonl(folder)
        assert original_path == ""
        assert work_days == set()

    def test_corrupt_line_does_not_discard_earlier_captures(self, tmp_path):
        """A corrupt JSON line mid-file preserves cwd/timestamp from earlier valid lines."""
        from indexing import _extract_from_jsonl

        folder = tmp_path / "project"
        folder.mkdir()

        lines = [
            json.dumps({"type": "pr-link", "timestamp": "2026-04-25T18:00:00Z"}),
            "this is not json {{{",
            json.dumps({"type": "system", "cwd": "/Users/me/repo"}),
        ]
        (folder / "session.jsonl").write_text("\n".join(lines) + "\n")

        original_path, work_days = _extract_from_jsonl(folder)
        assert original_path == "/Users/me/repo"
        assert "2026-04-25" in work_days

    def test_non_dict_json_line_is_skipped(self, tmp_path):
        """A valid JSON line that is not a dict (e.g., a list) is skipped without crashing."""
        from indexing import _extract_from_jsonl

        folder = tmp_path / "project"
        folder.mkdir()

        lines = [
            json.dumps([1, 2, 3]),
            json.dumps({"type": "system", "cwd": "/Users/me/repo", "timestamp": "2026-04-25T18:00:00Z"}),
        ]
        (folder / "session.jsonl").write_text("\n".join(lines) + "\n")

        original_path, work_days = _extract_from_jsonl(folder)
        assert original_path == "/Users/me/repo"
        assert "2026-04-25" in work_days


class TestExtractFromJsonlMtimeOrdering:
    """Tests that _extract_from_jsonl prefers the newest file's cwd."""

    def test_newest_cwd_wins(self, tmp_path):
        """Newest JSONL file's cwd is used, not the first alphabetically."""
        import os

        from indexing import _extract_from_jsonl

        folder = tmp_path / "project"
        folder.mkdir()

        # Older file with stale path (alphabetically first: aaa < zzz)
        old_file = folder / "aaa-old-session.jsonl"
        old_file.write_text(json.dumps({
            "cwd": "/home/olduser/myproject",
            "timestamp": "2026-01-01T10:00:00Z",
            "type": "user",
        }) + "\n")

        # Newer file with current path
        new_file = folder / "zzz-new-session.jsonl"
        new_file.write_text(json.dumps({
            "cwd": "/Users/newuser/myproject",
            "timestamp": "2026-02-01T10:00:00Z",
            "type": "user",
        }) + "\n")

        # Set explicit mtimes for deterministic ordering
        os.utime(old_file, (1000, 1000))
        os.utime(new_file, (2000, 2000))

        original_path, work_days = _extract_from_jsonl(folder)
        assert original_path == "/Users/newuser/myproject"
        assert "2026-01-01" in work_days
        assert "2026-02-01" in work_days


class TestBuildProjectsIndexFixPaths:
    """Tests for the --fix-paths flag in build_projects_index."""

    @pytest.fixture()
    def env(self, tmp_path):
        projects_dir = tmp_path / "projects"
        memory_dir = tmp_path / "memory"
        index_file = memory_dir / "projects-index.json"
        with mock.patch("indexing.get_projects_dir", return_value=projects_dir), \
             mock.patch("indexing.get_memory_dir", return_value=memory_dir), \
             mock.patch("indexing.get_projects_index_file", return_value=index_file):
            yield projects_dir, memory_dir, index_file

    def _make_jsonl_file(self, folder, session_id, cwd, timestamp):
        line = json.dumps({
            "cwd": cwd, "timestamp": timestamp,
            "type": "user", "sessionId": session_id,
            "message": {"role": "user", "content": "hello"},
        })
        jsonl_path = folder / f"{session_id}.jsonl"
        jsonl_path.write_text(line + "\n", encoding="utf-8")
        return jsonl_path

    def test_fix_paths_corrects_stale_entry(self, env):
        """--fix-paths replaces a stale transcript cwd with a suggested correction.

        Post-inversion, a path is stale only when the transcript cwd itself
        points at a missing dir (e.g. cross-machine migration). The home-
        substitution strategy maps the stale /home/<user>/… cwd onto $HOME.
        """
        projects_dir, memory_dir, index_file = env

        # Simulate migration: real project now lives under a (mocked) $HOME.
        fake_home = env[0].parent / "home"
        live_dir = fake_home / "myproject"
        live_dir.mkdir(parents=True, exist_ok=True)

        folder = projects_dir / "-enc-myproject"
        folder.mkdir(parents=True)
        # Transcript still records the old machine's path (doesn't exist here).
        self._make_jsonl_file(folder, "s1", "/home/olduser/myproject", "2026-01-15T10:00:00Z")

        with mock.patch("indexing.Path.home", return_value=fake_home):
            result = build_projects_index(fix_paths=True)

        projects = result["projects"]
        assert len(projects) == 1
        data = list(projects.values())[0]
        assert data["originalPath"] == str(live_dir)
        assert data["name"] == "myproject"

    def test_no_fix_without_flag(self, env):
        """Without --fix-paths, a stale transcript cwd is preserved (with warnings)."""
        projects_dir, memory_dir, index_file = env

        folder = projects_dir / "-enc-myproject"
        folder.mkdir(parents=True)
        # Stale cwd, no live directory to resolve to.
        self._make_jsonl_file(folder, "s1", "/home/olduser/myproject", "2026-01-15T10:00:00Z")

        result = build_projects_index(fix_paths=False)

        projects = result["projects"]
        data = list(projects.values())[0]
        # Path should still be the (stale) transcript cwd.
        assert data["originalPath"] == "/home/olduser/myproject"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
