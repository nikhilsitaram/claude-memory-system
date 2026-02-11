#!/usr/bin/env python3
"""
Project lifecycle management for Claude Code Memory System.

This is a LIBRARY, not a CLI tool. Functions are called by the /projects skill
through Python code blocks. Each function returns structured data for Claude
to interpret and present to the user.

All destructive operations require explicit confirmation parameter.

Usage (from Claude Code):
    import sys
    sys.path.insert(0, str(Path.home() / ".claude/scripts"))
    from project_manager import list_projects, find_orphaned_folders, ...
"""

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# Add scripts directory to path for local imports
script_dir = Path(__file__).parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

from memory_utils import (
    get_claude_dir,
    get_memory_dir,
    get_projects_dir,
    get_projects_index_file,
    get_project_memory_dir,
    load_json_file,
    save_json_file,
    project_name_to_filename,
    FileLock,
)

# Claude Code subdirectories that contain project-specific data
CLAUDE_SUBDIRS = ["projects", "file-history", "todos", "shell-snapshots", "debug"]


# =============================================================================
# Data Classes (for structured returns)
# =============================================================================


@dataclass
class ProjectStatus:
    """Status information for a project."""
    name: str
    path: str  # Canonical (lowercase) path used as index key
    original_path: str  # Original path as stored
    exists: bool  # Whether the directory exists on disk
    work_days: list[str]
    has_memory_file: bool
    memory_file_path: Optional[str]
    encoded_folders: list[str]  # Claude Code folder names
    issues: list[str] = field(default_factory=list)


@dataclass
class OrphanInfo:
    """Information about an orphaned Claude Code folder."""
    folder_name: str
    folder_path: str
    decoded_path: Optional[str]  # Best-effort decode (may be lossy)
    sessions_index_path: Optional[str]  # If sessions-index.json exists
    original_path_from_index: Optional[str]  # Authoritative path from sessions-index
    subdirs: list[str]  # Which CLAUDE_SUBDIRS exist
    file_count: int
    total_size_bytes: int


@dataclass
class ValidationResult:
    """Result of validating an operation."""
    valid: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class OperationPlan:
    """Detailed plan of what an operation will do."""
    operation: str  # "move", "merge-orphan", "cleanup"
    backups: list[str]  # Files that will be backed up
    moves: list[tuple[str, str]]  # (from, to) pairs
    merges: list[tuple[str, str]]  # (source, dest) pairs
    renames: list[tuple[str, str]]  # (old_name, new_name) for safety renames
    index_changes: dict[str, Any]  # Changes to projects-index.json
    summary: str  # Human-readable summary


# =============================================================================
# Path Encoding/Decoding
# =============================================================================


def encode_path(path: str) -> str:
    """
    Convert filesystem path to Claude Code's encoded folder name.

    Claude Code replaces / with - and . with - to create folder names.

    Example: /home/user/my-project -> -home-user-my-project

    Note: This encoding is LOSSY - you cannot reliably decode back to the
    original path. Always use sessions-index.json for authoritative paths.
    """
    # Match Claude Code's encoding exactly
    return path.replace("/", "-").replace(".", "-")


def decode_path_best_effort(encoded: str) -> str:
    """
    Best-effort decode of an encoded folder name back to a path.

    .. deprecated::
        This function is LOSSY and should NOT be used for path reconstruction.
        Use get_original_path_from_folder() to read sessions-index.json instead.
        This function exists only for display/debugging when no index is available.

    WARNING: This is LOSSY and should only be used for display purposes.
    For authoritative paths, always read from sessions-index.json.

    The encoding replaces both / and . with -, so we cannot distinguish:
    - /home/user/.config -> -home-user--config
    - /home/user/-config -> -home-user--config (if such path existed)

    This function assumes leading - is a /, and other - are / unless doubled.
    """
    if not encoded:
        return ""

    # Leading - is the root /
    if encoded.startswith("-"):
        result = "/" + encoded[1:]
    else:
        result = encoded

    # Replace - with / (best effort - may be wrong for paths with actual hyphens)
    result = result.replace("-", "/")

    return result


def get_original_path_from_folder(folder_path: Path) -> Optional[str]:
    """
    Get the authoritative original path from a Claude Code project folder.

    Checks sessions-index.json for:
    1. Root-level 'originalPath' (legacy/manual format)
    2. entries[0].projectPath (Claude Code's actual format)

    This is the only reliable way to get the original path since encoding is lossy.
    """
    sessions_index = folder_path / "sessions-index.json"
    if sessions_index.exists():
        try:
            data = json.loads(sessions_index.read_text(encoding="utf-8"))
            # Try root-level originalPath first (legacy format)
            if data.get("originalPath"):
                return data.get("originalPath")
            # Fall back to entries[0].projectPath (Claude Code format)
            entries = data.get("entries", [])
            if entries and entries[0].get("projectPath"):
                return entries[0]["projectPath"]
        except (json.JSONDecodeError, IOError):
            pass
    return None


# =============================================================================
# Discovery Functions
# =============================================================================


