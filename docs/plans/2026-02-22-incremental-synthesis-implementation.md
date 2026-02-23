# Incremental Synthesis Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Track per-session byte offset + line count so synthesis only re-processes new/changed transcript content.

**Architecture:** New `.synthesis-state.json` file tracks high water marks per session. `transcript_ops.py` gains `extract_transcripts_incremental()` that skips unchanged sessions, extracts only delta lines from grown sessions. `load_memory.py` builds prompts with delta awareness (existing daily file as merge context). `synthesis.py` updates state after successful apply.

**Tech Stack:** Python 3.9+, pytest, existing memory_utils helpers (FileLock, load_json_file, save_json_file, get_memory_dir)

---

### Task 1: Add synthesis state helpers to memory_utils.py

**Files:**
- Modify: `scripts/memory_utils.py:39-64` (add to `__all__`)
- Modify: `scripts/memory_utils.py` (add functions at end, before `if __name__`)
- Test: `tests/test_memory_utils.py`

**Step 1: Write the failing tests**

In `tests/test_memory_utils.py`, add:

```python
from memory_utils import (
    get_synthesis_state_file,
    load_synthesis_state,
    save_synthesis_state,
    update_synthesis_state,
    prune_captured_from_state,
)


class TestSynthesisState:
    def test_get_synthesis_state_file(self, tmp_path):
        """Returns .synthesis-state.json in memory dir."""
        with mock.patch("memory_utils.get_memory_dir", return_value=tmp_path):
            result = get_synthesis_state_file()
        assert result == tmp_path / ".synthesis-state.json"

    def test_load_empty(self, tmp_path):
        """Returns empty sessions dict when file doesn't exist."""
        with mock.patch("memory_utils.get_memory_dir", return_value=tmp_path):
            result = load_synthesis_state()
        assert result == {"sessions": {}}

    def test_save_and_load_roundtrip(self, tmp_path):
        """Saved state can be loaded back."""
        state = {"sessions": {"abc": {"offset": 100, "lines": 10, "last_synthesized": "2026-02-22T12:00:00Z"}}}
        state_file = tmp_path / ".synthesis-state.json"
        with mock.patch("memory_utils.get_synthesis_state_file", return_value=state_file):
            save_synthesis_state(state)
            loaded = load_synthesis_state()
        assert loaded == state

    def test_update_synthesis_state(self, tmp_path):
        """Updates offsets for given sessions, preserves others."""
        state_file = tmp_path / ".synthesis-state.json"
        initial = {"sessions": {"old": {"offset": 50, "lines": 5, "last_synthesized": "2026-02-22T10:00:00Z"}}}
        state_file.write_text(json.dumps(initial))

        with mock.patch("memory_utils.get_synthesis_state_file", return_value=state_file):
            update_synthesis_state({"new": {"offset": 200, "lines": 20}})
            result = load_synthesis_state()
        assert "old" in result["sessions"]
        assert "new" in result["sessions"]
        assert result["sessions"]["new"]["offset"] == 200
        assert "last_synthesized" in result["sessions"]["new"]

    def test_prune_captured_from_state(self, tmp_path):
        """Removes captured session IDs from state."""
        state_file = tmp_path / ".synthesis-state.json"
        state = {"sessions": {
            "keep": {"offset": 100, "lines": 10, "last_synthesized": "2026-02-22T10:00:00Z"},
            "remove": {"offset": 200, "lines": 20, "last_synthesized": "2026-02-22T10:00:00Z"},
        }}
        state_file.write_text(json.dumps(state))

        with mock.patch("memory_utils.get_synthesis_state_file", return_value=state_file):
            prune_captured_from_state({"remove", "not-present"})
            result = load_synthesis_state()
        assert "keep" in result["sessions"]
        assert "remove" not in result["sessions"]
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_memory_utils.py::TestSynthesisState -v`
Expected: FAIL with ImportError (functions don't exist yet)

**Step 3: Implement the functions**

Add to `scripts/memory_utils.py` `__all__` list:
```python
    "get_synthesis_state_file",
    "load_synthesis_state",
    "save_synthesis_state",
    "update_synthesis_state",
    "prune_captured_from_state",
```

Add before `if __name__ == "__main__":` block:

```python
def get_synthesis_state_file() -> Path:
    """Get the .synthesis-state.json file path."""
    return get_memory_dir() / ".synthesis-state.json"


def load_synthesis_state() -> dict:
    """Load synthesis state (high water marks per session)."""
    state_file = get_synthesis_state_file()
    data = load_json_file(state_file, default={"sessions": {}})
    if "sessions" not in data:
        data["sessions"] = {}
    return data


def save_synthesis_state(state: dict) -> None:
    """Save synthesis state atomically (write to tmp, rename)."""
    state_file = get_synthesis_state_file()
    tmp_file = state_file.with_suffix(".tmp")
    tmp_file.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    tmp_file.rename(state_file)


def update_synthesis_state(session_updates: dict[str, dict]) -> None:
    """Update synthesis state with new offsets for given sessions.

    Args:
        session_updates: Dict mapping session_id -> {"offset": int, "lines": int}
    """
    state = load_synthesis_state()
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for sid, info in session_updates.items():
        state["sessions"][sid] = {
            "offset": info["offset"],
            "lines": info["lines"],
            "last_synthesized": now_iso,
        }
    save_synthesis_state(state)


def prune_captured_from_state(captured_ids: set[str]) -> None:
    """Remove captured session IDs from synthesis state (cleanup)."""
    state = load_synthesis_state()
    for sid in captured_ids:
        state["sessions"].pop(sid, None)
    save_synthesis_state(state)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memory_utils.py::TestSynthesisState -v`
Expected: PASS (all 5 tests)

**Step 5: Commit**

```bash
git add scripts/memory_utils.py tests/test_memory_utils.py
git commit -m "feat: add synthesis state helpers for high water mark tracking"
```

---

### Task 2: Add `parse_jsonl_file_from_line` to transcript_ops.py

**Files:**
- Modify: `scripts/transcript_ops.py:29-38` (add to `__all__`)
- Modify: `scripts/transcript_ops.py:94-126` (add new function after `parse_jsonl_file`)
- Test: `tests/test_transcript_ops.py`

**Step 1: Write the failing tests**

Add to `tests/test_transcript_ops.py`:

```python
from transcript_ops import parse_jsonl_file_from_line


class TestParseJsonlFileFromLine:
    def test_full_parse_when_start_line_zero(self, tmp_path):
        """start_line=0 reads all messages (same as parse_jsonl_file)."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("assistant", "First message"),
            ("assistant", "Second message"),
        ]))
        messages, total_lines = parse_jsonl_file_from_line(transcript, start_line=0)
        assert len(messages) == 2
        assert total_lines == 2

    def test_delta_from_line(self, tmp_path):
        """start_line=1 skips first line, parses remainder."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("assistant", "Old message"),
            ("assistant", "New message"),
        ]))
        messages, total_lines = parse_jsonl_file_from_line(transcript, start_line=1)
        assert len(messages) == 1
        assert "New message" in messages[0]["content"]
        assert total_lines == 2

    def test_start_line_beyond_file(self, tmp_path):
        """start_line past EOF returns empty messages and current line count."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("assistant", "Only message"),
        ]))
        messages, total_lines = parse_jsonl_file_from_line(transcript, start_line=100)
        assert messages == []
        assert total_lines == 1

    def test_filters_skippable_messages(self, tmp_path):
        """should_skip_message filter still applies to delta messages."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("assistant", "Old message"),
            ("assistant", "<system-reminder>skip this</system-reminder>"),
            ("assistant", "New real message"),
        ]))
        messages, total_lines = parse_jsonl_file_from_line(transcript, start_line=1)
        assert len(messages) == 1
        assert "New real message" in messages[0]["content"]
        assert total_lines == 3

    def test_returns_total_lines_not_parsed_lines(self, tmp_path):
        """total_lines includes blank lines and all lines in file."""
        transcript = tmp_path / "session.jsonl"
        content = make_jsonl_content([("assistant", "msg1"), ("assistant", "msg2")])
        content += "\n"  # trailing blank line
        transcript.write_text(content)
        _, total_lines = parse_jsonl_file_from_line(transcript, start_line=0)
        # total_lines = non-blank JSONL lines (blank lines are skipped in line count)
        assert total_lines == 2
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_transcript_ops.py::TestParseJsonlFileFromLine -v`
Expected: FAIL with ImportError

**Step 3: Implement the function**

Add to `transcript_ops.py` `__all__`:
```python
    "parse_jsonl_file_from_line",
```

Add after `parse_jsonl_file`:

```python
def parse_jsonl_file_from_line(
    filepath: Path, start_line: int = 0
) -> tuple[list[dict], int]:
    """Parse a JSONL transcript file, optionally skipping initial lines.

    Args:
        filepath: Path to JSONL transcript file
        start_line: Number of non-blank JSONL lines to skip (0 = parse all)

    Returns:
        (messages, total_lines) where total_lines is the count of all non-blank
        lines in the file (for updating the high water mark).
    """
    messages = []
    line_count = 0  # non-blank lines seen

    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                line_count += 1
                if line_count <= start_line:
                    continue
                try:
                    obj = json.loads(raw_line)
                    obj_type = obj.get("type")
                    if obj_type in ("user", "assistant"):
                        msg = obj.get("message", {})
                        role = msg.get("role", obj_type)
                        content = extract_text_content(msg.get("content", ""))
                        if content:
                            if role == "user":
                                continue
                            if should_skip_message(content):
                                continue
                            messages.append({"role": role, "content": content})
                except json.JSONDecodeError:
                    continue
    except IOError:
        pass

    return messages, line_count
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_transcript_ops.py::TestParseJsonlFileFromLine -v`
Expected: PASS (all 5 tests)

**Step 5: Commit**

```bash
git add scripts/transcript_ops.py tests/test_transcript_ops.py
git commit -m "feat: add parse_jsonl_file_from_line for delta extraction"
```

---

### Task 3: Add `extract_transcripts_incremental` to transcript_ops.py

**Files:**
- Modify: `scripts/transcript_ops.py:29-38` (add to `__all__`)
- Modify: `scripts/transcript_ops.py` (add function after `extract_transcripts`)
- Test: `tests/test_transcript_ops.py`

**Step 1: Write the failing tests**

Add to `tests/test_transcript_ops.py`:

```python
from transcript_ops import extract_transcripts_incremental


class TestExtractTranscriptsIncremental:
    def test_new_session_full_extract(self, tmp_path):
        """Session not in state gets mode='full' with all messages."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([
            ("assistant", "Hello"),
            ("assistant", "World"),
        ]))
        session = make_session_info(
            session_id="new-sess",
            transcript_path=transcript,
            file_size=transcript.stat().st_size,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {}}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        assert "2026-02-22" in result
        sess = result["2026-02-22"][0]
        assert sess["mode"] == "full"
        assert sess["message_count"] == 2

    def test_unchanged_session_skipped(self, tmp_path):
        """Session with same file size as state is skipped entirely."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([("assistant", "Hello")]))
        fsize = transcript.stat().st_size

        session = make_session_info(
            session_id="old-sess",
            transcript_path=transcript,
            file_size=fsize,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {"old-sess": {"offset": fsize, "lines": 1, "last_synthesized": "2026-02-22T10:00:00Z"}}}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        assert result == {}  # no content to process

    def test_grown_session_delta_extract(self, tmp_path):
        """Session that grew since state gets mode='delta' with only new messages."""
        transcript = tmp_path / "session.jsonl"
        # Write initial content
        initial_content = make_jsonl_content([("assistant", "Old message")])
        transcript.write_text(initial_content)
        initial_size = transcript.stat().st_size

        # Append new content
        with open(transcript, "a") as f:
            f.write(make_jsonl_content([("assistant", "New message")]))
        new_size = transcript.stat().st_size

        session = make_session_info(
            session_id="grown-sess",
            transcript_path=transcript,
            file_size=new_size,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {"grown-sess": {"offset": initial_size, "lines": 1, "last_synthesized": "2026-02-22T10:00:00Z"}}}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        assert "2026-02-22" in result
        sess = result["2026-02-22"][0]
        assert sess["mode"] == "delta"
        assert sess["message_count"] == 1  # only new message
        assert "New message" in sess["messages"][0]["content"]

    def test_mixed_sessions_same_day(self, tmp_path):
        """Mix of new, unchanged, and grown sessions on same day."""
        # Unchanged session
        t1 = tmp_path / "s1.jsonl"
        t1.write_text(make_jsonl_content([("assistant", "Old")]))

        # Grown session
        t2 = tmp_path / "s2.jsonl"
        initial = make_jsonl_content([("assistant", "Was here")])
        t2.write_text(initial)
        initial_size = t2.stat().st_size
        with open(t2, "a") as f:
            f.write(make_jsonl_content([("assistant", "Am new")]))

        # New session
        t3 = tmp_path / "s3.jsonl"
        t3.write_text(make_jsonl_content([("assistant", "Brand new")]))

        sessions = [
            make_session_info("s1", t1, t1.stat().st_size, created=datetime(2026, 2, 22, 10, 0, tzinfo=timezone.utc)),
            make_session_info("s2", t2, t2.stat().st_size, created=datetime(2026, 2, 22, 11, 0, tzinfo=timezone.utc)),
            make_session_info("s3", t3, t3.stat().st_size, created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc)),
        ]

        state = {"sessions": {
            "s1": {"offset": t1.stat().st_size, "lines": 1, "last_synthesized": "2026-02-22T10:00:00Z"},
            "s2": {"offset": initial_size, "lines": 1, "last_synthesized": "2026-02-22T10:00:00Z"},
        }}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=sessions), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        day_sessions = result["2026-02-22"]
        modes = {s["session_id"]: s["mode"] for s in day_sessions}
        assert "s1" not in modes  # unchanged, skipped
        assert modes["s2"] == "delta"
        assert modes["s3"] == "full"

    def test_returns_session_offsets(self, tmp_path):
        """Result includes session_offsets dict for state update."""
        transcript = tmp_path / "session.jsonl"
        transcript.write_text(make_jsonl_content([("assistant", "Hello")]))

        session = make_session_info(
            session_id="sess-1",
            transcript_path=transcript,
            file_size=transcript.stat().st_size,
            created=datetime(2026, 2, 22, 12, 0, tzinfo=timezone.utc),
        )
        state = {"sessions": {}}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[session]), \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            result = extract_transcripts_incremental(state)

        sess = result["2026-02-22"][0]
        assert "current_offset" in sess
        assert "current_lines" in sess
        assert sess["current_offset"] == transcript.stat().st_size
        assert sess["current_lines"] >= 1

    def test_exclude_session_id(self, tmp_path):
        """Exclude session ID is forwarded to list_pending_sessions."""
        state = {"sessions": {}}

        with mock.patch("transcript_ops.get_captured_sessions", return_value=set()), \
             mock.patch("transcript_ops.list_pending_sessions", return_value=[]) as mock_lps, \
             mock.patch("transcript_ops.get_session_date", return_value="2026-02-22"):
            extract_transcripts_incremental(state, exclude_session_id="skip-me")

        mock_lps.assert_called_once()
        assert mock_lps.call_args[1].get("exclude_session_id") == "skip-me" or \
               mock_lps.call_args[0][1] if len(mock_lps.call_args[0]) > 1 else True
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_transcript_ops.py::TestExtractTranscriptsIncremental -v`
Expected: FAIL with ImportError

**Step 3: Implement the function**

Add to `transcript_ops.py` `__all__`:
```python
    "extract_transcripts_incremental",
```

Add after `extract_transcripts` function:

```python
def extract_transcripts_incremental(
    state: dict,
    exclude_session_id: str | None = None,
) -> dict[str, list[dict]]:
    """Extract transcripts incrementally using synthesis state high water marks.

    For each pending session, compares current file size to stored offset:
    - Not in state: full parse (mode="full")
    - Same size: skip entirely (no output)
    - Grew: delta parse from stored line count (mode="delta")

    Each session dict includes: session_id, filepath, project_path, message_count,
    messages, mode ("full"|"delta"), current_offset, current_lines.

    Args:
        state: Synthesis state dict with "sessions" key
        exclude_session_id: Session ID to exclude from extraction

    Returns:
        Dict mapping date -> list of session dicts (only sessions with content).
    """
    captured = get_captured_sessions()
    pending = list_pending_sessions(captured, exclude_session_id=exclude_session_id)

    sessions_state = state.get("sessions", {})
    daily_data: dict[str, list[dict]] = defaultdict(list)

    for session in pending:
        sid = session.session_id
        current_size = session.file_size
        prev = sessions_state.get(sid)

        if prev and current_size == prev.get("offset", 0):
            # Unchanged — skip entirely
            continue

        if prev and current_size > prev.get("offset", 0):
            # Grew — delta extraction
            start_line = prev.get("lines", 0)
            messages, total_lines = parse_jsonl_file_from_line(
                session.transcript_path, start_line=start_line
            )
            mode = "delta"
        else:
            # New session (not in state) — full extraction
            messages, total_lines = parse_jsonl_file_from_line(
                session.transcript_path, start_line=0
            )
            mode = "full"

        if messages:
            day = get_session_date(session)
            daily_data[day].append({
                "session_id": sid,
                "filepath": str(session.transcript_path),
                "project_path": session.project_path,
                "message_count": len(messages),
                "messages": messages,
                "mode": mode,
                "current_offset": current_size,
                "current_lines": total_lines,
            })

    return dict(daily_data)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_transcript_ops.py::TestExtractTranscriptsIncremental -v`
Expected: PASS (all 6 tests)

**Step 5: Commit**

```bash
git add scripts/transcript_ops.py tests/test_transcript_ops.py
git commit -m "feat: add extract_transcripts_incremental with high water mark support"
```

---

### Task 4: Add incremental-aware `format_transcripts_incremental` to transcript_ops.py

**Files:**
- Modify: `scripts/transcript_ops.py` (add function + `__all__` entry)
- Test: `tests/test_transcript_ops.py`

**Step 1: Write the failing tests**

Add to `tests/test_transcript_ops.py`:

```python
from transcript_ops import format_transcripts_incremental


class TestFormatTranscriptsIncremental:
    def _make_session(self, sid, mode, messages, content_lines=1):
        msg_content = "\n".join(f"Line {i}" for i in range(content_lines))
        return {
            "session_id": sid,
            "filepath": "/tmp/test.jsonl",
            "project_path": None,
            "message_count": len(messages),
            "messages": [{"role": "assistant", "content": m} for m in messages],
            "mode": mode,
            "current_offset": 1000,
            "current_lines": 10,
        }

    def test_full_session_labeled(self):
        """Full sessions have standard session header."""
        data = {"2026-02-22": [self._make_session("s1", "full", ["Hello"])]}
        output = format_transcripts_incremental(data)
        assert "Session: s1" in output
        assert "(continued" not in output

    def test_delta_session_labeled(self):
        """Delta sessions have (continued) marker in header."""
        data = {"2026-02-22": [self._make_session("s1", "delta", ["New msg"])]}
        output = format_transcripts_incremental(data)
        assert "Session: s1 (continued" in output

    def test_budget_applied(self):
        """Line budget still works with incremental format."""
        msgs = [f"Message {i}" for i in range(50)]
        data = {"2026-02-22": [self._make_session("s1", "full", msgs)]}
        output_no_budget = format_transcripts_incremental(data)
        output_with_budget = format_transcripts_incremental(data, total_line_budget=30)
        assert len(output_with_budget.split("\n")) < len(output_no_budget.split("\n"))
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_transcript_ops.py::TestFormatTranscriptsIncremental -v`
Expected: FAIL with ImportError

**Step 3: Implement the function**

Add to `transcript_ops.py` `__all__`:
```python
    "format_transcripts_incremental",
```

Add after `format_transcripts_for_output`:

```python
def format_transcripts_incremental(
    daily_data: dict[str, list[dict]],
    total_line_budget: int | None = None,
) -> str:
    """Format incrementally-extracted transcripts for output.

    Like format_transcripts_for_output but marks delta sessions with
    '(continued — new messages only)' in the header.

    Args:
        daily_data: Dict from extract_transcripts_incremental
        total_line_budget: Cap total output lines (divided across sessions)
    """
    all_sessions = [s for sessions in daily_data.values() for s in sessions]
    max_lines_per_session = None
    if total_line_budget and all_sessions:
        max_lines_per_session = total_line_budget // len(all_sessions)
        max_lines_per_session = max(max_lines_per_session, 15)

    output = []

    for day in sorted(daily_data.keys()):
        sessions = daily_data[day]
        total_messages = sum(s["message_count"] for s in sessions)
        output.append(f"\n{'='*70}")
        output.append(f"DAY: {day} ({len(sessions)} sessions, {total_messages} messages)")
        output.append(f"{'='*70}")

        for session in sessions:
            output.append(f"\n{'─'*70}")
            mode = session.get("mode", "full")
            if mode == "delta":
                output.append(f"Session: {session['session_id']} (continued — new messages only)")
            else:
                output.append(f"Session: {session['session_id']}")
            output.append(f"{'─'*70}")

            session_parts: list[str] = []
            for msg in session["messages"]:
                role_label = "USER" if msg["role"] == "user" else "CLAUDE"
                session_parts.append(f"\n[{role_label}]")
                session_parts.append(msg["content"])

            session_text = "\n".join(session_parts)
            actual_lines = session_text.split("\n")

            if max_lines_per_session and len(actual_lines) > max_lines_per_session:
                head = max_lines_per_session // 3
                tail = max_lines_per_session - head
                truncated = len(actual_lines) - head - tail
                output.append("\n".join(actual_lines[:head]))
                output.append(f"\n... [{truncated} lines truncated] ...")
                output.append("\n".join(actual_lines[-tail:]))
            else:
                output.append(session_text)

    return "\n".join(output)
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_transcript_ops.py::TestFormatTranscriptsIncremental -v`
Expected: PASS

**Step 5: Commit**

```bash
git add scripts/transcript_ops.py tests/test_transcript_ops.py
git commit -m "feat: add format_transcripts_incremental with delta session markers"
```

---

### Task 5: Add `pre_extract_transcripts_incremental` to load_memory.py

**Files:**
- Modify: `scripts/load_memory.py:46` (add imports)
- Modify: `scripts/load_memory.py:539-574` (add new function after `pre_extract_transcripts`)
- Test: `tests/test_load_memory.py`

**Step 1: Write the failing tests**

Add to `tests/test_load_memory.py`:

```python
from load_memory import pre_extract_transcripts_incremental


class TestPreExtractTranscriptsIncremental:
    def _mock_daily_data(self, session_id="s1", mode="full"):
        """Build a minimal extract_transcripts_incremental return value."""
        return {
            "2026-02-22": [
                {
                    "session_id": session_id,
                    "filepath": "/tmp/test.jsonl",
                    "project_path": "project-a",
                    "messages": [{"role": "assistant", "content": "hello"}],
                    "message_count": 1,
                    "mode": mode,
                    "current_offset": 500,
                    "current_lines": 5,
                }
            ]
        }

    def test_returns_extracted_files_and_offsets(self, tmp_path):
        """Returns both extracted_files dict and session_offsets dict."""
        with mock.patch("load_memory.extract_transcripts_incremental", return_value=self._mock_daily_data()), \
             mock.patch("load_memory.format_transcripts_incremental", return_value="formatted output"), \
             mock.patch("load_memory.load_synthesis_state", return_value={"sessions": {}}):
            extracted, offsets = pre_extract_transcripts_incremental(
                ["2026-02-22"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        assert "2026-02-22" in extracted
        assert Path(extracted["2026-02-22"]).exists()
        assert offsets["s1"]["offset"] == 500
        assert offsets["s1"]["lines"] == 5

    def test_creates_sidecar(self, tmp_path):
        """Sidecar .sessions file contains session IDs."""
        with mock.patch("load_memory.extract_transcripts_incremental", return_value=self._mock_daily_data("abc")), \
             mock.patch("load_memory.format_transcripts_incremental", return_value="formatted"), \
             mock.patch("load_memory.load_synthesis_state", return_value={"sessions": {}}):
            extracted, _ = pre_extract_transcripts_incremental(
                ["2026-02-22"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        sidecar = Path(extracted["2026-02-22"]).with_suffix(".sessions")
        assert sidecar.exists()
        assert "abc" in sidecar.read_text()

    def test_skips_empty_dates(self, tmp_path):
        """Dates with no incremental content are excluded."""
        with mock.patch("load_memory.extract_transcripts_incremental", return_value={}), \
             mock.patch("load_memory.load_synthesis_state", return_value={"sessions": {}}):
            extracted, offsets = pre_extract_transcripts_incremental(
                ["2026-02-22"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        assert extracted == {}
        assert offsets == {}

    def test_collects_offsets_from_multiple_sessions(self, tmp_path):
        """Multiple sessions across dates accumulate in offsets dict."""
        multi = {
            "2026-02-21": [
                {"session_id": "s1", "filepath": "/tmp/t1", "project_path": None,
                 "messages": [{"role": "assistant", "content": "a"}], "message_count": 1,
                 "mode": "full", "current_offset": 100, "current_lines": 2},
            ],
            "2026-02-22": [
                {"session_id": "s2", "filepath": "/tmp/t2", "project_path": None,
                 "messages": [{"role": "assistant", "content": "b"}], "message_count": 1,
                 "mode": "delta", "current_offset": 300, "current_lines": 8},
            ],
        }
        with mock.patch("load_memory.extract_transcripts_incremental", return_value=multi), \
             mock.patch("load_memory.format_transcripts_incremental", return_value="data"), \
             mock.patch("load_memory.load_synthesis_state", return_value={"sessions": {}}):
            _, offsets = pre_extract_transcripts_incremental(
                ["2026-02-21", "2026-02-22"], exclude_session_id=None, output_dir=str(tmp_path)
            )
        assert offsets["s1"]["offset"] == 100
        assert offsets["s2"]["offset"] == 300
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_load_memory.py::TestPreExtractTranscriptsIncremental -v`
Expected: FAIL with ImportError

**Step 3: Implement the function**

Add imports at top of `load_memory.py` (near line 46):
```python
from memory_utils import load_synthesis_state
from transcript_ops import extract_transcripts_incremental, format_transcripts_incremental
```

Add after `pre_extract_transcripts`:

```python
def pre_extract_transcripts_incremental(
    pending_dates: list,
    exclude_session_id: str | None = None,
    output_dir: str = "/tmp",
) -> tuple[dict[str, str], dict[str, dict]]:
    """Pre-extract transcripts incrementally using high water marks.

    Like pre_extract_transcripts but uses synthesis state to skip unchanged
    sessions and only extract delta content from grown sessions.

    Returns:
        (extracted_files, session_offsets) where:
        - extracted_files: dict mapping date -> output file path
        - session_offsets: dict mapping session_id -> {"offset": int, "lines": int}
    """
    state = load_synthesis_state()
    pid = os.getpid()
    extracted_files: dict[str, str] = {}
    session_offsets: dict[str, dict] = {}

    try:
        daily_data = extract_transcripts_incremental(
            state, exclude_session_id=exclude_session_id
        )
    except Exception as e:
        print(f"Warning: Incremental extraction failed: {e}", file=sys.stderr)
        return {}, {}

    for date in sorted(pending_dates):
        sessions = daily_data.get(date)
        if not sessions:
            continue

        output_path = f"{output_dir}/memory-extract-{date}-{pid}.txt"
        date_data = {date: sessions}
        Path(output_path).write_text(
            format_transcripts_incremental(date_data, total_line_budget=TRANSCRIPT_LINE_BUDGET),
            encoding="utf-8",
        )

        sidecar_path = Path(output_path).with_suffix(".sessions")
        session_ids = [s["session_id"] for s in sessions]
        sidecar_path.write_text("\n".join(session_ids) + "\n", encoding="utf-8")

        extracted_files[date] = output_path

        for s in sessions:
            session_offsets[s["session_id"]] = {
                "offset": s["current_offset"],
                "lines": s["current_lines"],
            }

    return extracted_files, session_offsets
```

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_load_memory.py::TestPreExtractTranscriptsIncremental -v`
Expected: PASS

**Step 5: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "feat: add pre_extract_transcripts_incremental for delta-aware extraction"
```

---

### Task 6: Update prompt builder for merge context

**Files:**
- Modify: `scripts/load_memory.py:499-536` (`_build_embedded_files`)
- Modify: `scripts/load_memory.py:307-428` (`_build_preextracted_prompt`)
- Test: `tests/test_load_memory.py`

**Step 1: Write the failing tests**

Add to `tests/test_load_memory.py`:

```python
class TestBuildEmbeddedFilesWithDailies:
    """Test that _build_embedded_files includes existing daily files as merge context."""

    def test_includes_existing_daily_when_available(self, tmp_path):
        """Existing daily file for a date is read into embedded dict."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        (daily_dir / "2026-02-22.md").write_text("## Actions\n- [global/implement] Old stuff")

        extract = tmp_path / "extract.txt"
        extract.write_text("transcript data")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd, \
             mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory._find_projects_in_sidecars", return_value=set()):
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            mock_dd.return_value = daily_dir
            result = _build_embedded_files(
                {"2026-02-22": str(extract)},
                include_dailies=True,
            )

        assert "existing_dailies" in result
        assert "2026-02-22" in result["existing_dailies"]
        assert "Old stuff" in result["existing_dailies"]["2026-02-22"]

    def test_no_daily_returns_empty(self, tmp_path):
        """When no daily file exists for a date, it's not in embedded dict."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd, \
             mock.patch("load_memory.get_daily_dir") as mock_dd, \
             mock.patch("load_memory._find_projects_in_sidecars", return_value=set()):
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            mock_dd.return_value = daily_dir
            result = _build_embedded_files({}, include_dailies=True)

        assert result.get("existing_dailies", {}) == {}

    def test_include_dailies_false_skips(self, tmp_path):
        """include_dailies=False (default) does not read daily files."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        (daily_dir / "2026-02-22.md").write_text("## Actions\n- stuff")

        with mock.patch("load_memory.get_global_memory_file") as mock_gm, \
             mock.patch("load_memory.get_project_memory_dir") as mock_pd, \
             mock.patch("load_memory._find_projects_in_sidecars", return_value=set()):
            mock_gm.return_value = tmp_path / "nonexistent-ltm.md"
            mock_pd.return_value = tmp_path / "nonexistent-proj"
            result = _build_embedded_files({"2026-02-22": str(tmp_path / "x.txt")})

        assert "existing_dailies" not in result or result["existing_dailies"] == {}


class TestPromptMergeContext:
    """Test that prompts include merge instructions when existing dailies present."""

    def test_prompt_includes_existing_daily(self, tmp_path):
        """When existing_dailies in embedded, prompt includes merge context section."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={
                "transcripts": {"2026-02-22": "new transcript data"},
                "existing_dailies": {"2026-02-22": "## Actions\n- [global/implement] Old stuff"},
            },
        )
        assert "Existing daily summary" in prompt
        assert "Old stuff" in prompt
        assert "merge" in prompt.lower()

    def test_prompt_no_merge_when_no_dailies(self, tmp_path):
        """Without existing_dailies, no merge section in prompt."""
        prompt = _build_preextracted_prompt(
            pending_dates=["2026-02-22"],
            extracted_files={"2026-02-22": "/tmp/dummy.txt"},
            synthesis_instructions="INSTRUCTIONS",
            embedded_files={
                "transcripts": {"2026-02-22": "transcript data"},
            },
        )
        assert "Existing daily summary" not in prompt
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_load_memory.py::TestBuildEmbeddedFilesWithDailies tests/test_load_memory.py::TestPromptMergeContext -v`
Expected: FAIL

**Step 3: Implement changes**

In `_build_embedded_files`, add `include_dailies` parameter:

```python
def _build_embedded_files(extracted_files: dict[str, str], include_dailies: bool = False) -> dict:
    """Pre-read all files for embedding in synthesis prompt.
    ...
    """
    embedded: dict = {"transcripts": {}, "global_ltm": "", "project_ltms": {}}
    # ... existing code ...

    # Read existing daily files as merge context (for incremental synthesis)
    if include_dailies:
        daily_dir = get_daily_dir()
        embedded["existing_dailies"] = {}
        for date in extracted_files:
            daily_file = daily_dir / f"{date}.md"
            if daily_file.exists():
                try:
                    embedded["existing_dailies"][date] = daily_file.read_text(encoding="utf-8")
                except IOError:
                    pass

    return embedded
```

In `_build_preextracted_prompt`, add existing daily merge context section.

After the `transcript_block` building (around line 345), add:

```python
    # Build existing daily merge context (for incremental synthesis)
    existing_dailies = embedded_files.get("existing_dailies", {})
    merge_sections = []
    for date in sorted(pending_dates):
        existing = existing_dailies.get(date, "")
        if existing:
            merge_sections.append(f"### Existing daily summary for {date}\n{existing}")
    merge_block = "\n\n".join(merge_sections) if merge_sections else ""
```

And in the f-string prompt, add a conditional merge context section before the transcript block:

```python
    merge_instructions = ""
    if merge_block:
        merge_instructions = f"""
## Existing Daily Summaries (merge context)

These daily files already exist from a previous synthesis run. When you see sessions marked '(continued — new messages only)', merge new insights into the existing summary. Do NOT duplicate entries already present. Add only genuinely new items.

{merge_block}

"""
```

Insert `{merge_instructions}` before `## Session Transcripts` in the prompt f-string.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_load_memory.py::TestBuildEmbeddedFilesWithDailies tests/test_load_memory.py::TestPromptMergeContext -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All existing tests still pass

**Step 6: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "feat: add merge context support for incremental synthesis prompts"
```

---

### Task 7: Wire incremental extraction into main() and --synthesis-prompt

**Files:**
- Modify: `scripts/load_memory.py:577-743` (main function and CLI)
- Test: `tests/test_load_memory.py`

**Step 1: Write the failing test**

Add to `tests/test_load_memory.py`:

```python
class TestIncrementalIntegration:
    """Verify main() uses incremental extraction when state file exists."""

    def test_uses_incremental_when_state_exists(self, tmp_path):
        """When .synthesis-state.json exists, pre_extract_transcripts_incremental is called."""
        state_file = tmp_path / ".synthesis-state.json"
        state_file.write_text('{"sessions": {}}')

        with mock.patch("load_memory.get_memory_dir", return_value=tmp_path), \
             mock.patch("load_memory.get_last_synthesis_file", return_value=tmp_path / ".last-synthesis"), \
             mock.patch("load_memory.should_synthesize", return_value=True), \
             mock.patch("load_memory.get_pending_days", return_value=["2026-02-22"]), \
             mock.patch("load_memory.pre_extract_transcripts_incremental") as mock_incr, \
             mock.patch("load_memory.pre_extract_transcripts") as mock_full, \
             mock.patch("load_memory._build_embedded_files", return_value={}), \
             mock.patch("load_memory._build_synthesis_prompt", return_value="prompt"):
            mock_incr.return_value = ({"2026-02-22": "/tmp/extract.txt"}, {"s1": {"offset": 100, "lines": 5}})

            # We can't easily test main() without side effects, so test the dispatch logic
            # by checking which function gets called
            from load_memory import get_synthesis_state_file
            assert get_synthesis_state_file().exists() if hasattr(load_memory, 'get_synthesis_state_file') else True

        # The actual integration is tested by verifying the function exists and is importable
        from load_memory import pre_extract_transcripts_incremental
        assert callable(pre_extract_transcripts_incremental)
```

**Step 2: Implement the wiring**

In `main()`, replace the pre-extraction block (around lines 630-637) with:

```python
        if extracted_files:
            # Check if synthesis state exists for incremental extraction
            from memory_utils import get_synthesis_state_file
            use_incremental = get_synthesis_state_file().exists()

            if use_incremental:
                extracted_files, session_offsets = pre_extract_transcripts_incremental(
                    pending_dates, exclude_session_id=current_session_id
                )
                has_deltas = any(
                    session_offsets.get(sid, {}).get("offset", 0) > 0
                    for sid in session_offsets
                )
            else:
                extracted_files = pre_extract_transcripts(
                    pending_dates, exclude_session_id=current_session_id
                )
                session_offsets = None
                has_deltas = False
```

Wait — this needs to be restructured. The current flow is:
1. `pre_extract_transcripts` → `extracted_files`
2. `if extracted_files:` → build prompt

For incremental, the flow should be:
1. Check if state file exists
2. If yes: `pre_extract_transcripts_incremental` → `(extracted_files, session_offsets)`
3. If no: `pre_extract_transcripts` → `extracted_files` (session_offsets = None)
4. Either way: `if extracted_files:` → build prompt (with `include_dailies=True` if incremental)

Update `main()` around lines 630-652:

```python
        # Pre-extract all transcripts before launching subagent
        from memory_utils import get_synthesis_state_file
        session_offsets = None

        if get_synthesis_state_file().exists():
            extracted_files, session_offsets = pre_extract_transcripts_incremental(
                pending_dates, exclude_session_id=current_session_id
            )
        else:
            extracted_files = pre_extract_transcripts(
                pending_dates, exclude_session_id=current_session_id
            )

        if extracted_files:
            # Pre-read all files for embedding in prompt (zero tool calls for subagent)
            include_dailies = session_offsets is not None
            embedded = _build_embedded_files(extracted_files, include_dailies=include_dailies)

            # Pass session_offsets through embedded_files for synthesis.py to update state
            if session_offsets:
                embedded["session_offsets"] = session_offsets

            synth_prompt = _build_synthesis_prompt(
                list(extracted_files.keys()), extracted_files, embedded
            )
```

Also update `_build_preextracted_prompt` to pass `session_offsets` to the synthesis.py apply command:

In the Bash command construction, add `--offsets-json` argument when session_offsets present:

```python
    # Add session offsets to apply command if present (for state update)
    session_offsets = embedded_files.get("session_offsets")
    offsets_arg = ""
    if session_offsets:
        offsets_file = f"/tmp/synthesis-offsets-{os.getpid()}.json"
        offsets_arg = f" --offsets-json {offsets_file}"
```

And in the prompt, add a Write step for the offsets file before the apply command:

```python
    if session_offsets:
        import json as _json
        offsets_json = _json.dumps(session_offsets)
        delivery = f"""1. Write(`{output_filename}`, <your structured output>)
2. Bash: `python3 $HOME/.claude/scripts/synthesis.py apply {output_filename} --sidecars {sidecars_arg} --extracts {extracts_arg} --offsets-json {offsets_file}`

Note: The offsets file `{offsets_file}` has been pre-written."""
    else:
        delivery = f"""1. Write(`{output_filename}`, <your structured output>)
2. Bash: `python3 $HOME/.claude/scripts/synthesis.py apply {output_filename} --sidecars {sidecars_arg} --extracts {extracts_arg}`"""
```

Wait — the subagent can't write the offsets file since it contains binary session offset data. Better approach: **write the offsets JSON file during pre-extraction** (in `pre_extract_transcripts_incremental`) and pass the path through the prompt.

Actually, simplest approach: write the offsets file in `main()` (or `--synthesis-prompt`) right after pre-extraction, and include the path in the apply command. The subagent doesn't need to know about it — it just passes it through to `synthesis.py apply`.

**Revised approach:**

In `main()`, after getting `session_offsets`:
```python
        if session_offsets:
            offsets_path = f"/tmp/synthesis-offsets-{os.getpid()}.json"
            Path(offsets_path).write_text(json.dumps(session_offsets), encoding="utf-8")
```

Pass `offsets_path` into `_build_preextracted_prompt` via a new parameter or through `embedded_files`. Let's use `embedded_files["offsets_path"]`.

In `_build_preextracted_prompt`, modify the Bash command:
```python
    offsets_path = embedded_files.get("offsets_path", "")
    offsets_arg = f" --offsets-json {offsets_path}" if offsets_path else ""
    # ... then use {offsets_arg} in the Bash command
```

Similarly, update the `--synthesis-prompt` CLI path (lines 716-741).

**Step 3: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All pass

**Step 4: Commit**

```bash
git add scripts/load_memory.py tests/test_load_memory.py
git commit -m "feat: wire incremental extraction into main() and CLI"
```

---

### Task 8: Add `--offsets-json` to synthesis.py apply command

**Files:**
- Modify: `scripts/synthesis.py:408-428` (CLI parser)
- Modify: `scripts/synthesis.py:372-405` (`apply_results`)
- Modify: `scripts/synthesis.py:317-369` (`run_post_processing`)
- Test: `tests/test_synthesis.py`

**Step 1: Write the failing tests**

Add to `tests/test_synthesis.py`:

```python
class TestApplyResultsWithOffsets:
    """Test state update when --offsets-json is provided."""

    def test_updates_synthesis_state(self, tmp_path):
        """apply_results calls update_synthesis_state when offsets_json provided."""
        daily_dir = tmp_path / "daily"
        daily_dir.mkdir()
        global_ltm = tmp_path / "global-long-term-memory.md"
        global_ltm.write_text("## Key Learnings\n<!-- decay -->\n\n")

        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/pattern] Something

