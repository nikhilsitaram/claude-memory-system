# Synthesis Pipeline Efficiency Fixes

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Reduce synthesis wall-clock time from ~4m17s to ~1m by fixing 3 identified bottlenecks: subagent compliance, Bash output truncation, and post-processing subprocess overhead.

**Architecture:** Three independent fixes to `load_memory.py` (prompt output to file), `synthesis.py` (inline post-processing), and `SKILL.md` (subagent model). Each fix is self-contained with its own tests.

**Tech Stack:** Python 3.9+, pytest, existing memory system scripts

---

### Task 1: Write prompt to temp file instead of stdout

**Problem:** `load_memory.py --synthesis-prompt` prints the full prompt to stdout (~30KB), which hits Bash tool's 30K char truncation limit. This forces an extra Read round-trip in the main context.

**Files:**
- Modify: `scripts/load_memory.py:758-796` (the `--synthesis-prompt` branch)
- Test: `tests/test_load_memory.py`

**Step 1: Write the failing test**

Add a test that verifies `--synthesis-prompt` writes prompt to a temp file and prints only the file path + model to stdout.

```python
class TestSynthesisPromptFileOutput:
    """Test that --synthesis-prompt writes prompt to file, not stdout."""

    def test_writes_prompt_to_temp_file(self, tmp_path, monkeypatch):
        """Prompt content goes to temp file, stdout gets only model + path."""
        # Mock the functions that generate prompt content
        monkeypatch.setattr("load_memory.get_recent_days", lambda **kw: ["2026-02-23"])
        monkeypatch.setattr(
            "load_memory.pre_extract_transcripts_incremental",
            lambda dates, **kw: (
                {"2026-02-23": "/tmp/extract.txt"},
                {"sid1": {"offset": 100, "lines": 10}},
                {"2026-02-23": [{"session_id": "sid1", "project_path": "/test", "messages": ["hi"]}]},
            ),
        )
        monkeypatch.setattr(
            "load_memory._build_embedded_files",
            lambda *a, **kw: {"transcripts": {"2026-02-23": "test"}, "global_ltm": "", "project_ltms": {}},
        )
        monkeypatch.setattr(
            "load_memory._build_synthesis_prompt",
            lambda *a, **kw: "FAKE_PROMPT_CONTENT_HERE",
        )
        monkeypatch.setattr("load_memory.load_settings", lambda: {"synthesis": {"model": "haiku"}})
        # Use tmp_path for temp file output
        monkeypatch.setattr("load_memory.SYNTHESIS_PROMPT_DIR", str(tmp_path))

        import io
        captured = io.StringIO()
        monkeypatch.setattr("sys.stdout", captured)

        # Simulate the --synthesis-prompt path
        from load_memory import write_synthesis_prompt
        write_synthesis_prompt(exclude_session_id=None)

        output = captured.getvalue()
        lines = output.strip().split("\n")

        # First line: model
        assert lines[0] == "model=haiku"
        # Second line: prompt file path
        assert lines[1].startswith("prompt_file=")
        prompt_path = lines[1].split("=", 1)[1]
        # File exists and contains the prompt
        from pathlib import Path
        assert Path(prompt_path).exists()
        assert Path(prompt_path).read_text() == "FAKE_PROMPT_CONTENT_HERE"
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisPromptFileOutput -v`
Expected: FAIL — `write_synthesis_prompt` doesn't exist yet

**Step 3: Implement the change**

In `scripts/load_memory.py`, refactor the `--synthesis-prompt` block (lines 758-796) to:
1. Extract a `write_synthesis_prompt(exclude_session_id)` function
2. Write prompt to `/tmp/synthesis-prompt-{pid}.txt` instead of printing
3. Print only `model={model}` and `prompt_file={path}` to stdout

```python
def write_synthesis_prompt(exclude_session_id: str | None = None) -> None:
    """Generate synthesis prompt and write to temp file.

    Prints to stdout:
        model=<model>
        prompt_file=<path>
    """
    settings = load_settings()
    model = settings.get("synthesis", {}).get("model", "sonnet")

    pending_dates = get_recent_days(exclude_session_id=exclude_session_id)
    if not pending_dates:
        print("No pending transcripts.")
        return

    extracted_files, session_offsets, daily_data = pre_extract_transcripts_incremental(
        pending_dates, exclude_session_id=exclude_session_id
    )

    if not extracted_files:
        print("No pending transcripts with content.")
        return

    include_dailies = bool(session_offsets)
    embedded = _build_embedded_files(
        extracted_files, include_dailies=include_dailies, daily_data=daily_data
    )

    if session_offsets:
        offsets_path = f"/tmp/synthesis-offsets-{os.getpid()}.json"
        Path(offsets_path).write_text(json.dumps(session_offsets), encoding="utf-8")
        embedded["offsets_path"] = offsets_path

    prompt = _build_synthesis_prompt(list(extracted_files.keys()), extracted_files, embedded)

    # Write prompt to temp file instead of stdout (avoids 30K Bash truncation)
    prompt_path = f"/tmp/synthesis-prompt-{os.getpid()}.txt"
    Path(prompt_path).write_text(prompt, encoding="utf-8")

    print(f"model={model}")
    print(f"prompt_file={prompt_path}")
```

