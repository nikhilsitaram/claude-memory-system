# Claude Code Memory System

A markdown-based memory system for Claude Code that automatically captures session transcripts and provides tools for managing long-term memory.

## Features

- **Auto-capture**: Full transcripts saved on session end and before compaction
- **Two-tier memory**: Global patterns (always loaded) + project-specific learnings (loaded when in project), filtered by `[scope/*]` tags
- **Self-improvement loop**: Errors, best practices, and decisions are automatically extracted and routed to long-term memory
- **Age-based decay**: Learnings older than 30 days are automatically archived; pinned sections protected from decay
- **Project-aware loading**: Automatically loads project-specific history (configurable days) when working in a known project directory
- **Configurable settings**: Token budgets, working days, synthesis scheduling, and decay via `~/.claude/memory/settings.json`
- **Proactive recall**: Claude automatically searches older memory when historical context would help
- **Recovery**: Orphaned transcripts from ungraceful exits are recovered automatically on session start
- **Auto-synthesis**: Scheduled synthesis (first session of day + every N hours) to keep memory fresh
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
- **Skill invocations** for memory skills (synthesize, remember, recall, settings)
- **Task tool** calls with memory-related prompts
- **Bash operations** using `~/.claude/scripts/*` (indexing.py, token_usage.py, etc.)

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
| `/settings` | View/modify memory settings and check token usage |
| `/projects` | Manage project data - list status, merge orphans, cleanup stale entries |

## How It Works

### Session Lifecycle

1. **Session Start**: Loads long-term memory, global short-term (`[global/*]` entries from last 2 days), and project short-term (`[project/*]` entries from last 7 days). Also recovers any orphaned transcripts and prompts for synthesis if needed.
2. **During Session**: Use `/remember` to capture important notes; Claude proactively uses `/recall` when historical context would help
3. **Before Compaction**: Transcript saved (both manual `/compact` and automatic compaction)
4. **Session End**: Transcript automatically saved to `~/.claude/memory/transcripts/`

### Project-Aware Memory Loading

When you start a session in a project directory, the system automatically loads relevant context filtered by scope tags:

| Context Type | Default | Filter | Description |
|--------------|---------|--------|-------------|
| Global short-term | 2 days | `[global/*]` | Only entries tagged with global scope |
| Project short-term | 7 days | `[project/*]` | Only entries tagged with current project |

**Tag-based filtering**: Daily files contain entries tagged with scopes like `[global/implement]` or `[claude-memory-system/gotcha]`. Only matching entries are loaded - global short-term loads `[global/*]` entries, project short-term loads `[project-name/*]` entries. This keeps context relevant and reduces token usage.

**Working days**: The system scans for daily files with matching tagged content. Days without matching content are skipped.

**Project detection**: By default, uses exact path match only. Enable `projectSettings.includeSubdirectories` to match `/project/subdir` to `/project/`.

**Why "project days"?** If you work on a project sporadically (e.g., Jan 25, then Feb 15), all project days of context come from meaningful work sessions - no wasted context on empty days.

### Memory Structure

```
~/.claude/memory/
├── global-long-term-memory.md  # Global patterns, user profile (always loaded)
├── settings.json               # Memory system configuration
├── projects-index.json         # Project-to-work-days mapping
├── .last-synthesis             # UTC timestamp of last synthesis
├── .decay-archive.md           # Archived learnings (recoverable)
├── .migration-complete         # One-time migration marker
├── daily/
│   └── YYYY-MM-DD.md           # Summarized daily entries with learnings
├── project-memory/
│   └── {project}-long-term-memory.md  # Project-specific learnings (loaded when in project)
├── transcripts/
│   └── YYYY-MM-DD/
│       └── {session_id}.jsonl  # Raw session transcripts (deduplicated by session ID)
└── .captured                   # Tracks which sessions were already captured
```

**Two-tier memory**: Global memory contains user profile, patterns, and project-agnostic learnings. Project memory contains project-specific learnings and decisions. Both are updated during `/synthesize`.