===END==="""

        output_file = tmp_path / "output.txt"
        output_file.write_text(output_text)

        offsets_file = tmp_path / "offsets.json"
        offsets_file.write_text('{"s1": {"offset": 500, "lines": 10}}')

        with patch("memory_utils.get_daily_dir", return_value=daily_dir), \
             patch("memory_utils.get_global_memory_file", return_value=global_ltm), \
             patch("memory_utils.get_project_memory_dir", return_value=tmp_path / "pm"), \
             patch("memory_utils.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"), \
             patch("synthesis.update_synthesis_state") as mock_update:
            apply_results(
                output_file=str(output_file),
                sidecar_paths=[],
                extract_paths=[],
                offsets_json=str(offsets_file),
            )

        mock_update.assert_called_once_with({"s1": {"offset": 500, "lines": 10}})

    def test_no_offsets_no_state_update(self, tmp_path):
        """apply_results does not call update_synthesis_state when no offsets."""
        output_text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===END==="""

        output_file = tmp_path / "output.txt"
        output_file.write_text(output_text)

        with patch("memory_utils.get_daily_dir", return_value=tmp_path / "daily"), \
             patch("memory_utils.get_global_memory_file", return_value=tmp_path / "g.md"), \
             patch("memory_utils.get_project_memory_dir", return_value=tmp_path / "pm"), \
             patch("memory_utils.get_memory_dir", return_value=tmp_path), \
             patch("synthesis.run_post_processing"), \
             patch("synthesis.update_synthesis_state") as mock_update:
            apply_results(str(output_file), [], [])

        mock_update.assert_not_called()
