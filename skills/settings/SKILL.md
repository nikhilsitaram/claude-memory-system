---
name: settings
description: View and modify memory system settings, check token usage
user-invocable: true
---

<command-name>settings</command-name>

# Memory Settings Management

View, modify, and analyze the memory system configuration.

## Usage

- `/settings` or `/settings view` - Show current settings
- `/settings usage` - Show token usage breakdown
- `/settings set <path> <value>` - Modify a setting
- `/settings reset [path]` - Reset to defaults (all or specific setting)

## Instructions

When the user invokes this skill:

### For `/settings` or `/settings view`:

1. Read `~/.claude/memory/settings.json`
2. Display the settings in a formatted table:

```
## Memory System Settings

| Setting | Value | Description |
|---------|-------|-------------|
| Global short-term working days | 7 | Days with activity to load |
| Global short-term token limit | 15,000 | Soft limit for daily summaries |
| Global long-term token limit | 7,000 | Soft limit for global-long-term-memory.md |
| Project short-term working days | 7 | Project-specific history days |
| Project short-term token limit | 5,000 | Soft limit for project daily history |
| Project long-term token limit | 3,000 | Soft limit for project-memory/*.md |
| Include subdirectories | false | Match subdirs to parent project |
| Total token budget | 30,000 | Overall memory budget |

Settings file: `~/.claude/memory/settings.json`
```

### For `/settings usage`:

1. Read the settings file for limits
2. Calculate actual token usage:
   - global-long-term-memory.md: file size / 4
   - Daily files (last N working days): sum of file sizes / 4
   - Project long-term memory (if applicable): file size / 4
   - Project daily files (if applicable): sum of file sizes / 4
3. Display usage report:

```
## Memory Token Usage

| Component | Tokens | Limit | Status |
|-----------|--------|-------|--------|
| Global long-term | ~1,850 | 7,000 | ✓ |
| Global short-term (7 days) | ~8,760 | 15,000 | ✓ |
| Project long-term | ~960 | 3,000 | ✓ |
| Project short-term | ~2,100 | 5,000 | ✓ |
| **Total** | **~13,670** | **30,000** | **✓** |

✓ = Within limit, ⚠ = Over limit (soft warning)
```

Use Python for file size calculation:
```python
from pathlib import Path

memory_dir = Path.home() / ".claude" / "memory"

# Global long-term
global_memory = memory_dir / "global-long-term-memory.md"
if global_memory.exists():
    global_long_term_tokens = global_memory.stat().st_size // 4

# Global short-term (daily files, list, sort, take latest N)
daily_dir = memory_dir / "daily"
if daily_dir.exists():
    daily_files = sorted(daily_dir.glob("*.md"), reverse=True)[:7]
    global_short_term_tokens = sum(f.stat().st_size for f in daily_files) // 4

# Project long-term (if in a project)
project_memory_dir = memory_dir / "project-memory"
project_file = project_memory_dir / f"{project_name}-long-term-memory.md"
if project_file.exists():
    project_long_term_tokens = project_file.stat().st_size // 4
```

### For `/settings set <path> <value>`:

1. Parse the path (e.g., `projectShortTerm.workingDays`)
2. Validate the value (must be appropriate type)
3. Use Edit tool to update `~/.claude/memory/settings.json`
4. Confirm the change

Valid paths:
- `globalShortTerm.workingDays` (integer, 1-30)
- `globalShortTerm.tokenLimit` (integer, 1000-50000)
- `globalLongTerm.tokenLimit` (integer, 1000-50000)
- `projectShortTerm.workingDays` (integer, 1-30)
- `projectShortTerm.tokenLimit` (integer, 1000-50000)
- `projectLongTerm.tokenLimit` (integer, 1000-50000)
- `projectSettings.includeSubdirectories` (boolean)
- `totalTokenBudget` (integer, 10000-100000)

Example:
```
/settings set projectLongTerm.tokenLimit 5000
```

### For `/settings reset [path]`:

Reset settings to default values. The defaults are stored in `_defaults` section of settings.json.

- `/settings reset` - Reset ALL settings to defaults
- `/settings reset projectLongTerm.tokenLimit` - Reset specific setting

Default values:
- `globalShortTerm.workingDays`: 7
- `globalShortTerm.tokenLimit`: 15000
- `globalLongTerm.tokenLimit`: 7000
- `projectShortTerm.workingDays`: 7
- `projectShortTerm.tokenLimit`: 5000
- `projectLongTerm.tokenLimit`: 3000
- `projectSettings.includeSubdirectories`: false
- `totalTokenBudget`: 30000

## Token Guidance

| Memory Type | Suggested Range | Notes |
|-------------|-----------------|-------|
| Global long-term | 5K-10K | User profile, patterns (~2-4 pages) |
| Global short-term | 10K-20K | Recent working days (~1-2 pages/day) |
| Project long-term | 2K-5K | Project-specific accumulated learnings |
| Project short-term | 3K-8K | Project-specific recent history |
| **Total** | **25K-40K** | ~30K is efficient balance |

**Estimation**: 1 token ≈ 4 characters. Divide file bytes by 4.

## Working Days Behavior

Both global and project short-term memory use "working days" - days with actual activity:
- **Global short-term**: Scans `daily/*.md` files, loads N most recent
- **Project short-term**: Scans daily files tagged with project, loads N most recent for that project

Days without activity don't count against the limit.

## Subdirectory Option

When `projectSettings.includeSubdirectories` is `true`:
- Working in `/project/backend/` will load history for `/project/`
- Uses longest path match (most specific project wins)

**Warning**: May load excessive context for repos with many active subdirectories. Use with caution on large monorepos.

## Settings File Location

`~/.claude/memory/settings.json`

If this file doesn't exist, the memory system uses defaults from `DEFAULT_SETTINGS` in `memory_utils.py`.
