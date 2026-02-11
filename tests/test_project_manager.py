#!/usr/bin/env python3
"""
Unit tests for project_manager.py

Run with: python -m pytest tests/test_project_manager.py -v
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Add scripts directory to path
scripts_dir = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

from project_manager import (
    encode_path,
    decode_path_best_effort,
    get_original_path_from_folder,
    ValidationResult,
    validate_move,
    validate_merge_orphan,
    merge_sessions_index,
    rewrite_paths_in_file,
    update_session_index_paths,
    backup_files,
    restore_from_backup,
    list_projects,
    find_orphaned_folders,
    find_stale_entries,
    plan_move,
    plan_merge_orphan,
    plan_cleanup,
)


# =============================================================================
# Path Encoding Tests
# =============================================================================


class TestEncodePath:
    """Tests for encode_path function."""

    def test_simple_path(self):
        assert encode_path("/home/user/project") == "-home-user-project"

    def test_path_with_dots(self):
        """Dots are also replaced with hyphens."""
        assert encode_path("/home/user/.config") == "-home-user--config"

    def test_path_with_existing_hyphens(self):
        """Hyphens in the path are preserved (but this causes ambiguity)."""
        # Note: This is a known limitation - we can't distinguish
        # /home/user/my-project from /home/user/my/project after encoding
        assert encode_path("/home/user/my-project") == "-home-user-my-project"

    def test_empty_path(self):
        assert encode_path("") == ""

    def test_root_only(self):
        assert encode_path("/") == "-"


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

    def test_with_valid_sessions_index(self):
        """Should extract originalPath from sessions-index.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir) / "test-folder"
            folder.mkdir()

            sessions_index = folder / "sessions-index.json"
            sessions_index.write_text(json.dumps({
                "originalPath": "/home/user/my-project",
                "entries": []
            }))

            result = get_original_path_from_folder(folder)
            assert result == "/home/user/my-project"

    def test_without_sessions_index(self):
        """Should return None if no sessions-index.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir) / "test-folder"
            folder.mkdir()

            result = get_original_path_from_folder(folder)
            assert result is None

    def test_with_invalid_json(self):
        """Should return None if sessions-index.json is invalid."""
        with tempfile.TemporaryDirectory() as tmpdir:
            folder = Path(tmpdir) / "test-folder"
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

    def test_source_not_directory(self):
        """Should fail if source is a file, not directory."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            try:
                result = validate_move(Path(f.name), Path("/tmp/dest"))
                assert not result.valid
                assert any("not a directory" in issue for issue in result.issues)
            finally:
                os.unlink(f.name)

    def test_dest_parent_not_exists(self):
        """Should fail if destination parent doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            source.mkdir()

            result = validate_move(source, Path("/nonexistent/parent/dest"))
            assert not result.valid
            assert any("parent does not exist" in issue for issue in result.issues)

    def test_dest_exists_warning(self):
        """Should warn (not fail) if destination exists."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            dest = Path(tmpdir) / "dest"
            source.mkdir()
            dest.mkdir()

            result = validate_move(source, dest)
            assert result.valid  # Valid, just has warnings
            assert any("exists" in w for w in result.warnings)

    def test_valid_move(self):
        """Should pass for valid move scenario."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source"
            dest = Path(tmpdir) / "dest"
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
# Merge Sessions Index Tests
# =============================================================================


class TestMergeSessionsIndex:
    """Tests for merge_sessions_index function."""

    def test_merge_disjoint_entries(self):
        """Two files with different sessions should combine all."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.json"
            dest = Path(tmpdir) / "dest.json"

            source.write_text(json.dumps({
                "originalPath": "/old/path",
                "entries": [
                    {"id": "session-1", "created": "2026-01-01T10:00:00Z"},
                    {"id": "session-2", "created": "2026-01-02T10:00:00Z"},
                ]
            }))

            dest.write_text(json.dumps({
                "originalPath": "/new/path",
                "entries": [
                    {"id": "session-3", "created": "2026-01-03T10:00:00Z"},
                ]
            }))

            merged_count = merge_sessions_index(source, dest)
            assert merged_count == 2

            result = json.loads(dest.read_text())
            assert len(result["entries"]) == 3

    def test_merge_duplicate_sessions_keeps_newer(self):
        """Duplicate session IDs should keep the one with newer timestamp."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.json"
            dest = Path(tmpdir) / "dest.json"

            source.write_text(json.dumps({
                "originalPath": "/old/path",
                "entries": [
                    {"id": "session-1", "created": "2026-01-01T10:00:00Z",
                     "lastActive": "2026-01-01T15:00:00Z"},  # Newer
                ]
            }))

            dest.write_text(json.dumps({
                "originalPath": "/new/path",
                "entries": [
                    {"id": "session-1", "created": "2026-01-01T10:00:00Z",
                     "lastActive": "2026-01-01T12:00:00Z"},  # Older
                ]
            }))

            merged_count = merge_sessions_index(source, dest)
            assert merged_count == 1

            result = json.loads(dest.read_text())
            assert len(result["entries"]) == 1
            assert result["entries"][0]["lastActive"] == "2026-01-01T15:00:00Z"

    def test_merge_into_empty(self):
        """Merging into file with no entries should work."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.json"
            dest = Path(tmpdir) / "dest.json"

            source.write_text(json.dumps({
                "originalPath": "/old/path",
                "entries": [
                    {"id": "session-1", "created": "2026-01-01T10:00:00Z"},
                ]
            }))

            dest.write_text(json.dumps({
                "originalPath": "/new/path",
                "entries": []
            }))

            merged_count = merge_sessions_index(source, dest)
            assert merged_count == 1

            result = json.loads(dest.read_text())
            assert len(result["entries"]) == 1

    def test_merge_empty_source(self):
        """Merging from empty source should not change dest."""
        with tempfile.TemporaryDirectory() as tmpdir:
            source = Path(tmpdir) / "source.json"
            dest = Path(tmpdir) / "dest.json"

            source.write_text(json.dumps({
                "originalPath": "/old/path",
                "entries": []
            }))

            dest.write_text(json.dumps({
                "originalPath": "/new/path",
                "entries": [
                    {"id": "session-1", "created": "2026-01-01T10:00:00Z"},
                ]
            }))

            merged_count = merge_sessions_index(source, dest)
            assert merged_count == 0


