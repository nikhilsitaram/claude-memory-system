# Claude Code Memory System

A markdown-based memory system for Claude Code that persists context across sessions. Hooks, scripts, and skills automatically capture session transcripts and synthesize them into structured long-term memory.

## Features

- **Two-tier memory**: Global patterns (always loaded) + project-specific learnings (loaded when in project), filtered by `[scope/*]` tags
- **Auto-synthesis**: Runs via launchd/systemd timer outside active sessions. Per-date processing ensures multi-day backlogs produce correct daily files
- **Incremental processing**: Tracks per-session byte offsets to delta-extract only new content; skips unchanged sessions
- **Deterministic scoping**: Session CWD metadata determines project scope programmatically — no LLM guessing
- **Age-based decay**: Learnings older than 30 days are automatically archived; pinned sections protected
- **Project-aware loading**: Loads project-specific history when working in a known project directory, including git worktree resolution
- **Tag-based filtering**: Daily entries tagged with `[scope/type]` — only matching entries loaded per tier, reducing token waste
- **Error surfacing**: Synthesis failures logged and surfaced as alerts on next session start
- **Configurable**: Token budgets, working days, synthesis scheduling, and decay via settings.json
- **Manual tools**: `/remember` for notes, `/recall` for historical search, `/synthesize` for on-demand processing
- **Cross-platform**: Works on Windows, macOS, and Linux (Python 3.9+)

## Installation

```bash
git clone https://github.com/nikhilsitaram/claude-memory-system.git
cd claude-memory-system
python3 install.py    # or: python install.py
```

Start a new Claude Code session to activate the memory system.

## Requirements

- **Python 3.9+**
- **Claude Code CLI** — must be installed and run at least once (`~/.claude` must exist)

| Platform | Notes |
|----------|-------|
| **Linux** | Fully supported. Use `python3 install.py`. |
| **macOS** | Fully supported. Use `python3 install.py`. |
| **Windows** | Best-effort. Use `python install.py`. Recommended: WSL for full compatibility. |

The installer detects your Python command and configures hooks with absolute paths.

## Commands

| Command | Description |
|---------|-------------|
| `/remember [note]` | Save a note to today's daily log |
| `/synthesize` | Process transcripts into daily summaries and update long-term memory |
| `/recall [query]` | Search through all historical daily memory files |
| `/settings` | View/modify memory settings and check token usage |
| `/projects` | Manage project data — list status, merge orphans, cleanup stale entries |

## How It Works

### Session Lifecycle

1. **Session Start**: Loads long-term memory + filtered short-term memory. Checks for synthesis errors to surface.
2. **During Session**: Use `/remember` to capture notes; Claude proactively uses `/recall` for historical context.
3. **Session End**: Transcript saved. Launchd/systemd timer processes it out-of-session.

### Memory Architecture

**Modes** (`mode` setting):
- `full` (default): loads recall + global LTM + project LTM + project STM + global STM.
- `light`: loads only recall + global LTM. Useful when Claude Code's native project-scoped memory covers project context — this system then handles only cross-project memory and last-session continuity.

**Long-term memory** (curated, persistent):

| Tier | File | Loaded | Content |
|------|------|--------|---------|
| Global | `global-long-term-memory.md` | Every session | User profile, global patterns |
| Project | `project-memory/{project}-long-term-memory.md` | When `$PWD` matches | Project-specific learnings |

**Short-term memory** (recent daily summaries, filtered by scope tags):

| Tier | Source | Default Days | Filter |
|------|--------|------|--------|
| Global | `daily/*.md` | 2 | `[global/*]` tagged entries only |
| Project | `daily/*.md` | 5 | `[project-name/*]` tagged entries only |

**Learning flow:**
```
Session transcript → Phase 1 → Daily summary (Actions, Decisions, Learnings, Lessons)
                   → Phase 2 → Route to long-term memory
                   → Phase 3 → Age-based decay
```

### Tag-Based Filtering

Daily files contain entries tagged with scopes like `[global/implement]` or `[claude-memory-system/gotcha]`. Only matching entries are loaded:
- `[global/*]` entries → Global short-term memory (every session)
- `[project-name/*]` entries → Project short-term memory (when in that project)
- Pipe-delimited `[scope1|scope2/type]` for multi-scope entries
- Untagged content only appears in raw daily files

### Project Detection

Git subdirectories automatically map to their repository root unless gitignored. Git worktree paths are resolved to the main repo via `git rev-parse`.

**Working days**: Days without matching tagged content are skipped, so sporadic projects get all their context from meaningful work sessions.

### Memory Structure

```
~/.claude/memory/
├── global-long-term-memory.md    # Global patterns, user profile (always loaded)
├── settings.json                 # Memory system configuration
├── projects-index.json           # Project-to-work-days mapping
├── .last-synthesis               # UTC timestamp of last synthesis
├── .synthesis-state.json         # Per-session byte offset + line count tracking
├── .synthesis-errors.log         # Deferred synthesis error log (cleared after read)
├── .decay-archive.md             # Archived learnings (recoverable)
├── daily/
│   └── YYYY-MM-DD.md             # Summarized daily entries with tagged learnings
├── project-memory/
│   └── {project}-long-term-memory.md  # Project-specific learnings
└── templates/                    # Default file templates
```

