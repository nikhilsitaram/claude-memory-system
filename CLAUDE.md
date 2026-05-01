# Claude Code Memory System - Development Guide

Markdown-based memory persistence for Claude Code. See README.md for user-facing documentation.

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
│   ├── synthesis.py            # Synthesis output parser, applier, state updater
│   ├── project_manager.py      # Project lifecycle management library
│   ├── devtools.py             # Dev diagnostics + mark-routed dedup migration
│   └── synthesis_cron.py       # Deferred synthesis runner (systemd timer entry point)
├── skills/                     # /remember, /synthesize, /recall, /settings, /projects, /audit, /load-project-memory
├── systemd/                    # Systemd user units for deferred synthesis
├── tests/                      # Unit tests
└── templates/                  # Memory file templates + default settings.json
```

**Installs to:**
- `scripts/*.py` → `~/.claude/scripts/` (symlinked)
- `skills/*/` → `~/.claude/skills/` (symlinked)
- `templates/` → `~/.claude/memory/templates/` (always) + `~/.claude/memory/` (if not exists, copied)

## Making Changes

### Adding a Skill
1. Create `skills/<name>/SKILL.md` with frontmatter
2. Update `install.py`: add to `skills` list in `link_skills()`
3. Update `uninstall.py`: add to cleanup instructions

### Adding a Script
1. Create `scripts/<name>.py`
2. Add to `link_scripts()` in `install.py`
3. If it needs a hook, add in `merge_hooks()` function

## Testing

**Rule: Always add or update unit tests when adding new functions or modifying existing function behavior.**

Tests live in `tests/test_<module>.py` matching the script they test.

```bash
python3 -m pytest tests/ -q                  # Run all (do this first)
python3 -m pytest tests/ -v                  # Verbose for debugging
python3 install.py                           # Apply changes
python3 ~/.claude/scripts/load_memory.py     # Test memory loading
python3 ~/.claude/scripts/indexing.py list-recent  # Test session listing
python3 ~/.claude/scripts/decay.py --dry-run # Test decay
```

**Conventions:**
- Class per function/feature: `class TestFunctionName`
- Use pytest `tmp_path` fixture for filesystem isolation (not `tempfile`)
- Use `unittest.mock.patch` to mock path helpers (`get_projects_dir`, etc.)
- Use `@pytest.mark.parametrize` for input/output variations instead of separate test methods
- Shared factories in `tests/helpers.py`; path setup in `tests/conftest.py`
- Test happy path, edge cases, and error conditions
- Never hardcode configurable values — import constants (`DEFAULT_AGE_DAYS`, `DEFAULT_SETTINGS`, etc.) and derive test data from them (e.g., `timedelta(days=DEFAULT_AGE_DAYS * 2)` not `timedelta(days=60)`)

## Architecture

### Data Model

**Long-term memory** (curated, persistent):
| Tier | File | Loaded |
|------|------|--------|
| Global | `global-long-term-memory.md` | Every session |
| Project | `project-memory/{project}-long-term-memory.md` | When `$PWD` matches |

**Short-term memory** (recent daily summaries):
| Tier | Default Days | Filter |
|------|------|--------|
| Global | 2 | `[global/*]` tagged entries |
| Project | 5 | `[project-name/*]` tagged entries |

### Entry Formats

**Daily files:** `- [scope/type] Description`
**Long-term files:** `- (YYYY-MM-DD) [type] Description`
**Routed entries:** `- [routed][scope/type] Description` (skipped at load time)

| Category | Types |
|----------|-------|
| Actions | `implement`, `improve`, `document`, `analyze` |
| Decisions | `design`, `tradeoff`, `scope` |
| Learnings | `gotcha`, `pitfall`, `pattern` |
| Lessons | `insight`, `tip`, `workaround` |

### Key Pipelines

**Loading** (`load_memory.py`): Reads LTM files + filters daily files by scope tags → assembles full memory to stdout for SessionStart hook injection. Output order: read instruction → timestamp → recall → global LTM → project LTM → project STM → global STM. When output exceeds ~10K chars, Claude Code saves it to a session-specific file and shows the path + 2KB preview; the read instruction in the first 2KB prompts Claude to read the full file.

**Mode**: `mode: "full"` (default) emits all sections. `mode: "light"` emits only recall + global LTM, skipping project LTM/STM and global STM — for users who rely on Claude Code's native project-scoped memory and only want this system for cross-project memory plus last-session continuity.

**On-demand project loading** (`emit_project_memory()` in `load_memory.py`, exposed via `--project-memory [name]` and the `/load-project-memory` skill): Emits project LTM + project STM for the cwd-detected project or an explicit name. Used to recover project context mid-session, primarily under `mode: "light"`. The skill instructs the agent to summarize counts/date ranges and surface relevant entries — not just dump them.

**Recall** (`session_end_recall.py`): SessionEnd hook writes `pending-recall/{session_id}.md` for the next session. Each assistant message is per-message-truncated (head/tail split at `MAX_MESSAGE_LINES = 30`), then the budget is allocated 1/3 to oldest messages (head) and 2/3 to newest (tail). The latest message is force-included even if it alone overruns the tail allocation. When messages are skipped, a `... [N messages omitted] ...` marker is inserted between head and tail.

**Synthesis** (`synthesis.py` + `synthesis_cron.py`): Extract transcripts → inject scopes from CWD metadata → LLM summarizes → programmatic daily merge → LTM routing with keyword-overlap dedup (threshold 0.6) + route cap (5/file) → update `.synthesis-state.json` offsets.

**Decay** (`decay.py`): Scan LTM files for `(YYYY-MM-DD)` dated entries → archive entries older than threshold → purge expired archives. `## Pinned` section protected.

## Implementation Details

### Hooks (defined in `install.py` `merge_hooks()`)
- `SessionStart` — loads memory context via `load_memory.py`
- `PreToolUse` — auto-approves operations targeting `.claude/memory` paths
- `SessionEnd` — triggers deferred synthesis via systemd

Transcripts are read directly from `~/.claude/projects/` (source of truth), not copied via hooks.

### PreToolUse Auto-Approval
Subagents don't inherit permissions (GitHub #10906, #11934, #18172, #18950). The hook returns `{"permissionDecision": "allow"}` for `.claude/memory` path operations.

### Permission Path Formats
| Format | Meaning |
|--------|---------|
| `~/path` | Home-relative (use this) |
| `//path` | Absolute filesystem path |
| `/path` | Relative from settings file (avoid) |

Only matters for Read permissions; Edit/Write bypass via PreToolUse hook.

### Cross-Platform
- `pathlib.Path` for paths, `Path.home()` for home dir
- Directory-based locking (`mkdir` is atomic everywhere)
- Hook commands use absolute paths generated at install time

## Settings Defaults

Source of truth: `DEFAULT_SETTINGS` in `scripts/memory_utils.py`.

| Setting | Default |
|---------|---------|
| `mode` | `full` (alt: `light`) |
| `globalShortTerm.workingDays` | 2 |
| `globalLongTerm.tokenLimit` | 3,000 |
| `projectShortTerm.workingDays` | 5 |
| `projectLongTerm.tokenLimit` | 3,000 |
| `synthesis.intervalHours` | 0.5 |
| `synthesis.model` | sonnet |
| `synthesis.minSessionMessages` | 5 |
| `decay.ageDays` | 30 |
| `decay.projectWorkingDays` | 20 |
| `decay.archiveRetentionDays` | 365 |
| `previousSessionRecall.tokenLimit` | 1000 |

Short-term token limits: `workingDays × 750` (calculated in `_calculate_token_limits()`).
