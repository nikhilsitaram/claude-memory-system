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
| **Total token budget** | **30,000** | **Overall memory budget** |
| Global short-term working days | 2 | Days with activity to load |
| Global short-term token limit | 5,000 | Soft limit for daily summaries |
| Global long-term token limit | 8,000 | Soft limit for global-long-term-memory.md |
| Project short-term working days | 7 | Project-specific history days |
| Project short-term token limit | 10,000 | Soft limit for project daily history |
| Project long-term token limit | 7,000 | Soft limit for project-memory/*.md |
| Include subdirectories | false | Match subdirs to parent project |
| Synthesis interval (hours) | 2 | Hours between auto-synthesis prompts |
| Decay age (days) | 30 | Archive learnings older than this |
| Archive retention (days) | 365 | Purge archived items older than this |

Settings file: `~/.claude/memory/settings.json`

Run `/settings usage` to see current token usage.
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

Run the token usage script:
```bash
python3 $HOME/.claude/scripts/token_usage.py
```

This outputs key=value pairs that can be parsed for the usage report.

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
- `synthesis.intervalHours` (integer, 1-24)
- `decay.ageDays` (integer, 7-365)
- `decay.archiveRetentionDays` (integer, 30-730)
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
- `globalShortTerm.workingDays`: 2
- `globalShortTerm.tokenLimit`: 5000
- `globalLongTerm.tokenLimit`: 8000
- `projectShortTerm.workingDays`: 7
- `projectShortTerm.tokenLimit`: 10000
- `projectLongTerm.tokenLimit`: 7000
- `projectSettings.includeSubdirectories`: false
- `synthesis.intervalHours`: 2
- `decay.ageDays`: 30
- `decay.archiveRetentionDays`: 365
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

## Synthesis Scheduling

The `synthesis.intervalHours` setting controls how often auto-synthesis prompts appear:
- **First session of day (UTC)**: Always prompts for synthesis if transcripts pending
- **Subsequent sessions**: Only prompts if more than N hours since last synthesis
- **Default**: 2 hours

This prevents redundant synthesis prompts when starting multiple short sessions.

## Decay Settings

The decay system automatically archives old learnings to keep long-term memory lean:

- `decay.ageDays` (default: 30): Learnings older than this are archived
- `decay.archiveRetentionDays` (default: 365): Archived items older than this are purged

**What gets decayed:**
- Learnings in decay-eligible sections (Key Learnings, Error Patterns, Best Practices, etc.)
- Only learnings with creation dates older than `ageDays`

**What is protected:**
- Auto-pinned sections: About Me, Current Projects, Technical Environment, Patterns & Preferences
- Custom pinned section: `## Pinned` - move important learnings here to protect them
- Learnings without dates (legacy format) - add dates during synthesis to enable decay

**Archive location**: `~/.claude/memory/.decay-archive.md`

To recover an archived learning, manually copy it back to the appropriate section.

## Settings File Location

`~/.claude/memory/settings.json`

If this file doesn't exist, the memory system uses defaults from `DEFAULT_SETTINGS` in `memory_utils.py`.