Update the `__main__` block to call it:
```python
if len(sys.argv) > 1 and sys.argv[1] == "--synthesis-prompt":
    exclude_id = None
    if len(sys.argv) > 3 and sys.argv[2] == "--exclude-session":
        exclude_id = sys.argv[3]
    write_synthesis_prompt(exclude_session_id=exclude_id)
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisPromptFileOutput -v`
Expected: PASS

**Step 5: Update SKILL.md to parse file path**

In `skills/synthesize/SKILL.md`, update step 1:
```markdown
1. Get the synthesis prompt, model, and pre-extracted data:
   ```bash
   python3 $HOME/.claude/scripts/load_memory.py --synthesis-prompt
   ```
   - If output says "No pending transcripts", inform the user and stop.
   - First line: `model=<model>`. Second line: `prompt_file=<path>`.
   - Read the prompt file to get the subagent prompt.
```

**Step 6: Update auto-synthesis block in main()**

Update the auto-synthesis block (lines 680-695) similarly — write prompt to file, print path in the instruction block. The auto-synthesis instructions to the host agent should tell it to Read the prompt file.

**Step 7: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 8: Commit**

```bash
git add scripts/load_memory.py skills/synthesize/SKILL.md tests/test_load_memory.py
git commit -m "fix: write synthesis prompt to temp file to avoid 30K Bash truncation"
```

---

### Task 2: Replace post-processing subprocesses with function calls

**Problem:** `run_post_processing()` in `synthesis.py` spawns 3 sequential Python subprocesses (`devtools.py mark-routed`, `devtools.py validate-ltm`, `decay.py`). Each has ~50-100ms spawn overhead, totaling 150-300ms. These can be direct function imports.

**Files:**
- Modify: `scripts/synthesis.py:319-366` (`run_post_processing`)
- Modify: `scripts/devtools.py` (export `cmd_mark_routed`, `cmd_validate_ltm` functions or extract logic)
- Modify: `scripts/decay.py` (export a callable function)
- Test: `tests/test_synthesis.py`

**Step 1: Write the failing test**

Add a test that verifies `run_post_processing` does NOT spawn subprocesses.

```python
class TestRunPostProcessingNoSubprocess:
    """Verify post-processing uses function calls, not subprocess.run."""

    def test_no_subprocess_calls(self, tmp_path):
        """run_post_processing should not call subprocess.run."""
        with patch("synthesis.prune_stale_state_entries"), \
             patch("synthesis.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_mark_routed"), \
             patch("synthesis.run_validate_ltm"), \
             patch("synthesis.run_decay"), \
             patch("synthesis.subprocess.run") as mock_sub:
            run_post_processing(extract_paths=[])
        mock_sub.assert_not_called()
```

**Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_synthesis.py::TestRunPostProcessingNoSubprocess -v`
Expected: FAIL — subprocess.run is still called

**Step 3: Extract callable functions from devtools.py and decay.py**

In `scripts/devtools.py`, the `cmd_mark_routed` and `cmd_validate_ltm` functions take `args` (argparse.Namespace). We need thin wrappers that don't require args.

Option A (preferred — minimal change): Create wrapper functions in `synthesis.py` that import and call with dummy args.

Option B: Refactor devtools to separate logic from CLI args.

Go with Option A — add to `synthesis.py`:

```python
def run_mark_routed() -> None:
    """Run mark-routed dedup as function call (no subprocess)."""
    try:
        sys.path.insert(0, str(script_dir))
        from devtools import cmd_mark_routed
        import argparse
        args = argparse.Namespace(dry_run=False)
        cmd_mark_routed(args)
    except Exception:
        pass  # Non-critical

def run_validate_ltm() -> None:
    """Run LTM validation as function call (no subprocess)."""
    try:
        from devtools import cmd_validate_ltm
        import argparse
        args = argparse.Namespace()
        cmd_validate_ltm(args)
    except Exception:
        pass  # Non-critical

