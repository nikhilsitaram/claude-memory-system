#!/usr/bin/env python3
"""
Unit tests for project_manager.py

Run with: python -m pytest tests/test_project_manager.py -v
"""

import json
from pathlib import Path
from unittest import mock

import pytest
from project_manager import (  # noqa: I001
    backup_files,
    decode_path_best_effort,
    encode_path,
    execute_merge_orphan,
    execute_move,
    find_orphaned_folders,
    get_folder_original_path,
    get_original_path_from_folder,
    list_projects,
    plan_cleanup,
    plan_merge_orphan,
    plan_move,
    rebuild_and_verify_index,
    refresh_synthesis_offsets,
    restore_from_backup,
    rewrite_cwd_in_transcripts,
    rewrite_paths_in_file,
    validate_merge_orphan,
    validate_move,
)

# =============================================================================
# Path Encoding Tests
# =============================================================================


@pytest.mark.parametrize("path,expected", [
    ("/home/user/project", "-home-user-project"),
    ("/home/user/.config", "-home-user--config"),
    ("/home/user/my-project", "-home-user-my-project"),
    ("", ""),
    ("/", "-"),
])
def test_encode_path(path, expected):
    assert encode_path(path) == expected


class TestDecodePathBestEffort:
    """Tests for decode_path_best_effort function."""

    def test_simple_decode(self):
        """Basic decode - replace hyphens with slashes."""
        # Note: This loses information about original hyphens vs dots
        result = decode_path_best_effort("-home-user-project")
        assert result == "/home/user/project"

    def test_leading_hyphen_becomes_root(self):
        result = decode_path_best_effort("-home-user")
        assert result.startswith("/")

    def test_empty_string(self):
        assert decode_path_best_effort("") == ""

    def test_roundtrip_is_lossy(self):
        """Demonstrate that encode->decode doesn't preserve original."""
        original = "/home/user/my-project"
        encoded = encode_path(original)
        decoded = decode_path_best_effort(encoded)
        # The hyphen in "my-project" becomes a slash
        assert decoded != original


class TestGetOriginalPathFromFolder:
    """Tests for get_original_path_from_folder function."""

    def test_with_valid_sessions_index(self, tmp_path):
        """Should extract originalPath from sessions-index.json."""
        folder = tmp_path / "test-folder"
        folder.mkdir()

        sessions_index = folder / "sessions-index.json"
        sessions_index.write_text(json.dumps({
            "originalPath": "/home/user/my-project",
            "entries": []
        }))

        result = get_original_path_from_folder(folder)
        assert result == "/home/user/my-project"

    def test_without_sessions_index(self, tmp_path):
        """Should return None if no sessions-index.json."""
        folder = tmp_path / "test-folder"
        folder.mkdir()

        result = get_original_path_from_folder(folder)
        assert result is None

    def test_with_invalid_json(self, tmp_path):
        """Should return None if sessions-index.json is invalid."""
        folder = tmp_path / "test-folder"
        folder.mkdir()

        sessions_index = folder / "sessions-index.json"
        sessions_index.write_text("not valid json")

        result = get_original_path_from_folder(folder)
        assert result is None


# =============================================================================
# Validation Tests
# =============================================================================


class TestValidateMove:
    """Tests for validate_move function."""

    def test_source_not_exists(self):
        """Should fail if source doesn't exist."""
        result = validate_move(Path("/nonexistent/path"), Path("/tmp/dest"))
        assert not result.valid
        assert any("does not exist" in issue for issue in result.issues)

    def test_source_not_directory(self, tmp_path):
        """Should fail if source is a file, not directory."""
        f = tmp_path / "test.txt"
        f.write_text("content")
        result = validate_move(f, Path("/tmp/dest"))
        assert not result.valid
        assert any("not a directory" in issue for issue in result.issues)

    def test_dest_parent_not_exists(self, tmp_path):
        """Should fail if destination parent doesn't exist."""
        source = tmp_path / "source"
        source.mkdir()

        result = validate_move(source, Path("/nonexistent/parent/dest"))
        assert not result.valid
        assert any("parent does not exist" in issue for issue in result.issues)

    def test_dest_exists_warning(self, tmp_path):
        """Should warn (not fail) if destination exists."""
        source = tmp_path / "source"
        dest = tmp_path / "dest"
        source.mkdir()
        dest.mkdir()

        result = validate_move(source, dest)
        assert result.valid  # Valid, just has warnings
        assert any("exists" in w for w in result.warnings)

    def test_valid_move(self, tmp_path):
        """Should pass for valid move scenario."""
        source = tmp_path / "source"
        dest = tmp_path / "dest"
        source.mkdir()
        # dest doesn't exist, which is fine

        result = validate_move(source, dest)
        assert result.valid
        assert len(result.issues) == 0