# =============================================================================
# Path Rewriting Tests
# =============================================================================


class TestRewritePathsInFile:
    """Tests for rewrite_paths_in_file function."""

    def test_simple_replacement(self):
        """Should replace old path with new path."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("/home/user/old-project\n")
            f.write("some other text\n")
            f.write("/home/user/old-project/subdir\n")
            f.flush()

            try:
                count = rewrite_paths_in_file(
                    Path(f.name),
                    "/home/user/old-project",
                    "/home/user/new-project"
                )

                assert count == 2

                content = Path(f.name).read_text()
                assert "/home/user/new-project" in content
                assert "/home/user/new-project/subdir" in content
                assert "/home/user/old-project" not in content
            finally:
                os.unlink(f.name)

    def test_no_matches(self):
        """Should return 0 if no matches found."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("some text without the path\n")
            f.flush()

            try:
                count = rewrite_paths_in_file(
                    Path(f.name),
                    "/nonexistent/path",
                    "/new/path"
                )
                assert count == 0
            finally:
                os.unlink(f.name)

    def test_nonexistent_file(self):
        """Should return 0 for nonexistent file."""
        count = rewrite_paths_in_file(
            Path("/nonexistent/file.txt"),
            "/old",
            "/new"
        )
        assert count == 0


# =============================================================================
# Backup/Restore Tests
# =============================================================================