def list_projects() -> list[ProjectStatus]:
    """
    Get status of all projects in the memory system index.

    Returns a list of ProjectStatus objects with health information.
    """
    index_file = get_projects_index_file()
    index = load_json_file(index_file, {"projects": {}})
    projects_data = index.get("projects", {})
    project_memory_dir = get_project_memory_dir()

    results = []

    for canonical_path, data in projects_data.items():
        name = data.get("name", "unknown")
        original_path = data.get("originalPath", canonical_path)
        work_days = data.get("workDays", [])
        encoded_paths = data.get("encodedPaths", [])

        # Check if directory exists
        exists = Path(original_path).exists()

        # Check for memory file
        memory_filename = project_name_to_filename(name)
        memory_file = project_memory_dir / memory_filename
        has_memory_file = memory_file.exists()

        # Build issues list
        issues = []
        if not exists:
            issues.append(f"path missing: {original_path}")

        status = ProjectStatus(
            name=name,
            path=canonical_path,
            original_path=original_path,
            exists=exists,
            work_days=work_days,
            has_memory_file=has_memory_file,
            memory_file_path=str(memory_file) if has_memory_file else None,
            encoded_folders=encoded_paths,
            issues=issues,
        )
        results.append(status)

    return results


def find_orphaned_folders() -> list[OrphanInfo]:
    """
    Find Claude Code folders that don't match any valid project in the index.

    An "orphan" is a folder in ~/.claude/projects/ where:
    1. The originalPath in sessions-index.json doesn't exist on disk, OR
    2. The folder is not tracked in our projects-index.json

    Returns information about each orphan for the skill to present to the user.
    """
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return []

    # Load our index to know which folders are tracked
    index_file = get_projects_index_file()
    index = load_json_file(index_file, {"projects": {}})

    # Build set of all tracked encoded paths
    tracked_encoded: set[str] = set()
    for data in index.get("projects", {}).values():
        tracked_encoded.update(data.get("encodedPaths", []))

    orphans = []

    for folder in projects_dir.iterdir():
        if not folder.is_dir():
            continue

        folder_name = folder.name

        # Get authoritative original path from sessions-index.json
        original_path = get_original_path_from_folder(folder)

        # Determine if this is an orphan
        # First check: if folder is tracked in our index, it's not an orphan
        # (handles renames where sessions-index.json still has old path)
        if folder_name in tracked_encoded:
            continue

        is_orphan = False
        if original_path:
            # Has sessions-index.json - check if path exists on disk
            if not Path(original_path).exists():
                is_orphan = True
        else:
            # No sessions-index.json and not tracked - orphan
            is_orphan = True

        if not is_orphan:
            continue

        # Gather information about the orphan
        subdirs = [
            subdir
            for subdir in CLAUDE_SUBDIRS
            if (get_claude_dir() / subdir / folder_name).exists()
        ]

        # Count files and size in the projects folder
        file_count = 0
        total_size = 0
        for item in folder.rglob("*"):
            if item.is_file():
                file_count += 1
                try:
                    total_size += item.stat().st_size
                except OSError:
                    pass

        sessions_index_path = folder / "sessions-index.json"

        orphan = OrphanInfo(
            folder_name=folder_name,
            folder_path=str(folder),
            decoded_path=decode_path_best_effort(folder_name) if not original_path else None,
            sessions_index_path=str(sessions_index_path) if sessions_index_path.exists() else None,
            original_path_from_index=original_path,
            subdirs=subdirs,
            file_count=file_count,
            total_size_bytes=total_size,
        )
        orphans.append(orphan)

    return orphans


def find_stale_entries() -> list[dict]:
    """
    Find index entries where originalPath doesn't exist on disk.

    Returns list of dicts with entry details for reporting.
    """
    index_file = get_projects_index_file()
    index = load_json_file(index_file, {"projects": {}})
    projects_data = index.get("projects", {})

    stale = []

    for canonical_path, data in projects_data.items():
        original_path = data.get("originalPath", canonical_path)
        if not Path(original_path).exists():
            stale.append({
                "canonical_path": canonical_path,
                "original_path": original_path,
                "name": data.get("name", "unknown"),
                "work_days": data.get("workDays", []),
                "encoded_paths": data.get("encodedPaths", []),
            })

    return stale


# =============================================================================
# Validation Functions
# =============================================================================


def validate_move(old_path: Path, new_path: Path) -> ValidationResult:
    """
    Validate that a move operation can succeed.

    Checks for:
    - Source exists and is a directory
    - Destination parent exists
    - Write permissions
    - Potential conflicts at destination
    """
    issues = []
    warnings = []

    # Check source
    if not old_path.exists():
        issues.append(f"Source does not exist: {old_path}")
    elif not old_path.is_dir():
        issues.append(f"Source is not a directory: {old_path}")

    # Check destination parent
    if not new_path.parent.exists():
        issues.append(f"Destination parent does not exist: {new_path.parent}")

    # Check if destination exists
    if new_path.exists():
        warnings.append(f"Destination already exists: {new_path}")
        warnings.append("Will need to choose: merge, clean, or abort")

    # Check write permissions
    claude_dir = get_claude_dir()
    if claude_dir.exists() and not os.access(claude_dir, os.W_OK):
        issues.append(f"No write permission to Claude directory: {claude_dir}")

    memory_dir = get_memory_dir()
    if memory_dir.exists() and not os.access(memory_dir, os.W_OK):
        issues.append(f"No write permission to memory directory: {memory_dir}")

    return ValidationResult(
        valid=len(issues) == 0,
        issues=issues,
        warnings=warnings,
    )


