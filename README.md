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
- **Auto-synthesis**: Unprocessed transcripts are automatically processed at session start
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

## Permissions

The memory system uses a **PreToolUse hook** to auto-approve memory operations without prompting. This approach works around a Claude Code limitation where subagents don't inherit permissions from `settings.json` ([GitHub #10906](https://github.com/anthropics/claude-code/issues/10906)).

### How It Works

When Claude (or a subagent) calls a tool that targets memory files:

1. The PreToolUse hook (`~/.claude/hooks/pretooluse-allow-memory.sh`) runs before the tool executes
2. The hook checks if the operation targets `.claude/memory` paths
3. If yes, it returns `{"permissionDecision": "allow"}` to auto-approve
4. If no, normal permission flow applies

### What's Auto-Approved

- **Read/Edit/Write** operations on `~/.claude/memory/**`
- **Skill invocations** for memory skills (synthesize, remember, recall, reload, settings)
- **Task tool** calls with memory-related prompts
- **Python operations** using `indexing.py`

The installer also adds minimal Read permissions for fallback:
- `Read(~/.claude/**)` - Read memory and skill files
- `Read(//{home}/.claude/projects/**)` - Read project transcripts for orphan recovery

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

**Project detection**: By default, uses exact path match only. Enable `projectSettings.includeSubdirectories` to match `/project/subdir` to `/project/`.

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
  "globalShortTerm": { "workingDays": 7, "tokenLimit": 15000 },
  "globalLongTerm": { "tokenLimit": 7000 },
  "projectShortTerm": { "workingDays": 7, "tokenLimit": 5000 },
  "projectLongTerm": { "tokenLimit": 3000 },
  "projectSettings": { "includeSubdirectories": false },
  "totalTokenBudget": 30000
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `globalShortTerm.workingDays` | 7 | Recent days with activity to load (daily summaries) |
| `globalShortTerm.tokenLimit` | 15000 | Token limit for daily summaries |
| `globalLongTerm.tokenLimit` | 7000 | Token limit for global long-term memory |
| `projectShortTerm.workingDays` | 7 | Project-specific days to load (only days with project activity) |
| `projectShortTerm.tokenLimit` | 5000 | Token limit for project daily history |
| `projectLongTerm.tokenLimit` | 3000 | Token limit for project long-term memory |
| `projectSettings.includeSubdirectories` | false | Match subdirs to parent project |
| `totalTokenBudget` | 30000 | Overall memory token budget (informational) |

Token limits are soft warnings, not hard caps. Use `/settings usage` to check current token consumption.

### Synthesis Workflow

Synthesis runs automatically at session start when unprocessed transcripts exist. Claude spawns a background subagent to process them, keeping the main conversation context lean.

The synthesis process:

1. **Phase 0**: Update project index (maps projects to work days)
2. **Phase 1**: Convert raw transcripts into daily summaries with tagged learnings (`[scope/type]`)
3. **Phase 2**: Route learnings to appropriate long-term memory:
   - `[global/*]` → `global-long-term-memory.md`
   - `[{project}/*]` → `project-memory/{project}-long-term-memory.md`

**Learning types**: `error`, `best-practice`, `data-quirk`, `decision`, `command`

**Manual synthesis**: You can also run `/synthesize` at any time to process pending transcripts immediately.

## Uninstallation

```bash
cd claude-memory-system
python3 uninstall.py           # Remove hooks/permissions, keep memory data
python3 uninstall.py --purge   # Remove everything including memory data
```

## Updates

```bash
cd claude-memory-system
git pull
python3 install.py    # or: python install.py
```

The installer is idempotent and preserves your existing memory data.

## File Locations

| Component | Location |
|-----------|----------|
| Memory data | `~/.claude/memory/` |
| Memory settings | `~/.claude/memory/settings.json` |
| Project index | `~/.claude/memory/projects-index.json` |
| Scripts | `~/.claude/scripts/` |
| Skills | `~/.claude/skills/` |
| Claude settings | `~/.claude/settings.json` |

## License

MIT
