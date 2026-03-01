# Deferred Synthesis Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Decouple synthesis from active Claude Code sessions by running it as a systemd oneshot service triggered by a SessionEnd hook and a 2h timer.

**Architecture:** SessionStart loads memory only (no synthesis logic). A new `synthesis_cron.py` script handles scheduling, extraction, prompt building, and headless `claude -p` invocation. Systemd timer + SessionEnd hook trigger it automatically.

**Tech Stack:** Python 3.9+, systemd user units, `claude -p` CLI

---

### Task 1: Add `synthesis.deferred` to settings defaults

**Files:**
- Modify: `scripts/memory_utils.py:220-224` (DEFAULT_SETTINGS synthesis block)
- Modify: `templates/settings.json:24-29` (synthesis block)
- Modify: `tests/test_load_memory.py` (any tests referencing synthesis settings)

**Step 1: Write the failing test**

In `tests/test_load_memory.py`, add a test that verifies the `deferred` key exists in loaded settings:

```python
class TestSynthesisDeferredSetting:
    def test_default_deferred_is_false(self, tmp_path):
        """synthesis.deferred defaults to False."""
        settings_file = tmp_path / "settings.json"
        with patch("memory_utils.get_settings_file", return_value=settings_file):
            settings = load_settings()
        assert settings["synthesis"]["deferred"] is False
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisDeferredSetting -v`
Expected: FAIL (KeyError: 'deferred')

**Step 3: Add `deferred` to DEFAULT_SETTINGS**

In `scripts/memory_utils.py` at line ~222, add `"deferred": False` to the `synthesis` dict:

```python
"synthesis": {
    "intervalHours": 2,
    "model": "sonnet",
    "background": True,
    "deferred": False,
},
```

**Step 4: Update templates/settings.json**

Add `"deferred": false` to the synthesis block and update the `_comment` and `_defaults` sections.

**Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisDeferredSetting -v`
Expected: PASS

**Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass (no regressions)

**Step 7: Commit**

```bash
git add scripts/memory_utils.py templates/settings.json tests/test_load_memory.py
git commit -m "feat: add synthesis.deferred setting (default: false)"
```

---

### Task 2: Gate in-session synthesis on `deferred` setting

**Files:**
- Modify: `scripts/load_memory.py:700-750` (main() synthesis block)
- Modify: `tests/test_load_memory.py`

**Step 1: Write the failing test**

Add a test that when `synthesis.deferred` is True, `main()` does NOT print `AUTO-SYNTHESIZE REQUIRED`:

```python
class TestSynthesisDeferred:
    def test_deferred_true_skips_auto_synthesis(self, tmp_path, capsys):
        """When synthesis.deferred=True, main() should not print AUTO-SYNTHESIZE banner."""
        # Set up memory dir with a daily file to trigger pending dates
        # Mock load_settings to return deferred=True
        # Run main()
        # Assert "AUTO-SYNTHESIZE" NOT in captured output
        ...

    def test_deferred_false_preserves_auto_synthesis(self, tmp_path, capsys):
        """When synthesis.deferred=False, existing behavior is preserved."""
        # Same setup but deferred=False
        # Assert "AUTO-SYNTHESIZE" IS in captured output (when there are pending transcripts)
        ...
```

Use the same patterns as existing `TestAutoSynthesize*` tests in the file.

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisDeferred -v`
Expected: FAIL

**Step 3: Implement the gate**

In `scripts/load_memory.py`, wrap the synthesis block (lines ~701-750) with:

```python
    synthesis_deferred = settings.get("synthesis", {}).get("deferred", False)
    if pending_dates and should_synthesize(settings) and not synthesis_deferred:
```

This is a one-line change: add `and not synthesis_deferred` to the existing condition.

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisDeferred -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "feat: gate in-session synthesis on synthesis.deferred setting"
```

---

### Task 3: Create `synthesis_cron.py` core logic

**Files:**
- Create: `scripts/synthesis_cron.py`
- Create: `tests/test_synthesis_cron.py`

This is the main entry point for the systemd service. It needs to:
1. Load settings and check `synthesis.deferred` is True
2. Check `should_synthesize()` (reuse from load_memory.py)
3. Call `write_synthesis_prompt()` to extract + build prompt
4. Invoke `claude -p` with the prompt
5. Handle success/failure logging

**Step 1: Write the failing tests**

Create `tests/test_synthesis_cron.py`:

```python
"""Tests for synthesis_cron.py — systemd-triggered deferred synthesis."""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from synthesis_cron import (
    should_run_deferred_synthesis,
    build_claude_command,
    run_synthesis,
)