class TestValidateMergeOrphan:
    """Tests for validate_merge_orphan function."""

    def test_target_not_exists(self):
        """Should fail if target directory doesn't exist."""
        # Mock the projects_dir to avoid needing real Claude folders
        with mock.patch("project_manager.get_projects_dir") as mock_projects:
            mock_projects.return_value = Path("/tmp/mock-projects")
            Path("/tmp/mock-projects").mkdir(parents=True, exist_ok=True)
            (Path("/tmp/mock-projects") / "orphan-folder").mkdir(exist_ok=True)

            result = validate_merge_orphan("orphan-folder", Path("/nonexistent/target"))
            assert not result.valid
            assert any("does not exist" in issue for issue in result.issues)


# =============================================================================
# Plan Merge Orphan Tests
# =============================================================================


class TestPlanMergeOrphan:
    """Tests for plan_merge_orphan function."""

    def _setup_and_plan(self, tmp_path, orphan_name, target_path, setup_fn=None):
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        claude_dir = tmp_path / "claude"
        claude_dir.mkdir()
        index_file = memory_dir / "projects-index.json"
        index_file.write_text('{"projects":{}}')
        if setup_fn:
            setup_fn(projects_dir)
        with mock.patch("project_manager.get_projects_dir", return_value=projects_dir), \
             mock.patch("project_manager.get_projects_index_file", return_value=index_file), \
             mock.patch("project_manager.get_claude_dir", return_value=claude_dir), \
             mock.patch("project_manager.get_project_memory_dir", return_value=memory_dir / "project-memory"):
            return plan_merge_orphan(orphan_name, target_path)

    def test_same_encoded_path_skips_folder_operations(self, tmp_path):
        """When orphan name equals target encoded path, no moves/merges/renames should be planned."""
        target_path = Path("/home/user/personal/investing")
        orphan_name = encode_path(str(target_path))  # -home-user-personal-investing

        def setup(projects_dir):
            orphan_folder = projects_dir / orphan_name
            orphan_folder.mkdir()
            # Create a session file so it's not empty
            (orphan_folder / "abc123.jsonl").write_text('{"type":"test"}\n')

        plan = self._setup_and_plan(tmp_path, orphan_name, target_path, setup_fn=setup)

        assert plan.moves == [], f"Expected no moves, got {plan.moves}"
        assert plan.merges == [], f"Expected no merges, got {plan.merges}"
        assert plan.renames == [], f"Expected no renames, got {plan.renames}"

    def test_different_encoded_path_plans_move(self, tmp_path):
        """When orphan name differs from target encoded path and target doesn't exist, should plan a move."""
        orphan_name = "-home-user-old-investing"
        target_path = Path("/home/user/personal/investing")

        def setup(projects_dir):
            (projects_dir / orphan_name).mkdir()

        plan = self._setup_and_plan(tmp_path, orphan_name, target_path, setup_fn=setup)

        assert len(plan.moves) == 1, f"Expected 1 move, got {plan.moves}"
        assert len(plan.renames) == 1, f"Expected 1 rename, got {plan.renames}"

    def test_different_encoded_path_plans_merge_when_target_exists(self, tmp_path):
        """When both orphan and target folders exist, should plan a merge + rename."""
        orphan_name = "-home-user-old-investing"
        target_path = Path("/home/user/personal/investing")
        target_encoded = encode_path(str(target_path))

        def setup(projects_dir):
            # Both folders exist
            (projects_dir / orphan_name).mkdir()
            (projects_dir / target_encoded).mkdir()

        plan = self._setup_and_plan(tmp_path, orphan_name, target_path, setup_fn=setup)

        assert len(plan.merges) == 1, f"Expected 1 merge, got {plan.merges}"
        assert len(plan.renames) == 1, f"Expected 1 rename, got {plan.renames}"


# =============================================================================
# Path Rewriting Tests
# =============================================================================


class TestRewritePathsInFile:
    """Tests for rewrite_paths_in_file function."""

    def test_simple_replacement(self, tmp_path):
        """Should replace old path with new path."""
        f = tmp_path / "test.txt"
        f.write_text(
            "/home/user/old-project\n"
            "some other text\n"
            "/home/user/old-project/subdir\n"
        )

        count = rewrite_paths_in_file(
            f,
            "/home/user/old-project",
            "/home/user/new-project"
        )

        assert count == 2

        content = f.read_text()
        assert "/home/user/new-project" in content
        assert "/home/user/new-project/subdir" in content
        assert "/home/user/old-project" not in content

    def test_no_matches(self, tmp_path):
        """Should return 0 if no matches found."""
        f = tmp_path / "test.txt"
        f.write_text("some text without the path\n")

        count = rewrite_paths_in_file(
            f,
            "/nonexistent/path",
            "/new/path"
        )
        assert count == 0

    def test_nonexistent_file(self):
        """Should return 0 for nonexistent file."""
        count = rewrite_paths_in_file(
            Path("/nonexistent/file.txt"),
            "/old",
            "/new"
        )
        assert count == 0


# =============================================================================
# Transcript cwd Rewrite Tests (move durability)
# =============================================================================


