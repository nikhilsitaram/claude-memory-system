# Claude Code Memory System - Development Guide

## Project Purpose

Markdown-based memory persistence for Claude Code. Installs hooks, scripts, and skills that enable context across sessions.

## Repo Structure

```
claude-memory-system/
├── install.py / uninstall.py   # Cross-platform installers
├── scripts/
│   ├── memory_utils.py         # Shared utilities: paths, settings, locking
│   ├── load_memory.py          # SessionStart hook - loads memory + orphan recovery
│   ├── save_session.py         # SessionEnd/PreCompact hook - saves transcript
│   ├── indexing.py             # Transcript extraction + project index building
│   ├── decay.py                # Age-based decay for long-term memory
│   └── project_manager.py      # Project lifecycle management library
├── skills/                     # /remember, /synthesize, /recall, /reload, /settings, /projects
├── tests/                      # Unit tests
└── templates/                  # Memory file templates + default settings.json
```

**Installs to:**
- `scripts/*.py` → `~/.claude/scripts/`
- `skills/*/` → `~/.claude/skills/`
- `templates/` → `~/.claude/memory/templates/` (always) + `~/.claude/memory/` (if not exists)

## Two-Tier Memory Architecture

| Tier | File | Loaded | Content |
|------|------|--------|---------|
| Global | `~/.claude/memory/global-long-term-memory.md` | Every session | User profile, global patterns |
| Project | `~/.claude/memory/project-memory/{project}-long-term-memory.md` | When `$PWD` matches | Project-specific learnings |

**Learning flow:**
```
Session transcript → /synthesize Phase 1 → Daily summary (Actions, Decisions, Learnings)
                  → /synthesize Phase 2 → Route to long-term memory
```

**Daily file format:**
- `## Actions` - What was done, tagged `[scope/action]`
- `## Decisions` - Choices with rationale, tagged `[scope/decision]`
- `## Learnings` - Patterns/gotchas, tagged `[scope/type]` (no date - comes from filename)
  - Line 1: `- **Title** [scope/type]: Description`
  - Line 2: `  - Lesson: Actionable takeaway`

**Long-term file format (decay-eligible sections):**
- `## Key Actions` - Significant actions (from daily Actions)
- `## Key Decisions` - Important choices (from daily Decisions)
- `## Key Learnings` - Patterns/insights (from daily Learnings line 1)
- `## Key Lessons` - Actionable takeaways (from daily Learnings line 2)

**Tag types:** `error`, `best-practice`, `data-quirk`, `decision`, `command`

**Filtering:** Tags determine scope (not content). Untagged content treated as global.

## Making Changes

### Adding a Skill
1. Create `skills/<name>/SKILL.md` with frontmatter
2. Update `install.py`: add to `create_directories()` and `copy_skills()`
3. Update `uninstall.py`: add to cleanup instructions

### Adding a Script
1. Create `scripts/<name>.py`
2. Add to `copy_scripts()` in `install.py`
3. If it needs a hook, add in `merge_hooks()` function

### Testing
```bash
python3 install.py                           # Apply changes
python3 ~/.claude/scripts/load_memory.py     # Test memory loading
python3 ~/.claude/scripts/indexing.py list-pending  # Test indexing
python3 ~/.claude/scripts/decay.py --dry-run # Test decay
```

## Key Implementation Details

### Hooks (defined in `install.py` `merge_hooks()`)
- `SessionStart` - loads memory, runs orphan recovery
- `SessionEnd` / `PreCompact` - saves transcript
- `PreToolUse` - auto-approves memory operations (workaround for subagent permission bug)

### PreToolUse Auto-Approval
Subagents don't inherit permissions (GitHub #10906, #11934, #18172, #18950). The PreToolUse hook returns `{"permissionDecision": "allow"}` for operations targeting `.claude/memory` paths.

### Permission Path Formats
| Format | Meaning |
|--------|---------|
| `~/path` | Home-relative (use this) |
| `//path` | Absolute filesystem path |
| `/path` | ❌ Relative from settings file |

Note: Only matters for Read permissions; Edit/Write bypass via PreToolUse hook.

### Cross-Platform
- Uses `pathlib.Path` for paths, `Path.home()` for home dir
- Directory-based locking (mkdir is atomic everywhere)
- Hook commands use absolute paths generated at install time

## Settings Reference

Use `/settings` skill to view/modify. Key settings in `~/.claude/memory/settings.json`:

| Setting | Default | Notes |
|---------|---------|-------|
| `globalShortTerm.workingDays` | 2 | Days of global daily summaries |
| `projectShortTerm.workingDays` | 7 | Days of project history |
| `*LongTerm.tokenLimit` | 5,000 | Fixed limit per long-term file |
| `synthesis.intervalHours` | 2 | Hours between auto-synthesis |
| `decay.ageDays` | 30 | Archive learnings older than this |

Short-term token limits calculated as `workingDays × 1500`.

## Features Summary

| Feature | Implementation |
|---------|----------------|
| Age-based decay | Learnings with `(YYYY-MM-DD)` date archived after 30 days; `## Pinned` section protected |
| Orphan recovery | Runs on SessionStart, recovers transcripts from ungraceful exits |
| Synthesis scheduling | First session of day + every N hours (default 2) |
| Project detection | Matches `$PWD` to `projects-index.json`; loads project memory + recent project days |
