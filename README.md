# Claude Code Memory System

A markdown-based memory system for Claude Code that automatically captures session transcripts and provides tools for managing long-term memory.

## Features

- **Auto-capture**: Full transcripts saved on session end and before compaction
- **Project-aware loading**: Automatically loads last 14 "project days" when working in a known project directory
- **Proactive recall**: Claude automatically searches older memory when historical context would help
- **Recovery**: Orphaned transcripts from ungraceful exits are recovered via cron job
- **Auto-synthesize prompt**: Unprocessed transcripts trigger reminder at session start
- **Manual notes**: `/remember` for specific highlights
- **Historical search**: `/recall` for searching older memory
- **Shareable**: One-command installation for teammates

## Installation

```bash
git clone https://github.com/nikhilsitaram/claude-memory-system.git
cd claude-memory-system
./install.sh
```

Start a new Claude Code session to activate the memory system.

The installer automatically adds these permissions so Claude can manage memory files without prompting:

- `Read(~/.claude/**)` - Read memory files
- `Edit(~/.claude/memory/**)` - Edit daily summaries and long-term memory
- `Write(~/.claude/memory/**)` - Create new daily summaries
- `Bash(rm -rf ~/.claude/memory/transcripts/*)` - Delete processed transcripts

## Requirements

- Claude Code CLI installed and run at least once (`~/.claude` must exist)
- `jq` command-line JSON processor
- cron daemon (for recovery of ungraceful exits)

### WSL Users

The cron daemon may not be running by default. To start it:

```bash
sudo service cron start
```

To auto-start cron, add to your `~/.bashrc`:

```bash
sudo service cron start 2>/dev/null
```

## Commands

| Command | Description |
|---------|-------------|
| `/remember [note]` | Save a note to today's daily log |
| `/synthesize` | Process transcripts into daily summaries and update long-term memory |
| `/recall [query]` | Search through all historical daily memory files |
| `/reload` | Synthesize pending transcripts and reload memory (use after `/clear`) |

**Note**: `/clear` does not trigger hooks ([GitHub #21578](https://github.com/anthropics/claude-code/issues/21578)), so use `/reload` afterward to restore memory context.

## How It Works

### Session Lifecycle

1. **Session Start**: Loads long-term memory, last 7 days of daily summaries, and project-specific history
2. **During Session**: Use `/remember` to capture important notes; Claude proactively uses `/recall` when historical context would help
3. **Before Compaction**: Transcript saved (both manual `/compact` and automatic compaction)
4. **Session End**: Transcript automatically saved to `~/.claude/memory/transcripts/`
5. **Recovery**: Hourly cron job recovers any missed transcripts from ungraceful exits

### Project-Aware Memory Loading

When you start a session in a project directory, the system automatically loads that project's historical context:

| Context Type | Window |
|--------------|--------|
| All projects | Last 7 calendar days |
| Current project | Last 14 "project days" (days with actual work) |

**Project detection**: Uses exact path match only (subdirectories don't inherit project context).

**Why "project days"?** If you work on a project sporadically (e.g., Jan 25, then Feb 15), all 14 project days of context come from meaningful work sessions - no wasted context on empty days.

**Manual loading**: To load any project's history from anywhere:
```bash
python3 ~/.claude/scripts/load-project-memory.py --list  # See all projects
python3 ~/.claude/scripts/load-project-memory.py ~/path/to/project  # Load specific project
```

### Memory Structure

```
~/.claude/memory/
├── LONG_TERM.md              # Synthesized knowledge about you
├── projects-index.json       # Project-to-work-days mapping
├── daily/
│   └── YYYY-MM-DD.md         # Summarized daily entries
├── transcripts/
│   └── YYYY-MM-DD/
│       └── {session_id}.jsonl  # Raw session transcripts (deduplicated by session ID)
└── .captured                 # Tracks which sessions were already captured
```

### Synthesis Workflow

Run `/synthesize` periodically (weekly recommended) to:

1. **Phase 0**: Update project index (maps projects to work days)
2. **Phase 1**: Convert raw transcripts into daily summaries (with project tags)
3. **Phase 2**: Update long-term memory with patterns and insights

## Uninstallation

```bash
cd claude-memory-system
./uninstall.sh
```

This removes the cron job and hooks but preserves your memory data. To fully remove:

```bash
rm -rf ~/.claude/memory
rm -rf ~/.claude/skills/{remember,synthesize,recall,reload}
rm ~/.claude/scripts/{load-memory,save-session,recover-transcripts,load-project-memory}.sh
rm ~/.claude/scripts/load-project-memory.py
```

## Updates

```bash
cd claude-memory-system
git pull
./install.sh
```

## File Locations

| Component | Location |
|-----------|----------|
| Memory data | `~/.claude/memory/` |
| Project index | `~/.claude/memory/projects-index.json` |
| Scripts | `~/.claude/scripts/` |
| Skills | `~/.claude/skills/` |
| Settings | `~/.claude/settings.json` |
| Recovery log | `~/.claude/memory/recovery.log` |

## License

MIT
