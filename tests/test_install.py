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
from memory_utils import DEFAULT_SETTINGS

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
# detect_uv_command
# ---------------------------------------------------------------------------


class TestDetectUvCommand:
    def test_returns_path_when_uv_present(self):
        with mock.patch("install.shutil.which", return_value="/opt/homebrew/bin/uv"):
            assert install.detect_uv_command() == "/opt/homebrew/bin/uv"

    def test_exits_when_uv_missing(self, capsys):
        with mock.patch("install.shutil.which", return_value=None):
            with pytest.raises(SystemExit):
                install.detect_uv_command()
        output = capsys.readouterr().out
        assert "uv" in output
        assert "astral.sh/uv" in output


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
        settings = install.merge_hooks({}, "uv")
        assert "PreToolUse" in settings["hooks"]
        assert "SessionStart" in settings["hooks"]

    def test_does_not_duplicate_existing_hooks(self):
        settings = install.merge_hooks({}, "uv")
        count_before = len(settings["hooks"]["SessionStart"])
        settings = install.merge_hooks(settings, "uv")
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


# ---------------------------------------------------------------------------
# install_systemd_units
# ---------------------------------------------------------------------------


class TestInstallSystemdUnits:
    def test_copies_service_file_with_uv_path_substituted(self, tmp_path):
        """Service unit is installed with __UV_PATH__ replaced by the resolved uv path."""
        script_dir = tmp_path / "repo"
        systemd_src = script_dir / "systemd"
        systemd_src.mkdir(parents=True)
        (systemd_src / "claude-memory-synthesis.service").write_text(
            "[Service]\nExecStart=__UV_PATH__ run %h/.claude/scripts/synthesis_cron.py"
        )
        (systemd_src / "claude-memory-synthesis.timer").write_text("[Timer]\nOnCalendar=*:0/15")

        systemd_user_dir = tmp_path / ".config" / "systemd" / "user"

        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            install.install_systemd_units(script_dir, "/home/user/.local/bin/uv")

        installed = (systemd_user_dir / "claude-memory-synthesis.service").read_text()
        assert "__UV_PATH__" not in installed
        assert "ExecStart=/home/user/.local/bin/uv run" in installed

    def test_copies_timer_file(self, tmp_path):
        """Timer unit is installed verbatim to ~/.config/systemd/user/."""
        script_dir = tmp_path / "repo"
        systemd_src = script_dir / "systemd"
        systemd_src.mkdir(parents=True)
        (systemd_src / "claude-memory-synthesis.service").write_text("[Service]\nExecStart=/bin/true")
        (systemd_src / "claude-memory-synthesis.timer").write_text("[Timer]\nOnCalendar=*:0/15")

        systemd_user_dir = tmp_path / ".config" / "systemd" / "user"

        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            install.install_systemd_units(script_dir, "/home/user/.local/bin/uv")

        assert (systemd_user_dir / "claude-memory-synthesis.timer").exists()
        assert (systemd_user_dir / "claude-memory-synthesis.timer").read_text() == "[Timer]\nOnCalendar=*:0/15"

    def test_calls_daemon_reload_and_enable(self, tmp_path):
        """After copying, calls systemctl daemon-reload and enable --now."""
        script_dir = tmp_path / "repo"
        systemd_src = script_dir / "systemd"
        systemd_src.mkdir(parents=True)
        (systemd_src / "claude-memory-synthesis.service").write_text("[Service]")
        (systemd_src / "claude-memory-synthesis.timer").write_text("[Timer]")

        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            install.install_systemd_units(script_dir, "/home/user/.local/bin/uv")

        # Expect 3 calls: version check, daemon-reload, enable --now
        assert mock_run.call_count == 3
        calls = mock_run.call_args_list
        # First call: version check
        assert calls[0][0][0] == ["systemctl", "--user", "--version"]
        # Second call: daemon-reload
        assert calls[1][0][0] == ["systemctl", "--user", "daemon-reload"]
        # Third call: enable --now timer
        assert calls[2][0][0] == [
            "systemctl", "--user", "enable", "--now",
            "claude-memory-synthesis.timer",
        ]

    def test_skips_if_systemd_not_available(self, tmp_path, capsys):
        """Gracefully skips if systemctl not found."""
        script_dir = tmp_path / "repo"

        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.subprocess.run", side_effect=FileNotFoundError):
            install.install_systemd_units(script_dir, "/home/user/.local/bin/uv")

        output = capsys.readouterr().out
        assert "systemctl not available" in output
        # No systemd dir created
        systemd_user_dir = tmp_path / ".config" / "systemd" / "user"
        assert not systemd_user_dir.exists()

    def test_skips_if_systemctl_times_out(self, tmp_path, capsys):
        """Gracefully skips if systemctl --version times out."""
        script_dir = tmp_path / "repo"
        import subprocess

        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.subprocess.run", side_effect=subprocess.TimeoutExpired("systemctl", 5)):
            install.install_systemd_units(script_dir, "/home/user/.local/bin/uv")

        output = capsys.readouterr().out
        assert "systemctl not available" in output

    def test_skips_missing_unit_files(self, tmp_path):
        """Does not fail if unit files are missing from repo."""
        script_dir = tmp_path / "repo"
        # No systemd dir at all

        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            install.install_systemd_units(script_dir, "/home/user/.local/bin/uv")

        # Systemd user dir created (mkdir) but no files copied
        systemd_user_dir = tmp_path / ".config" / "systemd" / "user"
        assert systemd_user_dir.is_dir()
        assert not (systemd_user_dir / "claude-memory-synthesis.service").exists()
        assert not (systemd_user_dir / "claude-memory-synthesis.timer").exists()