class TestShouldRunDeferredSynthesis:
    """Tests for the scheduling check."""

    def test_returns_false_when_not_deferred(self, tmp_path):
        """If synthesis.deferred is False, should not run."""
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"deferred": False, "intervalHours": 2}
        }):
            assert should_run_deferred_synthesis() is False

    def test_returns_false_when_recently_synthesized(self, tmp_path):
        """If .last-synthesis is recent, should not run."""
        last_synth = tmp_path / ".last-synthesis"
        last_synth.write_text(datetime.now(timezone.utc).isoformat())
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"deferred": True, "intervalHours": 2}
        }), patch("synthesis_cron.get_last_synthesis_file", return_value=last_synth):
            assert should_run_deferred_synthesis() is False

    def test_returns_true_when_deferred_and_due(self, tmp_path):
        """If deferred=True and enough time passed, should run."""
        last_synth = tmp_path / ".last-synthesis"
        # Don't create it — never synthesized
        with patch("synthesis_cron.load_settings", return_value={
            "synthesis": {"deferred": True, "intervalHours": 2}
        }), patch("synthesis_cron.get_last_synthesis_file", return_value=last_synth):
            assert should_run_deferred_synthesis() is True


class TestBuildClaudeCommand:
    """Tests for building the claude -p command."""

    def test_includes_allowed_tools(self):
        cmd = build_claude_command(model="sonnet", prompt_file="/tmp/test.txt")
        assert "--allowedTools" in cmd
        assert "Write" in cmd
        assert "Bash" in cmd
        assert "Read" in cmd

    def test_includes_model(self):
        cmd = build_claude_command(model="haiku", prompt_file="/tmp/test.txt")
        assert "--model" in cmd
        assert "haiku" in cmd

    def test_includes_no_session_persistence(self):
        cmd = build_claude_command(model="sonnet", prompt_file="/tmp/test.txt")
        assert "--no-session-persistence" in cmd

    def test_does_not_include_dangerous_skip(self):
        cmd = build_claude_command(model="sonnet", prompt_file="/tmp/test.txt")
        assert "--dangerously-skip-permissions" not in cmd


class TestRunSynthesis:
    """Tests for the full synthesis pipeline."""

    def test_skips_when_no_pending(self, tmp_path, capsys):
        """When write_synthesis_prompt prints 'No pending', should exit cleanly."""
        with patch("synthesis_cron.write_synthesis_prompt") as mock_wsp:
            mock_wsp.return_value = None  # No prompt file returned
            result = run_synthesis()
        assert result == 0
        assert "No pending" in capsys.readouterr().out or result == 0

    def test_calls_claude_p_with_prompt(self, tmp_path):
        """When prompt is generated, should invoke claude -p."""
        prompt_file = tmp_path / "synthesis-prompt.txt"
        prompt_file.write_text("test prompt")

        with patch("synthesis_cron.write_synthesis_prompt") as mock_wsp, \
             patch("synthesis_cron.subprocess.run") as mock_run, \
             patch("synthesis_cron.load_settings", return_value={
                 "synthesis": {"model": "sonnet", "deferred": True, "intervalHours": 2}
             }):
            mock_wsp.return_value = ("sonnet", str(prompt_file))
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = run_synthesis()

        assert result == 0
        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert "claude" in cmd[0] if isinstance(cmd, list) else "claude" in cmd
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis_cron.py -v`
Expected: FAIL (ImportError — module doesn't exist)

**Step 3: Implement `synthesis_cron.py`**

Create `scripts/synthesis_cron.py`:

```python
#!/usr/bin/env python3
"""
Deferred synthesis runner for systemd timer / SessionEnd hook.

Extracts transcripts, builds synthesis prompt, and invokes
headless `claude -p` to run synthesis outside active sessions.

Usage:
    python3 synthesis_cron.py           # Normal run (checks schedule)
    python3 synthesis_cron.py --force   # Skip schedule check
"""

import argparse
import io
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from load_memory import (
    get_last_synthesis_file,
    should_synthesize,
    write_synthesis_prompt,
)
from memory_utils import load_settings


def should_run_deferred_synthesis() -> bool:
    """Check if deferred synthesis should run now."""
    settings = load_settings()
    if not settings.get("synthesis", {}).get("deferred", False):
        return False
    return should_synthesize(settings)


def build_claude_command(model: str, prompt_file: str) -> list[str]:
    """Build the claude -p command with explicit tool permissions."""
    return [
        "claude",
        "-p",
        "--no-session-persistence",
        "--model", model,
        "--allowedTools", "Write", "Bash", "Read",
        f"Read {prompt_file} and follow the instructions in it exactly. Use only Write and Bash tools.",
    ]