class TestRewriteCwdInTranscripts:
    """Tests for rewrite_cwd_in_transcripts — makes a move survive index rebuild."""

    OLD = "/home/user/old-project"
    NEW = "/home/user/new-project"

    def _write_transcript(self, folder, session_id, cwd, extra_lines=None):
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"{session_id}.jsonl"
        lines = [json.dumps({
            "type": "user", "cwd": cwd, "timestamp": "2026-06-23T10:00:00.000Z",
        })]
        for entry in (extra_lines or []):
            lines.append(json.dumps(entry))
        f.write_text("\n".join(lines) + "\n")
        return f

    def test_rewrites_exact_cwd(self, tmp_path):
        f = self._write_transcript(tmp_path / "folder", "sess1", self.OLD)
        result = rewrite_cwd_in_transcripts(tmp_path / "folder", self.OLD, self.NEW)
        assert result["files_modified"] == 1
        assert result["session_ids"] == ["sess1"]
        assert json.loads(f.read_text().splitlines()[0])["cwd"] == self.NEW

    def test_rewrites_cwd_prefix_for_subdir_session(self, tmp_path):
        """A session run in a subdirectory has cwd under the project root."""
        f = self._write_transcript(tmp_path / "folder", "sess1", self.OLD + "/sub")
        rewrite_cwd_in_transcripts(tmp_path / "folder", self.OLD, self.NEW)
        assert json.loads(f.read_text().splitlines()[0])["cwd"] == self.NEW + "/sub"

    def test_leaves_sibling_prefix_untouched(self, tmp_path):
        """A sibling project sharing a path prefix must NOT be rewritten."""
        sibling = self.OLD + "-other"  # /home/user/old-project-other
        f = self._write_transcript(tmp_path / "folder", "sess1", sibling)
        result = rewrite_cwd_in_transcripts(tmp_path / "folder", self.OLD, self.NEW)
        assert result["files_modified"] == 0
        assert json.loads(f.read_text().splitlines()[0])["cwd"] == sibling

    def test_leaves_non_cwd_occurrences_untouched(self, tmp_path):
        """The old path inside message content must NOT be rewritten."""
        extra = {"type": "assistant", "content": f"see {self.OLD}/file.py"}
        f = self._write_transcript(tmp_path / "folder", "sess1", self.OLD, [extra])
        rewrite_cwd_in_transcripts(tmp_path / "folder", self.OLD, self.NEW)
        lines = f.read_text().splitlines()
        assert json.loads(lines[0])["cwd"] == self.NEW
        assert f"{self.OLD}/file.py" in lines[1]  # content preserved verbatim

    def test_no_match_returns_zero(self, tmp_path):
        self._write_transcript(tmp_path / "folder", "sess1", "/home/user/other")
        result = rewrite_cwd_in_transcripts(tmp_path / "folder", self.OLD, self.NEW)
        assert result == {"files_modified": 0, "session_ids": []}

    def test_missing_folder_returns_zero(self, tmp_path):
        result = rewrite_cwd_in_transcripts(tmp_path / "nope", self.OLD, self.NEW)
        assert result == {"files_modified": 0, "session_ids": []}

    def test_indexer_reads_rewritten_cwd(self, tmp_path):
        """Regression: the cwd build_projects_index reads must reflect the new path."""
        from indexing import _extract_from_jsonl

        folder = tmp_path / "folder"
        self._write_transcript(folder, "sess1", self.OLD)
        assert _extract_from_jsonl(folder)[0] == self.OLD
        rewrite_cwd_in_transcripts(folder, self.OLD, self.NEW)
        assert _extract_from_jsonl(folder)[0] == self.NEW


class TestRefreshSynthesisOffsets:
    """Tests for refresh_synthesis_offsets — keeps synthesis state consistent."""

    def test_updates_offset_to_file_size_preserving_lines(self, tmp_path):
        folder = tmp_path / "folder"
        folder.mkdir()
        f = folder / "sess1.jsonl"
        f.write_text("x" * 100)
        state = {"sessions": {"sess1": {"offset": 40, "lines": 5, "last_synthesized": "old"}}}
        with mock.patch("project_manager.load_synthesis_state", return_value=state), \
             mock.patch("project_manager.save_synthesis_state") as save:
            n = refresh_synthesis_offsets(["sess1"], folder)
        assert n == 1
        assert state["sessions"]["sess1"]["offset"] == 100
        assert state["sessions"]["sess1"]["lines"] == 5  # line count unchanged
        save.assert_called_once()

    def test_skips_session_not_in_state(self, tmp_path):
        folder = tmp_path / "folder"
        folder.mkdir()
        (folder / "sess1.jsonl").write_text("data")
        with mock.patch("project_manager.load_synthesis_state", return_value={"sessions": {}}), \
             mock.patch("project_manager.save_synthesis_state") as save:
            n = refresh_synthesis_offsets(["sess1"], folder)
        assert n == 0
        save.assert_not_called()

    def test_skips_missing_transcript_file(self, tmp_path):
        folder = tmp_path / "folder"
        folder.mkdir()
        state = {"sessions": {"sess1": {"offset": 1, "lines": 1}}}
        with mock.patch("project_manager.load_synthesis_state", return_value=state), \
             mock.patch("project_manager.save_synthesis_state") as save:
            n = refresh_synthesis_offsets(["sess1"], folder)
        assert n == 0
        save.assert_not_called()