### Entry Format

**Daily files** have four sections:
- `## Actions` — What was done, tagged `[scope/implement]`, `[scope/improve]`, etc.
- `## Decisions` — Choices with rationale, tagged `[scope/design]`, `[scope/tradeoff]`, etc.
- `## Learnings` — Patterns/gotchas, tagged `[scope/gotcha]`, `[scope/pattern]`, etc.
- `## Lessons` — Actionable takeaways, tagged `[scope/insight]`, `[scope/tip]`, etc.

**Long-term files** mirror with `## Key Actions`, `## Key Decisions`, `## Key Learnings`, `## Key Lessons`.

**Long-term entry format**: `- (YYYY-MM-DD) [type] Description` — date prefix enables age-based decay.

Entries routed from daily to LTM are marked `[routed]` in the daily file and skipped at load time.

### Settings

Configure via `~/.claude/memory/settings.json` or `/settings set <path> <value>`:

```json
{
  "mode": "full",
  "globalShortTerm": { "workingDays": 2 },
  "globalLongTerm": { "tokenLimit": 3000 },
  "projectShortTerm": { "workingDays": 5 },
  "projectLongTerm": { "tokenLimit": 3000 },
  "synthesis": {
    "intervalHours": 0.5,
    "model": "sonnet",
    "minSessionMessages": 5
  },
  "decay": {
    "ageDays": 30,
    "projectWorkingDays": 20,
    "archiveRetentionDays": 365
  }
}
```

| Setting | Default | Description |
|---------|---------|-------------|
| `mode` | `full` | `full` loads all sections; `light` loads only previous-session recall + global LTM (skips project LTM/STM and global STM — useful when relying on Claude Code's native project-scoped memory) |
| `globalShortTerm.workingDays` | 2 | Recent days of global daily summaries to load |
| `globalLongTerm.tokenLimit` | 3,000 | Token limit for global long-term memory |
| `projectShortTerm.workingDays` | 5 | Project-specific days to load |
| `projectLongTerm.tokenLimit` | 3,000 | Token limit for project long-term memory |
| `synthesis.intervalHours` | 0.5 | Hours between auto-synthesis checks |
| `synthesis.model` | sonnet | Model for synthesis LLM calls |
| `synthesis.minSessionMessages` | 5 | Skip sessions with fewer messages |
| `decay.ageDays` | 30 | Archive global LTM entries older than N days |
| `decay.projectWorkingDays` | 20 | Archive project LTM entries after N project work days |
| `decay.archiveRetentionDays` | 365 | Purge archived items after N days |
| `previousSessionRecall.tokenLimit` | 500 | Token budget for previous-session recall; 1/3 head + 2/3 tail allocation |

**Calculated token limits** (short-term): `workingDays × 750 tokens/day`

| Component | Calculation | Default |
|-----------|-------------|---------|
| Global short-term | 2 × 750 | 1,500 |
| Global long-term | fixed | 3,000 |
| Project short-term | 5 × 750 | 3,750 |
| Project long-term | fixed | 3,000 |
| **Total budget** | | **11,250** |

Token limits are soft warnings, not hard caps. Use `/settings usage` to check current consumption.

### Age-Based Decay

**Protected from decay (auto-pinned):**
- `## About Me`, `## Current Projects`, `## Technical Environment`, `## Patterns & Preferences`
- `## Pinned` — move important learnings here to protect them

**Subject to decay:**
- `## Key Actions`, `## Key Decisions`, `## Key Learnings`, `## Key Lessons`

Entries with dates older than `decay.ageDays` (default: 30) are moved to `.decay-archive.md`. Archived items older than `decay.archiveRetentionDays` (default: 365) are purged.

### Synthesis

A launchd/systemd user timer runs `synthesis_cron.py` outside active sessions via `claude -p`. SessionEnd hook triggers on session exit. When multiple dates are pending, each is processed as a separate LLM call.

**Error handling**: Failures are logged to `.synthesis-errors.log` and surfaced as an alert on next session start. Eager timestamps are cleared on failure so the next timer interval can retry.

The synthesis pipeline:
1. Update project index (maps projects to work days)
2. Extract and scope transcripts using session CWD metadata
3. Synthesize into daily summaries with tagged entries
4. Route significant learnings to long-term memory (keyword-overlap dedup, route cap of 5/file)
5. Apply age-based decay
6. Update synthesis state (per-session byte offsets)

### Permissions

The memory system uses a **PreToolUse hook** to auto-approve memory operations without prompting. This works around a Claude Code limitation where subagents don't inherit permissions ([GitHub #10906](https://github.com/anthropics/claude-code/issues/10906)).

**What's auto-approved:**
- Read/Edit/Write operations on `~/.claude/memory/**`
- Skill invocations for memory skills
- Task tool calls with memory-related prompts
- Bash operations using `~/.claude/scripts/*`

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
python3 install.py
```

The installer is idempotent and preserves existing memory data.

## File Locations

| Component | Location |
|-----------|----------|
| Memory data | `~/.claude/memory/` |
| Settings | `~/.claude/memory/settings.json` |
| Project index | `~/.claude/memory/projects-index.json` |
| Scripts | `~/.claude/scripts/` |
| Skills | `~/.claude/skills/` |

## License

MIT