def validate_merge_orphan(orphan_name: str, target_path: Path) -> ValidationResult:
    """
    Validate that an orphan merge operation can succeed.

    Checks for:
    - Orphan folder exists
    - Target directory exists
    - Target has Claude Code data to merge into
    """
    issues = []
    warnings = []

    projects_dir = get_projects_dir()
    orphan_folder = projects_dir / orphan_name

    # Check orphan exists
    if not orphan_folder.exists():
        issues.append(f"Orphan folder does not exist: {orphan_folder}")
    elif not orphan_folder.is_dir():
        issues.append(f"Orphan path is not a directory: {orphan_folder}")

    # Check target exists
    if not target_path.exists():
        issues.append(f"Target directory does not exist: {target_path}")
    elif not target_path.is_dir():
        issues.append(f"Target is not a directory: {target_path}")

    # Check target has Claude Code data
    target_encoded = encode_path(str(target_path))
    target_folder = projects_dir / target_encoded
    if not target_folder.exists():
        warnings.append(f"Target has no existing Claude Code data at: {target_folder}")
        warnings.append("Orphan data will become the only data for this project")

    return ValidationResult(
        valid=len(issues) == 0,
        issues=issues,
        warnings=warnings,
    )


# =============================================================================
# Planning Functions
# =============================================================================


def plan_move(old_path: Path, new_path: Path, merge_mode: str = "merge") -> OperationPlan:
    """
    Generate detailed plan for a move operation.

    Args:
        old_path: Current project directory
        new_path: New location for project
        merge_mode: How to handle conflicts ("merge", "clean", "abort")
    """
    old_encoded = encode_path(str(old_path))
    new_encoded = encode_path(str(new_path))

    backups = []
    moves = []
    merges = []
    renames = []
    index_changes = {}

    # Plan backup of index files
    index_file = get_projects_index_file()
    if index_file.exists():
        backups.append(str(index_file))

    history_file = get_claude_dir() / "history.jsonl"
    if history_file.exists():
        backups.append(str(history_file))

    # Check for memory file
    project_name = old_path.name
    memory_filename = project_name_to_filename(project_name)
    old_memory_file = get_project_memory_dir() / memory_filename
    if old_memory_file.exists():
        backups.append(str(old_memory_file))

    # Plan Claude Code folder moves/merges
    projects_dir = get_projects_dir()
    old_project_folder = projects_dir / old_encoded
    new_project_folder = projects_dir / new_encoded

    if old_project_folder.exists():
        if new_project_folder.exists():
            if merge_mode == "merge":
                merges.append((str(old_project_folder), str(new_project_folder)))
            elif merge_mode == "clean":
                # Will delete new first
                moves.append((str(old_project_folder), str(new_project_folder)))
        else:
            moves.append((str(old_project_folder), str(new_project_folder)))

    # Plan other Claude subdirectory moves
    for subdir in CLAUDE_SUBDIRS:
        if subdir == "projects":
            continue  # Already handled above
        subdir_path = get_claude_dir() / subdir
        old_subdir = subdir_path / old_encoded
        new_subdir = subdir_path / new_encoded
        if old_subdir.exists():
            if new_subdir.exists() and merge_mode == "merge":
                merges.append((str(old_subdir), str(new_subdir)))
            else:
                moves.append((str(old_subdir), str(new_subdir)))

    # Plan index changes
    index = load_json_file(index_file, {"projects": {}})
    old_canonical = str(old_path).lower()
    if old_canonical in index.get("projects", {}):
        index_changes["remove"] = old_canonical
        index_changes["add"] = {
            "path": str(new_path).lower(),
            "data": {
                "name": new_path.name,
                "originalPath": str(new_path),
                "encodedPaths": [new_encoded],
                "workDays": index["projects"][old_canonical].get("workDays", []),
            }
        }

    # Build summary
    summary_parts = [
        f"Move project from {old_path} to {new_path}",
        f"Backup {len(backups)} files before changes",
    ]
    if moves:
        summary_parts.append(f"Move {len(moves)} Claude Code folders")
    if merges:
        summary_parts.append(f"Merge {len(merges)} existing folders")
    if index_changes:
        summary_parts.append("Update projects-index.json")

    return OperationPlan(
        operation="move",
        backups=backups,
        moves=moves,
        merges=merges,
        renames=renames,
        index_changes=index_changes,
        summary="\n".join(summary_parts),
    )


