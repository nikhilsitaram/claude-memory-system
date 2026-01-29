# Claude Code Memory System

A markdown-based memory system for Claude Code that automatically captures session transcripts and provides tools for managing long-term memory.

## Features

- **Auto-capture**: Full transcripts saved on session end and before compaction
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

## How It Works

### Session Lifecycle

1. **Session Start**: Loads long-term memory and last 7 days of daily summaries
2. **During Session**: Use `/remember` to capture important notes; Claude proactively uses `/recall` when historical context would help
3. **Before Compaction**: Transcript saved (both manual `/compact` and automatic compaction)
4. **Session End**: Transcript automatically saved to `~/.claude/memory/transcripts/`
5. **Recovery**: Hourly cron job recovers any missed transcripts from ungraceful exits

### Memory Structure

```
~/.claude/memory/
├── LONG_TERM.md              # Synthesized knowledge about you
├── daily/
│   └── YYYY-MM-DD.md         # Summarized daily entries
├── transcripts/
│   └── YYYY-MM-DD/
│       └── {session_id}.jsonl  # Raw session transcripts (deduplicated by session ID)
└── .captured                 # Tracks which sessions were already captured
```

### Synthesis Workflow

Run `/synthesize` periodically (weekly recommended) to:

1. **Phase 1**: Convert raw transcripts into daily summaries
2. **Phase 2**: Update long-term memory with patterns and insights

## Uninstallation

```bash
cd claude-memory-system
./uninstall.sh
```

This removes the cron job and hooks but preserves your memory data. To fully remove:

```bash
rm -rf ~/.claude/memory
rm -rf ~/.claude/skills/{remember,synthesize,recall}
rm ~/.claude/scripts/{load-memory,save-session,recover-transcripts}.sh
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
| Scripts | `~/.claude/scripts/` |
| Skills | `~/.claude/skills/` |
| Settings | `~/.claude/settings.json` |
| Recovery log | `~/.claude/memory/recovery.log` |

## License

MIT
