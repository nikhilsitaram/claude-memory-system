"""Tests for install.py — verifies shared imports and install functions."""

import json
import sys
from collections import namedtuple
from unittest import mock

import pytest
from memory_utils import MIN_PYTHON as UTILS_MIN_PYTHON

# Fake version_info that supports both tuple comparison and .major/.minor attrs
_VersionInfo = namedtuple("version_info", "major minor micro releaselevel serial")


def _version(major, minor):
    return _VersionInfo(major, minor, 0, "final", 0)

from memory_utils import get_claude_dir as utils_get_claude_dir
from memory_utils import get_memory_dir as utils_get_memory_dir
from memory_utils import load_json_file as utils_load_json_file
from memory_utils import save_json_file as utils_save_json_file

import install

# ---------------------------------------------------------------------------
# Shared import tests — install.py re-exports from memory_utils
# ---------------------------------------------------------------------------


class TestSharedImports:
    """Verify install.py uses memory_utils functions, not local copies."""

    def test_min_python_is_same_object(self):
        assert install.MIN_PYTHON is UTILS_MIN_PYTHON

    def test_get_claude_dir_is_same_function(self):
        assert install.get_claude_dir is utils_get_claude_dir

    def test_get_memory_dir_is_same_function(self):
        assert install.get_memory_dir is utils_get_memory_dir

    def test_load_json_file_is_same_function(self):
        assert install.load_json_file is utils_load_json_file

    def test_save_json_file_is_same_function(self):
        assert install.save_json_file is utils_save_json_file


# ---------------------------------------------------------------------------
# check_python_version — install.py keeps its own (user-facing with URLs)
# ---------------------------------------------------------------------------


class TestCheckPythonVersion:
    def test_passes_on_current_version(self):
        # Should not raise
        install.check_python_version()

    def test_exits_on_old_version(self):
        with mock.patch.object(sys, "version_info", _version(3, 7)):
            with pytest.raises(SystemExit):
                install.check_python_version()

    def test_error_message_includes_install_links(self, capsys):
        with mock.patch.object(sys, "version_info", _version(3, 7)):
            with pytest.raises(SystemExit):
                install.check_python_version()
        output = capsys.readouterr().out
        assert "python.org" in output
        assert "pyenv" in output
        assert "conda" in output


# ---------------------------------------------------------------------------
# detect_python_command
# ---------------------------------------------------------------------------


class TestDetectPythonCommand:
    def test_returns_string(self):
        result = install.detect_python_command()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_fallback_to_executable(self):
        """When no python3/python found, falls back to sys.executable."""
        with mock.patch("install.subprocess.run", side_effect=FileNotFoundError):
            result = install.detect_python_command()
            assert result == sys.executable


# ---------------------------------------------------------------------------
# create_directories
# ---------------------------------------------------------------------------


class TestCreateDirectories:
    def test_creates_all_directories(self, tmp_path):
        with mock.patch("memory_utils.Path.home", return_value=tmp_path):
            install.create_directories()

        expected = [
            tmp_path / ".claude" / "memory" / "daily",
            tmp_path / ".claude" / "memory" / "project-memory",
            tmp_path / ".claude" / "memory" / "templates",
            tmp_path / ".claude" / "memory" / ".backups",
            tmp_path / ".claude" / "scripts",
            tmp_path / ".claude" / "hooks",
            tmp_path / ".claude" / "skills",
        ]
        for d in expected:
            assert d.is_dir(), f"Missing directory: {d}"


# ---------------------------------------------------------------------------
# link_file
# ---------------------------------------------------------------------------


class TestLinkFile:
    def test_creates_symlink(self, tmp_path):
        src = tmp_path / "source.txt"
        src.write_text("hello")
        dest = tmp_path / "link.txt"

        install.link_file(src, dest)

        assert dest.is_symlink()
        assert dest.read_text() == "hello"

    def test_replaces_existing_file(self, tmp_path):
        src = tmp_path / "source.txt"
        src.write_text("new")
        dest = tmp_path / "link.txt"
        dest.write_text("old")

        install.link_file(src, dest)

        assert dest.is_symlink()
        assert dest.read_text() == "new"

    def test_replaces_existing_symlink(self, tmp_path):
        src1 = tmp_path / "old_source.txt"
        src1.write_text("old")
        src2 = tmp_path / "new_source.txt"
        src2.write_text("new")
        dest = tmp_path / "link.txt"
        dest.symlink_to(src1)

        install.link_file(src2, dest)

        assert dest.resolve() == src2
        assert dest.read_text() == "new"


# ---------------------------------------------------------------------------
# hook_entry_key
# ---------------------------------------------------------------------------


