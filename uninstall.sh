#!/bin/bash
echo "Uninstalling Claude Code Memory System..."

# Remove cron job
crontab -l 2>/dev/null | grep -v "recover-transcripts.sh" | crontab - 2>/dev/null || true

echo "Removed cron job"

# Remove hooks from settings.json
SETTINGS_FILE="$HOME/.claude/settings.json"
if [ -f "$SETTINGS_FILE" ]; then
    python3 << 'PYTHON'
import json
import os

settings_file = os.path.expanduser("~/.claude/settings.json")

try:
    with open(settings_file, 'r') as f:
        settings = json.load(f)
except (json.JSONDecodeError, IOError):
    print("Could not read settings.json, skipping hook removal")
    exit(0)

if "hooks" in settings:
    # Remove memory system hooks
    for event in ["SessionStart", "SessionEnd", "PreCompact"]:
        if event in settings["hooks"]:
            settings["hooks"][event] = [
                h for h in settings["hooks"][event]
                if not any(
                    hook.get("command", "").startswith("bash ~/.claude/scripts/")
                    and ("load-memory" in hook.get("command", "") or "save-session" in hook.get("command", ""))
                    for hook in h.get("hooks", [])
                )
            ]
            # Remove empty arrays
            if not settings["hooks"][event]:
                del settings["hooks"][event]

    # Remove empty hooks object
    if not settings["hooks"]:
        del settings["hooks"]

# Remove memory system permissions
permissions_to_remove = [
    "Read(~/.claude/**)",
    "Edit(~/.claude/memory/**)",
    "Write(~/.claude/memory/**)",
    "Bash(rm -rf ~/.claude/memory/transcripts/*)",
]

if "permissions" in settings and "allow" in settings["permissions"]:
    settings["permissions"]["allow"] = [
        p for p in settings["permissions"]["allow"]
        if p not in permissions_to_remove
    ]
    # Remove empty allow array
    if not settings["permissions"]["allow"]:
        del settings["permissions"]["allow"]
    # Remove empty permissions object
    if not settings["permissions"]:
        del settings["permissions"]
    print("Removed memory system permissions")

with open(settings_file, 'w') as f:
    json.dump(settings, f, indent=2)

print(f"Updated {settings_file}")
PYTHON
fi

echo ""
echo "Memory data preserved at ~/.claude/memory/"
echo "To fully remove, run:"
echo "  rm -rf ~/.claude/memory"
echo "  rm -rf ~/.claude/skills/{remember,synthesize,recall,reload}"
echo "  rm ~/.claude/scripts/{load-memory,save-session,recover-transcripts}.sh"
echo "  rm ~/.claude/scripts/load-project-memory.py"