class TestRebuildAndVerifyIndex:
    """Tests for rebuild_and_verify_index — post-move durability check."""

    def test_durable_when_entry_resolves(self, tmp_path):
        import indexing

        proj = tmp_path / "new-project"
        proj.mkdir()
        fake = {"projects": {str(proj).lower(): {"name": "new-project", "originalPath": str(proj)}}}
        with mock.patch.object(indexing, "build_projects_index", return_value=fake):
            result = rebuild_and_verify_index(str(proj))
        assert result["durable"] is True
        assert result["entry"]["name"] == "new-project"
        assert result["stale_paths"] == []

    def test_not_durable_when_entry_missing(self, tmp_path):
        import indexing

        proj = tmp_path / "new-project"
        proj.mkdir()
        with mock.patch.object(indexing, "build_projects_index", return_value={"projects": {}}):
            result = rebuild_and_verify_index(str(proj))
        assert result["durable"] is False
        assert "NOT durable" in result["message"]

    def test_not_durable_when_path_absent_on_disk(self, tmp_path):
        import indexing

        proj = tmp_path / "gone"  # never created
        fake = {"projects": {str(proj).lower(): {"name": "gone", "originalPath": str(proj)}}}
        with mock.patch.object(indexing, "build_projects_index", return_value=fake):
            result = rebuild_and_verify_index(str(proj))
        assert result["durable"] is False
        assert str(proj) in result["stale_paths"]

    def test_lookup_key_resolved_like_build_projects_index(self, tmp_path):
        """The lookup must apply resolve_session_path (worktree/subdir collapse),
        matching how build_projects_index keys entries — else a worktree/subdir
        project falsely reports NOT durable."""
        import indexing

        proj = tmp_path / "worktree-checkout"
        proj.mkdir()
        resolved = tmp_path / "main-repo"
        resolved.mkdir()
        # build_projects_index would key under the *resolved* path
        fake = {"projects": {str(resolved).lower(): {"name": "main-repo", "originalPath": str(resolved)}}}
        with mock.patch("project_manager.resolve_session_path", side_effect=lambda p: str(resolved)), \
             mock.patch.object(indexing, "build_projects_index", return_value=fake):
            result = rebuild_and_verify_index(str(proj))
        assert result["durable"] is True
        assert result["entry"]["name"] == "main-repo"


class TestGetFolderOriginalPath:
    """Tests for get_folder_original_path — cwd fallback when no sessions-index."""

    def _write_transcript(self, folder, session_id, cwd):
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"{session_id}.jsonl"
        f.write_text(json.dumps({"cwd": cwd, "timestamp": "2026-06-23T10:00:00.000Z"}) + "\n")
        return f

    def test_prefers_transcript_cwd_over_index(self, tmp_path):
        """The transcript cwd wins over a (possibly stale) sessions-index.json,
        matching build_projects_index so the rewrite anchor is the indexed path."""
        folder = tmp_path / "folder"
        folder.mkdir()
        (folder / "sessions-index.json").write_text(json.dumps({
            "originalPath": "/from/index", "entries": [],
        }))
        self._write_transcript(folder, "sess1", "/from/cwd")
        assert get_folder_original_path(folder) == "/from/cwd"

    def test_falls_back_to_index_when_no_transcript_cwd(self, tmp_path):
        """With only a legacy sessions-index.json (no transcript), use its path."""
        folder = tmp_path / "folder"
        folder.mkdir()
        (folder / "sessions-index.json").write_text(json.dumps({
            "originalPath": "/from/index", "entries": [],
        }))
        assert get_folder_original_path(folder) == "/from/index"

    def test_falls_back_to_cwd_when_no_index(self, tmp_path):
        folder = tmp_path / "folder"
        self._write_transcript(folder, "sess1", "/from/cwd")
        assert get_folder_original_path(folder) == "/from/cwd"

    def test_newest_transcript_cwd_wins(self, tmp_path):
        import os

        folder = tmp_path / "folder"
        older = self._write_transcript(folder, "old", "/path/older")
        newer = self._write_transcript(folder, "new", "/path/newer")
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))
        assert get_folder_original_path(folder) == "/path/newer"

    def test_returns_none_for_empty_folder(self, tmp_path):
        folder = tmp_path / "folder"
        folder.mkdir()
        assert get_folder_original_path(folder) is None

    def test_returns_none_for_missing_folder(self, tmp_path):
        assert get_folder_original_path(tmp_path / "nope") is None


# =============================================================================
# Execute Move / Merge-Orphan End-to-End Tests (cwd-rewrite wiring)
# =============================================================================