def run_synthesis(force: bool = False) -> int:
    """Run the full deferred synthesis pipeline.

    Returns:
        0 on success or nothing to do, 1 on failure.
    """
    if not force and not should_run_deferred_synthesis():
        print("Synthesis not due (deferred=false or recently ran)")
        return 0

    # Capture write_synthesis_prompt output to parse model + prompt_file
    old_stdout = sys.stdout
    sys.stdout = captured = io.StringIO()
    try:
        write_synthesis_prompt()
    finally:
        sys.stdout = old_stdout

    output = captured.getvalue()

    if "No pending" in output:
        print("No pending transcripts")
        return 0

    # Parse model=<model> and prompt_file=<path> from output
    model = "sonnet"
    prompt_file = None
    for line in output.strip().splitlines():
        if line.startswith("model="):
            model = line.split("=", 1)[1]
        elif line.startswith("prompt_file="):
            prompt_file = line.split("=", 1)[1]

    if not prompt_file or not Path(prompt_file).exists():
        print(f"Error: No prompt file generated", file=sys.stderr)
        return 1

    # Write eager timestamp to prevent concurrent runs
    get_last_synthesis_file().write_text(
        __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
        encoding="utf-8",
    )

    # Build and run claude -p
    cmd = build_claude_command(model, prompt_file)
    env = os.environ.copy()
    env["CLAUDECODE"] = ""  # Unset nesting guard

    print(f"Running synthesis with model={model}")
    try:
        result = subprocess.run(
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout
        )
    except subprocess.TimeoutExpired:
        print("Error: Synthesis timed out after 300s", file=sys.stderr)
        return 1

    if result.returncode != 0:
        print(f"Error: claude -p exited {result.returncode}", file=sys.stderr)
        if result.stderr:
            print(result.stderr[:500], file=sys.stderr)
        return 1

    print("Synthesis complete")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Deferred synthesis runner")
    parser.add_argument("--force", action="store_true", help="Skip schedule check")
    args = parser.parse_args()
    return run_synthesis(force=args.force)


if __name__ == "__main__":
    sys.exit(main())
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis_cron.py -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 6: Commit**

```bash
git add scripts/synthesis_cron.py tests/test_synthesis_cron.py
git commit -m "feat: add synthesis_cron.py for systemd-driven deferred synthesis"
```

---

### Task 4: Create systemd unit files

**Files:**
- Create: `systemd/claude-memory-synthesis.service`
- Create: `systemd/claude-memory-synthesis.timer`

**Step 1: Create the service unit**

Create `systemd/claude-memory-synthesis.service`:

```ini
[Unit]
Description=Claude Memory Synthesis
After=network.target

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/.claude/scripts/synthesis_cron.py
Environment=CLAUDECODE=
TimeoutStartSec=300

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=claude-synthesis
```

Note: `%h` expands to the user's home directory in systemd user units.

**Step 2: Create the timer unit**

Create `systemd/claude-memory-synthesis.timer`:

```ini
[Unit]
Description=Run Claude Memory Synthesis periodically

[Timer]
OnCalendar=*-*-* 0/2:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

**Step 3: Commit**

```bash
git add systemd/
git commit -m "feat: add systemd service and timer units for deferred synthesis"
```

---

### Task 5: Install systemd units and SessionEnd hook in `install.py`

**Files:**
- Modify: `install.py` (add `install_systemd_units()`, add SessionEnd hook, update `link_scripts()`)
- Modify: `tests/test_install.py`

**Step 1: Write the failing tests**

Add to `tests/test_install.py`:

```python
class TestInstallSystemdUnits:
    def test_copies_service_file(self, tmp_path):
        """Service unit is installed to ~/.config/systemd/user/."""
        ...

    def test_copies_timer_file(self, tmp_path):
        """Timer unit is installed to ~/.config/systemd/user/."""
        ...

    def test_skips_if_systemd_not_available(self, tmp_path):
        """Gracefully skips if systemctl not found."""
        ...


class TestSessionEndHook:
    def test_session_end_hook_added_to_settings(self, tmp_path):
        """merge_hooks adds SessionEnd hook with systemctl command."""
        settings = {"hooks": {}}
        result = merge_hooks(settings, "python3")
        assert "SessionEnd" in result["hooks"]
        hooks = result["hooks"]["SessionEnd"]
        assert any("systemctl" in h.get("hooks", [{}])[0].get("command", "")
                    for h in hooks if h.get("hooks"))
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_install.py::TestInstallSystemdUnits -v`
Expected: FAIL

**Step 3: Implement**

Add to `install.py`:

1. Add `synthesis_cron.py` to `scripts_to_link` list in `link_scripts()`.

2. Add new function `install_systemd_units()`:

```python
def install_systemd_units(script_dir: Path) -> None:
    """Install systemd user units for deferred synthesis."""
    systemd_user_dir = Path.home() / ".config" / "systemd" / "user"

    # Check if systemctl is available
    try:
        subprocess.run(["systemctl", "--user", "--version"],
                       capture_output=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        print("Note: systemctl not available, skipping systemd unit installation")
        return

    systemd_user_dir.mkdir(parents=True, exist_ok=True)

    units = ["claude-memory-synthesis.service", "claude-memory-synthesis.timer"]
    for unit in units:
        src = script_dir / "systemd" / unit
        if src.exists():
            dest = systemd_user_dir / unit
            shutil.copy2(src, dest)

    # Reload and enable timer
    subprocess.run(["systemctl", "--user", "daemon-reload"],
                   capture_output=True, timeout=10)
    subprocess.run(["systemctl", "--user", "enable", "--now",
                    "claude-memory-synthesis.timer"],
                   capture_output=True, timeout=10)

    print("Installed systemd units (timer enabled)")
```

3. Add SessionEnd hook to `merge_hooks()`:

```python
# In hooks_to_add dict, add:
"SessionEnd": [
    {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "command": "systemctl --user start --no-block claude-memory-synthesis.service",
                "timeout": 5,
            }
        ],
    }
],
```

4. Call `install_systemd_units(script_dir)` in `main()` after `link_scripts()`.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_install.py -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 6: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat: install systemd units and SessionEnd hook for deferred synthesis"
```

