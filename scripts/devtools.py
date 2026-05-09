#!/usr/bin/env python3
"""
Developer tools for Claude Code Memory System.

Dev diagnostics and mark-routed dedup migration. Installed to ~/.claude/scripts/ via symlink.

Usage (after install — these scripts live in ~/.claude/scripts/):
    uv run --no-project $HOME/.claude/scripts/devtools.py verify-install [--mode all|install-only|verify-only|smoke-test]
    uv run --no-project $HOME/.claude/scripts/devtools.py memory-status [--mode all|pending|tokens|synthesis|decay|daily]
    uv run --no-project $HOME/.claude/scripts/devtools.py extract-debug [DAY] [--mode all|sessions|extract|state|content]

Requirements: Python 3.9+ (uv-managed)
"""

import argparse
import filecmp
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_DIR = Path(__file__).parent.parent.resolve()
CLAUDE_DIR = Path.home() / ".claude"
SCRIPTS_DIR = CLAUDE_DIR / "scripts"
SKILLS_DIR = CLAUDE_DIR / "skills"
MEMORY_DIR = CLAUDE_DIR / "memory"


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode, result.stdout, result.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return 1, "", str(e)


def _print_result(label: str, ok: bool, detail: str = ""):
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {label}"
    if detail:
        line += f" — {detail}"
    print(line)


def cmd_verify_install(args: argparse.Namespace) -> int:
    """Verify installation integrity."""
    do_all = args.mode == "all"
    failures = 0

    if do_all or args.mode == "install-only":
        print("Running install.py...")
        rc, out, err = _run([sys.executable, str(REPO_DIR / "install.py")])
        _print_result("install.py", rc == 0, err.strip()[:80] if rc != 0 else "")
        if rc != 0:
            failures += 1

    if do_all or args.mode == "verify-only":
        print("\nVerifying scripts...")
        scripts = [
            "memory_utils.py", "load_memory.py", "indexing.py",
            "transcript_ops.py", "project_manager.py", "decay.py", "synthesis.py",
            "token_usage.py",
        ]
        for name in scripts:
            src = REPO_DIR / "scripts" / name
            dst = SCRIPTS_DIR / name
            if not src.exists():
                _print_result(name, False, "not in repo")
                failures += 1
            elif not dst.exists():
                _print_result(name, False, "not installed")
                failures += 1
            elif filecmp.cmp(str(src), str(dst), shallow=False):
                _print_result(name, True)
            else:
                _print_result(name, False, "differs from repo")
                failures += 1

        print("\nVerifying skills...")
        for skill in ["remember", "synthesize", "recall", "settings", "projects"]:
            src = REPO_DIR / "skills" / skill / "SKILL.md"
            dst = SKILLS_DIR / skill / "SKILL.md"
            if not src.exists():
                _print_result(f"skills/{skill}", False, "not in repo")
                failures += 1
            elif not dst.exists():
                _print_result(f"skills/{skill}", False, "not installed")
                failures += 1
            elif filecmp.cmp(str(src), str(dst), shallow=False):
                _print_result(f"skills/{skill}", True)
            else:
                _print_result(f"skills/{skill}", False, "differs from repo")
                failures += 1

    if do_all or args.mode == "smoke-test":
        print("\nSmoke tests...")
        for label, cmd in [
            ("memory_utils.py", [sys.executable, str(SCRIPTS_DIR / "memory_utils.py")]),
            ("indexing.py list-recent", [sys.executable, str(SCRIPTS_DIR / "indexing.py"), "list-recent"]),
            ("decay.py --dry-run", [sys.executable, str(SCRIPTS_DIR / "decay.py"), "--dry-run"]),
        ]:
            rc, out, err = _run(cmd)
            _print_result(label, rc == 0, err.strip()[:80] if rc != 0 else "")
            if rc != 0:
                failures += 1

    print(f"\n{'All checks passed!' if failures == 0 else f'{failures} check(s) failed.'}")
    return 1 if failures > 0 else 0


