#!/usr/bin/env python3
"""
Cross-platform installer for Claude Code Memory System.

This script:
1. Checks Python version (requires 3.9+)
2. Detects available Python command (python3 vs python)
3. Backs up existing settings.json
4. Creates directory structure
5. Copies scripts and skills
6. Merges hooks into settings.json (with absolute paths)
7. Adds permissions
8. Builds project index

Usage:
    python3 install.py
    python install.py

Requirements: Python 3.9+
"""

import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Minimum Python version
MIN_PYTHON = (3, 9)


def check_python_version() -> None:
    """Check Python version and exit if too old."""
    if sys.version_info < MIN_PYTHON:
        print(f"Error: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required")
        print(f"Current version: {sys.version_info.major}.{sys.version_info.minor}")
        print()
        print("Options:")
        print("  - Install a newer Python: https://www.python.org/downloads/")
        print("  - Use pyenv: https://github.com/pyenv/pyenv")
        print("  - Use conda: https://docs.conda.io/")
        sys.exit(1)


def detect_python_command() -> str:
    """
    Detect which Python command to use in hooks.

    Checks python3 first (preferred on Unix), then python.
    Returns the command that points to Python 3.9+.
    """
    for cmd in ["python3", "python"]:
        try:
            result = subprocess.run(
                [cmd, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                version_str = result.stdout.strip()
                parts = version_str.split(".")
                if len(parts) >= 2:
                    major, minor = int(parts[0]), int(parts[1])
                    if major >= 3 and minor >= 9:
                        return cmd
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            continue

    # Fall back to current executable
    return sys.executable


def get_script_dir() -> Path:
    """Get the directory containing this install script."""
    return Path(__file__).parent.resolve()


def get_claude_dir() -> Path:
    """Get the Claude configuration directory."""
    return Path.home() / ".claude"


def get_memory_dir() -> Path:
    """Get the memory directory."""
    return get_claude_dir() / "memory"


def backup_settings(settings_file: Path) -> Path | None:
    """Backup existing settings.json. Returns backup path or None."""
    if not settings_file.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = settings_file.parent / f"settings.json.backup.{timestamp}"

    try:
        shutil.copy2(settings_file, backup_path)
        print(f"Backed up settings to: {backup_path}")
        return backup_path
    except IOError as e:
        print(f"Warning: Could not backup settings: {e}")
        return None


def load_json_file(filepath: Path) -> dict:
    """Load JSON file with error handling."""
    if not filepath.exists():
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        print(f"Warning: Could not parse {filepath}: {e}")
        print("Creating new settings file")
        return {}


def save_json_file(filepath: Path, data: dict) -> None:
    """Save dict to JSON file."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def create_directories() -> None:
    """Create required directory structure."""
    dirs = [
        get_memory_dir() / "daily",
        get_memory_dir() / "transcripts",
        get_memory_dir() / "project-memory",
        get_memory_dir() / ".backups",
        get_claude_dir() / "scripts",
        get_claude_dir() / "hooks",
        get_claude_dir() / "skills" / "remember",
        get_claude_dir() / "skills" / "synthesize",
        get_claude_dir() / "skills" / "recall",
        get_claude_dir() / "skills" / "reload",
        get_claude_dir() / "skills" / "settings",
        get_claude_dir() / "skills" / "projects",
    ]

    for dir_path in dirs:
        dir_path.mkdir(parents=True, exist_ok=True)

    print("Created directory structure")


def copy_scripts(script_dir: Path) -> None:
    """Copy Python scripts to ~/.claude/scripts/."""
    dest_dir = get_claude_dir() / "scripts"

    scripts_to_copy = [
        "memory_utils.py",
        "load_memory.py",
        "save_session.py",
        "indexing.py",
        "load-project-memory.py",  # Keep the existing utility
        "project_manager.py",  # Project lifecycle management
    ]

    for script_name in scripts_to_copy:
        src = script_dir / "scripts" / script_name
        if src.exists():
            dest = dest_dir / script_name
            shutil.copy2(src, dest)
            # Make executable on Unix
            if os.name != "nt":
                dest.chmod(dest.stat().st_mode | 0o755)

    print("Copied scripts to ~/.claude/scripts/")


def copy_hooks(script_dir: Path) -> None:
    """Copy hook scripts to ~/.claude/hooks/."""
    dest_dir = get_claude_dir() / "hooks"

    hooks_to_copy = [
        "pretooluse-allow-memory.sh",
    ]

    for hook_name in hooks_to_copy:
        src = script_dir / "hooks" / hook_name
        if src.exists():
            dest = dest_dir / hook_name
            shutil.copy2(src, dest)
            # Make executable on Unix
            if os.name != "nt":
                dest.chmod(dest.stat().st_mode | 0o755)

    print("Copied hooks to ~/.claude/hooks/")


def copy_skills(script_dir: Path) -> None:
    """Copy skill files to ~/.claude/skills/."""
    skills_dir = get_claude_dir() / "skills"

    skills = ["remember", "synthesize", "recall", "reload", "settings", "projects"]

    for skill in skills:
        src_dir = script_dir / "skills" / skill
        dest_dir = skills_dir / skill

        if src_dir.exists():
            # Copy SKILL.md
            src_skill = src_dir / "SKILL.md"
            if src_skill.exists():
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_skill, dest_dir / "SKILL.md")

    print("Copied skills to ~/.claude/skills/")


def copy_templates(script_dir: Path) -> None:
    """Copy template files if they don't exist."""
    memory_dir = get_memory_dir()

    # Copy global-long-term-memory.md template if it doesn't exist
    long_term_file = memory_dir / "global-long-term-memory.md"
    if not long_term_file.exists():
        src = script_dir / "templates" / "global-long-term-memory.md"
        if src.exists():
            shutil.copy2(src, long_term_file)
            print("Created default global-long-term-memory.md")

    # Copy settings.json template if it doesn't exist
    settings_file = memory_dir / "settings.json"
    if not settings_file.exists():
        src = script_dir / "templates" / "settings.json"
        if src.exists():
            shutil.copy2(src, settings_file)
            print("Created default memory settings at ~/.claude/memory/settings.json")

    # Initialize .captured file
    captured_file = memory_dir / ".captured"
    if not captured_file.exists():
        captured_file.touch()


def hook_entry_key(entry: dict) -> tuple:
    """Generate a unique key for a hook entry based on matcher and commands."""
    matcher = entry.get("matcher", "")
    commands = tuple(h.get("command", "") for h in entry.get("hooks", []))
    return (matcher, commands)


def merge_hooks(settings: dict, python_cmd: str) -> dict:
    """Merge memory system hooks into settings."""
    home = str(Path.home())
    scripts_dir = f"{home}/.claude/scripts"
    hooks_dir = f"{home}/.claude/hooks"

    hooks_to_add = {
        # PreToolUse hook auto-allows memory operations for subagents
        # This works around Claude Code bug where subagents don't inherit permissions
        # (GitHub issues #10906, #11934, #18172, #18950)
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"bash {hooks_dir}/pretooluse-allow-memory.sh",
                    }
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "startup",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{python_cmd} {scripts_dir}/load_memory.py",
                        "timeout": 30,
                    }
                ],
            },
            {
                "matcher": "resume",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{python_cmd} {scripts_dir}/load_memory.py",
                        "timeout": 30,
                    }
                ],
            },
            {
                "matcher": "clear",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{python_cmd} {scripts_dir}/load_memory.py",
                        "timeout": 30,
                    }
                ],
            },
            {
                "matcher": "compact",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{python_cmd} {scripts_dir}/load_memory.py",
                        "timeout": 30,
                    }
                ],
            },
        ],
        "SessionEnd": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{python_cmd} {scripts_dir}/save_session.py",
                        "timeout": 30,
                    }
                ],
            }
        ],
        "PreCompact": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{python_cmd} {scripts_dir}/save_session.py",
                        "timeout": 30,
                    }
                ],
            }
        ],
    }

    if "hooks" not in settings:
        settings["hooks"] = {}

    for event, new_entries in hooks_to_add.items():
        if event not in settings["hooks"]:
            settings["hooks"][event] = new_entries
        else:
            # Only add entries that don't already exist (by matcher + commands)
            existing_keys = {hook_entry_key(e) for e in settings["hooks"][event]}
            for entry in new_entries:
                if hook_entry_key(entry) not in existing_keys:
                    settings["hooks"][event].append(entry)

    return settings


