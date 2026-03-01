# Deferred Synthesis via Systemd Timer + SessionEnd Hook

## Problem

Synthesis currently runs as a subagent inside the active Claude Code session:
- Consumes ~50K tokens of context (prompt + LLM inference)
- Competes with active work for session token budget
- LLM inference takes 10-60 seconds, blocking or degrading the session
- Even with `run_in_background=true`, the subagent occupies a slot

## Solution

Decouple synthesis from active sessions entirely. Run it as a systemd oneshot service triggered by:
1. **SessionEnd hook** -- fires immediately after each session, captures complete transcript
2. **Systemd timer (every 2h)** -- baseline safety net for idle periods

## Architecture

```
SessionStart (load_memory.py):
  |-- Load settings
  |-- Load LTM (global + project)
  |-- Load short-term (daily files)
  |-- Print <memory> block
  |-- NO synthesis logic

SessionEnd hook:
  |-- systemctl --user start --no-block claude-memory-synthesis.service
  |-- Returns instantly (~5ms)

Systemd timer (every 2h baseline):
  |-- Activates the same service unit

claude-memory-synthesis.service (oneshot):
  |-- scripts/synthesis_cron.py
      |-- should_synthesize() check (.last-synthesis timestamp)
      |-- Extract transcripts (fresh offsets, accurate to current state)
      |-- Build prompt via load_memory.py --synthesis-prompt
      |-- CLAUDECODE= claude -p --no-session-persistence \
      |     --model sonnet --allowedTools "Write Bash Read" < prompt
      |-- synthesis.py apply updates state + .last-synthesis
```

## Key Design Decisions

### Extraction happens at synthesis time, not SessionStart

Extracting at SessionStart and deferring synthesis creates a race condition:
multiple sessions between timer runs produce competing offset snapshots.
By extracting at cron time, offsets are always fresh and accurate.
This also means SessionStart gets simpler (remove ~50 lines).

### SessionEnd hook triggers the service, not synthesis directly

`systemctl --user start --no-block` returns in ~5ms. If the service is
already running (from timer or another SessionEnd), systemd queues the
next activation. No coordination logic needed.

### Headless `claude -p` uses subscription credits

No separate API account needed. `claude -p` authenticates via the same
OAuth token as interactive sessions. `--no-session-persistence` prevents
transcript clutter. `CLAUDECODE=` unsets the nesting guard.

### Explicit `--allowedTools` over `--dangerously-skip-permissions`

The synthesis subagent only needs three tools:
- `Read` -- reads the prompt file
- `Write` -- writes structured output to /tmp
- `Bash` -- calls `synthesis.py apply`

Scoping tools explicitly is safer than bypassing all permissions.

## Components

| Component | Change |
|-----------|--------|
| `load_memory.py` | Remove synthesis orchestration (lines 691-739). Remove `pre_extract_transcripts_incremental`, `_build_synthesis_prompt`, `_build_embedded_files`, `_build_preextracted_prompt` and related imports. Remove `should_synthesize()`. Keep `write_synthesis_prompt()` and `--synthesis-prompt` CLI mode. |
| `scripts/synthesis_cron.py` | **New.** Entry point for systemd service. Checks schedule, runs full pipeline. |
| `install.py` | Install systemd timer + service units. Add `synthesis.deferred` setting support. |
| `uninstall.py` | Clean up timer + service units. |
| `settings.json` | New setting: `synthesis.deferred` (default: `false`, opt-in). |
| `templates/settings.json` | Add `synthesis.deferred` default. |

## Systemd Units

### claude-memory-synthesis.service

```ini
[Unit]
Description=Claude Memory Synthesis
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/.claude/scripts/synthesis_cron.py
Environment=CLAUDECODE=
TimeoutStartSec=300
```

### claude-memory-synthesis.timer

```ini
[Unit]
Description=Run Claude Memory Synthesis every 2 hours

[Timer]
OnCalendar=*-*-* 0/2:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

### SessionEnd hook (in install.py merge_hooks)

```json
{
  "hooks": {
    "SessionEnd": [{
      "type": "command",
      "command": "systemctl --user start --no-block claude-memory-synthesis.service"
    }]
  }
}
```

## Settings

| Setting | Default | Notes |
|---------|---------|-------|
| `synthesis.deferred` | `false` | When true: no in-session synthesis, systemd timer + SessionEnd hook active |
| `synthesis.intervalHours` | 2 | Reused for timer schedule and should_synthesize() check |
| `synthesis.model` | `sonnet` | Passed to `claude -p --model` |

## Failure Handling

- If `claude -p` exits non-zero, `.last-synthesis` is NOT updated (written by synthesis.py apply on success only)
- Next timer run or SessionEnd retries automatically
- Systemd logs failures to journal: `journalctl --user -u claude-memory-synthesis`
- `TimeoutStartSec=300` kills stuck synthesis after 5 minutes

## Migration

- Default `synthesis.deferred: false` preserves current behavior
- User opts in via `/settings` or direct edit of settings.json
- `install.py` always installs the systemd units (harmless when deferred=false since synthesis_cron.py checks the setting)
- Alternatively: install.py only installs units when setting is true, requires re-running install.py after changing setting

## Testing

- Unit tests for synthesis_cron.py (mock claude -p, mock should_synthesize)
- Integration test: verify SessionStart no longer prints synthesis banner when deferred=true
- Manual: enable deferred, end a session, check `journalctl --user -u claude-memory-synthesis`
