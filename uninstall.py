#!/usr/bin/env python3
"""
Cross-platform uninstaller for Claude Code Memory System.

This script:
1. Removes memory system hooks from settings.json
2. Removes memory system permissions from settings.json
3. Optionally deletes all memory data with --purge flag

Usage:
    python3 uninstall.py           # Remove hooks/permissions, keep data
    python3 uninstall.py --purge   # Remove everything including memory data

Requirements: Python 3.9+
"""

import argparse
import json
import shutil
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

    # Patterns that identify memory system hooks
    memory_patterns = [
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

    # Exact permission strings installed by the memory system
    permissions_to_remove = [
        "Read(~/.claude/**)",
        f"Read(/{home}/.claude/projects/**)",
    ]

    original_count = len(settings["permissions"]["allow"])

    # Filter out memory system permissions (exact match)
    settings["permissions"]["allow"] = [
        p
        for p in settings["permissions"]["allow"]
        if p not in permissions_to_remove
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


def purge_memory_data() -> None:
    """Delete all memory system files (scripts, skills, hooks, data)."""
    claude_dir = get_claude_dir()

    # Files and directories to remove
    items_to_remove = [
        # Memory data
        claude_dir / "memory",
        # Skills
        claude_dir / "skills" / "remember",
        claude_dir / "skills" / "synthesize",
        claude_dir / "skills" / "recall",
        claude_dir / "skills" / "reload",
        claude_dir / "skills" / "settings",
        # Hook scripts
        claude_dir / "hooks" / "pretooluse-allow-memory.sh",
        # Scripts
        claude_dir / "scripts" / "memory_utils.py",
        claude_dir / "scripts" / "load_memory.py",
        claude_dir / "scripts" / "save_session.py",
        claude_dir / "scripts" / "indexing.py",
        claude_dir / "scripts" / "load-project-memory.py",
    ]

    removed = []
    for item in items_to_remove:
        if item.exists():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            removed.append(str(item.relative_to(Path.home())))

    # Clean up empty parent directories
    for parent in [claude_dir / "hooks", claude_dir / "scripts", claude_dir / "skills"]:
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            removed.append(str(parent.relative_to(Path.home())))

    if removed:
        print(f"Removed {len(removed)} items:")
        for item in removed:
            print(f"  ~/{item}")


def print_cleanup_instructions() -> None:
    """Print instructions for fully removing the memory system."""
    print()
    print("=" * 60)
    print("Memory system hooks and permissions removed.")
    print("=" * 60)
    print()
    print("Memory data preserved at: ~/.claude/memory/")
    print()
    print("To fully remove all files, run:")
    print("  python3 uninstall.py --purge")
    print()
    print("Or manually:")
    print()
    print("  rm -rf ~/.claude/memory")
    print("  rm -rf ~/.claude/skills/{remember,synthesize,recall,reload,settings}")
    print("  rm -rf ~/.claude/hooks  # if empty after removing memory hook")
    print("  rm ~/.claude/scripts/{memory_utils,load_memory,save_session,indexing,load-project-memory}.py")


def main() -> int:
    """Main uninstallation routine."""
    parser = argparse.ArgumentParser(
        description="Uninstall Claude Code Memory System"
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete all memory data, scripts, skills, and hooks",
    )
    args = parser.parse_args()

    print("Uninstalling Claude Code Memory System...")
    print()

    # Check if settings file exists
    settings_file = get_claude_dir() / "settings.json"

    if settings_file.exists():
        # Load settings
        settings = load_json_file(settings_file)

        if settings:
            # Remove hooks
            settings = remove_hooks(settings)

            # Remove permissions
            settings = remove_permissions(settings)

            # Save updated settings
            save_json_file(settings_file, settings)
            print(f"Updated {settings_file}")
    else:
        print("No settings.json found, skipping hook/permission removal.")

    # Purge data if requested
    if args.purge:
        print()
        purge_memory_data()
        print()
        print("=" * 60)
        print("Memory system completely removed.")
        print("=" * 60)
    else:
        # Print cleanup instructions
        print_cleanup_instructions()

    return 0


if __name__ == "__main__":
    sys.exit(main())