def plan_merge_orphan(orphan_name: str, target_path: Path) -> OperationPlan:
    """
    Generate detailed plan for merging orphaned data into an existing project.

    This is for when the source directory no longer exists but Claude Code
    folders remain with the old data.
    """
    target_encoded = encode_path(str(target_path))

    backups = []
    moves = []
    merges = []
    renames = []
    index_changes = {}

    projects_dir = get_projects_dir()
    orphan_folder = projects_dir / orphan_name
    target_folder = projects_dir / target_encoded

    # Plan backups
    index_file = get_projects_index_file()
    if index_file.exists():
        backups.append(str(index_file))

    history_file = get_claude_dir() / "history.jsonl"
    if history_file.exists():
        backups.append(str(history_file))

    # Check for orphan's memory file (need to detect project name)
    original_path = get_original_path_from_folder(orphan_folder)
    if original_path:
        orphan_project_name = Path(original_path).name
        orphan_memory_filename = project_name_to_filename(orphan_project_name)
        orphan_memory_file = get_project_memory_dir() / orphan_memory_filename
        if orphan_memory_file.exists():
            backups.append(str(orphan_memory_file))

    # Check for target's memory file
    target_project_name = target_path.name
    target_memory_filename = project_name_to_filename(target_project_name)
    target_memory_file = get_project_memory_dir() / target_memory_filename
    if target_memory_file.exists():
        backups.append(str(target_memory_file))

    # Plan folder operations
    if orphan_folder.exists():
        if target_folder.exists():
            merges.append((str(orphan_folder), str(target_folder)))
        else:
            moves.append((str(orphan_folder), str(target_folder)))

        # Safety rename after merge
        renames.append((str(orphan_folder), f"{orphan_folder}.merged.bak"))

    # Plan other Claude subdirectory merges
    for subdir in CLAUDE_SUBDIRS:
        if subdir == "projects":
            continue
        subdir_path = get_claude_dir() / subdir
        orphan_subdir = subdir_path / orphan_name
        target_subdir = subdir_path / target_encoded
        if orphan_subdir.exists():
            if target_subdir.exists():
                merges.append((str(orphan_subdir), str(target_subdir)))
            else:
                moves.append((str(orphan_subdir), str(target_subdir)))
            renames.append((str(orphan_subdir), f"{orphan_subdir}.merged.bak"))

    # Plan index changes
    index = load_json_file(index_file, {"projects": {}})

    # Find orphan's entry by encoded path
    orphan_entry = None
    orphan_canonical = None
    for canonical_path, data in index.get("projects", {}).items():
        if orphan_name in data.get("encodedPaths", []):
            orphan_entry = data
            orphan_canonical = canonical_path
            break

    if orphan_entry:
        index_changes["remove"] = orphan_canonical
        # Merge work days into target
        target_canonical = str(target_path).lower()
        if target_canonical in index.get("projects", {}):
            existing_days = set(index["projects"][target_canonical].get("workDays", []))
            orphan_days = set(orphan_entry.get("workDays", []))
            index_changes["merge_work_days"] = {
                "target": target_canonical,
                "days_to_add": sorted(orphan_days - existing_days),
            }

    # Build summary
    summary_parts = [
        f"Merge orphaned project data into {target_path}",
        f"Orphan folder: {orphan_name}",
        f"Backup {len(backups)} files before changes",
    ]
    if moves:
        summary_parts.append(f"Move {len(moves)} folders (no existing target)")
    if merges:
        summary_parts.append(f"Merge {len(merges)} folders with existing data")
    if renames:
        summary_parts.append(f"Rename {len(renames)} orphan folders to .merged.bak (not deleted)")
    if index_changes.get("merge_work_days"):
        days = index_changes["merge_work_days"]["days_to_add"]
        summary_parts.append(f"Add {len(days)} work days to target project")

    return OperationPlan(
        operation="merge-orphan",
        backups=backups,
        moves=moves,
        merges=merges,
        renames=renames,
        index_changes=index_changes,
        summary="\n".join(summary_parts),
    )


def plan_cleanup() -> OperationPlan:
    """
    Generate plan for cleanup operation (removing stale index entries).

    Note: Cleanup only modifies the index, it does NOT delete any folders.
    The user should manually delete folders after reviewing.
    """
    stale = find_stale_entries()

    backups = []
    index_changes = {}

    if stale:
        index_file = get_projects_index_file()
        if index_file.exists():
            backups.append(str(index_file))

        index_changes["remove_entries"] = [entry["canonical_path"] for entry in stale]

    summary_parts = [
        f"Found {len(stale)} stale index entries",
    ]
    for entry in stale:
        summary_parts.append(f"  - {entry['name']}: {entry['original_path']} ({len(entry['work_days'])} work days)")

    if stale:
        summary_parts.append("")
        summary_parts.append("This will remove entries from projects-index.json only.")
        summary_parts.append("Orphaned folders (if any) will NOT be deleted - review manually.")

    return OperationPlan(
        operation="cleanup",
        backups=backups,
        moves=[],
        merges=[],
        renames=[],
        index_changes=index_changes,
        summary="\n".join(summary_parts),
    )


# =============================================================================
# Execution Helpers
# =============================================================================


def backup_files(files: list[Path]) -> Path:
    """
    Create timestamped backup directory with copies of all files.

    Returns the backup directory path.
    """
    backup_dir = get_memory_dir() / ".backups" / datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir.mkdir(parents=True, exist_ok=True)

    for f in files:
        f = Path(f)
        if f.exists():
            # Preserve relative structure for nested files
            dest = backup_dir / f.name
            shutil.copy2(f, dest)

    return backup_dir


