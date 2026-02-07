---
name: synthesize
description: Process raw session transcripts into daily summaries and update long-term memory with patterns and insights. Run during first session of the day, when prompted, or according to a set schedule.
user-invocable: true
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
   - First line of output: `model=<model>`. The rest is the subagent prompt (with dates and file paths already embedded).

2. Launch a synthesis subagent (**foreground** — manual `/synthesize` always blocks so user sees results):
   ```
   Task(
     subagent_type: "general-purpose",
     model: <model from first line>,
     prompt: <rest of output after first line>
   )
   ```

3. Report the subagent's summary to the user.

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
