#!/usr/bin/env python3
"""
Cross-platform uninstaller for Claude Code Memory System.

This script:
1. Removes memory system hooks from settings.json (both bash and Python)
2. Removes memory system permissions from settings.json
3. Removes old cron job if it exists
4. Preserves memory data (doesn't delete ~/.claude/memory/)

Usage:
    python3 uninstall.py
    python uninstall.py

The script will NOT delete your memory data. To fully remove:
    rm -rf ~/.claude/memory
    rm -rf ~/.claude/skills/{remember,synthesize,recall,reload,settings}
    rm -f ~/.claude/hooks/pretooluse-allow-memory.sh
    rm ~/.claude/scripts/{memory_utils,load_memory,save_session,indexing,load-project-memory}.py

Requirements: Python 3.9+
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def get_claude_dir() -> Path:
    """Get the Claude configuration directory."""
    return Path.home() / ".claude"


def load_json_file(filepath: Path) -> dict:
    """Load JSON file with error handling."""
    if not filepath.exists():
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Warning: Could not parse {filepath}: {e}")
        return {}


def save_json_file(filepath: Path, data: dict) -> None:
    """Save dict to JSON file."""
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def remove_hooks(settings: dict) -> dict:
    """Remove memory system hooks from settings."""
    if "hooks" not in settings:
        return settings

    # Patterns that identify memory system hooks (both bash and Python versions)
    memory_patterns = [
        "load-memory.sh",
        "save-session.sh",
        "recover-transcripts.sh",
        "load_memory.py",
        "save_session.py",
        "pretooluse-allow-memory.sh",
    ]

    removed_count = 0

    for event in ["SessionStart", "SessionEnd", "PreCompact", "PreToolUse"]:
        if event not in settings["hooks"]:
            continue

        original_count = len(settings["hooks"][event])

        # Filter out memory system hooks
        settings["hooks"][event] = [
            entry
            for entry in settings["hooks"][event]
            if not any(
                pattern in hook.get("command", "")
                for hook in entry.get("hooks", [])
                for pattern in memory_patterns
            )
        ]

        removed_count += original_count - len(settings["hooks"][event])

        # Remove empty arrays
        if not settings["hooks"][event]:
            del settings["hooks"][event]

    # Remove empty hooks object
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]

    if removed_count > 0:
        print(f"Removed {removed_count} hook entries")

    return settings


def remove_permissions(settings: dict) -> dict:
    """Remove memory system permissions from settings."""
    if "permissions" not in settings or "allow" not in settings["permissions"]:
        return settings

    home = str(Path.home())

    # Patterns that identify memory system permissions
    # Match both absolute paths and tilde paths
    permission_patterns = [
        "/.claude/**",
        "/.claude/memory",
        "/.claude/projects",
        "~/.claude/memory",
        "rm -rf",
        "claude-memory-system",  # Repo directory permissions
    ]

    original_count = len(settings["permissions"]["allow"])

    # Filter out memory system permissions
    settings["permissions"]["allow"] = [
        p
        for p in settings["permissions"]["allow"]
        if not any(pattern in p for pattern in permission_patterns)
    ]

    removed_count = original_count - len(settings["permissions"]["allow"])

    # Remove empty allow array
    if not settings["permissions"]["allow"]:
        del settings["permissions"]["allow"]

    # Remove empty permissions object
    if "permissions" in settings and not settings["permissions"]:
        del settings["permissions"]

    if removed_count > 0:
        print(f"Removed {removed_count} permissions")

    return settings


def remove_cron_job() -> None:
    """Remove memory system cron job if it exists."""
    if os.name == "nt":
        # Windows doesn't have cron
        return

    try:
        # Get current crontab
        result = subprocess.run(
            ["crontab", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
        )

        if result.returncode == 0 and "recover-transcripts.sh" in result.stdout:
            # Remove the memory system cron job
            new_crontab = "\n".join(
                line
                for line in result.stdout.splitlines()
                if "recover-transcripts.sh" not in line
            )

            # Update crontab
            proc = subprocess.Popen(
                ["crontab", "-"],
                stdin=subprocess.PIPE,
                text=True,
            )
            proc.communicate(input=new_crontab)

            print("Removed cron job")

    except (subprocess.TimeoutExpired, FileNotFoundError):
        # crontab not available
        pass


def print_cleanup_instructions() -> None:
    """Print instructions for fully removing the memory system."""
    print()
    print("=" * 60)
    print("Memory system hooks and permissions removed.")
    print("=" * 60)
    print()
    print("Memory data preserved at: ~/.claude/memory/")
    print("Memory settings preserved at: ~/.claude/memory/settings.json")
    print()
    print("To fully remove all files, run:")
    print()
    print("  rm -rf ~/.claude/memory")
    print("  rm -rf ~/.claude/skills/{remember,synthesize,recall,reload,settings}")
    print("  rm -f ~/.claude/hooks/pretooluse-allow-memory.sh")
    print("  rm ~/.claude/scripts/memory_utils.py")
    print("  rm ~/.claude/scripts/load_memory.py")
    print("  rm ~/.claude/scripts/save_session.py")
    print("  rm ~/.claude/scripts/indexing.py")
    print("  rm ~/.claude/scripts/load-project-memory.py")
    print()
    print("Or on Windows PowerShell:")
    print()
    print("  Remove-Item -Recurse ~/.claude/memory")
    print("  Remove-Item -Recurse ~/.claude/skills/remember,")
    print("                       ~/.claude/skills/synthesize,")
    print("                       ~/.claude/skills/recall,")
    print("                       ~/.claude/skills/reload,")
    print("                       ~/.claude/skills/settings")
    print("  Remove-Item ~/.claude/scripts/memory_utils.py,")
    print("              ~/.claude/scripts/load_memory.py,")
    print("              ~/.claude/scripts/save_session.py,")
    print("              ~/.claude/scripts/indexing.py,")
    print("              ~/.claude/scripts/load-project-memory.py")


def main() -> int:
    """Main uninstallation routine."""
    print("Uninstalling Claude Code Memory System...")
    print()

    # Check if settings file exists
    settings_file = get_claude_dir() / "settings.json"

    if not settings_file.exists():
        print("No settings.json found, nothing to uninstall.")
        return 0

    # Load settings
    settings = load_json_file(settings_file)

    if not settings:
        print("Could not read settings.json, skipping hook removal.")
        return 0

    # Remove hooks
    settings = remove_hooks(settings)

    # Remove permissions
    settings = remove_permissions(settings)

    # Save updated settings
    save_json_file(settings_file, settings)
    print(f"Updated {settings_file}")

    # Remove cron job
    remove_cron_job()

    # Print cleanup instructions
    print_cleanup_instructions()

    return 0


if __name__ == "__main__":
    sys.exit(main())
