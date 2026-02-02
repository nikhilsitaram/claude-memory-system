# Claude Code Memory System

A markdown-based memory system for Claude Code that automatically captures session transcripts and provides tools for managing long-term memory.

## Features

- **Auto-capture**: Full transcripts saved on session end and before compaction
- **Two-tier memory**: Global patterns (always loaded) + project-specific learnings (loaded when in project)
- **Self-improvement loop**: Errors, best practices, and decisions are automatically extracted and routed to long-term memory
- **Project-aware loading**: Automatically loads project-specific history (configurable days) when working in a known project directory
- **Configurable settings**: Token budgets, working days, and subdirectory matching via `~/.claude/memory/settings.json`
- **Proactive recall**: Claude automatically searches older memory when historical context would help
- **Recovery**: Orphaned transcripts from ungraceful exits are recovered automatically on session start
- **Auto-synthesize prompt**: Unprocessed transcripts trigger reminder at session start
- **Manual notes**: `/remember` for specific highlights
- **Historical search**: `/recall` for searching older memory
- **Cross-platform**: Works on Windows, macOS, and Linux (Python 3.9+ required)
- **Shareable**: One-command installation for teammates

## Installation

```bash
git clone https://github.com/nikhilsitaram/claude-memory-system.git
cd claude-memory-system
python3 install.py    # or: python install.py
```

Start a new Claude Code session to activate the memory system.

The installer automatically adds these permissions so Claude can manage memory files without prompting:

- `Read($HOME/.claude/**)` - Read memory files
- `Edit($HOME/.claude/memory/**)`, `Edit($HOME/.claude/memory/*)`, `Edit($HOME/.claude/memory/daily/*)`, `Edit($HOME/.claude/memory/project-memory/*)` - Edit memory files
- `Write($HOME/.claude/memory/**)`, `Write($HOME/.claude/memory/*)`, `Write($HOME/.claude/memory/daily/*)`, `Write($HOME/.claude/memory/project-memory/*)` - Write memory files
- `Bash(rm -rf $HOME/.claude/memory/transcripts/*)` - Delete processed transcripts

**Why so many patterns?** Permissions use absolute paths (not `~`) and include both recursive (`**`) and direct (`*`) patterns for subagent compatibility. Subagents spawned via the Task tool have stricter permission matching.

## Requirements

- **Python 3.9+** - Required for all platforms
- **Claude Code CLI** - Must be installed and run at least once (`~/.claude` must exist)

### Platform Notes

| Platform | Notes |
|----------|-------|
| **Linux** | Fully supported. Use `python3 install.py`. |
| **macOS** | Fully supported. Use `python3 install.py`. |
| **Windows** | Best-effort support. Use `python install.py`. Recommended: Use WSL for guaranteed compatibility. |

The installer automatically detects your Python command (`python3` vs `python`) and configures hooks with absolute paths for cross-platform compatibility.

## Commands

| Command | Description |
|---------|-------------|
| `/remember [note]` | Save a note to today's daily log |
| `/synthesize` | Process transcripts into daily summaries and update long-term memory |
| `/recall [query]` | Search through all historical daily memory files |
| `/reload` | Synthesize pending transcripts and reload memory (use after `/clear`) |
| `/settings` | View/modify memory settings and check token usage |