class TestHookEntryKey:
    def test_extracts_matcher_and_commands(self):
        entry = {
            "matcher": "startup",
            "hooks": [{"command": "python3 load.py"}, {"command": "bash hook.sh"}],
        }
        key = install.hook_entry_key(entry)
        assert key == ("startup", ("python3 load.py", "bash hook.sh"))

    def test_empty_entry(self):
        key = install.hook_entry_key({})
        assert key == ("", ())


# ---------------------------------------------------------------------------
# merge_hooks
# ---------------------------------------------------------------------------


class TestMergeHooks:
    def test_adds_hooks_to_empty_settings(self):
        settings = install.merge_hooks({}, "python3")
        assert "PreToolUse" in settings["hooks"]
        assert "SessionStart" in settings["hooks"]

    def test_does_not_duplicate_existing_hooks(self):
        settings = install.merge_hooks({}, "python3")
        count_before = len(settings["hooks"]["SessionStart"])
        settings = install.merge_hooks(settings, "python3")
        count_after = len(settings["hooks"]["SessionStart"])
        assert count_before == count_after


# ---------------------------------------------------------------------------
# merge_permissions
# ---------------------------------------------------------------------------


class TestMergePermissions:
    def test_adds_permissions_to_empty_settings(self):
        settings = install.merge_permissions({})
        assert len(settings["permissions"]["allow"]) > 0

    def test_does_not_duplicate_existing_permissions(self):
        settings = install.merge_permissions({})
        count_before = len(settings["permissions"]["allow"])
        settings = install.merge_permissions(settings)
        count_after = len(settings["permissions"]["allow"])
        assert count_before == count_after


# ---------------------------------------------------------------------------
# remove_obsolete_hooks
# ---------------------------------------------------------------------------


class TestRemoveObsoleteHooks:
    def test_removes_save_session_hooks(self):
        settings = {
            "hooks": {
                "SessionEnd": [
                    {"hooks": [{"command": "python3 save_session.py"}]},
                    {"hooks": [{"command": "python3 other.py"}]},
                ]
            }
        }
        result = install.remove_obsolete_hooks(settings)
        assert len(result["hooks"]["SessionEnd"]) == 1
        assert "other.py" in result["hooks"]["SessionEnd"][0]["hooks"][0]["command"]

    def test_removes_entire_event_if_all_hooks_obsolete(self):
        settings = {
            "hooks": {
                "SessionEnd": [
                    {"hooks": [{"command": "python3 save_session.py"}]},
                ]
            }
        }
        result = install.remove_obsolete_hooks(settings)
        assert "SessionEnd" not in result["hooks"]

    def test_no_op_on_clean_settings(self):
        settings = {"hooks": {"PreToolUse": [{"hooks": [{"command": "bash hook.sh"}]}]}}
        result = install.remove_obsolete_hooks(settings)
        assert result == settings


# ---------------------------------------------------------------------------
# copy_templates
# ---------------------------------------------------------------------------


class TestCopyTemplates:
    def test_copies_templates_and_defaults(self, tmp_path):
        # Set up repo structure
        script_dir = tmp_path / "repo"
        templates_src = script_dir / "templates"
        templates_src.mkdir(parents=True)
        (templates_src / "global-long-term-memory.md").write_text("# LTM")
        (templates_src / "project-long-term-memory.md").write_text("# Project")
        (templates_src / "daily-template.md").write_text("# Daily")
        (templates_src / "settings.json").write_text('{"version": 3}')

        with mock.patch("memory_utils.Path.home", return_value=tmp_path):
            install.create_directories()
            install.copy_templates(script_dir)

        memory_dir = tmp_path / ".claude" / "memory"
        # Templates copied
        assert (memory_dir / "templates" / "global-long-term-memory.md").exists()
        assert (memory_dir / "templates" / "project-long-term-memory.md").exists()
        # Defaults created
        assert (memory_dir / "global-long-term-memory.md").read_text() == "# LTM"
        assert json.loads((memory_dir / "settings.json").read_text())["version"] == 3

    def test_does_not_overwrite_existing_files(self, tmp_path):
        script_dir = tmp_path / "repo"
        templates_src = script_dir / "templates"
        templates_src.mkdir(parents=True)
        (templates_src / "global-long-term-memory.md").write_text("# New")
        (templates_src / "settings.json").write_text('{"version": 99}')

        with mock.patch("memory_utils.Path.home", return_value=tmp_path):
            install.create_directories()
            memory_dir = tmp_path / ".claude" / "memory"
            # Pre-create files with existing content
            (memory_dir / "global-long-term-memory.md").write_text("# Existing")
            (memory_dir / "settings.json").write_text('{"version": 1}')

            install.copy_templates(script_dir)

        # Existing files preserved
        assert (memory_dir / "global-long-term-memory.md").read_text() == "# Existing"
        assert json.loads((memory_dir / "settings.json").read_text())["version"] == 1
