# Claude Code Memory System - Development Guide

## Project Purpose

Markdown-based memory persistence for Claude Code. Installs hooks, scripts, and skills that enable context across sessions.

## Repo Structure

```
claude-memory-system/
├── install.py / uninstall.py   # Cross-platform installers
├── scripts/
│   ├── memory_utils.py         # Shared utilities: paths, settings, filtering, locking
│   ├── load_memory.py          # SessionStart hook - loads memory
│   ├── indexing.py             # Session discovery, project index, CLI
│   ├── transcript_ops.py      # Transcript parsing and extraction (split from indexing)
│   ├── decay.py                # Age-based decay for long-term memory
│   ├── project_manager.py      # Project lifecycle management library
│   └── devtools.py             # Repo-local dev diagnostics (not installed)
├── skills/                     # /remember, /synthesize, /recall, /settings, /projects
├── tests/                      # Unit tests
└── templates/                  # Memory file templates + default settings.json
```

**Installs to:**
- `scripts/*.py` → `~/.claude/scripts/`
- `skills/*/` → `~/.claude/skills/`
- `templates/` → `~/.claude/memory/templates/` (always) + `~/.claude/memory/` (if not exists)

## Two-Tier Memory Architecture

**Long-term memory** (curated, persistent):
| Tier | File | Loaded | Content |
|------|------|--------|---------|
| Global | `~/.claude/memory/global-long-term-memory.md` | Every session | User profile, global patterns |
| Project | `~/.claude/memory/project-memory/{project}-long-term-memory.md` | When `$PWD` matches | Project-specific learnings |

**Short-term memory** (recent daily summaries, filtered by scope tags):
| Tier | Source | Days | Filter |
|------|--------|------|--------|
| Global | `~/.claude/memory/daily/*.md` | 2 | `[global/*]` tagged entries only |
| Project | `~/.claude/memory/daily/*.md` | 7 | `[project-name/*]` tagged entries only |

**Learning flow:**
```
Session transcript → /synthesize Phase 1 → Daily summary (Actions, Decisions, Learnings)
                  → /synthesize Phase 2 → Route to long-term memory
```

**Daily file format:**
- `## Actions` - What was done, tagged `[scope/action]`
- `## Decisions` - Choices with rationale, tagged `[scope/decision]`
- `## Learnings` - Patterns/gotchas, format: `- [scope/type] Description`
- `## Lessons` - Actionable takeaways, format: `- [scope/type] Takeaway`

**Long-term file format (decay-eligible sections):**
- `## Key Actions` - Significant actions (from daily Actions)
- `## Key Decisions` - Important choices (from daily Decisions)
- `## Key Learnings` - Patterns/insights (from daily Learnings)
- `## Key Lessons` - Actionable takeaways (from daily Lessons)

**Long-term entry format:** `- (YYYY-MM-DD) [type] Description` (date first, then subtype)

**Action types:** `implement`, `improve`, `document`, `analyze`
**Decision types:** `design`, `tradeoff`, `scope`
**Learning types:** `gotcha`, `pitfall`, `pattern`
**Lesson types:** `insight`, `tip`, `workaround`

**Filtering:** Tags determine which short-term memory tier content appears in:
- `[global/*]` → Global Short-Term Memory (loaded every session)
- `[project-name/*]` → Project Short-Term Memory (loaded when in that project)
- Untagged content is excluded from short-term (only appears in raw daily files)

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

**Rule: Always add or update unit tests when adding new functions or modifying existing function behavior.** Tests live in `tests/test_<module>.py` matching the script they test. Run `python3 -m pytest tests/ -q` before considering any change complete.

Test conventions:
- Class per function/feature: `class TestFunctionName`
- Use `tempfile.TemporaryDirectory` for filesystem isolation
- Use `unittest.mock.patch` to mock path helpers (`get_projects_dir`, etc.)
- Test happy path, edge cases, and error conditions

```bash
python3 -m pytest tests/ -q                  # Run all unit tests (do this first)
python3 -m pytest tests/ -v                  # Verbose output for debugging
python3 install.py                           # Apply changes
python3 ~/.claude/scripts/load_memory.py     # Test memory loading
python3 ~/.claude/scripts/indexing.py list-pending  # Test indexing
python3 ~/.claude/scripts/indexing.py extract 2026-02-06 --output /tmp/test.txt  # Test extract (no marking)
python3 ~/.claude/scripts/indexing.py mark-captured --sidecar /tmp/test.sessions  # Test marking
python3 ~/.claude/scripts/decay.py --dry-run # Test decay
```

## Key Implementation Details

### Hooks (defined in `install.py` `merge_hooks()`)
- `SessionStart` - loads memory context
- `PreToolUse` - auto-approves memory operations (workaround for subagent permission bug)

Note: Transcripts are read directly from Claude Code's storage (`~/.claude/projects/`), not copied via hooks.

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
| `synthesis.model` | sonnet | Model for synthesis subagent |
| `synthesis.background` | true | Run auto-synthesis in background |
| `decay.ageDays` | 30 | Global LTM: archive after N calendar days |
| `decay.projectWorkingDays` | 20 | Project LTM: archive after N project work days |

Short-term token limits calculated as `workingDays × 750` (reduced due to scope filtering).

## Features Summary

| Feature | Implementation |
|---------|----------------|
| Tag-based filtering | Short-term memory filtered by `[scope/*]` tags; global loads `[global/*]`, project loads `[project/*]` |
| Age-based decay | Entries with `(YYYY-MM-DD)` date prefix archived after 30 days; `## Pinned` section protected |
| Safe capture workflow | `extract` is pure read (never marks); `mark-captured --sidecar` skips today's sessions; subagent failure = full retry |
| Session exclusion | `--exclude-session` flag + auto-uncapture on resume prevent active session data loss |
| Direct transcript reading | Reads from `~/.claude/projects/` (source of truth); `.captured` file tracks processed sessions |
| Synthesis scheduling | First session of day + every N hours (default 2); `load_memory.py` parses session_id from stdin |
| Background synthesis | Auto-synthesis runs in background by default (configurable); embedded prompt eliminates SKILL.md read |
| Project detection | Matches `$PWD` to `projects-index.json`; loads project memory + project-tagged entries |