---

### Task 6: Update `uninstall.py` to clean up systemd units

**Files:**
- Modify: `uninstall.py`

**Step 1: Implement cleanup**

Add to `uninstall.py`:

1. Add `synthesis_cron.py` to `purge_memory_data()` items list.

2. Add function to stop and remove systemd units:

```python
def remove_systemd_units() -> None:
    """Stop and remove systemd user units for deferred synthesis."""
    try:
        subprocess.run(["systemctl", "--user", "stop", "claude-memory-synthesis.timer"],
                       capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "disable", "claude-memory-synthesis.timer"],
                       capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "stop", "claude-memory-synthesis.service"],
                       capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    systemd_dir = Path.home() / ".config" / "systemd" / "user"
    for unit in ["claude-memory-synthesis.service", "claude-memory-synthesis.timer"]:
        unit_file = systemd_dir / unit
        if unit_file.exists():
            unit_file.unlink()
            print(f"  Removed systemd unit: {unit}")

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
```

3. Add `"synthesis_cron.py"` to the `memory_patterns` list in `remove_hooks()` and add `"SessionEnd"` to the `events_to_clean` loop.

4. Call `remove_systemd_units()` in `main()` before purge check.

**Step 2: Commit**

```bash
git add uninstall.py
git commit -m "feat: uninstall.py cleans up systemd units and SessionEnd hook"
```

---

### Task 7: Update CLAUDE.md documentation

**Files:**
- Modify: `CLAUDE.md`

**Step 1: Update docs**

1. Add `synthesis_cron.py` to the repo structure tree.
2. Add `systemd/` directory to the tree.
3. Update the Hooks section to mention the SessionEnd hook.
4. Add `synthesis.deferred` to the Settings Reference table.
5. Add "Deferred synthesis" row to the Features Summary table.

**Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document deferred synthesis feature"
```

---

### Task 8: Integration test — manual verification

**No code changes. Manual verification steps.**

**Step 1: Install and verify**

```bash
cd /home/nsitaram/claude-memory-system/.worktrees/deferred-synthesis
python3 install.py
```

Verify:
- `~/.config/systemd/user/claude-memory-synthesis.service` exists
- `~/.config/systemd/user/claude-memory-synthesis.timer` exists
- `systemctl --user list-timers` shows the timer
- `~/.claude/scripts/synthesis_cron.py` symlink exists

**Step 2: Test with deferred=false (default)**

Start a Claude Code session. Verify synthesis still triggers as a subagent (existing behavior unchanged).

**Step 3: Enable deferred mode**

Edit `~/.claude/memory/settings.json`: set `synthesis.deferred: true`.

**Step 4: Test SessionStart without synthesis**

Start a new session. Verify:
- Memory loads normally
- No "AUTO-SYNTHESIZE REQUIRED" banner appears
- No subagent spawns

**Step 5: Test manual trigger**

```bash
python3 ~/.claude/scripts/synthesis_cron.py --force
```

Verify synthesis runs headlessly.

**Step 6: Test SessionEnd trigger**

```bash
journalctl --user -u claude-memory-synthesis -f
```

In another terminal, start and immediately exit a Claude Code session. Verify the service fires.

**Step 7: Test timer**

```bash
systemctl --user status claude-memory-synthesis.timer
```

Verify the timer is active with the correct 2h schedule.

---

### Task 9: Run full test suite and finalize

**Step 1: Run all tests**

```bash
python3 -m pytest tests/ -v
```

Expected: All pass, including new tests.

**Step 2: Verify no regressions**

Check that existing synthesis tests still pass (they test the in-session path which should work when `deferred=false`).

**Step 3: Final commit if any loose changes**

```bash
git status
# If any unstaged changes, stage and commit
```