def merge_permissions(settings: dict) -> dict:
    """Merge memory system permissions into settings.

    Note: Edit/Write permissions are NOT included here because the PreToolUse hook
    (pretooluse-allow-memory.sh) auto-approves all memory-related operations.
    This works around a Claude Code bug where subagents don't inherit permissions
    from settings.json (GitHub issues #10906, #11934, #18172, #18950).
    """
    home = str(Path.home())

    # Permission path formats (per GitHub issue #6881):
    #   //path = absolute filesystem path (double slash)
    #   ~/path = home directory expansion
    #   /path  = RELATIVE from settings file (NOT what we want!)
    permissions_to_add = [
        # Read for memory/skill files (fallback for main agent)
        "Read(~/.claude/**)",
        # Projects directory access (orphan recovery reads transcript paths)
        f"Read(/{home}/.claude/projects/**)",
    ]

    if "permissions" not in settings:
        settings["permissions"] = {}
    if "allow" not in settings["permissions"]:
        settings["permissions"]["allow"] = []

    added = []
    for permission in permissions_to_add:
        if permission not in settings["permissions"]["allow"]:
            settings["permissions"]["allow"].append(permission)
            added.append(permission)

    if added:
        print(f"Added {len(added)} permissions")


    return settings


def build_project_index(python_cmd: str) -> None:
    """Build initial project index."""
    scripts_dir = get_claude_dir() / "scripts"
    indexing_script = scripts_dir / "indexing.py"

    if not indexing_script.exists():
        print("Note: Project index will be built on first /synthesize")
        return

    try:
        result = subprocess.run(
            [python_cmd, str(indexing_script), "build-index"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            # Parse and display summary
            for line in result.stdout.splitlines():
                if line.strip():
                    print(f"  {line}")
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        print(f"Note: Project index will be built on first /synthesize ({e})")


def print_success_message() -> None:
    """Print installation success message."""
    print()
    print("=" * 60)
    print("Memory system installed!")
    print("=" * 60)
    print()
    print("Available commands:")
    print("  /remember   - Save notes to daily log")
    print("  /synthesize - Process transcripts & update long-term memory")
    print("  /recall     - Search historical memory")
    print("  /reload     - Synthesize + load memory (use after /clear)")
    print("  /settings   - View/modify memory settings & token usage")
    print("  /projects   - Manage projects (move, merge orphans, cleanup)")
    print()
    print("Memory location: ~/.claude/memory/")
    print("  - global-long-term-memory.md  (loaded every session)")
    print("  - project-memory/             (loaded when in matching project)")
    print("  - daily/                      (recent session summaries)")
    print()
    print("Settings file: ~/.claude/memory/settings.json")
    print()
    print("Start a new Claude Code session to activate the memory system.")


def main() -> int:
    """Main installation routine."""
    print("Installing Claude Code Memory System...")
    print()

    # Check Python version
    check_python_version()

    # Check if Claude Code has been run
    claude_dir = get_claude_dir()
    if not claude_dir.exists():
        print("Error: ~/.claude directory not found.")
        print("Run Claude Code at least once before installing the memory system.")
        return 1

    # Detect Python command for hooks
    python_cmd = detect_python_command()
    print(f"Using Python command: {python_cmd}")

    # Get script directory
    script_dir = get_script_dir()

    # Create directories
    create_directories()

    # Copy files
    copy_scripts(script_dir)
    copy_hooks(script_dir)
    copy_skills(script_dir)
    copy_templates(script_dir)

    # Update settings.json
    settings_file = claude_dir / "settings.json"
    backup_settings(settings_file)

    settings = load_json_file(settings_file)

    # Add hooks
    settings = merge_hooks(settings, python_cmd)

    # Add permissions
    settings = merge_permissions(settings)

    # Save updated settings
    save_json_file(settings_file, settings)
    print(f"Updated {settings_file}")

    # Build project index
    print()
    print("Building project index...")
    build_project_index(python_cmd)

    # Success message
    print_success_message()

    return 0


if __name__ == "__main__":
    sys.exit(main())
