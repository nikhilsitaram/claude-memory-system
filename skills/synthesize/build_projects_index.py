#!/usr/bin/env python3
"""
Build a project-to-work-days index from Claude Code's sessions-index.json files.

This script scans all sessions-index.json files in ~/.claude/projects/ and builds
a mapping of projects to the dates they have work sessions. This enables
project-aware memory loading.

Output: ~/.claude/memory/projects-index.json

Usage:
    python3 build_projects_index.py
"""

import json
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict


def main():
    projects_dir = Path.home() / ".claude" / "projects"
    memory_dir = Path.home() / ".claude" / "memory"
    output_file = memory_dir / "projects-index.json"

    # Ensure memory dir exists
    memory_dir.mkdir(parents=True, exist_ok=True)

    # Collect all projects and their work days
    # Key: lowercase project path for consistent lookup
    # Value: project metadata
    projects = {}

    # Also track path variations (case differences) that map to same project
    path_variations = defaultdict(set)

    for project_folder in projects_dir.iterdir():
        if not project_folder.is_dir():
            continue

        sessions_file = project_folder / "sessions-index.json"
        if not sessions_file.exists():
            continue

        try:
            with open(sessions_file, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Warning: Could not read {sessions_file}: {e}")
            continue

        original_path = data.get("originalPath", "")
        if not original_path:
            continue

        entries = data.get("entries", [])
        if not entries:
            continue

        # Extract work days from session entries
        work_days = set()
        for entry in entries:
            created = entry.get("created")
            if created:
                try:
                    # Parse ISO format: "2026-01-25T21:48:21.826Z"
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    work_days.add(dt.strftime("%Y-%m-%d"))
                except ValueError:
                    continue

        if not work_days:
            continue

        # Use lowercase path as the canonical key for lookups
        canonical_path = original_path.lower()

        # Track all path variations
        path_variations[canonical_path].add(original_path)

        # If this project already exists (case variation), merge work days
        if canonical_path in projects:
            existing_days = set(projects[canonical_path]["workDays"])
            existing_days.update(work_days)
            projects[canonical_path]["workDays"] = sorted(existing_days)
            # Keep track of all encoded paths (folders)
            if project_folder.name not in projects[canonical_path]["encodedPaths"]:
                projects[canonical_path]["encodedPaths"].append(project_folder.name)
        else:
            # Extract project name from path
            project_name = Path(original_path).name

            projects[canonical_path] = {
                "name": project_name,
                "originalPath": original_path,  # Keep one original for display
                "encodedPaths": [project_folder.name],
                "workDays": sorted(work_days),
            }

    # Build output structure
    output = {
        "version": 1,
        "lastUpdated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "projects": projects,
        # Include a lookup table for path variations (for debugging)
        "pathVariations": {k: sorted(v) for k, v in path_variations.items() if len(v) > 1},
    }

    # Write output
    with open(output_file, "w") as f:
        json.dump(output, f, indent=2)

    # Print summary
    print(f"Built project index: {output_file}")
    print(f"  Projects found: {len(projects)}")
    for path, data in sorted(projects.items()):
        print(f"    {data['name']}: {len(data['workDays'])} work days")
        if len(data["encodedPaths"]) > 1:
            print(f"      (merged from {len(data['encodedPaths'])} folders)")


if __name__ == "__main__":
    main()
