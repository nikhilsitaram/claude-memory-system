# Claude Code Memory System - Development Guide

## Project Purpose

This repo provides a markdown-based memory persistence system for Claude Code. It installs hooks, scripts, and skills that enable Claude to remember context across sessions.

## Repo Structure

```
claude-memory-system/
├── install.sh              # Installs everything to ~/.claude/
├── uninstall.sh            # Removes hooks and cron job
├── scripts/
│   ├── load-memory.sh      # SessionStart hook - loads memory + project history
│   ├── load-project-memory.py  # Manual project memory loader
│   ├── save-session.sh     # SessionEnd/PreCompact hook - saves transcript
│   └── recover-transcripts.sh  # Cron job - recovers orphaned transcripts
├── skills/
│   ├── remember/SKILL.md   # /remember - save notes
│   ├── synthesize/         # /synthesize - process transcripts
│   │   ├── SKILL.md
│   │   ├── extract_transcripts.py
│   │   └── build_projects_index.py  # Builds project-to-work-days index
│   ├── recall/SKILL.md     # /recall - search history
│   ├── reload/SKILL.md     # /reload - synthesize + load after /clear
│   └── settings/SKILL.md   # /settings - view/modify memory config
└── templates/
    ├── LONG_TERM.md        # Initial long-term memory template
    └── settings.json       # Default memory settings
```

## Installation Locations

| Repo Path | Installs To |
|-----------|-------------|
| `scripts/*.sh` | `~/.claude/scripts/` |
| `scripts/*.py` | `~/.claude/scripts/` |
| `skills/*/` | `~/.claude/skills/` |
| `templates/LONG_TERM.md` | `~/.claude/memory/` (if not exists) |
| `templates/settings.json` | `~/.claude/memory/` (if not exists) |

The install script also:
- Creates `~/.claude/memory/{daily,transcripts}/` directories
- Adds hooks to `~/.claude/settings.json` (SessionStart, SessionEnd, PreCompact)
- Adds permissions to settings.json:
  - `Read(~/.claude/**)` - Read memory files
  - `Edit(~/.claude/memory/**)` - Edit daily summaries
  - `Write(~/.claude/memory/**)` - Create new summaries
  - `Bash(rm -rf ~/.claude/memory/transcripts/*)` - Delete processed transcripts
- Sets up hourly cron job for transcript recovery
- Builds initial project index (`~/.claude/memory/projects-index.json`)

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

- **~/.claude/settings.json**: User's Claude Code settings (hooks, permissions)
- **~/.claude/memory/settings.json**: Memory system configuration (working days, token limits)
- **LONG_TERM.md**: Synthesized user profile and patterns
- **projects-index.json**: Maps project paths to their work days (for project-aware loading)
- **daily/YYYY-MM-DD.md**: Daily session summaries (may include `<!-- projects: ... -->` tags)
- **transcripts/{session_id}.jsonl**: Raw session data (JSONL format)
- **.captured**: Tracks which session IDs have been saved (prevents duplicates)

## Project-Aware Loading

The `load-memory.sh` script loads project-specific history when:
1. The project index (`projects-index.json`) exists
2. `$PWD` matches a known project path (exact match by default, or prefix match if `includeSubdirectories` is enabled)

This loads the last N "project days" (configurable in settings, default 7), separate from the N working days loaded for all projects.

**Working days vs calendar days**: The system scans existing daily files rather than looping through calendar dates. If you take days off, those empty days don't count against your quota.

**Subdirectory matching**: By default disabled. Enable via `/settings set projectMemory.includeSubdirectories true` to match `/project/backend/` to `/project/`.

## Settings Configuration

Memory system settings are stored in `~/.claude/memory/settings.json`:

```json
{
  "shortTermMemory": { "workingDays": 7, "tokenLimit": 15000 },
  "projectMemory": { "workingDays": 7, "tokenLimit": 8000, "includeSubdirectories": false },
  "longTermMemory": { "tokenLimit": 7000 },
  "totalTokenBudget": 30000
}
```

Token limits are informational (soft warnings), not hard caps. Use `/settings usage` to check consumption.