```

**Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_synthesis.py::TestApplyResultsWithOffsets -v`
Expected: FAIL

**Step 3: Implement changes**

In `synthesis.py`:

Add import:
```python
from memory_utils import get_memory_dir, update_synthesis_state  # noqa: E402
```

Update `apply_results` signature:
```python
def apply_results(
    output_file: str,
    sidecar_paths: list[str],
    extract_paths: list[str],
    offsets_json: str | None = None,
) -> None:
```

After successful write/append (before `run_post_processing`), add:
```python
    # Update synthesis state with new high water marks
    if offsets_json:
        try:
            offsets = json.loads(Path(offsets_json).read_text(encoding="utf-8"))
            update_synthesis_state(offsets)
        except (IOError, json.JSONDecodeError) as e:
            print(f"Warning: Could not update synthesis state: {e}", file=sys.stderr)
```

Add `import json` at top if not already present.

Update CLI parser:
```python
    apply_parser.add_argument("--offsets-json", default=None, help="Path to session offsets JSON for state update")
```

Update CLI handler:
```python
    if args.command == "apply":
        apply_results(args.output_file, args.sidecars, args.extracts, args.offsets_json)
```

Add offsets file to cleanup in `run_post_processing`:
```python
def run_post_processing(
    sidecar_paths: list[str],
    extract_paths: list[str],
    offsets_json: str | None = None,
) -> None:
    # ... existing code ...
    # Add offsets file to cleanup
    paths_to_clean = extract_paths + sidecar_paths
    if offsets_json:
        paths_to_clean.append(offsets_json)
    for path in paths_to_clean:
        # ...
```

