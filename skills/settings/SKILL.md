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

## Instructions

When the user invokes this skill:

### For `/settings` or `/settings view`:

1. Read `~/.claude/memory/settings.json`
2. Display the settings in a formatted table:

```
## Memory System Settings

| Setting | Value | Description |
|---------|-------|-------------|
| Short-term working days | 7 | Days with activity to load |
| Short-term token limit | 15,000 | Soft limit for daily summaries |
| Project working days | 7 | Project-specific history days |
| Project token limit | 8,000 | Soft limit for project memory |
| Include subdirectories | false | Match subdirs to parent project |
| Long-term token limit | 7,000 | Soft limit for LONG_TERM.md |
| Total token budget | 30,000 | Overall memory budget |

Settings file: `~/.claude/memory/settings.json`
```

### For `/settings usage`:

1. Read the settings file for limits
2. Calculate actual token usage:
   - LONG_TERM.md: `wc -c` / 4
   - Daily files (last N working days): sum of `wc -c` / 4
   - Project files (if applicable): sum of `wc -c` / 4
3. Display usage report:

```
## Memory Token Usage

| Component | Tokens | Limit | Status |
|-----------|--------|-------|--------|
| Long-term | ~4,200 | 7,000 | ✓ |
| Short-term (7 days) | ~12,800 | 15,000 | ✓ |
| Project | ~6,100 | 8,000 | ✓ |
| **Total** | **~23,100** | **30,000** | **✓** |

✓ = Within limit, ⚠ = Over limit (soft warning)
```

Use this bash command to get file sizes:
```bash
# Long-term
wc -c ~/.claude/memory/LONG_TERM.md

# Daily files (most recent N)
ls -1 ~/.claude/memory/daily/*.md | sort -r | head -n 7 | xargs wc -c

# Total daily directory
du -sb ~/.claude/memory/daily/
```

### For `/settings set <path> <value>`:

1. Parse the path (e.g., `shortTermMemory.workingDays`)
2. Validate the value (must be appropriate type)
3. Use Edit tool to update `~/.claude/memory/settings.json`
4. Confirm the change

Valid paths:
- `shortTermMemory.workingDays` (integer, 1-30)
- `shortTermMemory.tokenLimit` (integer, 1000-50000)
- `projectMemory.workingDays` (integer, 1-30)
- `projectMemory.tokenLimit` (integer, 1000-50000)
- `projectMemory.includeSubdirectories` (boolean)
- `longTermMemory.tokenLimit` (integer, 1000-50000)
- `totalTokenBudget` (integer, 10000-100000)

Example:
```
/settings set shortTermMemory.workingDays 10
```

## Token Guidance

| Memory Type | Suggested Range | Notes |
|-------------|-----------------|-------|
| Long-term | 5K-10K | User profile, patterns (~2-4 pages) |
| Short-term | 10K-20K | Recent working days (~1-2 pages/day) |
| Project | 5K-10K | Project-specific context |
| **Total** | **25K-40K** | ~30K is efficient balance |

**Estimation**: 1 token ≈ 4 characters. Divide file bytes by 4.

## Subdirectory Option

When `projectMemory.includeSubdirectories` is `true`:
- Working in `/project/backend/` will load history for `/project/`
- Uses longest path match (most specific project wins)

**Warning**: May load excessive context for repos with many active subdirectories. Use with caution on large monorepos.

## Settings File Location

`~/.claude/memory/settings.json`

If this file doesn't exist, the memory system uses defaults:
- 7 working days (short-term and project)
- 30,000 total token budget
- Subdirectory matching disabled
