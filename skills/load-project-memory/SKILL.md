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
2. Run the loader script via Bash. Use **one of the two forms below** — do not pass the literal string `<name>` as argv. Always quote the project name to prevent shell metacharacter interpretation:

   ```bash
   # No argument — uses cwd-based project detection
   uv run $HOME/.claude/scripts/load_memory.py --project-memory
   ```

   ```bash
   # Explicit project name (substitute the user's argument; keep it quoted)
   uv run $HOME/.claude/scripts/load_memory.py --project-memory "swyfft"
   ```

3. The script prints a `<project-memory>...</project-memory>` block containing:
   - `## Project Long-Term Memory: <name>` (the project's LTM file)
   - `## Project Short-Term Memory: <name>` (recent daily entries scoped to the project)

4. The output flows directly into context. After it loads, **do not stop at "memory loaded"** — read it and report back to the user with the following structure:

   **a. Counts and date ranges.** Count the number of memory entries (lines starting with `- `, including those nested under section headers) in each block, and find the oldest and newest dates. LTM entries are dated inline as `(YYYY-MM-DD)`; STM entries are grouped under `### YYYY-MM-DD` headers. Open with one line in this format:

   > 5 days of STM memory loaded (20 entries, 2026-04-26 to 2026-04-30). 20 days of LTM memory loaded (50 entries, 2025-09-15 to 2026-04-30).

   If a section is missing (LTM-only or STM-only output), only report the section that loaded.

   **b. Relevance scan.** Review what's been discussed in the current session and pick out 3–5 specific memory entries that look most relevant — gotchas about files you're touching, prior design decisions on the same subsystem, patterns that contradict or confirm the current approach, etc. Quote them verbatim. If nothing stands out as relevant, say so — don't pad.

   > Here are some selected memories which may be relevant to what we're working on:
   > - (2026-02-04) [pattern] PreToolUse hooks for subagent permissions — ...
   > - ...

   **c. Verdict.** Close with one of:
   - **Actionable suggestions** if memory reveals a conflict, missed gotcha, or better approach: state what to change and why, citing the memory entry.
   - **Confirmation** if memory aligns with the current direction: "The memory confirms what we're doing — looks good."

   Do not skip the verdict. The whole point of the skill is to surface what the user can't see.

## Notes

- This is the on-demand equivalent of what `mode: full` loads automatically at SessionStart. It does **not** load global short-term memory; if you need that too, switch the system to full mode via `/settings set mode full` (next session) or invoke `load_memory.py` directly.
- If the named project has no LTM file and no tagged daily entries, the script exits non-zero with a short stderr message.
- Project name lookup uses the same names registered in `~/.claude/memory/projects-index.json`. Run `/projects` to list known names.