# ---------------------------------------------------------------------------
# _session_end_command
# ---------------------------------------------------------------------------


class TestSessionEndCommand:
    def test_returns_launchctl_on_darwin(self):
        with mock.patch("install.sys") as mock_sys, \
             mock.patch("install.os") as mock_os:
            mock_sys.platform = "darwin"
            mock_os.getuid.return_value = 501
            result = install._session_end_command()
        assert "launchctl kickstart" in result
        assert "gui/501/" in result
        assert install.LAUNCHD_LABEL in result

    def test_returns_systemctl_on_linux(self):
        with mock.patch("install.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = install._session_end_command()
        assert "systemctl" in result
        assert "--no-block" in result


# ---------------------------------------------------------------------------
# SessionEnd hook
# ---------------------------------------------------------------------------


class TestSessionEndHook:
    def test_session_end_hook_added_to_settings(self):
        """merge_hooks adds a SessionEnd hook."""
        settings = {"hooks": {}}
        result = install.merge_hooks(settings, "uv")
        assert "SessionEnd" in result["hooks"]
        hooks = result["hooks"]["SessionEnd"]
        assert len(hooks) >= 1

    def test_session_end_hook_has_short_timeout(self):
        """SessionEnd hook has a short timeout since it's fire-and-forget."""
        settings = install.merge_hooks({}, "uv")
        hooks = settings["hooks"]["SessionEnd"]
        for entry in hooks:
            for h in entry.get("hooks", []):
                cmd = h.get("command", "")
                if "claude-memory-synthesis" in cmd:
                    assert h.get("timeout", 999) <= 5

    def test_session_end_hook_not_duplicated(self):
        """Running merge_hooks twice does not duplicate SessionEnd entries."""
        settings = install.merge_hooks({}, "uv")
        count_before = len(settings["hooks"]["SessionEnd"])
        settings = install.merge_hooks(settings, "uv")
        count_after = len(settings["hooks"]["SessionEnd"])
        assert count_before == count_after

    def test_migration_removes_old_python3_recall_hook(self):
        """Old `python3 .../session_end_recall.py` entries are removed on re-install."""
        settings = {
            "hooks": {
                "SessionEnd": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 /home/u/.claude/scripts/session_end_recall.py",
                                "timeout": 10,
                            },
                            {
                                "type": "command",
                                "command": "systemctl --user start --no-block claude-memory-synthesis.service",
                                "timeout": 5,
                            },
                        ],
                    }
                ]
            }
        }
        result = install.merge_hooks(settings, "uv")
        commands = [
            h.get("command", "")
            for entry in result["hooks"]["SessionEnd"]
            for h in entry.get("hooks", [])
        ]
        # Old python3-invoked recall is gone
        assert not any(c.startswith("python3 ") for c in commands)
        # New uv-invoked recall is present
        assert any("uv run" in c and "session_end_recall.py" in c for c in commands)

    def test_migration_removes_old_python3_session_start_hook(self):
        """Old `python3 .../load_memory.py` SessionStart entries are removed on re-install."""
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 /home/u/.claude/scripts/load_memory.py",
                                "timeout": 30,
                            }
                        ],
                    }
                ]
            }
        }
        result = install.merge_hooks(settings, "uv")
        commands = [
            h.get("command", "")
            for entry in result["hooks"]["SessionStart"]
            for h in entry.get("hooks", [])
        ]
        assert not any(c.startswith("python3 ") for c in commands)
        # All four matchers (startup/resume/clear/compact) added with uv run
        assert sum(1 for c in commands if "uv run" in c and "load_memory.py" in c) == 4

    def test_migration_removes_old_systemctl_hook(self):
        """On macOS, old systemctl hook is replaced with launchctl."""
        settings = {
            "hooks": {
                "SessionEnd": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "systemctl --user start --no-block claude-memory-synthesis.service",
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }
        with mock.patch("install.sys") as mock_sys, \
             mock.patch("install.os") as mock_os:
            mock_sys.platform = "darwin"
            mock_os.getuid.return_value = 501
            result = install.merge_hooks(settings, "uv")

        hooks = result["hooks"]["SessionEnd"]
        commands = [
            h.get("command", "")
            for entry in hooks
            for h in entry.get("hooks", [])
        ]
        assert not any("systemctl" in c for c in commands)
        assert any("launchctl" in c for c in commands)

    def test_migration_removes_old_launchctl_hook(self):
        """On Linux, old launchctl hook is replaced with systemctl."""
        settings = {
            "hooks": {
                "SessionEnd": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "launchctl kickstart gui/501/com.claude.memory-synthesis",
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }
        with mock.patch("install.sys") as mock_sys:
            mock_sys.platform = "linux"
            result = install.merge_hooks(settings, "uv")

        hooks = result["hooks"]["SessionEnd"]
        commands = [
            h.get("command", "")
            for entry in hooks
            for h in entry.get("hooks", [])
        ]
        assert not any("launchctl" in c for c in commands)
        assert any("systemctl" in c for c in commands)

    def test_preserves_non_synthesis_session_end_hooks(self):
        """Migration only removes synthesis hooks, not other SessionEnd hooks."""
        settings = {
            "hooks": {
                "SessionEnd": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": "echo done"}],
                    },
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "systemctl --user start --no-block claude-memory-synthesis.service",
                            }
                        ],
                    },
                ]
            }
        }
        result = install.merge_hooks(settings, "uv")
        commands = [
            h.get("command", "")
            for entry in result["hooks"]["SessionEnd"]
            for h in entry.get("hooks", [])
        ]
        assert any("echo done" in c for c in commands)


