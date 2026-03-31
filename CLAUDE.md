# Claude Code Memory System - Development Guide

SQL-backed knowledge graph memory persistence for Claude Code. See README.md for user-facing documentation.

## Repo Structure

```
claude-memory-system/
├── scripts/
│   ├── install.py              # Cross-platform installer
│   ├── uninstall.py            # Cross-platform uninstaller
│   ├── memory_utils.py         # Shared utilities: paths, settings, filtering, locking
│   ├── load_memory.py          # SessionStart hook - SQL-ranked context loading
│   ├── memory_server.py        # MCP server - search/write/delete/traverse tools
│   ├── web_app.py              # Web frontend - browse/search/edit knowledge graph
│   ├── indexing.py             # Session discovery, project index, CLI
│   ├── transcript_ops.py       # Transcript parsing and extraction
│   ├── decay.py                # Salience-based decay for memory lifecycle
│   ├── synthesis.py            # Synthesis output parser and DB apply pipeline
│   ├── synthesis_cron.py       # Deferred synthesis runner (launchd/systemd)
│   ├── storage.py              # SQLite storage layer (data_points, edges)
│   ├── embeddings.py           # Vector embedding and semantic search
│   ├── health.py               # Memory health diagnostics
│   └── session_import.py       # Cross-machine session import utility
├── skills/                     # /synthesize, /settings, /projects (recall/remember deprecated)
├── templates/web/              # Web frontend HTML
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
2. Update `scripts/install.py`: add to `skills` list in `link_skills()`
3. Update `scripts/uninstall.py`: add to cleanup instructions

### Adding a Script
1. Create `scripts/<name>.py`
2. Add to `link_scripts()` in `scripts/install.py`
3. If it needs a hook, add in `merge_hooks()` function

## Python

**Always use the project venv Python** (`.venv/bin/python3`), never bare `python3`. The venv has sqlite-vec and fastembed which are required by embeddings, consolidation, and the MCP server. Bare `python3` resolves to Homebrew Python which lacks these packages and causes silent failures.

## Testing

**Rule: Always add or update unit tests when adding new functions or modifying existing function behavior.**

Tests live in `tests/test_<module>.py` matching the script they test.

```bash
.venv/bin/python3 -m pytest tests/ -q                  # Run all (do this first)
.venv/bin/python3 -m pytest tests/ -v                  # Verbose for debugging
.venv/bin/python3 scripts/install.py                    # Apply changes
.venv/bin/python3 ~/.claude/scripts/load_memory.py     # Test memory loading
.venv/bin/python3 ~/.claude/scripts/indexing.py list-recent  # Test session listing
.venv/bin/python3 ~/.claude/scripts/decay.py --dry-run # Test decay
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

**Unified `data_points` table** (replaces chunks + nodes):
| Type | Loaded | Decays? |
|------|--------|---------|
| `profile` (scope=user) | Every session, always | No (salience=1.0) |
| `memory` (scope=global) | Every session, ranked | Yes (normal decay) |
| `memory` (scope=project) | When CWD matches | Yes (normal decay) |
| `session_context` | Most recent for project | Yes |
| `entity` | Not directly loaded | Via edges |

**MCP Tools** (Claude calls these automatically via `memory_server.py`):
- `search_memories` — vector + graph hybrid search
- `write_memory` — atomic DB write with embedding + provenance
- `delete_memory` — soft delete with audit trail
- `traverse_graph` — knowledge graph navigation

### Key Pipelines

**Loading** (`load_memory.py`): SQL queries against `data_points` table in priority order: user profile → session continuity → project memories → global knowledge → recent activity. ~6K token budget, ~60ms latency. Access tracking on served data_points.

**Synthesis** (`synthesis.py` + `synthesis_cron.py`): Extract transcripts → vector pre-retrieval → LLM produces `MEMORY_OPS` JSON → `apply_memory_ops_v3` writes to `data_points` + creates provenance edges. No markdown writing.

**Decay** (`decay.py`): Applies salience decay to `data_points` entries older than threshold. `## Pinned` section protected.

## Implementation Details

### Hooks (defined in `scripts/install.py` `merge_hooks()`)
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
| `globalLongTerm.tokenLimit` | 3,000 |
| `projectLongTerm.tokenLimit` | 3,000 |
| `synthesis.intervalHours` | 0.5 |
| `synthesis.model` | sonnet |
| `synthesis.background` | true |
| `synthesis.deferred` | true |
| `synthesis.minSessionMessages` | 10 |
| `synthesis.recentWorkingDays` | 7 |
| `synthesis.backfill.recentWorkingDays` | 7 |
| `decay.ageDays` | 30 |
| `decay.projectWorkingDays` | 20 |

## Available Commands

### Skills (slash commands)
- `/synthesize` — run synthesis manually
- `/settings` — view/modify memory settings
- `/projects` — project status and cleanup
- `/recall` — **deprecated**, use `search_memories` MCP tool
- `/remember` — **deprecated**, use `write_memory` MCP tool

### MCP Tools (auto-registered via `mcpServers.memory` in settings.json)
- `search_memories` — vector + graph hybrid search across knowledge graph
- `write_memory` — atomic write with embedding + provenance tracking
- `delete_memory` — soft delete with audit trail
- `traverse_graph` — navigate entity relationships

### Web UI
```bash
.venv/bin/python3 ~/.claude/scripts/web_app.py   # start at http://localhost:8742
```