class TestBackupFiles:
    """Tests for backup_files function."""

    def test_creates_backup_directory(self):
        """Should create timestamped backup directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a test file
            test_file = Path(tmpdir) / "test.json"
            test_file.write_text('{"key": "value"}')

            # Mock the memory directory
            with mock.patch("project_manager.get_memory_dir") as mock_mem:
                mock_mem.return_value = Path(tmpdir) / "memory"

                backup_path = backup_files([test_file])

                assert backup_path.exists()
                assert backup_path.is_dir()
                assert (backup_path / "test.json").exists()

    def test_copies_file_contents(self):
        """Backup should preserve file contents."""
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = Path(tmpdir) / "test.json"
            original_content = '{"important": "data"}'
            test_file.write_text(original_content)

            with mock.patch("project_manager.get_memory_dir") as mock_mem:
                mock_mem.return_value = Path(tmpdir) / "memory"

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

    def test_restore_projects_index(self):
        """Should restore projects-index.json to correct location."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create backup directory with a file
            backup_dir = Path(tmpdir) / "backup"
            backup_dir.mkdir()
            (backup_dir / "projects-index.json").write_text('{"restored": true}')

            # Mock the target location
            memory_dir = Path(tmpdir) / "memory"
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

    def test_plan_includes_backup_list(self):
        """Plan should list files to backup."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_path = Path(tmpdir) / "old"
            new_path = Path(tmpdir) / "new"
            old_path.mkdir()

            # Create mock index file
            with mock.patch("project_manager.get_projects_index_file") as mock_idx, \
                 mock.patch("project_manager.get_claude_dir") as mock_claude, \
                 mock.patch("project_manager.get_project_memory_dir") as mock_mem, \
                 mock.patch("project_manager.load_json_file") as mock_load:

                mock_idx.return_value = Path(tmpdir) / "projects-index.json"
                mock_claude.return_value = Path(tmpdir)
                mock_mem.return_value = Path(tmpdir) / "project-memory"
                mock_load.return_value = {"projects": {}}

                # Create the index file so it shows up in backups
                (Path(tmpdir) / "projects-index.json").write_text("{}")

                plan = plan_move(old_path, new_path)

                assert plan.operation == "move"
                assert len(plan.backups) > 0

    def test_plan_summary_is_human_readable(self):
        """Plan summary should describe the operation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_path = Path(tmpdir) / "old"
            new_path = Path(tmpdir) / "new"
            old_path.mkdir()

            with mock.patch("project_manager.get_projects_index_file") as mock_idx, \
                 mock.patch("project_manager.get_claude_dir") as mock_claude, \
                 mock.patch("project_manager.get_project_memory_dir") as mock_mem, \
                 mock.patch("project_manager.load_json_file") as mock_load:

                mock_idx.return_value = Path(tmpdir) / "projects-index.json"
                mock_claude.return_value = Path(tmpdir)
                mock_mem.return_value = Path(tmpdir) / "project-memory"
                mock_load.return_value = {"projects": {}}

                plan = plan_move(old_path, new_path)

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

    def test_with_valid_project(self):
        """Should return project info for valid projects."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a real directory
            project_dir = Path(tmpdir) / "my-project"
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
                mock_mem.return_value = Path(tmpdir) / "project-memory"
                mock_idx.return_value = Path(tmpdir) / "index.json"

                projects = list_projects()

                assert len(projects) == 1
                assert projects[0].name == "my-project"
                assert projects[0].exists == True
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
            assert projects[0].exists == False
            assert len(projects[0].issues) > 0
            assert "missing" in projects[0].issues[0].lower()


# =============================================================================
# Update Session Index Paths Tests
# =============================================================================


class TestUpdateSessionIndexPaths:
    """Tests for update_session_index_paths function."""

    def test_updates_matching_files(self):
        """Should update sessions-index.json files containing old path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            projects_dir.mkdir()

            # Create two project folders with sessions-index.json
            folder1 = projects_dir / "-home-user-old-project"
            folder1.mkdir()
            (folder1 / "sessions-index.json").write_text(json.dumps({
                "originalPath": "/home/user/old-project",
                "entries": []
            }))

            folder2 = projects_dir / "-home-user-old-project-sub"
            folder2.mkdir()
            (folder2 / "sessions-index.json").write_text(json.dumps({
                "originalPath": "/home/user/old-project/sub",
                "entries": []
            }))

            with mock.patch("project_manager.get_projects_dir") as mock_dir:
                mock_dir.return_value = projects_dir

                count = update_session_index_paths(
                    "/home/user/old-project", "/home/user/new-project"
                )

            assert count == 2

            data1 = json.loads((folder1 / "sessions-index.json").read_text())
            assert data1["originalPath"] == "/home/user/new-project"

            data2 = json.loads((folder2 / "sessions-index.json").read_text())
            assert data2["originalPath"] == "/home/user/new-project/sub"

    def test_skips_non_matching_files(self):
        """Should not modify files that don't contain old path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            projects_dir.mkdir()

            folder = projects_dir / "-home-user-other"
            folder.mkdir()
            original = json.dumps({
                "originalPath": "/home/user/other",
                "entries": []
            })
            (folder / "sessions-index.json").write_text(original)

            with mock.patch("project_manager.get_projects_dir") as mock_dir:
                mock_dir.return_value = projects_dir

                count = update_session_index_paths(
                    "/home/user/old-project", "/home/user/new-project"
                )

            assert count == 0
            assert (folder / "sessions-index.json").read_text() == original

    def test_skips_folders_without_sessions_index(self):
        """Should skip folders that don't have sessions-index.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            projects_dir.mkdir()

            folder = projects_dir / "-home-user-old-project"
            folder.mkdir()
            # No sessions-index.json created

            with mock.patch("project_manager.get_projects_dir") as mock_dir:
                mock_dir.return_value = projects_dir

                count = update_session_index_paths(
                    "/home/user/old-project", "/home/user/new-project"
                )

            assert count == 0

    def test_returns_zero_for_missing_projects_dir(self):
        """Should return 0 if projects directory doesn't exist."""
        with mock.patch("project_manager.get_projects_dir") as mock_dir:
            mock_dir.return_value = Path("/nonexistent/projects")

            count = update_session_index_paths("/old", "/new")

        assert count == 0