class TestSessionEndRecallHook:
    """Tests for the session_end_recall.py SessionEnd hook."""

    def test_session_end_has_two_hooks(self):
        """SessionEnd hook has recall script first, then deferred synthesis."""
        settings = {"hooks": {}}
        result = install.merge_hooks(settings, "uv")
        session_end = result["hooks"]["SessionEnd"]
        assert len(session_end) == 1
        hooks = session_end[0]["hooks"]
        assert len(hooks) == 2
        assert "session_end_recall.py" in hooks[0]["command"]
        assert hooks[0]["timeout"] == 10
        assert "memory-synthesis" in hooks[1]["command"]
        assert hooks[1]["timeout"] == 5

    def test_recall_script_in_link_scripts(self):
        """session_end_recall.py is in the scripts to link."""
        import inspect
        source = inspect.getsource(install.link_scripts)
        assert "session_end_recall.py" in source


# ---------------------------------------------------------------------------
# PreCompact hook
# ---------------------------------------------------------------------------


class TestPreCompactHook:
    def test_precompact_hook_added_to_settings(self):
        """merge_hooks adds a PreCompact hook."""
        settings = {"hooks": {}}
        result = install.merge_hooks(settings, "uv")
        assert "PreCompact" in result["hooks"]
        hooks = result["hooks"]["PreCompact"]
        assert len(hooks) >= 1

    def test_precompact_hook_has_short_timeout(self):
        """PreCompact synthesis trigger has a short timeout since it's fire-and-forget."""
        settings = install.merge_hooks({}, "uv")
        hooks = settings["hooks"]["PreCompact"]
        for entry in hooks:
            for h in entry.get("hooks", []):
                assert h.get("timeout", 999) <= 10

    def test_precompact_hook_not_duplicated(self):
        """Running merge_hooks twice does not duplicate PreCompact entries."""
        settings = install.merge_hooks({}, "uv")
        count_before = len(settings["hooks"]["PreCompact"])
        settings = install.merge_hooks(settings, "uv")
        count_after = len(settings["hooks"]["PreCompact"])
        assert count_before == count_after

    def test_precompact_hook_has_two_hooks(self):
        """PreCompact hook has recall script first, then deferred synthesis."""
        settings = {"hooks": {}}
        result = install.merge_hooks(settings, "uv")
        precompact = result["hooks"]["PreCompact"]
        assert len(precompact) == 1
        hooks = precompact[0]["hooks"]
        assert len(hooks) == 2
        assert "session_end_recall.py" in hooks[0]["command"]
        assert hooks[0]["timeout"] == 10
        assert "memory-synthesis" in hooks[1]["command"]
        assert hooks[1]["timeout"] == 5

    def test_migration_removes_old_synthesis_precompact_hook(self):
        """Existing PreCompact synthesis hooks are replaced on re-install."""
        settings = {
            "hooks": {
                "PreCompact": [
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "systemctl --user start --no-block claude-memory-synthesis.service",
                                "timeout": 5,
                            }
                        ],
                    }
                ]
            }
        }
        with mock.patch("install.sys") as mock_sys, \
             mock.patch("install.os") as mock_os:
            mock_sys.platform = "darwin"
            mock_os.getuid.return_value = 501
            result = install.merge_hooks(settings, "uv")
        commands = [
            h.get("command", "")
            for entry in result["hooks"]["PreCompact"]
            for h in entry.get("hooks", [])
        ]
        assert not any("systemctl --user start --no-block claude-memory-synthesis" in c for c in commands)

    def test_preserves_non_synthesis_precompact_hooks(self):
        """Migration only removes synthesis hooks, not other PreCompact hooks."""
        settings = {
            "hooks": {
                "PreCompact": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": "echo compacting"}],
                    },
                    {
                        "matcher": "",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "systemctl --user start --no-block claude-memory-synthesis.service",
                            }
                        ],
                    },
                ]
            }
        }
        result = install.merge_hooks(settings, "uv")
        commands = [
            h.get("command", "")
            for entry in result["hooks"]["PreCompact"]
            for h in entry.get("hooks", [])
        ]
        assert any("echo compacting" in c for c in commands)


