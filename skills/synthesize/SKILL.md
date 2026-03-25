---
name: synthesize
description: Use when processing pending session transcripts into daily summaries — first session of the day, on manual request, or on schedule
user-invokable: true
---

# Synthesize Skill

Launch a subagent to process memory transcripts into daily summaries and selectively route key learnings to long-term memory.

## Execution

**IMPORTANT:** This skill MUST be executed by launching a subagent. Do NOT run synthesis in the main context.

1. Get the synthesis prompt, model, and pre-extracted data:
   ```bash
   python3 $HOME/.claude/scripts/load_memory.py --synthesis-prompt
   ```
   - If output says "No pending transcripts", inform the user and stop.
   - First line: `model=<model>`. Second line: `prompt_file=<path>`.

2. Launch a synthesis subagent (**foreground** — manual `/synthesize` always blocks so user sees results).
   **IMPORTANT:** Do NOT read the prompt file. Pass the file path to the subagent and let it read the file itself. This avoids the main agent regenerating ~50K tokens of prompt content.
   ```
   Task(
     subagent_type: "general-purpose",
     model: <model from first line>,
     prompt: "Read <prompt_file path> and follow the instructions in it exactly. Use only Write and Bash tools."
   )
   ```

3. Report the subagent's summary to the user.

---

## Backfill Mode

When the user invokes `/synthesize --backfill`, `/synthesize --backfill --days 30`, or `/synthesize --backfill --import-from /path`:

1. Build the command: `python3 $HOME/.claude/scripts/synthesis_cron.py --backfill [--days N] [--import-from <path>]`
2. Run via Bash tool (NOT as a subagent — backfill needs interactive confirmation)
3. The script prints a scope report showing projects, session counts, and model breakdown
4. The script asks "Proceed? [y/N]" — relay this to the user and pass their response
5. Stream progress output to the user as it runs
6. Report final results

**Examples:**
- `/synthesize --backfill` → `python3 $HOME/.claude/scripts/synthesis_cron.py --backfill`
- `/synthesize --backfill --days 30` → `python3 $HOME/.claude/scripts/synthesis_cron.py --backfill --days 30`
- `/synthesize --backfill --import-from /Volumes/Backup/claude/projects` → `python3 $HOME/.claude/scripts/synthesis_cron.py --backfill --import-from /Volumes/Backup/claude/projects`

**Important:** Backfill can take several minutes for large session histories. Set a generous timeout (600000ms) on the Bash tool call.

---

## Reference: Tag Types

**Actions:** `implement`, `improve`, `document`, `analyze`
**Decisions:** `design`, `tradeoff`, `scope`
**Learnings:** `gotcha`, `pitfall`, `pattern`
**Lessons:** `insight`, `tip`, `workaround`

## Reference: Pinning Criteria

Items in long-term memory can be moved to `## Pinned` section (protected from decay):
- Fundamental architecture patterns
- Safety-critical information
- Cross-project patterns that proved valuable over time
