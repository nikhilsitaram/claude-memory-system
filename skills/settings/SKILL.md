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
| **Total token budget** | **23,500** | **Calculated: sum of 4 components** |
| Global long-term token limit | 5,000 | Fixed - user profile, patterns |
| Global short-term working days | 2 | Days with activity to load |
| Global short-term token limit | 3,000 | Calculated: workingDays × 1,500 |
| Project long-term token limit | 5,000 | Fixed - project-specific learnings |
| Project short-term working days | 7 | Project-specific history days |
| Project short-term token limit | 10,500 | Calculated: workingDays × 1,500 |
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

| Component | ~Tokens | Limit | % Used | Status |
|-----------|---------|-------|--------|--------|
| Global long-term | ~2,360 | 5,000 | 47% | ✓ |
| Global short-term (2 days) | ~2,067 | 3,000 | 69% | ✓ |
| Project long-term | ~2,229 | 5,000 | 45% | ✓ |
| Project short-term (7 days) | ~4,205 | 10,500 | 40% | ✓ |
| **Total** | **~10,861** | **23,500** | **46%** | **✓** |

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
- `globalShortTerm.workingDays` (integer, 1-30) - also updates tokenLimit automatically
- `globalLongTerm.tokenLimit` (integer, 1000-50000)
- `projectShortTerm.workingDays` (integer, 1-30) - also updates tokenLimit automatically
- `projectLongTerm.tokenLimit` (integer, 1000-50000)
- `projectSettings.includeSubdirectories` (boolean)
- `synthesis.intervalHours` (integer, 1-24)
- `decay.ageDays` (integer, 7-365)
- `decay.archiveRetentionDays` (integer, 30-730)

**Note**: Short-term tokenLimits and totalTokenBudget are calculated automatically from workingDays.

Example:
```
/settings set projectLongTerm.tokenLimit 5000
```

### For `/settings reset [path]`:

Reset settings to default values. The defaults are stored in `_defaults` section of settings.json.

- `/settings reset` - Reset ALL settings to defaults
- `/settings reset projectLongTerm.tokenLimit` - Reset specific setting

Default values:
- `globalLongTerm.tokenLimit`: 5000 (fixed)
- `globalShortTerm.workingDays`: 2
- `projectLongTerm.tokenLimit`: 5000 (fixed)
- `projectShortTerm.workingDays`: 7
- `projectSettings.includeSubdirectories`: false
- `synthesis.intervalHours`: 2
- `decay.ageDays`: 30
- `decay.archiveRetentionDays`: 365

**Calculated values** (from `memory_utils.py`):
- Short-term tokenLimits = workingDays × 1500
- totalTokenBudget = sum of all 4 limits

## Token Guidance

| Memory Type | Default | Formula | Notes |
|-------------|---------|---------|-------|
| Global long-term | 5,000 | fixed | User profile, patterns, key learnings |
| Global short-term | 3,000 | workingDays × 1,500 | Recent daily summaries |
| Project long-term | 5,000 | fixed | Project-specific accumulated learnings |
| Project short-term | 10,500 | workingDays × 1,500 | Project-specific recent history |
| **Total** | **23,500** | sum of above | Keeps context efficient |

**Estimation**: 1 token ≈ 4 characters. Divide file bytes by 4.

**Formula basis**: 1,500 tokens/day provides headroom over observed max (~1,200 tokens/day).

**Changing limits**: Adjust `workingDays` - tokenLimits are calculated automatically by `memory_utils.py`.

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
- Entries in decay-eligible sections (Key Actions, Key Decisions, Key Learnings, Key Lessons)
- Only entries with creation dates older than `ageDays`

**What is protected:**
- Auto-pinned sections: About Me, Current Projects, Technical Environment, Patterns & Preferences
- Custom pinned section: `## Pinned` - move important learnings here to protect them
- Entries without dates are protected from decay

**Archive location**: `~/.claude/memory/.decay-archive.md`

To recover an archived learning, manually copy it back to the appropriate section.

## Settings File Location

`~/.claude/memory/settings.json`

If this file doesn't exist, the memory system uses defaults from `DEFAULT_SETTINGS` in `memory_utils.py`.