def _fake_claude_env(tmp_path):
    """Build an isolated ~/.claude layout and the project_manager patch map."""
    claude = tmp_path / ".claude"
    projects_dir = claude / "projects"
    memory_dir = claude / "memory"
    proj_mem = memory_dir / "project-memory"
    for d in (projects_dir, memory_dir, proj_mem):
        d.mkdir(parents=True)
    (claude / "history.jsonl").write_text("")
    index_file = memory_dir / "projects-index.json"
    patches = mock.patch.multiple(
        "project_manager",
        get_projects_dir=mock.MagicMock(return_value=projects_dir),
        get_memory_dir=mock.MagicMock(return_value=memory_dir),
        get_claude_dir=mock.MagicMock(return_value=claude),
        get_project_memory_dir=mock.MagicMock(return_value=proj_mem),
        get_projects_index_file=mock.MagicMock(return_value=index_file),
        load_synthesis_state=mock.MagicMock(return_value={"sessions": {}}),
    )
    return claude, projects_dir, index_file, patches


def _write_cwd_transcript(folder, session_id, cwd):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{session_id}.jsonl").write_text(
        json.dumps({"type": "user", "cwd": cwd, "timestamp": "2026-06-23T10:00:00.000Z"}) + "\n"
    )


class TestExecuteMoveEndToEnd:
    """End-to-end: execute_move must rewrite the destination transcript cwd."""

    def test_rewrites_dest_cwd_and_updates_index(self, tmp_path):
        _claude, projects_dir, index_file, patches = _fake_claude_env(tmp_path)

        old = tmp_path / "work" / "old-project"
        new = tmp_path / "work" / "new-project"
        old.mkdir(parents=True)
        old_enc = encode_path(str(old))
        _write_cwd_transcript(projects_dir / old_enc, "sess1", str(old))
        index_file.write_text(json.dumps({"projects": {str(old).lower(): {
            "name": "old-project", "originalPath": str(old),
            "encodedPaths": [old_enc], "workDays": ["2026-06-23"],
        }}}))

        with patches:
            result = execute_move(old, new, confirmed=True)

        assert result["success"] is True
        assert result["cwd_files_rewritten"] == 1

        new_enc = encode_path(str(new))
        dest = projects_dir / new_enc / "sess1.jsonl"
        assert dest.exists()
        assert json.loads(dest.read_text().splitlines()[0])["cwd"] == str(new)

        idx = json.loads(index_file.read_text())["projects"]
        assert str(new).lower() in idx
        assert str(old).lower() not in idx


class TestExecuteMergeOrphanEndToEnd:
    """End-to-end: merge-orphan must rewrite cwd even with no sessions-index.json."""

    def test_rewrites_cwd_via_jsonl_fallback(self, tmp_path):
        _claude, projects_dir, index_file, patches = _fake_claude_env(tmp_path)

        orphan_path = tmp_path / "work" / "orphan-proj"   # gone from disk
        target = tmp_path / "work" / "target-proj"
        target.mkdir(parents=True)

        orphan_enc = encode_path(str(orphan_path))
        target_enc = encode_path(str(target))
        # Orphan folder: only a transcript (no sessions-index.json) -> cwd fallback
        _write_cwd_transcript(projects_dir / orphan_enc, "osess", str(orphan_path))
        # Target folder already exists with its own transcript on the new path
        _write_cwd_transcript(projects_dir / target_enc, "tsess", str(target))
        index_file.write_text(json.dumps({"projects": {str(orphan_path).lower(): {
            "name": "orphan-proj", "originalPath": str(orphan_path),
            "encodedPaths": [orphan_enc], "workDays": ["2026-06-23"],
        }}}))

        with patches:
            result = execute_merge_orphan(orphan_enc, target, confirmed=True)

        assert result["success"] is True
        # cwd rewrite fired despite no sessions-index.json (the bug we fixed)
        assert result["cwd_files_rewritten"] == 1
        assert result["orphan_project_name"] == "orphan-proj"

        merged = projects_dir / target_enc / "osess.jsonl"
        assert merged.exists()
        assert json.loads(merged.read_text().splitlines()[0])["cwd"] == str(target)
        # We never write sessions-index.json — current Claude Code ignores it.
        assert not (projects_dir / target_enc / "sessions-index.json").exists()

    def test_rewrites_cwd_when_legacy_index_path_diverges(self, tmp_path):
        """Seam contract: when a legacy sessions-index.json holds a path that
        DIFFERS from the transcript cwd, the rewrite anchor must still be the
        transcript cwd (what build_projects_index keys on) — otherwise the
        rewrite matches nothing and the merge reverts on the next synthesis."""
        _claude, projects_dir, index_file, patches = _fake_claude_env(tmp_path)

        orphan_path = tmp_path / "work" / "orphan-proj"   # gone from disk
        target = tmp_path / "work" / "target-proj"
        target.mkdir(parents=True)

        orphan_enc = encode_path(str(orphan_path))
        target_enc = encode_path(str(target))
        # Transcript records the REAL old path...
        _write_cwd_transcript(projects_dir / orphan_enc, "osess", str(orphan_path))
        # ...but a stale legacy sessions-index.json claims a DIFFERENT path.
        (projects_dir / orphan_enc / "sessions-index.json").write_text(json.dumps({
            "originalPath": "/stale/divergent/orphan-proj", "entries": [],
        }))
        _write_cwd_transcript(projects_dir / target_enc, "tsess", str(target))
        index_file.write_text(json.dumps({"projects": {str(orphan_path).lower(): {
            "name": "orphan-proj", "originalPath": str(orphan_path),
            "encodedPaths": [orphan_enc], "workDays": ["2026-06-23"],
        }}}))

        with patches:
            result = execute_merge_orphan(orphan_enc, target, confirmed=True)

        assert result["success"] is True
        # Anchored on the transcript cwd, not the stale index path → rewrite fires.
        assert result["cwd_files_rewritten"] == 1
        merged = projects_dir / target_enc / "osess.jsonl"
        assert json.loads(merged.read_text().splitlines()[0])["cwd"] == str(target)