def run_decay() -> None:
    """Run decay as function call (no subprocess)."""
    try:
        from decay import main as decay_main
        decay_main()
    except Exception:
        pass  # Non-critical
```

Check that `decay.py` has a callable `main()`. If its `main()` calls `sys.exit()`, we need to handle that.

Then update `run_post_processing` to call these directly:
```python
def run_post_processing(extract_paths, offsets_json=None):
    # ... existing prune + cleanup ...

    # Direct function calls instead of subprocesses
    run_mark_routed()
    run_validate_ltm()
    run_decay()

    # Update timestamp
    ts_file = get_memory_dir() / ".last-synthesis"
    ts_file.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
```

**Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_synthesis.py::TestRunPostProcessingNoSubprocess -v`
Expected: PASS

**Step 5: Update existing tests**

Existing tests mock `synthesis.subprocess.run`. Update them to mock the new function names instead:
- `patch("synthesis.subprocess.run")` → `patch("synthesis.run_mark_routed")`, `patch("synthesis.run_validate_ltm")`, `patch("synthesis.run_decay")`

**Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 7: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "perf: replace post-processing subprocesses with direct function calls"
```

---

### Task 3: Fix subagent model for reliable tool use compliance

**Problem:** The haiku model (used by default for synthesis) doesn't reliably follow the "Use Write and Bash tools" instruction. It returned structured output as plain text instead of writing to a file and running `synthesis.py apply`. This defeats the zero-tool synthesis design and forces manual recovery (2 extra round trips).

**Files:**
- Modify: `templates/settings.json` (change default model)
- Modify: `scripts/load_memory.py` (update default in code)
- Modify: `CLAUDE.md` (update settings reference)
- Test: `tests/test_load_memory.py`

**Step 1: Determine the right fix**

Two options:
- **Option A:** Change default model from `haiku` to `sonnet` — more reliable tool use, higher cost
- **Option B:** Keep haiku but restructure the prompt to improve compliance — uncertain improvement

Go with **Option A** — sonnet is the right tradeoff because:
1. Synthesis runs at most every 2 hours
2. The cost difference per run is small (~$0.01 vs ~$0.003)
3. Reliability eliminates 2 wasted round trips in the main context (which costs more than the model difference)

**Step 2: Write the failing test**

```python
class TestSynthesisModelDefault:
    def test_default_model_is_sonnet(self):
        """Default synthesis model should be sonnet for reliable tool use."""
        from memory_utils import DEFAULT_SETTINGS
        assert DEFAULT_SETTINGS["synthesis"]["model"] == "sonnet"
```

**Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisModelDefault -v`
Expected: FAIL — current default is "haiku"

**Step 4: Update defaults**

In `templates/settings.json`: change `"model": "haiku"` to `"model": "sonnet"`
In `scripts/memory_utils.py`: update `DEFAULT_SETTINGS` dict if model default is hardcoded there
In `scripts/load_memory.py:766`: the fallback `settings.get("synthesis", {}).get("model", "sonnet")` — already says "sonnet" as fallback, but the settings.json template overrides it to "haiku"
In `CLAUDE.md`: update the settings reference table row for `synthesis.model`

**Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_load_memory.py::TestSynthesisModelDefault -v`
Expected: PASS

**Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 7: Commit**

```bash
git add templates/settings.json scripts/memory_utils.py CLAUDE.md tests/test_load_memory.py
git commit -m "fix: change default synthesis model to sonnet for reliable tool use"
```

---

### Task 4: Verify end-to-end and final commit

**Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All pass

**Step 2: Run ruff**

Run: `ruff check scripts/ tests/`
Expected: No errors

**Step 3: Reinstall**

Run: `python3 install.py`

**Step 4: Verify synthesis prompt output**

Run: `python3 ~/.claude/scripts/load_memory.py --synthesis-prompt`
Expected: Two lines only — `model=sonnet` and `prompt_file=/tmp/synthesis-prompt-*.txt`

**Step 5: Final commit if any cleanup needed**

---

## Expected Impact

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| Main-context round trips | 5 (Bash→Read→Task→Write→Bash) | 3 (Bash→Read→Task) | -40% |
| Subagent compliance | Fails on haiku | Reliable on sonnet | Eliminates manual recovery |
| Post-processing overhead | 150-300ms (3 subprocesses) | ~10-30ms (function calls) | -80% |
| Estimated wall time | ~4m17s | ~1m30s | -65% |