# ---------------------------------------------------------------------------
# install_launchd_agent
# ---------------------------------------------------------------------------


class TestInstallLaunchdAgent:
    def test_creates_plist_file(self, tmp_path):
        """Plist is created in ~/Library/LaunchAgents/."""
        import plistlib

        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.os.getuid", return_value=501), \
             mock.patch("install.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            install.install_launchd_agent("/opt/homebrew/bin/uv")

        plist_path = tmp_path / "Library" / "LaunchAgents" / f"{install.LAUNCHD_LABEL}.plist"
        assert plist_path.exists()

        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)

        assert plist["Label"] == install.LAUNCHD_LABEL
        assert plist["StartInterval"] == int(DEFAULT_SETTINGS["synthesis"]["intervalHours"] * 3600)
        assert plist["EnvironmentVariables"]["CLAUDECODE"] == ""
        # ProgramArguments = [uv_path, "run", script_path]
        assert plist["ProgramArguments"][1] == "run"
        assert "synthesis_cron.py" in plist["ProgramArguments"][2]

    def test_calls_bootout_then_bootstrap(self, tmp_path):
        """Calls launchctl bootout (cleanup) then bootstrap (load)."""
        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.os.getuid", return_value=501), \
             mock.patch("install.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            install.install_launchd_agent("/opt/homebrew/bin/uv")

        calls = mock_run.call_args_list
        assert len(calls) == 2
        # First: bootout
        assert "bootout" in calls[0][0][0]
        assert "gui/501/" in calls[0][0][0][2]
        # Second: bootstrap
        assert "bootstrap" in calls[1][0][0]
        assert "gui/501" in calls[1][0][0][2]

    def test_creates_log_directory(self, tmp_path):
        """Creates ~/Library/Logs/claude-memory/ for output."""
        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.os.getuid", return_value=501), \
             mock.patch("install.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            install.install_launchd_agent("/opt/homebrew/bin/uv")

        assert (tmp_path / "Library" / "Logs" / "claude-memory").is_dir()

    def test_uses_passed_uv_path(self, tmp_path):
        """ProgramArguments uses the absolute uv path passed in."""
        import plistlib

        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.os.getuid", return_value=501), \
             mock.patch("install.subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(returncode=0)
            install.install_launchd_agent("/opt/homebrew/bin/uv")

        plist_path = tmp_path / "Library" / "LaunchAgents" / f"{install.LAUNCHD_LABEL}.plist"
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)
        assert plist["ProgramArguments"][0] == "/opt/homebrew/bin/uv"
        assert plist["ProgramArguments"][1] == "run"

    def test_warns_on_bootstrap_failure(self, tmp_path, capsys):
        """Prints warning if launchctl bootstrap fails."""
        with mock.patch("install.Path.home", return_value=tmp_path), \
             mock.patch("install.os.getuid", return_value=501), \
             mock.patch("install.subprocess.run") as mock_run:
            # bootout succeeds, bootstrap fails
            mock_run.side_effect = [
                mock.Mock(returncode=0),
                mock.Mock(returncode=1),
            ]
            install.install_launchd_agent("/opt/homebrew/bin/uv")

        output = capsys.readouterr().out
        assert "Warning: launchctl bootstrap failed" in output