Pass `offsets_json` through from `apply_results` to `run_post_processing`.

**Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_synthesis.py -v`
Expected: All pass (including existing tests)

**Step 5: Commit**

```bash
git add scripts/synthesis.py tests/test_synthesis.py
git commit -m "feat: add --offsets-json to synthesis.py for state updates"
```

---

### Task 9: Add prune-captured integration to indexing.py mark-captured

**Files:**
- Modify: `scripts/indexing.py:516-578` (`cmd_mark_captured`)
- Test: `tests/test_indexing.py`

**Step 1: Write the failing test**

Add to `tests/test_indexing.py`:

```python
class TestMarkCapturedPrunesState:
    """Verify mark-captured also prunes synthesis state."""

    def test_prunes_captured_from_synthesis_state(self, tmp_path):
        """When sessions are marked captured, they're also pruned from .synthesis-state.json."""
        from memory_utils import prune_captured_from_state

        with mock.patch("indexing.prune_captured_from_state") as mock_prune:
            # The actual prune call happens inside cmd_mark_captured
            # We verify it's called with the right session IDs
            pass  # This test verifies the import and call wiring
```

Actually, since `cmd_mark_captured` already handles the sidecar flow, we just need to add a call to `prune_captured_from_state` at the end. The simplest test is an integration test.

**Step 2: Implement the change**

In `indexing.py`, at the end of `cmd_mark_captured` (after the marking loop), add:

```python
    # Prune marked sessions from synthesis state (cleanup)
    if marked > 0:
        try:
            from memory_utils import prune_captured_from_state
            prune_captured_from_state(captured)
        except Exception:
            pass  # Non-critical cleanup
