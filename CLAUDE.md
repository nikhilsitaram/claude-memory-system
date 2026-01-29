# Claude Code Memory System - Development Guide

## Project Purpose

This repo provides a markdown-based memory persistence system for Claude Code. It installs hooks, scripts, and skills that enable Claude to remember context across sessions.

## Repo Structure

```
claude-memory-system/
├── install.sh              # Installs everything to ~/.claude/
├── uninstall.sh            # Removes hooks and cron job
├── scripts/
│   ├── load-memory.sh      # SessionStart hook - loads memory
│   ├── save-session.sh     # SessionEnd/PreCompact hook - saves transcript
│   └── recover-transcripts.sh  # Cron job - recovers orphaned transcripts
├── skills/
│   ├── remember/SKILL.md   # /remember - save notes
│   ├── synthesize/         # /synthesize - process transcripts
│   │   ├── SKILL.md
│   │   └── extract_transcripts.py
│   ├── recall/SKILL.md     # /recall - search history
│   └── reload/SKILL.md     # /reload - synthesize + load after /clear
└── templates/
    └── LONG_TERM.md        # Initial long-term memory template
```

## Installation Locations

| Repo Path | Installs To |
|-----------|-------------|
| `scripts/*.sh` | `~/.claude/scripts/` |
| `skills/*/` | `~/.claude/skills/` |
| `templates/LONG_TERM.md` | `~/.claude/memory/` (if not exists) |

The install script also:
- Creates `~/.claude/memory/{daily,transcripts}/` directories
- Adds hooks to `~/.claude/settings.json` (SessionStart, SessionEnd, PreCompact)
- Adds permissions to settings.json:
  - `Read(~/.claude/**)` - Read memory files
  - `Edit(~/.claude/memory/**)` - Edit daily summaries
  - `Write(~/.claude/memory/**)` - Create new summaries
  - `Bash(rm -rf ~/.claude/memory/transcripts/*)` - Delete processed transcripts
- Sets up hourly cron job for transcript recovery

## Making Changes

### Adding a New Skill

1. Create `skills/<name>/SKILL.md` with frontmatter (name, description, user-invocable)
2. Update `install.sh`:
   - Add to `mkdir -p ~/.claude/skills/{...}` line
   - Add `cp` command for the skill
   - Add to "Available commands" echo section
3. Update `uninstall.sh`:
   - Add to the `rm -rf` instruction in the final echo

### Adding a New Script

1. Create `scripts/<name>.sh`
2. Scripts are copied via `cp "$SCRIPT_DIR/scripts/"*.sh` (automatic)
3. If it needs a hook, add the hook config in `install.sh` Python section
4. If it needs cron, add cron setup in `install.sh`
5. Update `uninstall.sh` if hook/cron cleanup needed

### Modifying Hooks

Hooks are defined in `install.sh` in the `hooks_to_add` dict. Available events:
- `SessionStart` - fires on startup, resume, clear, compact
- `SessionEnd` - fires on session end
- `PreCompact` - fires before context compaction

Remember to update `uninstall.sh` event list if adding new hook events.

## Testing Changes

```bash
# Re-run install to apply changes
./install.sh

# Start a new Claude Code session to test
claude

# Check settings were applied
cat ~/.claude/settings.json | jq .

# Check cron job
crontab -l | grep recover-transcripts
```

## Key Files Reference

- **settings.json**: User's Claude Code settings (hooks, permissions)
- **LONG_TERM.md**: Synthesized user profile and patterns
- **daily/YYYY-MM-DD.md**: Daily session summaries
- **transcripts/{session_id}.jsonl**: Raw session data (JSONL format)
- **.captured**: Tracks which session IDs have been saved (prevents duplicates)
