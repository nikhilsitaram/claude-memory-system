#!/usr/bin/env python3
"""Session import utility for cross-machine migration.

Copies session transcripts from a source directory (backup, external drive)
into ~/.claude/projects/ with path prefix remapping and deduplication.
"""

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from memory_utils import get_projects_dir


@dataclass
class ImportResult:
    """Result of a session import operation."""

    copied: int = 0
    skipped: int = 0
    projects: int = 0
    mismatches: list[str] = field(default_factory=list)


def detect_prefixes(folder_names: list[str]) -> dict[str, list[str]]:
    """Detect home directory prefix(es) from folder names.

    Folder names in ~/.claude/projects/ encode filesystem paths by replacing
    '/' with '-'. E.g., /home/nsitaram/swyfft -> -home-nsitaram-swyfft.

    Returns dict mapping each detected prefix to its list of folder names.
    """
    prefixes: dict[str, list[str]] = {}
    for name in folder_names:
        parts = name.split("-")
        # Folder names start with '-', so parts[0] is empty
        # Typical: ['', 'home', 'nsitaram', 'project', 'name']
        if len(parts) >= 3:
            prefix = f"-{parts[1]}-{parts[2]}-"
            prefixes.setdefault(prefix, []).append(name)
    return prefixes


def _get_current_prefix() -> str:
    """Get the current machine's folder name prefix."""
    home = Path.home()
    return "-" + str(home).lstrip("/").replace("/", "-") + "-"


def import_sessions(
    source_path: str | Path,
    target_projects_dir: Path | None = None,
) -> ImportResult:
    """Import sessions from an external directory.

    Args:
        source_path: Path to source projects directory (e.g., backup/projects/)
        target_projects_dir: Override target (default: ~/.claude/projects/)

    Returns:
        ImportResult with counts and any mismatches.
    """
    source = Path(source_path)
    target = target_projects_dir or get_projects_dir()
    result = ImportResult()

    if not source.exists() or not source.is_dir():
        return result

    # Collect source folders that have .jsonl files
    source_folders = []
    for item in sorted(source.iterdir()):
        if item.is_dir() and list(item.glob("*.jsonl")):
            source_folders.append(item)

    if not source_folders:
        return result

    current_prefix = _get_current_prefix()
    source_prefixes = detect_prefixes([f.name for f in source_folders])

    # Build existing target folders for fuzzy matching by last segment
    existing_targets: dict[str, str] = {}
    if target.exists():
        for t in target.iterdir():
            if t.is_dir():
                parts = t.name.rsplit("-", 1)
                suffix = parts[-1] if len(parts) > 1 else t.name
                existing_targets[suffix] = t.name

    seen_projects: set[str] = set()

    for src_prefix, folder_names in source_prefixes.items():
        for folder_name in folder_names:
            src_folder = source / folder_name

            # Remap prefix
            if src_prefix == current_prefix:
                target_name = folder_name
            else:
                suffix = folder_name[len(src_prefix):]
                target_name = current_prefix + suffix

            # Check if target folder exists; if not, try fuzzy match
            target_folder = target / target_name
            if not target_folder.exists():
                project_suffix = folder_name.rsplit("-", 1)[-1]
                if project_suffix in existing_targets:
                    target_name = existing_targets[project_suffix]
                    target_folder = target / target_name
                    result.mismatches.append(
                        f"{folder_name} -> {target_name} (fuzzy match by '{project_suffix}')"
                    )

            target_folder.mkdir(parents=True, exist_ok=True)
            seen_projects.add(target_name)

            # Copy .jsonl files with dedup
            for jsonl_file in src_folder.glob("*.jsonl"):
                target_file = target_folder / jsonl_file.name
                if target_file.exists():
                    result.skipped += 1
                    continue

                shutil.copy2(jsonl_file, target_file)  # preserves mtime
                result.copied += 1

    result.projects = len(seen_projects)
    return result
