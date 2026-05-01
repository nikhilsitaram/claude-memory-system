---
name: load-project-memory
description: Use when user wants to load a project's long-term and short-term memory mid-session — typically when running in light mode and Claude lacks project-specific context
user-invokable: true
---

# Load Project Memory

Inject a project's long-term memory and recent short-term history into the current session. Useful when:

- Memory system is in `light` mode (project sections are skipped at SessionStart)
- You've switched focus to a different project mid-session
- Claude doesn't seem to remember prior work on the project at hand

## Usage

- `/load-project-memory` — load memory for the project matching `$PWD`
- `/load-project-memory <name>` — load memory for a named project (e.g. `/load-project-memory swyfft`)

## Instructions

1. Parse the optional project name argument from the user's invocation.
2. Run the loader script via Bash:

   ```bash
   python3 $HOME/.claude/scripts/load_memory.py --project-memory [name]
   ```

   Omit `[name]` to use cwd-based detection.

3. The script prints a `<project-memory>...</project-memory>` block containing:
   - `## Project Long-Term Memory: <name>` (the project's LTM file)
   - `## Project Short-Term Memory: <name>` (recent daily entries scoped to the project)

4. The output flows directly into context — no further action is needed. Acknowledge briefly to the user that the project's memory has been loaded.

## Notes

- This is the on-demand equivalent of what `mode: full` loads automatically at SessionStart. It does **not** load global short-term memory; if you need that too, switch the system to full mode via `/settings set mode full` (next session) or invoke `load_memory.py` directly.
- If the named project has no LTM file and no tagged daily entries, the script exits non-zero with a short stderr message.
- Project name lookup uses the same names registered in `~/.claude/memory/projects-index.json`. Run `/projects` to list known names.