class TestMoveDurabilityEndToEnd:
    """The headline contract: a move survives a REAL build_projects_index rebuild."""

    def test_execute_move_then_real_rebuild_reports_durable(self, tmp_path):
        import indexing

        _claude, projects_dir, index_file, patches = _fake_claude_env(tmp_path)
        memory_dir = _claude / "memory"

        old = tmp_path / "work" / "old-project"
        new = tmp_path / "work" / "new-project"
        old.mkdir(parents=True)
        old_enc = encode_path(str(old))
        _write_cwd_transcript(projects_dir / old_enc, "sess1", str(old))
        index_file.write_text(json.dumps({"projects": {str(old).lower(): {
            "name": "old-project", "originalPath": str(old),
            "encodedPaths": [old_enc], "workDays": ["2026-06-23"],
        }}}))

        # Patch indexing's OWN path-helper bindings too, so the real
        # build_projects_index (invoked by rebuild_and_verify_index) scans the
        # fake projects dir instead of the real ~/.claude.
        with patches, \
             mock.patch.object(indexing, "get_projects_dir", return_value=projects_dir), \
             mock.patch.object(indexing, "get_memory_dir", return_value=memory_dir), \
             mock.patch.object(indexing, "get_projects_index_file", return_value=index_file):
            move = execute_move(old, new, confirmed=True)
            assert move["success"] is True
            # No mock of build_projects_index — this runs the real hourly rebuild.
            verify = rebuild_and_verify_index(str(new))

        assert verify["durable"] is True, verify["message"]
        assert verify["entry"]["originalPath"] == str(new)
        # The rebuilt-from-transcripts index agrees: old path is gone.
        rebuilt = json.loads(index_file.read_text())["projects"]
        assert str(new).lower() in rebuilt
        assert str(old).lower() not in rebuilt


class TestPlanMergeOrphanCwdFallback:
    """plan_merge_orphan must resolve the orphan via cwd fallback too, so its
    backup set covers the memory file execute_merge_orphan will merge."""

    def test_plan_backs_up_orphan_memory_without_sessions_index(self, tmp_path):
        from memory_utils import project_name_to_filename

        claude, projects_dir, _index_file, patches = _fake_claude_env(tmp_path)
        proj_mem = claude / "memory" / "project-memory"

        orphan_path = tmp_path / "work" / "orphan-proj"   # gone from disk
        target = tmp_path / "work" / "target-proj"
        target.mkdir(parents=True)
        orphan_enc = encode_path(str(orphan_path))
        # Orphan folder has only a transcript (no sessions-index.json)
        _write_cwd_transcript(projects_dir / orphan_enc, "osess", str(orphan_path))
        # Orphan's project-memory file exists and must be backed up
        orphan_mem = proj_mem / project_name_to_filename("orphan-proj")
        orphan_mem.write_text("# orphan memory\n")

        with patches:
            plan = plan_merge_orphan(orphan_enc, target)

        assert any(str(orphan_mem) == b for b in plan.backups), (
            f"orphan memory file not in backup set: {plan.backups}"
        )


# =============================================================================
# Backup/Restore Tests
# =============================================================================


class TestBackupFiles:
    """Tests for backup_files function."""

    def test_creates_backup_directory(self, tmp_path):
        """Should create timestamped backup directory."""
        # Create a test file
        test_file = tmp_path / "test.json"
        test_file.write_text('{"key": "value"}')

        # Mock the memory directory
        with mock.patch("project_manager.get_memory_dir") as mock_mem:
            mock_mem.return_value = tmp_path / "memory"

            backup_path = backup_files([test_file])

            assert backup_path.exists()
            assert backup_path.is_dir()
            assert (backup_path / "test.json").exists()

    def test_copies_file_contents(self, tmp_path):
        """Backup should preserve file contents."""
        test_file = tmp_path / "test.json"
        original_content = '{"important": "data"}'
        test_file.write_text(original_content)

        with mock.patch("project_manager.get_memory_dir") as mock_mem:
            mock_mem.return_value = tmp_path / "memory"

            backup_path = backup_files([test_file])
            backed_up = (backup_path / "test.json").read_text()

            assert backed_up == original_content