def cmd_memory_status(args: argparse.Namespace) -> int:
    """Show memory system status."""
    do_all = args.mode == "all"

    sys.path.insert(0, str(REPO_DIR / "scripts"))
    from memory_utils import DEFAULT_SETTINGS, get_daily_dir, get_memory_dir, load_settings

    settings = load_settings()

    if do_all or args.mode == "recent":
        print("Recent transcripts:")
        from transcript_ops import get_recent_days
        days = get_recent_days()
        for d in days:
            print(f"  {d}")
        if not days:
            print("  None")
        print()

    if do_all or args.mode == "tokens":
        print("Token usage:")
        rc, out, _ = _run([sys.executable, str(SCRIPTS_DIR / "token_usage.py")])
        for line in (out if rc == 0 else "Error").strip().splitlines():
            print(f"  {line}")
        print()

    if do_all or args.mode == "synthesis":
        print("Synthesis status:")
        last_file = get_memory_dir() / ".last-synthesis"
        if last_file.exists():
            print(f"  Last: {last_file.read_text(encoding='utf-8').strip()}")
            print(f"  Interval: {settings.get('synthesis', {}).get('intervalHours', DEFAULT_SETTINGS['synthesis']['intervalHours'])}h")
        else:
            print("  Never synthesized")
        print()

    if do_all or args.mode == "decay":
        print("Decay status:")
        rc, out, _ = _run([sys.executable, str(SCRIPTS_DIR / "decay.py"), "--dry-run"])
        for line in (out if rc == 0 else "Error").strip().splitlines():
            print(f"  {line}")
        print()

    if do_all or args.mode == "daily":
        print("Daily files:")
        daily_dir = get_daily_dir()
        if daily_dir.exists():
            files = sorted(daily_dir.glob("*.md"), reverse=True)
            for f in files[:10]:
                print(f"  {f.name}  ({f.stat().st_size:,} bytes)")
            if len(files) > 10:
                print(f"  ... and {len(files) - 10} more")
        else:
            print("  No daily directory")
        print()

    return 0


def cmd_extract_debug(args: argparse.Namespace) -> int:
    """Debug transcript extraction for a specific day."""
    do_all = args.mode == "all"
    day = args.day or datetime.now().strftime("%Y-%m-%d")

    sys.path.insert(0, str(REPO_DIR / "scripts"))
    from indexing import get_session_date, list_all_sessions
    from memory_utils import load_synthesis_state

    state = load_synthesis_state()
    sessions_state = state.get("sessions", {})

    if do_all or args.mode in ("sessions", "state"):
        all_sessions = list_all_sessions()
        day_sessions = [s for s in all_sessions if get_session_date(s) == day]

        if args.mode != "state":
            print(f"Sessions for {day}:")
            for s in day_sessions:
                prev = sessions_state.get(s.session_id)
                if prev and s.file_size == prev.get("offset", 0):
                    status = "unchanged"
                elif prev and s.file_size > prev.get("offset", 0):
                    status = "grown"
                elif prev:
                    status = "shrunk"
                else:
                    status = "new"
                print(f"  {s.session_id[:12]}...  {s.file_size:>8,} bytes  [{status}]")
            if not day_sessions:
                print("  None")
            print()

        if do_all or args.mode == "state":
            print(f"Synthesized sessions for {day}:")
            synth = [s for s in day_sessions if s.session_id in sessions_state]
            for s in synth:
                prev = sessions_state[s.session_id]
                print(f"  {s.session_id}  offset={prev.get('offset')} lines={prev.get('lines')}")
            if not synth:
                print("  None")
            print()

    if do_all or args.mode in ("extract", "content"):
        from transcript_ops import extract_transcripts_incremental
        print(f"Extracting transcripts for {day}...")
        daily_data = extract_transcripts_incremental(state)
        if day in daily_data:
            sessions = daily_data[day]
            print(f"  {len(sessions)} session(s) with content")
            for s in sessions:
                print(f"  {s['session_id'][:12]}...  {s['message_count']} messages  [{s['mode']}]")
                if do_all or args.mode == "content":
                    for msg in s["messages"][:2]:
                        preview = msg["content"][:200]
                        if len(msg["content"]) > 200:
                            preview += "..."
                        print(f"    [{msg['role'].upper()}] {preview}")
        else:
            print("  No extractable content")
        print()

    return 0


