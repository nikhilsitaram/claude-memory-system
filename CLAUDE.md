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
│   └── load-project-memory.py  # Manual project memory loader
├── skills/
│   ├── remember/SKILL.md   # /remember - save notes
│   ├── synthesize/SKILL.md # /synthesize - process transcripts
│   ├── recall/SKILL.md     # /recall - search history
│   ├── reload/SKILL.md     # /reload - synthesize + load after /clear
│   └── settings/SKILL.md   # /settings - view/modify memory config
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
| `templates/global-long-term-memory.md` | `~/.claude/memory/` (if not exists) |
| `templates/settings.json` | `~/.claude/memory/` (if not exists) |

The install script also:
- Creates `~/.claude/memory/{daily,transcripts,project-memory}/` directories
- Adds hooks to `~/.claude/settings.json` (SessionStart, SessionEnd, PreCompact)
- Adds permissions to settings.json:
  - `Read({home}/.claude/**)` - Read memory/skill files (absolute path)
  - `Read(~/.claude/**)` - Read memory/skill files (tilde for subagent bootstrap)
  - `Edit({home}/.claude/memory/**)` - Edit memory files (with variants for subdirs)
  - `Write({home}/.claude/memory/**)` - Write memory files (with variants for subdirs)
  - `Read({home}/.claude/projects/**)` - Read project transcript paths (orphan recovery)

  **Note**: Subagents use Read/Glob/Grep tools for file access, and `indexing.py delete`
  for transcript deletion. No bash permissions needed - fully cross-platform.
- Builds initial project index (`~/.claude/memory/projects-index.json`)
- Auto-migrates `LONG_TERM.md` → `global-long-term-memory.md` if needed
- Removes old bash hooks and cron job (if migrating from bash version)

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
├── global-long-term-memory.md    # Global patterns (always loaded)
├── settings.json
├── projects-index.json
├── daily/
│   └── YYYY-MM-DD.md             # Daily summaries with ## Learnings
├── project-memory/
│   ├── granada-long-term-memory.md
│   ├── cartwheel-long-term-memory.md
│   └── claude-memory-system-long-term-memory.md
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
```

## Key Files Reference

- **~/.claude/settings.json**: User's Claude Code settings (hooks, permissions)
- **~/.claude/memory/settings.json**: Memory system configuration (working days, token limits)
- **global-long-term-memory.md**: Synthesized user profile and global patterns
- **project-memory/{project}-long-term-memory.md**: Project-specific learnings
- **projects-index.json**: Maps project paths to their work days (for project-aware loading)
- **daily/YYYY-MM-DD.md**: Daily session summaries (may include `<!-- projects: ... -->` tags and `## Learnings` section)
- **transcripts/{date}/{session_id}.jsonl**: Raw session data (JSONL format)
- **.captured**: Tracks which session IDs have been saved (prevents duplicates)

## Project-Aware Loading

The `load_memory.py` script loads project-specific memory when:
1. The project index (`projects-index.json`) exists
2. `$PWD` matches a known project path (exact match by default, or prefix match if `includeSubdirectories` is enabled)

This loads:
- Project-specific long-term memory (`project-memory/{project}-long-term-memory.md`)
- Last N "project days" (configurable in settings, default 7), separate from the N working days loaded for all projects

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

## Orphan Recovery

Orphan recovery (for transcripts from ungraceful session exits) now runs inline on every SessionStart, replacing the old cron-based approach. This provides:

- **Immediate recovery**: No waiting for hourly cron
- **Cross-platform**: Works on Windows, macOS, Linux (no cron required)
- **Atomic**: Uses directory-based locking to prevent race conditions

The recovery checks for transcript files in `~/.claude/projects/` that are older than 30 minutes and haven't been captured yet.

## Subagent Considerations

The `/synthesize` skill runs via a background subagent (Task tool) to avoid bloating the main conversation context. Key learnings:

### Permission Patterns
- **Use absolute paths**: Subagents don't expand `~` the same way as the parent session. Use `$HOME` or full paths.
- **Include explicit patterns**: Both `**` (recursive) and `*` (direct) patterns for reliability. Claude Code's glob matching can be strict.
- **Avoid chained commands**: Permissions like `Bash(rm file && rmdir dir)` don't match. Use single commands like `rm -rf dir/`.

### SKILL.md Instructions
When updating skills that run as subagents, be explicit about command formats:
```markdown
# Good - matches permission pattern
Delete transcripts: `rm -rf ~/.claude/memory/transcripts/YYYY-MM-DD/` (pre-approved)

# Bad - chained command won't match permissions
Delete transcripts: `rm file.jsonl && rmdir dir/`
```

### Testing Subagent Permissions
1. Create a test transcript in `~/.claude/memory/transcripts/`
2. Spawn a subagent via Task tool to run /synthesize
3. Watch for permission prompts - if prompted, the pattern doesn't match
4. Adjust permissions or skill instructions accordingly

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