**Note**: `/clear` does not trigger hooks ([GitHub #21578](https://github.com/anthropics/claude-code/issues/21578)), so use `/reload` afterward to restore memory context.

## How It Works

### Session Lifecycle

1. **Session Start**: Loads long-term memory, last 7 days of daily summaries, and project-specific history. Also recovers any orphaned transcripts from previous ungraceful exits.
2. **During Session**: Use `/remember` to capture important notes; Claude proactively uses `/recall` when historical context would help
3. **Before Compaction**: Transcript saved (both manual `/compact` and automatic compaction)
4. **Session End**: Transcript automatically saved to `~/.claude/memory/transcripts/`

### Project-Aware Memory Loading

When you start a session in a project directory, the system automatically loads that project's historical context:

| Context Type | Default | Description |
|--------------|---------|-------------|
| All projects | 7 working days | Days with actual session activity (not calendar days) |
| Current project | 7 working days | Additional project-specific history |

**Working days vs calendar days**: The system scans for existing daily files rather than looping through calendar dates. If you work Mon-Thu but not Fri-Sun, you get 7 actual work days of context, not 7 calendar days with 3 empty.

**Project detection**: By default, uses exact path match only. Enable `includeSubdirectories` in settings to match `/project/subdir` to `/project/`.

**Why "project days"?** If you work on a project sporadically (e.g., Jan 25, then Feb 15), all project days of context come from meaningful work sessions - no wasted context on empty days.

**Manual loading**: To load any project's history from anywhere:
```bash
python3 ~/.claude/scripts/load-project-memory.py --list  # See all projects
python3 ~/.claude/scripts/load-project-memory.py ~/path/to/project  # Load specific project
```

### Memory Structure

```
~/.claude/memory/
├── global-long-term-memory.md  # Global patterns, user profile (always loaded)
├── settings.json               # Memory system configuration
├── projects-index.json         # Project-to-work-days mapping
├── daily/
│   └── YYYY-MM-DD.md           # Summarized daily entries with learnings
├── project-memory/
│   └── {project}-long-term-memory.md  # Project-specific learnings (loaded when in project)
├── transcripts/
│   └── YYYY-MM-DD/
│       └── {session_id}.jsonl  # Raw session transcripts (deduplicated by session ID)
└── .captured                   # Tracks which sessions were already captured
```

**Two-tier memory**: Global memory contains user profile, patterns, and project-agnostic learnings. Project memory contains project-specific errors, data quirks, and decisions. Both are updated during `/synthesize`.

### Settings

Configure the memory system via `~/.claude/memory/settings.json`:

```json
{
  "shortTermMemory": {
    "workingDays": 7,
    "tokenLimit": 15000
  },
  "projectMemory": {
    "workingDays": 7,
    "tokenLimit": 8000,
    "includeSubdirectories": false
  },
  "longTermMemory": {
    "tokenLimit": 7000
  },
  "totalTokenBudget": 30000
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `shortTermMemory.workingDays` | 7 | Number of recent working days to load |
| `projectMemory.workingDays` | 7 | Number of project-specific days to load |
| `projectMemory.includeSubdirectories` | false | Match subdirs to parent project |
| `totalTokenBudget` | 30000 | Overall memory token budget (informational) |

Token limits are soft warnings, not hard caps. Use `/settings usage` to check current token consumption.

### Synthesis Workflow

Run `/synthesize` periodically (weekly recommended) to:

1. **Phase 0**: Update project index (maps projects to work days)
2. **Phase 1**: Convert raw transcripts into daily summaries with tagged learnings (`[scope/type]`)
3. **Phase 2**: Route learnings to appropriate long-term memory:
   - `[global/*]` → `global-long-term-memory.md`
   - `[{project}/*]` → `project-memory/{project}-long-term-memory.md`

**Learning types**: `error`, `best-practice`, `data-quirk`, `decision`, `command`

**Auto-synthesis**: At session start, if unprocessed transcripts exist, Claude spawns a background subagent to process them. This keeps the main conversation context lean while handling potentially large transcript files.

## Uninstallation

```bash
cd claude-memory-system
python3 uninstall.py    # or: python uninstall.py
```

This removes hooks and permissions but preserves your memory data and settings. To fully remove:

```bash
rm -rf ~/.claude/memory
rm -rf ~/.claude/skills/{remember,synthesize,recall,reload,settings}
rm ~/.claude/scripts/{memory_utils,load_memory,save_session,indexing,load-project-memory}.py
```

## Updates

```bash
cd claude-memory-system
git pull
python3 install.py    # or: python install.py
```

The installer is idempotent and handles migrations from previous versions (including the bash-based version).

## File Locations

| Component | Location |
|-----------|----------|
| Memory data | `~/.claude/memory/` |
| Memory settings | `~/.claude/memory/settings.json` |
| Project index | `~/.claude/memory/projects-index.json` |
| Scripts | `~/.claude/scripts/` |
| Skills | `~/.claude/skills/` |
| Claude settings | `~/.claude/settings.json` |

## Migration from Bash Version

If you previously installed the bash-based version (install.sh), the Python installer will:

1. Remove old bash hooks (load-memory.sh, save-session.sh)
2. Remove the cron job (recovery now runs on SessionStart)
3. Add new Python hooks
4. Preserve all your memory data

## License

MIT
