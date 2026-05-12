---
name: settings
description: Use when user wants to view, modify, or reset memory system configuration, or check token usage across memory tiers
user-invokable: true
---

# Memory Settings Management

View, modify, and analyze the memory system configuration.

## Usage

- `/settings` or `/settings view` — Show current settings
- `/settings usage` — Show token usage breakdown
- `/settings set <path> <value>` — Modify a setting
- `/settings reset [path]` — Reset to defaults (all or specific setting)

## Instructions

### For `/settings` or `/settings view`:

1. Read `~/.claude/memory/settings.json`
2. Display settings in a formatted table showing current values and descriptions

### For `/settings usage`:

Run the token usage script and display results:
```bash
uv run --no-project $HOME/.claude/scripts/token_usage.py
```

Display as a usage table with ~Tokens, Limit, % Used, and status indicator (within limit vs over limit).

### For `/settings set <path> <value>`:

1. Parse the dotted path (e.g., `projectShortTerm.workingDays`)
2. Validate the value against the type constraints below
3. Use Edit tool to update `~/.claude/memory/settings.json`
4. Confirm the change

**Note**: Short-term `tokenLimit` and `totalTokenBudget` are calculated automatically from `workingDays` (formula: `workingDays * 750`).

### For `/settings reset [path]`:

Reset to defaults from `_defaults` section of settings.json. Omit path to reset all.

## Valid Settings

| Path | Type | Range | Default | Notes |
|------|------|-------|---------|-------|
| `mode` | string | full/light | full | light skips project LTM/STM and global STM (recall + global LTM only) |
| `globalLongTerm.tokenLimit` | int | 1000-50000 | 3,000 | Fixed limit |
| `globalShortTerm.workingDays` | int | 1-30 | 2 | Also updates tokenLimit |
| `projectLongTerm.tokenLimit` | int | 1000-50000 | 3,000 | Fixed limit |
| `projectShortTerm.workingDays` | int | 1-30 | 5 | Also updates tokenLimit |
| `synthesis.intervalHours` | float | 0.25-24 | 1 | Hours between auto-synthesis |
| `synthesis.model` | string | haiku/sonnet/opus | sonnet | Model for synthesis subagent |
| `synthesis.minSessionMessages` | int | 0-100 | 5 | Skip sessions with fewer messages during synthesis |
| `decay.ageDays` | int | 7-365 | 30 | Archive learnings older than this |
| `decay.projectWorkingDays` | int | 5-100 | 20 | Archive project entries after N project work days |
| `decay.archiveRetentionDays` | int | 30-730 | 365 | Purge archived items older than this |

## Token Budget

| Component | Default | Formula |
|-----------|---------|---------|
| Global long-term | 3,000 | fixed |
| Global short-term | 1,500 | workingDays * 750 |
| Project long-term | 3,000 | fixed |
| Project short-term | 3,750 | workingDays * 750 |
| **Total** | **11,250** | sum of above |

**Estimation**: 1 token ~ 4 characters. 750 tokens/day based on ~400-600 observed after scope filtering.

## Setting Details

**Mode**: `full` loads all five sections (recall, global LTM, project LTM, project STM, global STM). `light` loads only recall and global LTM — useful when Claude Code's native project-scoped memory covers project context and you want the custom system to handle only cross-project memory plus session continuity.

**Working days**: Both tiers use "working days" — days that have matching tagged content (`[global/*]` or `[project-name/*]`). Days without matching content are skipped.

**Git subdirectories**: Subdirectories of a git repo automatically map to the repo root unless gitignored — no configuration required. Gitignored paths (e.g. `projects/`) stay as separate projects.

**Synthesis**: Runs via launchd/systemd timer (deferred). Manual `/synthesize` always runs in foreground.

**Decay**: Archives entries older than `ageDays` from decay-eligible sections (Key Actions/Decisions/Learnings/Lessons). `## Pinned` section, auto-pinned sections, and entries without dates are protected. Archive at `~/.claude/memory/.decay-archive.md`.

## Settings File

`~/.claude/memory/settings.json` — defaults from `DEFAULT_SETTINGS` in `memory_utils.py` if missing.