class TestRestoreFromBackup:
    """Tests for restore_from_backup function."""

    def test_restore_nonexistent_backup(self):
        """Should fail gracefully if backup doesn't exist."""
        result = restore_from_backup(Path("/nonexistent/backup"))
        assert not result["success"]
        assert "not found" in result["message"]

    def test_restore_projects_index(self, tmp_path):
        """Should restore projects-index.json to correct location."""
        # Create backup directory with a file
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        (backup_dir / "projects-index.json").write_text('{"restored": true}')

        # Mock the target location
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        with mock.patch("project_manager.get_projects_index_file") as mock_idx:
            mock_idx.return_value = memory_dir / "projects-index.json"

            result = restore_from_backup(backup_dir)

            assert result["success"]
            assert len(result["restored"]) == 1
            assert (memory_dir / "projects-index.json").exists()


# =============================================================================
# Planning Tests
# =============================================================================


class TestPlanMove:
    """Tests for plan_move function."""

    def _plan_move(self, tmp_path, old_path, new_path):
        with mock.patch("project_manager.get_projects_index_file") as mock_idx, \
             mock.patch("project_manager.get_claude_dir") as mock_claude, \
             mock.patch("project_manager.get_project_memory_dir") as mock_mem, \
             mock.patch("project_manager.load_json_file") as mock_load:
            mock_idx.return_value = tmp_path / "projects-index.json"
            mock_claude.return_value = tmp_path
            mock_mem.return_value = tmp_path / "project-memory"
            mock_load.return_value = {"projects": {}}
            (tmp_path / "projects-index.json").write_text("{}")
            return plan_move(old_path, new_path)

    def test_plan_includes_backup_list(self, tmp_path):
        """Plan should list files to backup."""
        old_path = tmp_path / "old"
        new_path = tmp_path / "new"
        old_path.mkdir()

        plan = self._plan_move(tmp_path, old_path, new_path)

        assert plan.operation == "move"
        assert len(plan.backups) > 0

    def test_plan_summary_is_human_readable(self, tmp_path):
        """Plan summary should describe the operation."""
        old_path = tmp_path / "old"
        new_path = tmp_path / "new"
        old_path.mkdir()

        plan = self._plan_move(tmp_path, old_path, new_path)

        assert "Move project" in plan.summary
        assert str(old_path) in plan.summary
        assert str(new_path) in plan.summary


class TestPlanCleanup:
    """Tests for plan_cleanup function."""

    def test_plan_with_no_stale_entries(self):
        """Should indicate nothing to clean."""
        with mock.patch("project_manager.find_stale_entries") as mock_stale, \
             mock.patch("project_manager.get_projects_index_file") as mock_idx:

            mock_stale.return_value = []
            mock_idx.return_value = Path("/mock/index.json")

            plan = plan_cleanup()

            assert plan.operation == "cleanup"
            assert "0 stale" in plan.summary

    def test_plan_with_stale_entries(self):
        """Should list stale entries in summary."""
        with mock.patch("project_manager.find_stale_entries") as mock_stale, \
             mock.patch("project_manager.get_projects_index_file") as mock_idx:

            mock_stale.return_value = [
                {
                    "canonical_path": "/old/path",
                    "original_path": "/Old/Path",
                    "name": "old-project",
                    "work_days": ["2026-01-01", "2026-01-02"],
                    "encoded_paths": ["-old-path"],
                }
            ]
            mock_idx.return_value = Path("/mock/index.json")

            plan = plan_cleanup()

            assert "1 stale" in plan.summary
            assert "old-project" in plan.summary
            assert "NOT be deleted" in plan.summary or "will NOT" in plan.summary.lower()


# =============================================================================
# Integration-Style Tests (with more mocking)
# =============================================================================


class TestListProjects:
    """Tests for list_projects function."""

    def test_empty_index(self):
        """Should return empty list for empty index."""
        with mock.patch("project_manager.load_json_file") as mock_load, \
             mock.patch("project_manager.get_project_memory_dir") as mock_mem:

            mock_load.return_value = {"projects": {}}
            mock_mem.return_value = Path("/mock/project-memory")

            projects = list_projects()
            assert projects == []

    def test_with_valid_project(self, tmp_path):
        """Should return project info for valid projects."""
        # Create a real directory
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        with mock.patch("project_manager.load_json_file") as mock_load, \
             mock.patch("project_manager.get_project_memory_dir") as mock_mem, \
             mock.patch("project_manager.get_projects_index_file") as mock_idx:

            mock_load.return_value = {
                "projects": {
                    str(project_dir).lower(): {
                        "name": "my-project",
                        "originalPath": str(project_dir),
                        "workDays": ["2026-01-01"],
                        "encodedPaths": ["-tmp-my-project"],
                    }
                }
            }
            mock_mem.return_value = tmp_path / "project-memory"
            mock_idx.return_value = tmp_path / "index.json"

            projects = list_projects()

            assert len(projects) == 1
            assert projects[0].name == "my-project"
            assert projects[0].exists is True
            assert len(projects[0].issues) == 0

    def test_with_missing_path(self):
        """Should mark project with issue if path doesn't exist."""
        with mock.patch("project_manager.load_json_file") as mock_load, \
             mock.patch("project_manager.get_project_memory_dir") as mock_mem, \
             mock.patch("project_manager.get_projects_index_file") as mock_idx:

            mock_load.return_value = {
                "projects": {
                    "/nonexistent/path": {
                        "name": "missing-project",
                        "originalPath": "/nonexistent/path",
                        "workDays": ["2026-01-01"],
                        "encodedPaths": ["-nonexistent-path"],
                    }
                }
            }
            mock_mem.return_value = Path("/mock/project-memory")
            mock_idx.return_value = Path("/mock/index.json")

            projects = list_projects()

            assert len(projects) == 1
            assert projects[0].exists is False
            assert len(projects[0].issues) > 0
            assert "missing" in projects[0].issues[0].lower()


