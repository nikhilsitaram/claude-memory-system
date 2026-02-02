#!/bin/bash
set -e

echo "Installing Claude Code Memory System..."

# Check dependencies
if ! command -v jq &> /dev/null; then
    echo "Error: jq is required but not installed."
    echo "Install with: brew install jq (Mac) or apt install jq (Ubuntu)"
    exit 1
fi

# Check if Claude Code has been run
if [ ! -d ~/.claude ]; then
    echo "Error: ~/.claude directory not found."
    echo "Run Claude Code at least once before installing the memory system."
    exit 1
fi

# Create directory structure
mkdir -p ~/.claude/memory/{daily,transcripts}
mkdir -p ~/.claude/scripts
mkdir -p ~/.claude/skills/{remember,synthesize,recall,reload,settings}

# Copy scripts
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SCRIPT_DIR/scripts/"*.sh ~/.claude/scripts/
cp "$SCRIPT_DIR/scripts/"*.py ~/.claude/scripts/
chmod +x ~/.claude/scripts/*.sh
chmod +x ~/.claude/scripts/*.py

# Copy skills
cp "$SCRIPT_DIR/skills/remember/SKILL.md" ~/.claude/skills/remember/
cp "$SCRIPT_DIR/skills/synthesize/SKILL.md" ~/.claude/skills/synthesize/
cp "$SCRIPT_DIR/skills/synthesize/extract_transcripts.py" ~/.claude/skills/synthesize/
cp "$SCRIPT_DIR/skills/synthesize/build_projects_index.py" ~/.claude/skills/synthesize/
cp "$SCRIPT_DIR/skills/recall/SKILL.md" ~/.claude/skills/recall/
cp "$SCRIPT_DIR/skills/reload/SKILL.md" ~/.claude/skills/reload/
cp "$SCRIPT_DIR/skills/settings/SKILL.md" ~/.claude/skills/settings/

# Initialize LONG_TERM.md if it doesn't exist
if [ ! -f ~/.claude/memory/LONG_TERM.md ]; then
    cp "$SCRIPT_DIR/templates/LONG_TERM.md" ~/.claude/memory/
fi

# Initialize settings.json if it doesn't exist
if [ ! -f ~/.claude/memory/settings.json ]; then
    cp "$SCRIPT_DIR/templates/settings.json" ~/.claude/memory/
    echo "Created default settings at ~/.claude/memory/settings.json"
fi

# Initialize .captured file
touch ~/.claude/memory/.captured

# Merge hooks into settings.json
SETTINGS_FILE="$HOME/.claude/settings.json"
if [ -f "$SETTINGS_FILE" ]; then
    # Backup existing
    cp "$SETTINGS_FILE" "$SETTINGS_FILE.backup"
    echo "Backed up existing settings to $SETTINGS_FILE.backup"
fi

# Create or merge settings
python3 << 'PYTHON'
import json
import os

settings_file = os.path.expanduser("~/.claude/settings.json")
hooks_to_add = {
    "hooks": {
        "SessionStart": [{
            "hooks": [{
                "type": "command",
                "command": "bash ~/.claude/scripts/load-memory.sh"
            }]
        }],
        "SessionEnd": [{
            "hooks": [{
                "type": "command",
                "command": "bash ~/.claude/scripts/save-session.sh"
            }]
        }],
        "PreCompact": [{
            "hooks": [{
                "type": "command",
                "command": "bash ~/.claude/scripts/save-session.sh"
            }]
        }]
    }
}

# Load existing settings or start fresh (with error handling)
try:
    if os.path.exists(settings_file):
        with open(settings_file, 'r') as f:
            settings = json.load(f)
    else:
        settings = {}
except (json.JSONDecodeError, IOError) as e:
    print(f"Warning: Could not parse existing settings.json: {e}")
    print("Creating new settings file")
    settings = {}

# Merge hooks
if "hooks" not in settings:
    settings["hooks"] = {}

for event, config in hooks_to_add["hooks"].items():
    if event not in settings["hooks"]:
        settings["hooks"][event] = config
    else:
        # Append to existing hooks for this event
        settings["hooks"][event].extend(config)

# Add permissions for memory system operations (use absolute paths for subagent compatibility)
home = os.path.expanduser("~")
permissions_to_add = [
    f"Read({home}/.claude/**)",                           # Read memory files
    f"Edit({home}/.claude/memory/**)",                    # Edit daily summaries and LONG_TERM.md
    f"Write({home}/.claude/memory/**)",                   # Write new daily summaries
    f"Bash(rm -rf {home}/.claude/memory/transcripts/*)",  # Delete processed transcripts
]

if "permissions" not in settings:
    settings["permissions"] = {}
if "allow" not in settings["permissions"]:
    settings["permissions"]["allow"] = []

for permission in permissions_to_add:
    if permission not in settings["permissions"]["allow"]:
        settings["permissions"]["allow"].append(permission)
        print(f"Added permission: {permission}")

with open(settings_file, 'w') as f:
    json.dump(settings, f, indent=2)

print(f"Updated {settings_file}")
PYTHON

# Set up hourly cron job (more robust method)
CRON_CMD="0 * * * * bash ~/.claude/scripts/recover-transcripts.sh >> ~/.claude/memory/recovery.log 2>&1"
TMPFILE=$(mktemp)
crontab -l 2>/dev/null | grep -v "recover-transcripts.sh" > "$TMPFILE" || true
echo "$CRON_CMD" >> "$TMPFILE"
crontab "$TMPFILE"
rm "$TMPFILE"
echo "Added hourly cron job for transcript recovery"

# WSL cron daemon check
if [[ "$(uname -r)" == *"WSL"* ]] || [[ "$(uname -r)" == *"microsoft"* ]]; then
    if ! pgrep cron > /dev/null 2>&1; then
        echo ""
        echo "WARNING: WSL detected but cron daemon is not running."
        echo "Run: sudo service cron start"
        echo "To auto-start, add to ~/.bashrc: sudo service cron start 2>/dev/null"
    fi
fi

# Build initial project index
echo "Building project index..."
python3 ~/.claude/skills/synthesize/build_projects_index.py 2>/dev/null || echo "Note: Project index will be built on first /synthesize"

echo ""
echo "Memory system installed!"
echo ""
echo "Available commands:"
echo "  /remember   - Save notes to daily log"
echo "  /synthesize - Process transcripts & update long-term memory"
echo "  /recall     - Search historical memory"
echo "  /reload     - Synthesize + load memory (use after /clear)"
echo "  /settings   - View/modify memory settings & token usage"
echo ""
echo "Memory location: ~/.claude/memory/"
echo "Settings file:   ~/.claude/memory/settings.json"
echo ""
echo "Start a new Claude Code session to activate the memory system."
