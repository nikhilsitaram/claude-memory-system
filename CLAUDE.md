# Claude Code Memory System - Development Guide

## Project Purpose

This repo provides a markdown-based memory persistence system for Claude Code. It installs hooks, scripts, and skills that enable Claude to remember context across sessions.

## Repo Structure

```
claude-memory-system/
├── install.py              # Cross-platform installer (Python 3.9+)
├── uninstall.py            # Cross-platform uninstaller
├── scripts/
│   ├── memory_utils.py     # Shared utilities: paths, settings, locking
│   ├── load_memory.py      # SessionStart hook - loads memory + orphan recovery
│   ├── save_session.py     # SessionEnd/PreCompact hook - saves transcript
│   ├── indexing.py         # Transcript extraction + project index building
│   ├── decay.py            # Age-based decay for long-term memory
│   ├── load-project-memory.py  # Manual project memory loader
│   └── project_manager.py  # Project lifecycle management library
├── skills/
│   ├── remember/SKILL.md   # /remember - save notes
│   ├── synthesize/SKILL.md # /synthesize - process transcripts
│   ├── recall/SKILL.md     # /recall - search history
│   ├── reload/SKILL.md     # /reload - synthesize + load after /clear
│   ├── settings/SKILL.md   # /settings - view/modify memory config
│   └── projects/SKILL.md   # /projects - manage project data
├── tests/
│   └── test_project_manager.py  # Unit tests for project_manager
└── templates/
    ├── global-long-term-memory.md  # Global patterns template
    ├── project-long-term-memory.md # Project memory template
    └── settings.json               # Default memory settings
```

## Installation Locations

| Repo Path | Installs To |
|-----------|-------------|
| `scripts/*.py` | `~/.claude/scripts/` |
| `skills/*/` | `~/.claude/skills/` |
| `templates/*.md` | `~/.claude/memory/templates/` (always updated) |
| `templates/global-long-term-memory.md` | `~/.claude/memory/` (if not exists) |
| `templates/settings.json` | `~/.claude/memory/` (if not exists) |

