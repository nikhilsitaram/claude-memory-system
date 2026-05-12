#!/usr/bin/env python3
"""
Cross-platform installer for Claude Code Memory System.

This script:
1. Checks Python version (requires 3.9+) for the installer itself
2. Detects `uv` (required — used to run all hook/script commands)
3. Backs up existing settings.json
4. Creates directory structure
5. Symlinks scripts, hooks, and skills (auto-applies repo changes)
6. Merges hooks into settings.json with `uv run` invocations
7. Adds permissions
8. Builds project index

Usage:
    uv run install.py

Requirements: Python 3.9+ (installer), uv (https://docs.astral.sh/uv/)
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Import shared utilities from scripts/memory_utils.py (same repo, no symlink needed)
sys.path.insert(0, str(Path(__file__).parent / "scripts"))
from memory_utils import (  # noqa: E402
    DEFAULT_SETTINGS,
    MIN_PYTHON,
    get_claude_dir,
    get_memory_dir,
    load_json_file,
    save_json_file,
)

LAUNCHD_LABEL = "com.claude.memory-synthesis"


def check_python_version() -> None:
    """Check Python version and exit if too old (user-facing with install links)."""
    if sys.version_info < MIN_PYTHON:
        print(f"Error: Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required")
        print(f"Current version: {sys.version_info.major}.{sys.version_info.minor}")
        print()
        print("Options:")
        print("  - Install a newer Python: https://www.python.org/downloads/")
        print("  - Use pyenv: https://github.com/pyenv/pyenv")
        print("  - Use conda: https://docs.conda.io/")
        sys.exit(1)


def detect_uv_command() -> str:
    """
    Locate the `uv` executable. Exits with an install hint if missing.

    Returns the absolute path to `uv` so hooks and launchd/systemd units
    don't depend on PATH at trigger time.
    """
    uv_path = shutil.which("uv")
    if uv_path:
        return uv_path

    print("Error: `uv` not found on PATH.")
    print()
    print("This installer uses uv to manage Python for all hooks and scripts.")
    print("Install uv: https://docs.astral.sh/uv/getting-started/installation/")
    print()
    print("Quick install:")
    print("  curl -LsSf https://astral.sh/uv/install.sh | sh")
    sys.exit(1)


def get_script_dir() -> Path:
    """Get the directory containing this install script.

    If running from a git worktree, resolves to the main working tree
    so that symlinks remain valid after worktree cleanup.
    """
    this_dir = Path(__file__).parent.resolve()

    # .git is a file (not a directory) in worktrees — detect and redirect
    git_marker = this_dir / ".git"
    if git_marker.is_file():
        try:
            result = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                capture_output=True, text=True, cwd=this_dir,
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if line.startswith("worktree "):
                        main_tree = Path(line.split(" ", 1)[1])
                        if main_tree != this_dir and (main_tree / "install.py").exists():
                            print(f"Note: Running from worktree; symlinks will target main repo: {main_tree}")
                            return main_tree
                        break  # First entry is always the main worktree
        except (FileNotFoundError, subprocess.SubprocessError):
            pass

    return this_dir


def create_directories() -> None:
    """Create required directory structure."""
    dirs = [
        get_memory_dir() / "daily",
        get_memory_dir() / "project-memory",
        get_memory_dir() / "templates",
        get_memory_dir() / ".backups",
        get_claude_dir() / "scripts",
        get_claude_dir() / "hooks",
        get_claude_dir() / "skills",
    ]

    for dir_path in dirs:
        dir_path.mkdir(parents=True, exist_ok=True)

    print("Created directory structure")


def link_file(src: Path, dest: Path) -> None:
    """Create a symlink from dest -> src, replacing any existing file or link."""
    if dest.is_symlink() or dest.exists():
        dest.unlink()
    dest.symlink_to(src)


def link_scripts(script_dir: Path) -> None:
    """Symlink Python scripts to ~/.claude/scripts/."""
    dest_dir = get_claude_dir() / "scripts"

    scripts_to_link = [
        "memory_utils.py",
        "load_memory.py",
        "indexing.py",  # Session discovery, transcript extraction, project index
        "transcript_ops.py",  # Transcript parsing and extraction (split from indexing)
        "project_manager.py",  # Project lifecycle management
        "decay.py",  # Age-based decay for long-term memory
        "devtools.py",  # Dev diagnostics and mark-routed migration
        "synthesis.py",  # Zero-tool background synthesis
        "synthesis_cron.py",  # Deferred synthesis (systemd timer entry point)
        "session_end_recall.py",  # SessionEnd recall writer
        "token_usage.py",  # Token usage calculation for /settings
    ]

    for script_name in scripts_to_link:
        src = script_dir / "scripts" / script_name
        if src.exists():
            link_file(src, dest_dir / script_name)

    print("Linked scripts to ~/.claude/scripts/")


def remove_legacy_scripts() -> None:
    """Remove scripts from previous versions that are no longer installed."""
    scripts_dir = get_claude_dir() / "scripts"
    legacy_scripts = [
        "save_session.py",          # Removed: SessionEnd hook replaced by direct reading
        "transcript_source.py",     # Removed: consolidated into indexing.py
        "load-project-memory.py",   # Removed: merged into load_memory.py
    ]

    for name in legacy_scripts:
        path = scripts_dir / name
        if path.exists():
            path.unlink()
            print(f"  Removed legacy script: {name}")


def link_hooks(script_dir: Path) -> None:
    """Symlink hook scripts to ~/.claude/hooks/."""
    dest_dir = get_claude_dir() / "hooks"

    hooks_to_link = [
        "pretooluse-allow-memory.sh",
    ]

    for hook_name in hooks_to_link:
        src = script_dir / "hooks" / hook_name
        if src.exists():
            link_file(src, dest_dir / hook_name)

    print("Linked hooks to ~/.claude/hooks/")


def link_skills(script_dir: Path) -> None:
    """Symlink skill directories to ~/.claude/skills/."""
    skills_dir = get_claude_dir() / "skills"

    skills = ["remember", "synthesize", "recall", "settings", "projects", "audit", "load-project-memory"]

    for skill in skills:
        src_dir = script_dir / "skills" / skill
        dest = skills_dir / skill

        if src_dir.exists():
            # Remove existing dir or symlink, then symlink the whole directory
            if dest.is_symlink():
                dest.unlink()
            elif dest.is_dir():
                shutil.rmtree(dest)
            dest.symlink_to(src_dir)

    print("Linked skills to ~/.claude/skills/")


def copy_templates(script_dir: Path) -> None:
    """Copy template files."""
    memory_dir = get_memory_dir()
    templates_dir = memory_dir / "templates"

    # Always copy templates to templates/ dir (for subagent reference)
    templates_to_copy = [
        "global-long-term-memory.md",
        "project-long-term-memory.md",
        "daily-template.md",
    ]
    for template_name in templates_to_copy:
        src = script_dir / "templates" / template_name
        if src.exists():
            shutil.copy2(src, templates_dir / template_name)
    print("Copied templates to ~/.claude/memory/templates/")

    # Copy global-long-term-memory.md to memory root if it doesn't exist
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



def install_systemd_units(script_dir: Path, uv_path: str) -> None:
    """Install systemd user units for deferred synthesis.

    `uv_path` must be an absolute path (as returned by `detect_uv_command`)
    so the templated .service file gets a self-contained ExecStart.
    """
    systemd_user_dir = Path.home() / ".config" / "systemd" / "user"

    # Check if systemctl is available
    try:
        subprocess.run(["systemctl", "--user", "--version"],
                       capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("Note: systemctl not available, skipping systemd unit installation")
        return

    systemd_user_dir.mkdir(parents=True, exist_ok=True)

    units = ["claude-memory-synthesis.service", "claude-memory-synthesis.timer"]
    for unit in units:
        src = script_dir / "systemd" / unit
        if src.exists():
            dest = systemd_user_dir / unit
            content = src.read_text(encoding="utf-8").replace("__UV_PATH__", uv_path)
            dest.write_text(content, encoding="utf-8")

    # Reload and enable timer
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, timeout=10)
    subprocess.run(["systemctl", "--user", "enable", "--now",
                    "claude-memory-synthesis.timer"],
                   capture_output=True, timeout=10)

    print("Installed systemd units (timer enabled)")


def install_launchd_agent(uv_path: str) -> None:
    """Install a launchd user agent for periodic synthesis on macOS.

    `uv_path` must be an absolute path (as returned by `detect_uv_command`)
    since launchd has minimal default PATH and won't find `uv` by name.
    """
    import plistlib

    launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    launch_agents_dir.mkdir(parents=True, exist_ok=True)

    plist_path = launch_agents_dir / f"{LAUNCHD_LABEL}.plist"
    scripts_dir = Path.home() / ".claude" / "scripts"
    log_dir = Path.home() / "Library" / "Logs" / "claude-memory"
    log_dir.mkdir(parents=True, exist_ok=True)
    home = str(Path.home())

    plist = {
        "Label": LAUNCHD_LABEL,
        # See merge_hooks for why `--no-project` matters.
        "ProgramArguments": [uv_path, "run", "--no-project", str(scripts_dir / "synthesis_cron.py")],
        "StartInterval": int(DEFAULT_SETTINGS["synthesis"]["intervalHours"] * 3600),
        "StandardOutPath": str(log_dir / "synthesis.log"),
        "StandardErrorPath": str(log_dir / "synthesis.err"),
        "EnvironmentVariables": {
            "CLAUDECODE": "",  # Unset nesting guard
            "PATH": f"{home}/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    }

    # Unload existing agent if present (ignore errors on first install)
    uid = os.getuid()
    subprocess.run(
        ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
        capture_output=True, timeout=10,
    )

    with open(plist_path, "wb") as f:
        plistlib.dump(plist, f)

    # Load the agent
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
        capture_output=True, timeout=10,
    )
    if result.returncode != 0:
        print(f"Warning: launchctl bootstrap failed (rc={result.returncode})")

    print(f"Installed launchd agent ({LAUNCHD_LABEL})")


def hook_entry_key(entry: dict) -> tuple:
    """Generate a unique key for a hook entry based on matcher and commands."""
    matcher = entry.get("matcher", "")
    commands = tuple(h.get("command", "") for h in entry.get("hooks", []))
    return (matcher, commands)


def remove_obsolete_hooks(settings: dict) -> dict:
    """
    Remove hooks that are no longer used (e.g., save_session.py).

    This handles migration from older versions where SessionEnd and PreCompact
    hooks were used to copy transcripts.
    """
    obsolete_patterns = [
        "save_session.py",
    ]

    hooks = settings.get("hooks", {})
    events_to_clean = ["SessionEnd", "PreCompact"]

    for event in events_to_clean:
        if event not in hooks:
            continue

        # Filter out entries that reference obsolete scripts
        new_entries = []
        removed_count = 0
        for entry in hooks[event]:
            entry_hooks = entry.get("hooks", [])
            # Check if any hook command contains obsolete patterns
            is_obsolete = any(
                any(pattern in h.get("command", "") for pattern in obsolete_patterns)
                for h in entry_hooks
            )
            if is_obsolete:
                removed_count += 1
            else:
                new_entries.append(entry)

        if removed_count > 0:
            print(f"  Removed {removed_count} obsolete {event} hook(s)")
            if new_entries:
                hooks[event] = new_entries
            else:
                del hooks[event]

    return settings


def merge_hooks(settings: dict, uv_path: str) -> dict:
    """Merge memory system hooks into settings."""
    home = str(Path.home())
    scripts_dir = f"{home}/.claude/scripts"
    hooks_dir = f"{home}/.claude/hooks"

    # `--no-project` is critical: without it, `uv run` walks up from the
    # current working directory looking for a pyproject.toml. When a hook
    # fires while Claude Code is open in a Python project, uv would
    # create a `.venv` and sync that project's dependencies as a side
    # effect of every memory hook. The flag pins uv to an ephemeral env.
    load_cmd = f"{uv_path} run --no-project {scripts_dir}/load_memory.py"
    recall_cmd = f"{uv_path} run --no-project {scripts_dir}/session_end_recall.py"

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
                "matcher": matcher,
                "hooks": [
                    {
                        "type": "command",
                        "command": load_cmd,
                        "timeout": 30,
                    }
                ],
            }
            for matcher in ("startup", "resume", "clear", "compact")
        ],
        # SessionEnd: recall writer only. Deferred synthesis runs on its own
        # schedule (launchd StartInterval / systemd timer derived from
        # synthesis.intervalHours), so no kickstart hook is needed.
        "SessionEnd": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": recall_cmd,
                        "timeout": 10,
                    },
                ],
            }
        ],
        # PreCompact: same as SessionEnd — recall writer only.
        "PreCompact": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": recall_cmd,
                        "timeout": 10,
                    },
                ],
            }
        ],
    }

    if "hooks" not in settings:
        settings["hooks"] = {}

    # Migration: strip every existing inner hook that references our memory
    # scripts or the synthesis scheduler before re-adding canonical entries.
    # Filter at the inner-hook level (not the entry level) so a user-added
    # hook that happens to share a matcher with one of ours survives.
    # `memory-synthesis` matches both systemd and launchd commands and
    # subsumes the prior platform-migration pass.
    memory_substrings = ("load_memory.py", "session_end_recall.py", "memory-synthesis")
    for event in ("SessionStart", "SessionEnd", "PreCompact"):
        if event not in settings.get("hooks", {}):
            continue
        for entry in settings["hooks"][event]:
            entry["hooks"] = [
                h for h in entry.get("hooks", [])
                if not any(s in h.get("command", "") for s in memory_substrings)
            ]
        # Drop entries left empty after stripping
        settings["hooks"][event] = [
            entry for entry in settings["hooks"][event] if entry.get("hooks")
        ]
        if not settings["hooks"][event]:
            del settings["hooks"][event]

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


def build_project_index(uv_path: str) -> None:
    """Build initial project index."""
    scripts_dir = get_claude_dir() / "scripts"
    indexing_script = scripts_dir / "indexing.py"

    if not indexing_script.exists():
        print("Note: Project index will be built on first /synthesize")
        return

    try:
        result = subprocess.run(
            [uv_path, "run", "--no-project", str(indexing_script), "build-index"],
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
    print("  /settings   - View/modify memory settings & token usage")
    print("  /projects   - Manage projects (move, merge orphans, cleanup)")
    print("  /audit      - Audit long-term memory for stale or incorrect entries")
    print("  /load-project-memory [name] - Load a project's LTM + STM on demand (useful in light mode)")
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

    # Detect uv (required) for hooks and scheduled units
    uv_path = detect_uv_command()
    print(f"Using uv: {uv_path}")

    # Get script directory
    script_dir = get_script_dir()

    # Create directories
    create_directories()

    # Link scripts, hooks, and skills (symlinks for auto-apply on repo changes)
    link_scripts(script_dir)
    if sys.platform == "darwin":
        install_launchd_agent(uv_path)
    else:
        install_systemd_units(script_dir, uv_path)
    link_hooks(script_dir)
    link_skills(script_dir)
    copy_templates(script_dir)

    # Clean up legacy scripts from previous versions
    remove_legacy_scripts()

    # Update settings.json
    settings_file = claude_dir / "settings.json"
    settings = load_json_file(settings_file, default={})

    # Remove obsolete hooks (e.g., save_session.py)
    settings = remove_obsolete_hooks(settings)

    # Add hooks
    settings = merge_hooks(settings, uv_path)

    # Add permissions
    settings = merge_permissions(settings)

    # Save updated settings
    save_json_file(settings_file, settings)
    print(f"Updated {settings_file}")

    # Build project index
    print()
    print("Building project index...")
    build_project_index(uv_path)

    # Success message
    print_success_message()

    return 0


if __name__ == "__main__":
    sys.exit(main())