def restore_from_backup(backup_dir: Path) -> dict:
    """
    Restore files from a backup directory created by this tool.

    Returns {"success": bool, "restored": list[str], "message": str}
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.exists():
        return {
            "success": False,
            "restored": [],
            "message": f"Backup directory not found: {backup_dir}"
        }

    restored = []

    for backup_file in backup_dir.iterdir():
        if not backup_file.is_file():
            continue

        # Determine original location based on filename
        if backup_file.name == "projects-index.json":
            dest = get_projects_index_file()
        elif backup_file.name.endswith("-long-term-memory.md"):
            dest = get_project_memory_dir() / backup_file.name
        elif backup_file.name == "history.jsonl":
            dest = get_claude_dir() / "history.jsonl"
        else:
            continue

        try:
            shutil.copy2(backup_file, dest)
            restored.append(str(dest))
        except IOError as e:
            return {
                "success": False,
                "restored": restored,
                "message": f"Failed to restore {backup_file.name}: {e}"
            }

    return {
        "success": True,
        "restored": restored,
        "message": f"Restored {len(restored)} files from {backup_dir}"
    }


def rebuild_sessions_index(folder: Path, project_path: str) -> dict:
    """
    Rebuild sessions-index.json from .jsonl files in a folder.

    Scans all .jsonl files and creates entries with basic metadata.
    Returns the rebuilt index data.
    """
    entries = []

    for jsonl_file in sorted(folder.glob("*.jsonl")):
        session_id = jsonl_file.stem
        try:
            stat = jsonl_file.stat()
        except OSError:
            continue

        mtime_ms = int(stat.st_mtime * 1000)
        created_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)

        # Try to extract first prompt for context
        first_prompt = "(recovered session)"
        try:
            with open(jsonl_file, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        msg = json.loads(line)
                        if msg.get("type") == "human":
                            content = msg.get("message", {}).get("content", "")
                            first_prompt = content[:150] if content else "(no prompt)"
                            break
                    except json.JSONDecodeError:
                        continue
        except IOError:
            pass

        entries.append({
            "sessionId": session_id,
            "fullPath": str(jsonl_file),
            "fileMtime": mtime_ms,
            "firstPrompt": first_prompt,
            "summary": "(recovered session)",
            "messageCount": 0,
            "created": created_dt.isoformat().replace("+00:00", "Z"),
            "modified": created_dt.isoformat().replace("+00:00", "Z"),
            "gitBranch": "",
            "projectPath": project_path,
            "isSidechain": False,
        })

    # Sort by created date
    entries.sort(key=lambda e: e["created"])

    return {
        "version": 1,
        "entries": entries,
        "originalPath": project_path,
    }


def merge_sessions_index(source: Path, dest: Path, project_path: Optional[str] = None) -> int:
    """
    Merge sessions-index.json files.

    Combines entries, dedupes by session ID, keeps newest for conflicts.
    If source has no entries, scans source folder for .jsonl files.
    Returns number of entries merged from source.
    """
    source_data = load_json_file(source, {})
    dest_data = load_json_file(dest, {})

    # If source has no entries, try to rebuild from .jsonl files
    source_entries = source_data.get("entries", [])
    if not source_entries and source.parent.is_dir():
        rebuilt = rebuild_sessions_index(
            source.parent,
            project_path or dest_data.get("originalPath", "")
        )
        source_entries = rebuilt.get("entries", [])

    if not source_entries:
        return 0

    # Build lookup of existing entries by session ID
    # Note: Claude Code uses "sessionId", not "id"
    dest_entries = {
        e.get("sessionId", e.get("id")): e
        for e in dest_data.get("entries", [])
        if e.get("sessionId") or e.get("id")
    }

    merged_count = 0

    for entry in source_entries:
        session_id = entry.get("sessionId", entry.get("id"))
        if not session_id:
            continue

        if session_id in dest_entries:
            # Keep newer entry
            existing = dest_entries[session_id]
            existing_time = existing.get("modified", existing.get("lastActive", existing.get("created", "")))
            new_time = entry.get("modified", entry.get("lastActive", entry.get("created", "")))
            if new_time > existing_time:
                dest_entries[session_id] = entry
                merged_count += 1
        else:
            dest_entries[session_id] = entry
            merged_count += 1

    # Rebuild entries list sorted by modified/created (newest first)
    dest_data["entries"] = sorted(
        dest_entries.values(),
        key=lambda e: e.get("modified", e.get("lastActive", e.get("created", ""))),
        reverse=True,
    )

    # Ensure version is set
    dest_data["version"] = 1

    # Keep the dest's originalPath as it's the valid one

    save_json_file(dest, dest_data)
    return merged_count


def rewrite_paths_in_file(filepath: Path, old_path: str, new_path: str) -> int:
    """
    Replace old_path with new_path in a file (streaming for large files).

    Returns replacement count.
    """
    filepath = Path(filepath)
    if not filepath.exists():
        return 0

    # For small files, do in-memory replacement
    try:
        size = filepath.stat().st_size
        if size < 10 * 1024 * 1024:  # 10MB
            content = filepath.read_text(encoding="utf-8")
            count = content.count(old_path)
            if count > 0:
                new_content = content.replace(old_path, new_path)
                filepath.write_text(new_content, encoding="utf-8")
            return count
    except (IOError, UnicodeDecodeError):
        pass

    # For large files, stream through
    temp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    count = 0

    try:
        with open(filepath, "r", encoding="utf-8") as infile, \
             open(temp_path, "w", encoding="utf-8") as outfile:
            for line in infile:
                line_count = line.count(old_path)
                if line_count > 0:
                    line = line.replace(old_path, new_path)
                    count += line_count
                outfile.write(line)

        # Replace original with temp
        temp_path.replace(filepath)
    except IOError:
        if temp_path.exists():
            temp_path.unlink()
        raise

    return count


def get_memory_files_for_merge(source_name: str, dest_name: str) -> dict:
    """
    Get content of memory files that need merging.

    Returns dict with source_content, dest_content, paths, and existence flags.
    Claude uses this to perform intelligent merge and write result.
    """
    project_memory_dir = get_project_memory_dir()

    source_filename = project_name_to_filename(source_name)
    dest_filename = project_name_to_filename(dest_name)

    source_file = project_memory_dir / source_filename
    dest_file = project_memory_dir / dest_filename

    source_content = ""
    dest_content = ""

    if source_file.exists():
        try:
            source_content = source_file.read_text(encoding="utf-8")
        except IOError:
            pass

    if dest_file.exists():
        try:
            dest_content = dest_file.read_text(encoding="utf-8")
        except IOError:
            pass

    return {
        "source_name": source_name,
        "dest_name": dest_name,
        "source_content": source_content,
        "dest_content": dest_content,
        "source_path": str(source_file),
        "dest_path": str(dest_file),
        "source_exists": source_file.exists(),
        "dest_exists": dest_file.exists(),
    }


def update_session_index_paths(old_path: str, new_path: str) -> int:
    """
    Update originalPath in sessions-index.json files across all affected project folders.

    When a project directory is renamed (e.g., ~/claude-code -> ~/swyfft),
    the sessions-index.json files inside ~/.claude/projects/ still reference
    the old path. This function updates them so find_orphaned_folders() doesn't
    flag them as orphans.

    Also updates sub-project folders (e.g., ~/claude-code/projects/foo).

    Returns the number of files updated.
    """
    projects_dir = get_projects_dir()
    if not projects_dir.exists():
        return 0

    updated = 0
    for folder in projects_dir.iterdir():
        if not folder.is_dir():
            continue
        sessions_file = folder / "sessions-index.json"
        if not sessions_file.exists():
            continue
        try:
            content = sessions_file.read_text(encoding="utf-8")
            if old_path in content:
                sessions_file.write_text(
                    content.replace(old_path, new_path), encoding="utf-8"
                )
                updated += 1
        except (IOError, UnicodeDecodeError):
            continue

    return updated


# =============================================================================
# Execution Functions
# =============================================================================


def execute_move(
    old_path: Path,
    new_path: Path,
    merge_mode: str = "merge",
    confirmed: bool = False,
) -> dict:
    """
    Execute full project move.

    Returns {"success": bool, "message": str, "backup_path": str}
    """
    old_path = Path(old_path)
    new_path = Path(new_path)

    if not confirmed:
        return {
            "success": False,
            "message": "Operation requires confirmation. Pass confirmed=True to execute.",
            "backup_path": None,
        }

    # Validate first
    validation = validate_move(old_path, new_path)
    if not validation.valid:
        return {
            "success": False,
            "message": f"Validation failed: {'; '.join(validation.issues)}",
            "backup_path": None,
        }

    plan = plan_move(old_path, new_path, merge_mode)

    # Use lock for thread safety
    lock_path = get_memory_dir() / ".project_manager.lock"
    try:
        with FileLock(lock_path, timeout=30):
            # Create backup
            backup_path = backup_files([Path(f) for f in plan.backups])

            old_encoded = encode_path(str(old_path))
            new_encoded = encode_path(str(new_path))

            # Execute merges
            for source, dest in plan.merges:
                source_path = Path(source)
                dest_path = Path(dest)

                # Merge sessions-index.json if both exist
                source_sessions = source_path / "sessions-index.json"
                dest_sessions = dest_path / "sessions-index.json"
                if source_sessions.exists() and dest_sessions.exists():
                    merge_sessions_index(source_sessions, dest_sessions)

                # Copy all other files from source to dest
                for item in source_path.iterdir():
                    if item.name == "sessions-index.json":
                        continue  # Already merged
                    dest_item = dest_path / item.name
                    if item.is_file():
                        shutil.copy2(item, dest_item)
                    elif item.is_dir() and not dest_item.exists():
                        shutil.copytree(item, dest_item)

                # Remove source after merge
                shutil.rmtree(source_path)

            # Execute moves
            for source, dest in plan.moves:
                source_path = Path(source)
                dest_path = Path(dest)
                if dest_path.exists() and merge_mode == "clean":
                    shutil.rmtree(dest_path)
                if source_path.exists():
                    shutil.move(str(source_path), str(dest_path))

            # Update projects-index.json
            index_file = get_projects_index_file()
            index = load_json_file(index_file, {"projects": {}})

            if plan.index_changes.get("remove"):
                old_canonical = plan.index_changes["remove"]
                if old_canonical in index.get("projects", {}):
                    del index["projects"][old_canonical]

            if plan.index_changes.get("add"):
                add_data = plan.index_changes["add"]
                index["projects"][add_data["path"]] = add_data["data"]

            index["lastUpdated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            save_json_file(index_file, index)

            # Rewrite paths in history.jsonl
            history_file = get_claude_dir() / "history.jsonl"
            if history_file.exists():
                rewrite_paths_in_file(history_file, str(old_path), str(new_path))

            # Update sessions-index.json files in all affected project folders
            update_session_index_paths(str(old_path), str(new_path))

            # Move the actual project directory
            if old_path.exists() and not new_path.exists():
                shutil.move(str(old_path), str(new_path))

            return {
                "success": True,
                "message": f"Successfully moved project from {old_path} to {new_path}",
                "backup_path": str(backup_path),
            }

    except TimeoutError:
        return {
            "success": False,
            "message": "Could not acquire lock. Another operation may be in progress.",
            "backup_path": None,
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error during move: {e}",
            "backup_path": str(backup_path) if "backup_path" in locals() else None,
        }


def execute_merge_orphan(
    orphan_name: str,
    target_path: Path,
    confirmed: bool = False,
) -> dict:
    """
    Merge orphaned project data into an existing project.

    After merging, orphan folders are RENAMED to {folder}.merged.bak
    (not deleted) for safety. User can manually delete later.

    Returns {"success": bool, "message": str, "backup_path": str, "renamed_folders": list}
    """
    target_path = Path(target_path)

    if not confirmed:
        return {
            "success": False,
            "message": "Operation requires confirmation. Pass confirmed=True to execute.",
            "backup_path": None,
            "renamed_folders": [],
        }

    # Validate
    validation = validate_merge_orphan(orphan_name, target_path)
    if not validation.valid:
        return {
            "success": False,
            "message": f"Validation failed: {'; '.join(validation.issues)}",
            "backup_path": None,
            "renamed_folders": [],
        }

    plan = plan_merge_orphan(orphan_name, target_path)

    lock_path = get_memory_dir() / ".project_manager.lock"
    renamed_folders = []

    try:
        with FileLock(lock_path, timeout=30):
            # Create backup
            backup_path = backup_files([Path(f) for f in plan.backups])

            projects_dir = get_projects_dir()
            target_encoded = encode_path(str(target_path))

            # Get original path from orphan for memory file lookup
            orphan_folder = projects_dir / orphan_name
            orphan_original_path = get_original_path_from_folder(orphan_folder)
            orphan_project_name = Path(orphan_original_path).name if orphan_original_path else None

            # Execute merges
            for source, dest in plan.merges:
                source_path = Path(source)
                dest_path = Path(dest)

                # Copy .jsonl files and subdirs first
                for item in source_path.iterdir():
                    if item.name == "sessions-index.json":
                        continue
                    dest_item = dest_path / item.name
                    if item.is_file() and not dest_item.exists():
                        shutil.copy2(item, dest_item)
                    elif item.is_dir() and not dest_item.exists():
                        shutil.copytree(item, dest_item)

                # Now merge sessions-index.json (will scan .jsonl files if entries missing)
                source_sessions = source_path / "sessions-index.json"
                dest_sessions = dest_path / "sessions-index.json"
                if source_sessions.exists():
                    if dest_sessions.exists():
                        merge_sessions_index(source_sessions, dest_sessions, str(target_path))
                    else:
                        # Copy sessions-index but update originalPath
                        data = load_json_file(source_sessions, {})
                        data["originalPath"] = str(target_path)
                        save_json_file(dest_sessions, data)

            # After all merges, rebuild the target's sessions-index to ensure all .jsonl files are indexed
            target_folder = projects_dir / target_encoded
            if target_folder.exists():
                rebuilt = rebuild_sessions_index(target_folder, str(target_path))
                target_sessions = target_folder / "sessions-index.json"
                # Merge rebuilt entries with any existing entries
                if target_sessions.exists():
                    existing = load_json_file(target_sessions, {})
                    existing_ids = {e.get("sessionId") for e in existing.get("entries", [])}
                    for entry in rebuilt.get("entries", []):
                        if entry.get("sessionId") not in existing_ids:
                            existing.setdefault("entries", []).append(entry)
                    existing["entries"].sort(
                        key=lambda e: e.get("modified", e.get("created", "")),
                        reverse=True
                    )
                    existing["originalPath"] = str(target_path)
                    save_json_file(target_sessions, existing)
                else:
                    save_json_file(target_sessions, rebuilt)

            # Execute moves
            for source, dest in plan.moves:
                source_path = Path(source)
                dest_path = Path(dest)
                if source_path.exists():
                    dest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(source_path), str(dest_path))

                    # Rebuild sessions-index.json with correct path and all .jsonl entries
                    dest_sessions = dest_path / "sessions-index.json"
                    rebuilt = rebuild_sessions_index(dest_path, str(target_path))
                    if dest_sessions.exists():
                        # Merge with existing entries
                        existing = load_json_file(dest_sessions, {})
                        existing_ids = {e.get("sessionId") for e in existing.get("entries", [])}
                        for entry in rebuilt.get("entries", []):
                            if entry.get("sessionId") not in existing_ids:
                                existing.setdefault("entries", []).append(entry)
                        existing["entries"].sort(
                            key=lambda e: e.get("modified", e.get("created", "")),
                            reverse=True
                        )
                        existing["originalPath"] = str(target_path)
                        save_json_file(dest_sessions, existing)
                    else:
                        save_json_file(dest_sessions, rebuilt)

            # Execute safety renames
            for old_name, new_name in plan.renames:
                old_path_rename = Path(old_name)
                new_path_rename = Path(new_name)
                if old_path_rename.exists():
                    old_path_rename.rename(new_path_rename)
                    renamed_folders.append(str(new_path_rename))

            # Update projects-index.json
            index_file = get_projects_index_file()
            index = load_json_file(index_file, {"projects": {}})

            if plan.index_changes.get("remove"):
                orphan_canonical = plan.index_changes["remove"]
                if orphan_canonical in index.get("projects", {}):
                    del index["projects"][orphan_canonical]

            if plan.index_changes.get("merge_work_days"):
                merge_info = plan.index_changes["merge_work_days"]
                target_canonical = merge_info["target"]
                if target_canonical in index.get("projects", {}):
                    existing_days = set(index["projects"][target_canonical].get("workDays", []))
                    existing_days.update(merge_info["days_to_add"])
                    index["projects"][target_canonical]["workDays"] = sorted(existing_days)

                    # Add orphan's encoded path to target's list
                    encoded_paths = index["projects"][target_canonical].get("encodedPaths", [])
                    if orphan_name not in encoded_paths:
                        # Actually, we renamed the orphan, so don't add it
                        pass

            index["lastUpdated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            save_json_file(index_file, index)

            # Rewrite paths in history.jsonl (if orphan had a known original path)
            if orphan_original_path:
                history_file = get_claude_dir() / "history.jsonl"
                if history_file.exists():
                    rewrite_paths_in_file(history_file, orphan_original_path, str(target_path))

            return {
                "success": True,
                "message": f"Successfully merged orphan '{orphan_name}' into {target_path}",
                "backup_path": str(backup_path),
                "renamed_folders": renamed_folders,
                "orphan_project_name": orphan_project_name,
                "target_project_name": target_path.name,
            }

    except TimeoutError:
        return {
            "success": False,
            "message": "Could not acquire lock. Another operation may be in progress.",
            "backup_path": None,
            "renamed_folders": renamed_folders,
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error during merge: {e}",
            "backup_path": str(backup_path) if "backup_path" in locals() else None,
            "renamed_folders": renamed_folders,
        }


def execute_cleanup(confirmed: bool = False) -> dict:
    """
    Remove stale entries from projects-index.json.

    Does NOT delete any folders - just cleans up the index.
    Returns {"success": bool, "message": str, "removed_entries": list}
    """
    if not confirmed:
        return {
            "success": False,
            "message": "Operation requires confirmation. Pass confirmed=True to execute.",
            "removed_entries": [],
            "backup_path": None,
        }

    plan = plan_cleanup()

    if not plan.index_changes.get("remove_entries"):
        return {
            "success": True,
            "message": "No stale entries to remove.",
            "removed_entries": [],
            "backup_path": None,
        }

    lock_path = get_memory_dir() / ".project_manager.lock"

    try:
        with FileLock(lock_path, timeout=30):
            # Backup
            backup_path = backup_files([Path(f) for f in plan.backups])

            # Update index
            index_file = get_projects_index_file()
            index = load_json_file(index_file, {"projects": {}})

            removed = []
            for canonical_path in plan.index_changes["remove_entries"]:
                if canonical_path in index.get("projects", {}):
                    del index["projects"][canonical_path]
                    removed.append(canonical_path)

            index["lastUpdated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            save_json_file(index_file, index)

            return {
                "success": True,
                "message": f"Removed {len(removed)} stale entries from index.",
                "removed_entries": removed,
                "backup_path": str(backup_path),
            }

    except TimeoutError:
        return {
            "success": False,
            "message": "Could not acquire lock. Another operation may be in progress.",
            "removed_entries": [],
            "backup_path": None,
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Error during cleanup: {e}",
            "removed_entries": [],
            "backup_path": str(backup_path) if "backup_path" in locals() else None,
        }


# =============================================================================
# List Backups
# =============================================================================


def list_backups() -> list[dict]:
    """
    List available backup directories.

    Returns list of {"path": str, "timestamp": str, "files": list[str]}
    """
    backups_dir = get_memory_dir() / ".backups"
    if not backups_dir.exists():
        return []

    backups = []
    for backup_dir in sorted(backups_dir.iterdir(), reverse=True):
        if not backup_dir.is_dir():
            continue

        files = [f.name for f in backup_dir.iterdir() if f.is_file()]
        backups.append({
            "path": str(backup_dir),
            "timestamp": backup_dir.name,
            "files": files,
        })

    return backups


# =============================================================================
# CLI for testing
# =============================================================================


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Project Manager CLI (for testing)")
    subparsers = parser.add_subparsers(dest="command")

    # list command
    list_parser = subparsers.add_parser("list", help="List all projects")
    list_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # orphans command
    orphans_parser = subparsers.add_parser("orphans", help="Find orphaned folders")
    orphans_parser.add_argument("--json", action="store_true", help="Output as JSON")

    # stale command
    stale_parser = subparsers.add_parser("stale", help="Find stale index entries")

    # backups command
    backups_parser = subparsers.add_parser("backups", help="List available backups")

    args = parser.parse_args()

    if args.command == "list":
        projects = list_projects()
        if args.json:
            import dataclasses
            print(json.dumps([dataclasses.asdict(p) for p in projects], indent=2))
        else:
            print("Projects:")
            for p in projects:
                status = "ok" if p.exists else "MISSING"
                memory = "has memory" if p.has_memory_file else "no memory"
                print(f"  [{status}] {p.name}: {p.original_path} ({len(p.work_days)} days, {memory})")
                for issue in p.issues:
                    print(f"        {issue}")

    elif args.command == "orphans":
        orphans = find_orphaned_folders()
        if args.json:
            import dataclasses
            print(json.dumps([dataclasses.asdict(o) for o in orphans], indent=2))
        else:
            print(f"Orphaned folders ({len(orphans)}):")
            for o in orphans:
                size_kb = o.total_size_bytes / 1024
                orig = o.original_path_from_index or o.decoded_path or "unknown"
                print(f"  {o.folder_name}")
                print(f"    Original: {orig}")
                print(f"    Files: {o.file_count}, Size: {size_kb:.1f} KB")

    elif args.command == "stale":
        stale = find_stale_entries()
        print(f"Stale entries ({len(stale)}):")
        for entry in stale:
            print(f"  {entry['name']}: {entry['original_path']} ({len(entry['work_days'])} days)")

    elif args.command == "backups":
        backups = list_backups()
        print(f"Available backups ({len(backups)}):")
        for b in backups:
            print(f"  {b['timestamp']}: {', '.join(b['files'])}")

    else:
        parser.print_help()