def cmd_mark_routed(args: argparse.Namespace) -> int:
    """One-time migration: mark daily entries that exist in LTM with [routed] prefix."""
    sys.path.insert(0, str(REPO_DIR / "scripts"))
    from memory_utils import (
        LTM_ENTRY_PATTERN,
        collect_ltm_files,
        get_daily_dir,
        is_routed_match,
    )

    dry_run = args.dry_run

    # 1. Collect all LTM entries (global + all project files)
    ltm_entries = []
    for ltm_file in collect_ltm_files():
        for line in ltm_file.read_text(encoding="utf-8").splitlines():
            if LTM_ENTRY_PATTERN.match(line):
                ltm_entries.append(line)

    print(f"Collected {len(ltm_entries)} LTM entries across all files")

    # 2. Process each daily file
    daily_dir = get_daily_dir()
    total_marked = 0

    if not daily_dir.exists():
        print("No daily directory found")
        return 0

    for daily_file in sorted(daily_dir.glob("*.md")):
        lines = daily_file.read_text(encoding="utf-8").splitlines()
        modified = False
        file_marked = 0
        new_lines = []

        in_routable_section = False
        for line in lines:
            # Track if we're in a routable section
            if line.startswith("## "):
                section = line.strip("# ").strip()
                in_routable_section = section in ("Actions", "Decisions", "Learnings", "Lessons")

            # Only check entries in routable sections
            if (in_routable_section
                    and re.match(r"^\s*-\s*\[(?!routed)", line)  # tagged entry, not already routed
                    and any(is_routed_match(line, ltm) for ltm in ltm_entries)):
                new_lines.append(re.sub(r"^(\s*-\s*)", r"\1[routed]", line))
                modified = True
                file_marked += 1
            else:
                new_lines.append(line)

        if modified:
            if dry_run:
                print(f"  {daily_file.name}: would mark {file_marked} entries")
            else:
                daily_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                print(f"  {daily_file.name}: marked {file_marked} entries")
            total_marked += file_marked

    action = "Would mark" if dry_run else "Marked"
    print(f"\n{action} {total_marked} entries across all daily files")

    # 3. Deduplicate within each LTM file (exact match + keyword similarity)
    total_deduped = 0
    for ltm_file in collect_ltm_files():
        lines = ltm_file.read_text(encoding="utf-8").splitlines()
        seen_exact: set[str] = set()
        seen_keyword_entries: list[str] = []
        new_lines = []
        file_deduped = 0

        for line in lines:
            # Only dedup dated entry lines
            if LTM_ENTRY_PATTERN.match(line):
                normalized = line.strip()
                # Exact match dedup
                if normalized in seen_exact:
                    file_deduped += 1
                    continue
                # Keyword similarity dedup (catches near-duplicates)
                # 0.7 threshold: below this, domain vocabulary overlap causes false positives
                if any(is_routed_match(normalized, seen, threshold=0.7) for seen in seen_keyword_entries):
                    if dry_run:
                        print(f"    near-dup: {normalized[:80]}...")
                    file_deduped += 1
                    continue
                seen_exact.add(normalized)
                seen_keyword_entries.append(normalized)
            new_lines.append(line)

        if file_deduped:
            if dry_run:
                print(f"  {ltm_file.name}: would remove {file_deduped} duplicate entries")
            else:
                ltm_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                print(f"  {ltm_file.name}: removed {file_deduped} duplicate entries")
            total_deduped += file_deduped

    if total_deduped:
        action = "Would remove" if dry_run else "Removed"
        print(f"\n{action} {total_deduped} duplicate LTM entries")

    return 0