The install script also:
- Creates `~/.claude/memory/{daily,transcripts,project-memory,templates}/` directories
- Adds hooks to `~/.claude/settings.json` (SessionStart, SessionEnd, PreCompact, PreToolUse)
- Adds minimal permissions to settings.json:
  - `Read(~/.claude/**)` - Read memory/skill files (tilde expansion)
  - `Read(//{home}/.claude/projects/**)` - Read project transcript paths (orphan recovery)

  **Note on Edit/Write**: The PreToolUse hook (`pretooluse-allow-memory.sh`) auto-approves
  all Edit/Write operations targeting `.claude/memory` paths. This replaces explicit
  Edit/Write permissions and works around a Claude Code bug where subagents don't
  inherit permissions (GitHub issues #10906, #11934, #18172, #18950).
- Builds initial project index (`~/.claude/memory/projects-index.json`)
- Auto-migrates `LONG_TERM.md` → `global-long-term-memory.md` if needed

## Two-Tier Memory Architecture

The memory system uses a two-tier approach to store learnings:

### Global Memory (`global-long-term-memory.md`)
- **Location**: `~/.claude/memory/global-long-term-memory.md`
- **When loaded**: Every session
- **Content**: Project-agnostic patterns, user profile, global best practices

### Project Memory (`project-memory/{project}-long-term-memory.md`)
- **Location**: `~/.claude/memory/project-memory/{project}-long-term-memory.md`
- **When loaded**: When `$PWD` matches a known project
- **Content**: Project-specific accumulated knowledge (errors, quirks, decisions)

### Learning Flow (Self-Improvement Loop)

```
Session transcript
    ↓ (Phase 1: /synthesize)
Daily summary with ## Learnings (tagged [scope/type])
    ↓ (Phase 2: /synthesize)
Route by tag:
  - [global/*] → global-long-term-memory.md
  - [{project}/*] → project-memory/{project}-long-term-memory.md
```

### Learning Tags

Learnings are tagged with `[scope/type]`:

**Scopes:**
- `global` - Project-agnostic patterns
- `{project-name}` - Project-specific (e.g., `granada`, `cartwheel`)

**Types:**
- `error` - Bugs, exceptions, failed commands
- `best-practice` - Patterns that worked well
- `data-quirk` - Data gotchas, edge cases
- `decision` - Important choices and rationale
- `command` - Useful queries, scripts

### Project Memory vs CLAUDE.md

| | CLAUDE.md | Project Long-Term Memory |
|---|---|---|
| **Purpose** | Instructions for Claude | Accumulated learnings |
| **Source** | Written by human | Auto-generated from synthesis |
| **Storage** | Checked into git | In ~/.claude/memory/ |
| **Content** | How to work with codebase | What was learned while working |

## Directory Structure

After synthesis, the memory directory looks like:

```
~/.claude/memory/
├── global-long-term-memory.md    # Global patterns (always loaded, has ## Pinned)
├── settings.json                 # Memory configuration
├── projects-index.json           # Project → work days mapping
├── .last-synthesis               # UTC timestamp of last synthesis
├── .decay-archive.md             # Archived learnings (recoverable)
├── .migration-complete           # One-time migration marker
├── .captured                     # Tracks saved session IDs
├── daily/
│   └── YYYY-MM-DD.md             # Daily summaries with ## Learnings
├── project-memory/
│   ├── granada-long-term-memory.md
│   ├── cartwheel-long-term-memory.md
│   └── claude-memory-system-long-term-memory.md
├── templates/                    # Reference templates (updated on install)
│   ├── global-long-term-memory.md
│   └── project-long-term-memory.md
└── transcripts/
    └── YYYY-MM-DD/
        └── {session_id}.jsonl
```

## Making Changes

### Adding a New Skill

1. Create `skills/<name>/SKILL.md` with frontmatter (name, description, user-invocable)
2. Update `install.py`:
   - Add directory to `create_directories()` function
   - Add to `copy_skills()` function if needed
   - Add to "Available commands" in `print_success_message()`
3. Update `uninstall.py`:
   - Add to the cleanup instructions in `print_cleanup_instructions()`

### Adding a New Script

1. Create `scripts/<name>.py`
2. Add to `copy_scripts()` in `install.py` if it should be copied
3. If it needs a hook, add the hook config in `merge_hooks()` function
4. Update `uninstall.py` if hook cleanup needed

### Modifying Hooks

Hooks are defined in `install.py` in the `merge_hooks()` function. Available events:
- `SessionStart` - fires on startup, resume, clear, compact
- `SessionEnd` - fires on session end
- `PreCompact` - fires before context compaction
- `PreToolUse` - fires before each tool call (used to auto-approve memory operations)

Remember to update `uninstall.py` event list if adding new hook events.

## Testing Changes

```bash
# Re-run install to apply changes
python3 install.py

# Start a new Claude Code session to test
claude

# Check settings were applied
python3 -c "import json; print(json.dumps(json.load(open('$HOME/.claude/settings.json')), indent=2))"

# Test memory loading directly
python3 ~/.claude/scripts/load_memory.py

# Test indexing
python3 ~/.claude/scripts/indexing.py list-pending
python3 ~/.claude/scripts/indexing.py build-index

# Test decay (dry-run)
python3 ~/.claude/scripts/decay.py --dry-run
```

## Key Files Reference

- **~/.claude/settings.json**: User's Claude Code settings (hooks, permissions)
- **~/.claude/memory/settings.json**: Memory system configuration (working days, token limits, synthesis, decay)
- **global-long-term-memory.md**: Synthesized user profile and global patterns (has `## Pinned` section)
- **project-memory/{project}-long-term-memory.md**: Project-specific learnings (has `## Pinned` section)
- **projects-index.json**: Maps project paths to their work days (for project-aware loading)
- **daily/YYYY-MM-DD.md**: Daily session summaries (may include `<!-- projects: ... -->` tags and `## Learnings` section)
- **transcripts/{date}/{session_id}.jsonl**: Raw session data (JSONL format)
- **.captured**: Tracks which session IDs have been saved (prevents duplicates)
- **.last-synthesis**: UTC ISO timestamp of last synthesis (controls scheduling)
- **.decay-archive.md**: Archive of decayed learnings (recoverable, purged after 1 year)
- **.migration-complete**: Marker preventing re-run of one-time migration

## Project-Aware Loading

The `load_memory.py` script loads project-specific memory when:
1. The project index (`projects-index.json`) exists
2. `$PWD` matches a known project path (exact match by default, or prefix match if `includeSubdirectories` is enabled)

This loads:
- Project-specific long-term memory (`project-memory/{project}-long-term-memory.md`)
- Last N "project days" (configurable in settings, default 7), separate from the N working days loaded for all projects

**Working days vs calendar days**: The system scans existing daily files rather than looping through calendar dates. If you take days off, those empty days don't count against your quota.

**Subdirectory matching**: By default disabled. Enable via `/settings set projectSettings.includeSubdirectories true` to match `/project/backend/` to `/project/`.

## Settings Configuration

Memory system settings are stored in `~/.claude/memory/settings.json`:

```json
{
  "globalShortTerm": { "workingDays": 2, "tokenLimit": 15000 },
  "globalLongTerm": { "tokenLimit": 7000 },
  "projectShortTerm": { "workingDays": 7, "tokenLimit": 5000 },
  "projectLongTerm": { "tokenLimit": 3000 },
  "projectSettings": { "includeSubdirectories": false },
  "synthesis": { "intervalHours": 2 },
  "decay": { "ageDays": 30, "archiveRetentionDays": 365 },
  "totalTokenBudget": 30000
}
```

**Key settings:**
- `globalShortTerm.workingDays`: 2 (reduced from 7 - project memory covers most context)
- `synthesis.intervalHours`: Hours between auto-synthesis prompts (always runs on first session of day)
- `decay.ageDays`: Learnings older than this are archived (default: 30)
- `decay.archiveRetentionDays`: Archived items older than this are purged (default: 365)

Token limits are informational (soft warnings), not hard caps. Use `/settings` to see current usage alongside limits.

## Project Management (`/projects`)

The `/projects` skill manages Claude Code project data when projects are renamed, moved, or need cleanup.

### Common Scenarios

**After renaming a project folder:**
```
# User manually renamed ~/personal/personal-shopper → ~/personal/cartwheel
# Claude Code folders remain at ~/.claude/projects/-home-nsitaram-personal-personal-shopper

/projects
# Shows: personal-shopper marked as "path missing", cartwheel as valid
# Suggests: merge orphaned data into cartwheel

/projects merge personal-shopper into cartwheel
# Merges session history, work days, memory files
# Renames orphan folder to .merged.bak (not deleted)
```

**Clean up stale entries:**
```
/projects cleanup
# Shows stale index entries where path no longer exists
# Removes entries from index (doesn't delete folders)
```

### How It Works

The skill uses `project_manager.py` library functions:
- `list_projects()` - Show all indexed projects with status
- `find_orphaned_folders()` - Find Claude folders without valid projects
- `plan_merge_orphan(orphan, target)` - Preview merge operation
- `execute_merge_orphan(orphan, target, confirmed=True)` - Execute merge

All destructive operations:
1. Create timestamped backups in `~/.claude/memory/.backups/`
2. Require explicit user confirmation
3. Rename (not delete) orphan folders after merge

### Path Encoding

Claude Code encodes project paths as folder names:
- `/home/user/my-project` → `-home-user-my-project`
- Both `/` and `.` become `-`

This encoding is **lossy** - you cannot reliably decode back. Always read `sessions-index.json` inside the folder for the authoritative original path.

## Synthesis Scheduling

Auto-synthesis prompts are controlled by `synthesis.intervalHours` setting:

- **First session of day (UTC)**: Always prompts if transcripts pending
- **Subsequent sessions**: Only prompts if more than N hours since last synthesis
- **Default**: 2 hours

The `.last-synthesis` file stores the UTC timestamp. Delete it to force synthesis on next session.

## Age-Based Decay

Learnings in long-term memory files are subject to 30-day decay (configurable via `decay.ageDays`):

**Auto-pinned sections** (never decay):
- `## About Me`, `## Current Projects`, `## Technical Environment`, `## Patterns & Preferences`
- `## Pinned` - move important learnings here to protect them

**Decay-eligible sections** (subject to 30-day archival):
- `## Key Learnings`, `## Error Patterns to Avoid`, `## Best Practices`
- Project sections: `## Data Quirks`, `## Key Decisions`, `## Useful Commands`

**Learning format with date**: `- **Title** [scope/type] (YYYY-MM-DD): Description`

The creation date `(YYYY-MM-DD)` enables age calculation. Learnings without dates are protected from decay (add dates during synthesis to enable decay).

**Archive**: Decayed learnings go to `.decay-archive.md`, retained for 365 days (configurable), then purged.

## Orphan Recovery

Orphan recovery (for transcripts from ungraceful session exits) now runs inline on every SessionStart, replacing the old cron-based approach. This provides:

- **Immediate recovery**: No waiting for hourly cron
- **Cross-platform**: Works on Windows, macOS, Linux (no cron required)
- **Atomic**: Uses directory-based locking to prevent race conditions

The recovery checks for transcript files in `~/.claude/projects/` that are older than 30 minutes and haven't been captured yet.

## Subagent Considerations

The `/synthesize` skill runs via a background subagent (Task tool) to avoid bloating the main conversation context.

### PreToolUse Hook for Auto-Approval

Built-in subagents don't inherit permissions from `settings.json` (GitHub issues #10906, #11934, #18172, #18950). The memory system works around this using a **PreToolUse hook** that auto-approves memory-related operations.

The hook (`~/.claude/hooks/pretooluse-allow-memory.sh`) checks if the operation targets:
- `.claude/memory` paths (Read/Edit/Write/Bash)
- Memory system skills (synthesize, remember, recall, reload, settings)
- Task tool with memory-related prompts
- `indexing.py` script operations

For these operations, it returns `{"permissionDecision": "allow"}` to bypass the permission prompt.

### How It Works

```
Subagent calls Edit tool
    ↓
PreToolUse hook runs (matcher: "*")
    ↓
Hook checks if path contains .claude/memory
    ↓
If yes: returns {"permissionDecision": "allow"} → tool runs without prompt
If no: returns nothing → normal permission flow (existing settings.json permissions apply)
```

### Key Learnings

- **PermissionRequest hooks don't work for subagents** - they have the same inheritance bug
- **PreToolUse hooks DO work** - they run before the tool executes and can override permission decisions
- **Use `"allow"` not `"ask"`** - "ask" forwards to foreground but still prompts; "allow" auto-approves
- **Hook must be in settings.json** - agent override files (`.claude/agents/`) don't reliably load for built-in agents

## Permission Path Formats

Claude Code permission patterns have specific path format requirements ([GitHub #6881](https://github.com/anthropics/claude-code/issues/6881)):

| Format | Interpretation | Example |
|--------|----------------|---------|
| `//path/...` | Absolute filesystem path | `Read(//home/user/.claude/**)` |
| `~/path/...` | Home directory expansion | `Read(~/.claude/**)` |
| `/path/...` | **Relative** from settings file! | ❌ Don't use for absolute paths |

**Note**: This path format knowledge is only relevant for Read permissions now. The memory
system uses a PreToolUse hook for Edit/Write operations, which bypasses the permission
system entirely by returning `{"permissionDecision": "allow"}` before permissions are checked.
The hook does simple string matching for `.claude/memory` in the input JSON, so path format
doesn't matter for those operations.

## Cross-Platform Notes

The Python rewrite ensures cross-platform compatibility:

| Feature | Implementation |
|---------|----------------|
| **Paths** | `pathlib.Path` handles separators automatically |
| **Home dir** | `Path.home()` works on all platforms |
| **File locking** | Directory-based (mkdir is atomic everywhere) |
| **Python detection** | Installer checks `python3` then `python` |
| **Hook commands** | Use absolute paths (no shell expansion needed) |

**Windows notes**:
- Best-effort support; recommend WSL for guaranteed compatibility
- Use `python install.py` (not `python3`)
- Hooks use absolute paths generated at install time
