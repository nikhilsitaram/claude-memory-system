#!/usr/bin/env python3
"""
Developer tools for Claude Code Memory System.

Repo-local utility — NOT installed to ~/.claude/scripts/.

Usage:
    python scripts/devtools.py verify-install [--mode all|install-only|verify-only|smoke-test]
    python scripts/devtools.py memory-status [--mode all|pending|tokens|synthesis|decay|daily]
    python scripts/devtools.py extract-debug [DAY] [--mode all|sessions|extract|captured|content]

Requirements: Python 3.9+
"""

import argparse
import filecmp
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
            "transcript_ops.py", "project_manager.py", "decay.py", "token_usage.py",
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
            ("indexing.py list-pending", [sys.executable, str(SCRIPTS_DIR / "indexing.py"), "list-pending"]),
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
    from memory_utils import get_memory_dir, get_daily_dir, load_settings

    settings = load_settings()

    if do_all or args.mode == "pending":
        print("Pending transcripts:")
        from transcript_ops import get_pending_days
        days = get_pending_days()
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
            print(f"  Interval: {settings.get('synthesis', {}).get('intervalHours', 2)}h")
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
    day = args.day or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    sys.path.insert(0, str(REPO_DIR / "scripts"))
    from memory_utils import get_captured_sessions
    from indexing import list_all_sessions, get_session_date

    captured = get_captured_sessions()

    if do_all or args.mode in ("sessions", "captured"):
        all_sessions = list_all_sessions()
        day_sessions = [s for s in all_sessions if get_session_date(s) == day]

        if args.mode != "captured":
            print(f"Sessions for {day}:")
            for s in day_sessions:
                status = "captured" if s.session_id in captured else "pending"
                print(f"  {s.session_id[:12]}...  {s.file_size:>8,} bytes  [{status}]")
            if not day_sessions:
                print("  None")
            print()

        if do_all or args.mode == "captured":
            print(f"Captured sessions for {day}:")
            cap = [s for s in day_sessions if s.session_id in captured]
            for s in cap:
                print(f"  {s.session_id}")
            if not cap:
                print("  None")
            print()

    if do_all or args.mode in ("extract", "content"):
        from transcript_ops import extract_transcripts
        print(f"Extracting transcripts for {day}...")
        daily_data = extract_transcripts(day)
        if day in daily_data:
            sessions = daily_data[day]
            print(f"  {len(sessions)} session(s) with content")
            for s in sessions:
                print(f"  {s['session_id'][:12]}...  {s['message_count']} messages")
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
    import re

    sys.path.insert(0, str(REPO_DIR / "scripts"))
    from memory_utils import (
        get_global_memory_file, get_project_memory_dir, get_daily_dir,
        extract_entry_keywords, is_routed_match,
    )

    dry_run = args.dry_run

    # 1. Collect all LTM entries (global + all project files)
    ltm_entries = []

    global_ltm = get_global_memory_file()
    if global_ltm.exists():
        for line in global_ltm.read_text(encoding="utf-8").splitlines():
            if re.match(r"^\s*-\s*\(", line):  # Lines starting with "- (YYYY-MM-DD)"
                ltm_entries.append(line)

    project_dir = get_project_memory_dir()
    if project_dir.exists():
        for pfile in project_dir.glob("*-long-term-memory.md"):
            for line in pfile.read_text(encoding="utf-8").splitlines():
                if re.match(r"^\s*-\s*\(", line):
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

        in_learnings_or_lessons = False
        for line in lines:
            # Track if we're in a Learnings or Lessons section
            if line.startswith("## "):
                section = line.strip("# ").strip()
                in_learnings_or_lessons = section in ("Learnings", "Lessons")

            # Only check entries in Learnings/Lessons sections
            if (in_learnings_or_lessons
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
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Developer tools for Claude Code Memory System")
    sub = parser.add_subparsers(dest="command")

    vi = sub.add_parser("verify-install", help="Verify installation integrity")
    vi.add_argument("--mode", choices=["all", "install-only", "verify-only", "smoke-test"], default="all")
    vi.set_defaults(func=cmd_verify_install)

    ms = sub.add_parser("memory-status", help="Show memory system status")
    ms.add_argument("--mode", choices=["all", "pending", "tokens", "synthesis", "decay", "daily"], default="all")
    ms.set_defaults(func=cmd_memory_status)

    ed = sub.add_parser("extract-debug", help="Debug transcript extraction")
    ed.add_argument("day", nargs="?", help="Day to debug (default: today)")
    ed.add_argument("--mode", choices=["all", "sessions", "extract", "captured", "content"], default="all")
    ed.set_defaults(func=cmd_extract_debug)

    mr = sub.add_parser("mark-routed", help="Mark daily entries that exist in LTM with [routed] prefix")
    mr.add_argument("--dry-run", action="store_true", help="Preview changes without modifying files")
    mr.set_defaults(func=cmd_mark_routed)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