### Settings

Configure the memory system via `~/.claude/memory/settings.json` or `/settings set <path> <value>`:

```json
{
  "globalShortTerm": { "workingDays": 2 },
  "globalLongTerm": { "tokenLimit": 5000 },
  "projectShortTerm": { "workingDays": 7 },
  "projectLongTerm": { "tokenLimit": 5000 },
  "projectSettings": { "includeSubdirectories": false },
  "synthesis": { "intervalHours": 2 },
  "decay": { "ageDays": 30, "archiveRetentionDays": 365 }
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `globalShortTerm.workingDays` | 2 | Recent days with activity to load |
| `globalLongTerm.tokenLimit` | 5,000 | Fixed limit for global long-term memory |
| `projectShortTerm.workingDays` | 7 | Project-specific days to load |
| `projectLongTerm.tokenLimit` | 5,000 | Fixed limit for project long-term memory |
| `projectSettings.includeSubdirectories` | false | Match subdirs to parent project |
| `synthesis.intervalHours` | 2 | Hours between auto-synthesis prompts |
| `decay.ageDays` | 30 | Archive learnings older than this |
| `decay.archiveRetentionDays` | 365 | Purge archived items after this |

**Calculated token limits**: Short-term limits are calculated dynamically in `memory_utils.py`:

| Component | Calculation | Default |
|-----------|-------------|---------|
| Global short-term | workingDays × 750 | 1,500 |
| Global long-term | fixed | 5,000 |
| Project short-term | workingDays × 750 | 5,250 |
| Project long-term | fixed | 5,000 |
| **Total budget** | sum of above | **16,750** |

The 750 tokens/day multiplier is based on ~400-600 observed after scope filtering. Short-term memory is filtered by `[scope/*]` tags, so only relevant entries are loaded. To increase short-term limits, change `workingDays`.

Token limits are soft warnings, not hard caps. Use `/settings usage` to check current token consumption.

### Age-Based Decay

Learnings in long-term memory files are subject to automatic archival:

**Protected from decay (auto-pinned):**
- `## About Me`, `## Current Projects`, `## Technical Environment`, `## Patterns & Preferences`
- `## Pinned` - move important learnings here to protect them

**Subject to decay:**
- `## Key Actions`, `## Key Decisions`, `## Key Learnings`, `## Key Lessons`

Learnings with creation dates older than `decay.ageDays` (default: 30) are moved to `~/.claude/memory/.decay-archive.md`. Archived items older than `decay.archiveRetentionDays` (default: 365) are purged.

**Entry format**: `- (YYYY-MM-DD) [type] Description`

The date prefix enables age tracking. Entries without dates are protected from decay.

### Synthesis Workflow

Synthesis is scheduled to balance freshness with efficiency:

- **First session of day (UTC)**: Always prompts if transcripts pending
- **Subsequent sessions**: Only prompts if more than `synthesis.intervalHours` since last synthesis

Claude spawns a background Sonnet subagent to process transcripts, keeping the main conversation context lean. The model is configurable via `synthesis.model` in settings.

The synthesis process:

1. **Phase 0**: Migration check (one-time: adds Pinned sections, dates to learnings)
2. **Phase 0.5**: Update project index (maps projects to work days)
3. **Phase 1**: Convert raw transcripts into daily summaries (1-4 KB each) with tagged learnings
4. **Phase 2**: Route learnings to appropriate long-term memory:
   - `[global/*]` → `global-long-term-memory.md`
   - `[{project}/*]` → `project-memory/{project}-long-term-memory.md`
5. **Phase 3**: Apply age-based decay (archive old learnings, purge expired archives)
6. **Phase 4**: Update synthesis timestamp

**Action types:** `implement`, `improve`, `document`, `analyze`
**Decision types:** `design`, `tradeoff`, `scope`
**Learning types:** `gotcha`, `pitfall`, `pattern`
**Lesson types:** `insight`, `tip`, `workaround`

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
