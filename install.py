#!/usr/bin/env python3
"""
Cross-platform installer for Claude Code Memory System.

This script:
1. Checks Python version (requires 3.9+)
2. Detects available Python command (python3 vs python)
3. Backs up existing settings.json
4. Removes old bash hooks (for migration)
5. Creates directory structure
6. Copies scripts and skills
7. Merges hooks into settings.json (with absolute paths)
8. Adds permissions
9. Removes cron job if exists (replaced by inline recovery)
10. Validates installation

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
        get_claude_dir() / "scripts",
        get_claude_dir() / "skills" / "remember",
        get_claude_dir() / "skills" / "synthesize",
        get_claude_dir() / "skills" / "recall",
        get_claude_dir() / "skills" / "reload",
        get_claude_dir() / "skills" / "settings",
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


def copy_skills(script_dir: Path) -> None:
    """Copy skill files to ~/.claude/skills/."""
    skills_dir = get_claude_dir() / "skills"

    skills = ["remember", "synthesize", "recall", "reload", "settings"]

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

    # Migrate LONG_TERM.md → global-long-term-memory.md if needed
    old_file = memory_dir / "LONG_TERM.md"
    new_file = memory_dir / "global-long-term-memory.md"

    if old_file.exists() and not new_file.exists():
        print("Migrating LONG_TERM.md → global-long-term-memory.md...")
        shutil.move(old_file, new_file)

    # Copy global-long-term-memory.md template if neither exists
    if not new_file.exists() and not old_file.exists():
        src = script_dir / "templates" / "global-long-term-memory.md"
        if src.exists():
            shutil.copy2(src, new_file)
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


def remove_old_bash_hooks(settings: dict) -> dict:
    """Remove old bash-based hooks (for migration from bash to Python)."""
    if "hooks" not in settings:
        return settings

    old_patterns = [
        "load-memory.sh",
        "save-session.sh",
        "recover-transcripts.sh",
    ]

    for event in ["SessionStart", "SessionEnd", "PreCompact"]:
        if event in settings["hooks"]:
            # Filter out old bash hooks
            settings["hooks"][event] = [
                entry
                for entry in settings["hooks"][event]
                if not any(
                    pattern in hook.get("command", "")
                    for hook in entry.get("hooks", [])
                    for pattern in old_patterns
                )
            ]
            # Remove empty arrays
            if not settings["hooks"][event]:
                del settings["hooks"][event]

    # Remove empty hooks object
    if "hooks" in settings and not settings["hooks"]:
        del settings["hooks"]

    return settings


def hook_entry_key(entry: dict) -> tuple:
    """Generate a unique key for a hook entry based on matcher and commands."""
    matcher = entry.get("matcher", "")
    commands = tuple(h.get("command", "") for h in entry.get("hooks", []))
    return (matcher, commands)


def merge_hooks(settings: dict, python_cmd: str) -> dict:
    """Merge memory system hooks into settings."""
    home = str(Path.home())
    scripts_dir = f"{home}/.claude/scripts"

    hooks_to_add = {
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
    """Merge memory system permissions into settings."""
    home = str(Path.home())

    # Permission path formats (per GitHub issue #6881):
    #   //path = absolute filesystem path (double slash)
    #   ~/path = home directory expansion
    #   /path  = RELATIVE from settings file (NOT what we want!)
    # Include both // and ~ variants for robustness
    permissions_to_add = [
        # Read for memory/skill files
        f"Read(/{home}/.claude/**)",   # double-slash absolute
        "Read(~/.claude/**)",           # tilde expansion
        # Edit for memory files
        f"Edit(/{home}/.claude/memory/**)",
        f"Edit(/{home}/.claude/memory/*)",
        f"Edit(/{home}/.claude/memory/daily/*)",
        f"Edit(/{home}/.claude/memory/project-memory/*)",
        "Edit(~/.claude/memory/**)",
        "Edit(~/.claude/memory/*)",
        "Edit(~/.claude/memory/daily/*)",
        "Edit(~/.claude/memory/project-memory/*)",
        # Write for memory files
        f"Write(/{home}/.claude/memory/**)",
        f"Write(/{home}/.claude/memory/*)",
        f"Write(/{home}/.claude/memory/daily/*)",
        f"Write(/{home}/.claude/memory/project-memory/*)",
        "Write(~/.claude/memory/**)",
        "Write(~/.claude/memory/*)",
        "Write(~/.claude/memory/daily/*)",
        "Write(~/.claude/memory/project-memory/*)",
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

    # Clean up old single-slash absolute path patterns (migration from pre-#6881 fix)
    # Single-slash /path is interpreted as RELATIVE, not absolute!
    old_patterns = [
        p for p in settings["permissions"]["allow"]
        if (p.startswith(f"Edit({home}") or p.startswith(f"Write({home}") or p.startswith(f"Read({home}"))
        and not p.startswith(f"Edit(/{home}") and not p.startswith(f"Write(/{home}") and not p.startswith(f"Read(/{home}")
    ]
    for p in old_patterns:
        settings["permissions"]["allow"].remove(p)
    if old_patterns:
        print(f"Removed {len(old_patterns)} old single-slash patterns (now using // for absolute paths)")

    return settings


def remove_cron_job() -> None:
    """Remove old cron job if it exists."""
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

            print("Removed old cron job (recovery now runs on SessionStart)")

    except (subprocess.TimeoutExpired, FileNotFoundError):
        # crontab not available
        pass


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
    copy_skills(script_dir)
    copy_templates(script_dir)

    # Update settings.json
    settings_file = claude_dir / "settings.json"
    backup_settings(settings_file)

    settings = load_json_file(settings_file)

    # Remove old bash hooks (migration)
    settings = remove_old_bash_hooks(settings)

    # Add new Python hooks
    settings = merge_hooks(settings, python_cmd)

    # Add permissions
    settings = merge_permissions(settings)

    # Save updated settings
    save_json_file(settings_file, settings)
    print(f"Updated {settings_file}")

    # Remove old cron job
    remove_cron_job()

    # Build project index
    print()
    print("Building project index...")
    build_project_index(python_cmd)

    # Success message
    print_success_message()

    return 0


if __name__ == "__main__":
    sys.exit(main())