# =============================================================================
# Find Orphaned Folders Tests
# =============================================================================


class TestFindOrphanedFolders:
    """Tests for find_orphaned_folders function."""

    def _find_orphans(self, tmp_path, index):
        projects_dir = tmp_path / "projects"
        with mock.patch("project_manager.get_projects_dir", return_value=projects_dir), \
             mock.patch("project_manager.get_projects_index_file", return_value=tmp_path / "index.json"), \
             mock.patch("project_manager.load_json_file", return_value=index), \
             mock.patch("project_manager.get_claude_dir", return_value=tmp_path):
            return find_orphaned_folders()

    def test_tracked_folder_not_orphan(self, tmp_path):
        """Folder in encodedPaths should not be flagged as orphan,
        even if sessions-index.json has stale originalPath."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Create a folder with stale originalPath
        folder = projects_dir / "-home-user-new-project"
        folder.mkdir()
        (folder / "sessions-index.json").write_text(json.dumps({
            "originalPath": "/home/user/old-project",  # Stale!
            "entries": []
        }))

        index = {
            "projects": {
                "/home/user/new-project": {
                    "name": "new-project",
                    "originalPath": "/home/user/new-project",
                    "encodedPaths": ["-home-user-new-project"],
                    "workDays": [],
                }
            }
        }

        orphans = self._find_orphans(tmp_path, index)
        assert len(orphans) == 0

    def test_untracked_folder_with_stale_path_is_orphan(self, tmp_path):
        """Folder not in encodedPaths with stale originalPath should be orphan."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        folder = projects_dir / "-home-user-old-project"
        folder.mkdir()
        (folder / "sessions-index.json").write_text(json.dumps({
            "originalPath": "/home/user/old-project",  # Path doesn't exist
            "entries": []
        }))

        index = {"projects": {}}  # Not tracked

        orphans = self._find_orphans(tmp_path, index)
        assert len(orphans) == 1
        assert orphans[0].folder_name == "-home-user-old-project"

    def test_folder_with_valid_path_not_orphan(self, tmp_path):
        """Folder whose originalPath exists on disk should not be orphan."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Create the actual project directory so path exists
        real_project = tmp_path / "my-project"
        real_project.mkdir()

        folder = projects_dir / "-tmp-my-project"
        folder.mkdir()
        (folder / "sessions-index.json").write_text(json.dumps({
            "originalPath": str(real_project),
            "entries": []
        }))

        index = {"projects": {}}  # Not tracked, but path exists

        orphans = self._find_orphans(tmp_path, index)
        assert len(orphans) == 0

    def test_old_encoded_path_also_tracked(self, tmp_path):
        """Old encoded path kept in encodedPaths should not be orphan."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        # Simulate: folder was renamed from old to new,
        # but old encoded name kept in encodedPaths for transcript discovery
        folder = projects_dir / "-home-user-old-name"
        folder.mkdir()
        (folder / "sessions-index.json").write_text(json.dumps({
            "originalPath": "/home/user/old-name",  # Stale
            "entries": []
        }))

        index = {
            "projects": {
                "/home/user/new-name": {
                    "name": "new-name",
                    "originalPath": "/home/user/new-name",
                    "encodedPaths": [
                        "-home-user-new-name",
                        "-home-user-old-name",  # Old path kept
                    ],
                    "workDays": [],
                }
            }
        }

        orphans = self._find_orphans(tmp_path, index)
        assert len(orphans) == 0

    def test_untracked_folder_resolves_path_from_transcript_cwd(self, tmp_path):
        """With no sessions-index.json, an untracked folder whose transcript cwd
        points at a missing path is an orphan, and original_path is the cwd."""
        projects_dir = tmp_path / "projects"
        projects_dir.mkdir()

        folder = projects_dir / "-home-user-gone-project"
        folder.mkdir()
        # No sessions-index.json — only a transcript carrying the cwd.
        (folder / "sess.jsonl").write_text(json.dumps({
            "cwd": "/home/user/gone-project",
            "timestamp": "2026-01-01T10:00:00Z",
        }) + "\n")

        index = {"projects": {}}  # Not tracked

        orphans = self._find_orphans(tmp_path, index)
        assert len(orphans) == 1
        assert orphans[0].folder_name == "-home-user-gone-project"
        assert orphans[0].original_path == "/home/user/gone-project"
        assert orphans[0].sessions_index_path is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