def cmd_validate_ltm(args: argparse.Namespace) -> int:
    """Validate LTM files for duplicates, misrouted entries, and entry count."""
    sys.path.insert(0, str(REPO_DIR / "scripts"))
    from memory_utils import (
        LTM_ENTRY_PATTERN,
        collect_ltm_files,
        get_global_memory_file,
        get_projects_index_file,
        is_routed_match,
        load_json_file,
    )

    issues: list[str] = []

    # Load registered project names
    projects_index = load_json_file(get_projects_index_file(), {})
    registered_projects = {
        data.get("name", "")
        for data in projects_index.get("projects", {}).values()
        if data.get("name")
    }

    # Collect all LTM files with scope labels
    global_ltm = get_global_memory_file()
    ltm_files: list[tuple[str, Path]] = []
    for ltm_file in collect_ltm_files():
        if ltm_file == global_ltm:
            ltm_files.append(("global", ltm_file))
        else:
            pname = ltm_file.stem.replace("-long-term-memory", "")
            ltm_files.append((pname, ltm_file))

    for scope, ltm_file in ltm_files:
        lines = ltm_file.read_text(encoding="utf-8").splitlines()
        entries: list[str] = []
        entry_count = 0

        for line in lines:
            if LTM_ENTRY_PATTERN.match(line):
                normalized = line.strip()
                entry_count += 1
                # Check exact duplicates
                if normalized in entries:
                    issues.append(f"[{scope}] EXACT DUP: {normalized[:80]}...")
                else:
                    # Check near-duplicates (0.7 threshold avoids domain vocabulary false positives)
                    for existing in entries:
                        if is_routed_match(normalized, existing, threshold=0.7):
                            issues.append(
                                f"[{scope}] NEAR DUP:\n"
                                f"  kept: {existing[:80]}...\n"
                                f"  dup:  {normalized[:80]}..."
                            )
                            break
                entries.append(normalized)

        # Check entry count in decay-eligible sections
        in_decay_section = False
        decay_count = 0
        for line in lines:
            if line.startswith("## "):
                section = line.strip("# ").strip()
                in_decay_section = section in (
                    "Key Actions", "Key Decisions", "Key Learnings", "Key Lessons"
                )
            elif in_decay_section and LTM_ENTRY_PATTERN.match(line):
                decay_count += 1

        if decay_count > 40:
            issues.append(f"[{scope}] HIGH ENTRY COUNT: {decay_count} entries in decay sections (consider pruning)")

        # Check unregistered project files
        if scope != "global" and scope not in registered_projects:
            issues.append(f"[{scope}] UNREGISTERED PROJECT: {ltm_file.name} has no match in projects-index.json")

        print(f"  {ltm_file.name}: {entry_count} entries ({decay_count} in decay sections)")

    if issues:
        print(f"\n{len(issues)} issue(s) found:")
        for issue in issues:
            print(f"  - {issue}")
        return 1

    print("\nNo issues found.")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Show synthesis run statistics (24h and 7d summaries)."""
    sys.path.insert(0, str(REPO_DIR / "scripts"))
    from memory_utils import get_synthesis_stats_file

    stats_file = get_synthesis_stats_file()
    if not stats_file.exists():
        print("No synthesis stats recorded yet.")
        return 0

    now = datetime.now(timezone.utc)
    records_24h: list[dict] = []
    records_7d: list[dict] = []

    with stats_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                ts = datetime.fromisoformat(record["ts"].replace("Z", "+00:00"))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            age_hours = (now - ts).total_seconds() / 3600
            if age_hours <= 24:
                records_24h.append(record)
            if age_hours <= 168:  # 7 days
                records_7d.append(record)

    def _summarize(records: list[dict]) -> dict:
        if not records:
            return {
                "runs": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "avg_duration": 0.0,
                "errors": 0,
            }
        total_duration = sum(r.get("duration_s", 0) for r in records)
        return {
            "runs": len(records),
            "input_tokens": sum(r.get("input_tokens", 0) for r in records),
            "output_tokens": sum(r.get("output_tokens", 0) for r in records),
            "avg_duration": total_duration / len(records),
            "errors": sum(1 for r in records if r.get("status") == "error"),
        }

    s24 = _summarize(records_24h)
    s7d = _summarize(records_7d)

    print("Synthesis stats")
    print(f"{'':20s} {'Last 24h':>10s}    {'Last 7d':>10s}")
    print(f"{'Runs:':20s} {s24['runs']:>10,}    {s7d['runs']:>10,}")
    print(f"{'Input tokens:':20s} {s24['input_tokens']:>10,}    {s7d['input_tokens']:>10,}")
    print(f"{'Output tokens:':20s} {s24['output_tokens']:>10,}    {s7d['output_tokens']:>10,}")
    print(f"{'Avg duration:':20s} {s24['avg_duration']:>9.1f}s    {s7d['avg_duration']:>9.1f}s")
    print(f"{'Errors:':20s} {s24['errors']:>10,}    {s7d['errors']:>10,}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Developer tools for Claude Code Memory System")
    sub = parser.add_subparsers(dest="command")

    vi = sub.add_parser("verify-install", help="Verify installation integrity")
    vi.add_argument("--mode", choices=["all", "install-only", "verify-only", "smoke-test"], default="all")
    vi.set_defaults(func=cmd_verify_install)

    ms = sub.add_parser("memory-status", help="Show memory system status")
    ms.add_argument("--mode", choices=["all", "recent", "tokens", "synthesis", "decay", "daily"], default="all")
    ms.set_defaults(func=cmd_memory_status)

    ed = sub.add_parser("extract-debug", help="Debug transcript extraction")
    ed.add_argument("day", nargs="?", help="Day to debug (default: today)")
    ed.add_argument("--mode", choices=["all", "sessions", "extract", "state", "content"], default="all")
    ed.set_defaults(func=cmd_extract_debug)

    mr = sub.add_parser("mark-routed", help="Mark daily entries that exist in LTM with [routed] prefix")
    mr.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files")
    mr.set_defaults(func=cmd_mark_routed)

    vl = sub.add_parser("validate-ltm", help="Validate LTM files for duplicates and issues")
    vl.set_defaults(func=cmd_validate_ltm)

    st = sub.add_parser("stats", help="Show synthesis run statistics")
    st.set_defaults(func=cmd_stats)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