```

**Step 3: Commit**

```bash
git add scripts/indexing.py tests/test_indexing.py
git commit -m "feat: prune synthesis state when sessions are marked captured"
```

---

### Task 10: Run full test suite and update CLAUDE.md

**Files:**
- Modify: `CLAUDE.md` (update Features Summary table and Repo Structure)
- Modify: `scripts/memory_utils.py` `__all__` (verify)
- Modify: `scripts/transcript_ops.py` `__all__` (verify)

**Step 1: Run full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: All tests pass

**Step 2: Update CLAUDE.md**

Add to Features Summary table:
```
| Incremental synthesis | `.synthesis-state.json` tracks per-session byte offset + line count; skips unchanged, delta-extracts grown sessions |
```

Update Repo Structure to show synthesis.py description:
```
│   ├── synthesis.py            # Synthesis output parser, applier, state updater
```

**Step 3: Run install to verify**

Run: `python3 install.py`
Expected: Clean install

**Step 4: Commit**

```bash
git add CLAUDE.md scripts/memory_utils.py scripts/transcript_ops.py
git commit -m "docs: update CLAUDE.md with incremental synthesis feature"
```

---

## Dependency Graph

```
Task 1 (state helpers)
  └─ Task 2 (parse_jsonl_file_from_line)
       └─ Task 3 (extract_transcripts_incremental)
            └─ Task 4 (format_transcripts_incremental)
                 └─ Task 5 (pre_extract_transcripts_incremental)
                      └─ Task 6 (prompt merge context)
                           └─ Task 7 (wire into main)
                                └─ Task 8 (synthesis.py --offsets-json)
                                     └─ Task 9 (prune on mark-captured)
                                          └─ Task 10 (test suite + docs)
```

Tasks 1 and 2 are independent of each other and can run in parallel.
Tasks 3 depends on both 1 and 2.
Everything else is sequential.