# =============================================================================
# Find Orphaned Folders Tests
# =============================================================================


class TestFindOrphanedFolders:
    """Tests for find_orphaned_folders function."""

    def test_tracked_folder_not_orphan(self):
        """Folder in encodedPaths should not be flagged as orphan,
        even if sessions-index.json has stale originalPath."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
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

            with mock.patch("project_manager.get_projects_dir") as mock_dir, \
                 mock.patch("project_manager.get_projects_index_file") as mock_idx, \
                 mock.patch("project_manager.load_json_file") as mock_load, \
                 mock.patch("project_manager.get_claude_dir") as mock_claude:
                mock_dir.return_value = projects_dir
                mock_idx.return_value = Path(tmpdir) / "index.json"
                mock_load.return_value = index
                mock_claude.return_value = Path(tmpdir)

                orphans = find_orphaned_folders()

            assert len(orphans) == 0

    def test_untracked_folder_with_stale_path_is_orphan(self):
        """Folder not in encodedPaths with stale originalPath should be orphan."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            projects_dir.mkdir()

            folder = projects_dir / "-home-user-old-project"
            folder.mkdir()
            (folder / "sessions-index.json").write_text(json.dumps({
                "originalPath": "/home/user/old-project",  # Path doesn't exist
                "entries": []
            }))

            index = {"projects": {}}  # Not tracked

            with mock.patch("project_manager.get_projects_dir") as mock_dir, \
                 mock.patch("project_manager.get_projects_index_file") as mock_idx, \
                 mock.patch("project_manager.load_json_file") as mock_load, \
                 mock.patch("project_manager.get_claude_dir") as mock_claude:
                mock_dir.return_value = projects_dir
                mock_idx.return_value = Path(tmpdir) / "index.json"
                mock_load.return_value = index
                mock_claude.return_value = Path(tmpdir)

                orphans = find_orphaned_folders()

            assert len(orphans) == 1
            assert orphans[0].folder_name == "-home-user-old-project"

    def test_folder_with_valid_path_not_orphan(self):
        """Folder whose originalPath exists on disk should not be orphan."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
            projects_dir.mkdir()

            # Create the actual project directory so path exists
            real_project = Path(tmpdir) / "my-project"
            real_project.mkdir()

            folder = projects_dir / "-tmp-my-project"
            folder.mkdir()
            (folder / "sessions-index.json").write_text(json.dumps({
                "originalPath": str(real_project),
                "entries": []
            }))

            index = {"projects": {}}  # Not tracked, but path exists

            with mock.patch("project_manager.get_projects_dir") as mock_dir, \
                 mock.patch("project_manager.get_projects_index_file") as mock_idx, \
                 mock.patch("project_manager.load_json_file") as mock_load, \
                 mock.patch("project_manager.get_claude_dir") as mock_claude:
                mock_dir.return_value = projects_dir
                mock_idx.return_value = Path(tmpdir) / "index.json"
                mock_load.return_value = index
                mock_claude.return_value = Path(tmpdir)

                orphans = find_orphaned_folders()

            assert len(orphans) == 0

    def test_old_encoded_path_also_tracked(self):
        """Old encoded path kept in encodedPaths should not be orphan."""
        with tempfile.TemporaryDirectory() as tmpdir:
            projects_dir = Path(tmpdir) / "projects"
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

            with mock.patch("project_manager.get_projects_dir") as mock_dir, \
                 mock.patch("project_manager.get_projects_index_file") as mock_idx, \
                 mock.patch("project_manager.load_json_file") as mock_load, \
                 mock.patch("project_manager.get_claude_dir") as mock_claude:
                mock_dir.return_value = projects_dir
                mock_idx.return_value = Path(tmpdir) / "index.json"
                mock_load.return_value = index
                mock_claude.return_value = Path(tmpdir)

                orphans = find_orphaned_folders()

            assert len(orphans) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
