---
name: synthesize
description: Process raw session transcripts into daily summaries and update long-term memory with patterns and insights. Run during first session of the day, when prompted, or according to a set schedule.
user-invocable: true
---

# Synthesize Skill

Launch a subagent to process memory transcripts into daily summaries and selectively route key learnings to long-term memory.

## Execution

**IMPORTANT:** This skill MUST be executed by launching a subagent. Do NOT run synthesis in the main context.

1. Check for pending transcripts:
   ```bash
   python3 $HOME/.claude/scripts/indexing.py list-pending
   ```

2. If no pending transcripts, inform the user and stop.

3. Get the synthesis prompt and model (single source of truth):
   ```bash
   python3 $HOME/.claude/scripts/load_memory.py --synthesis-prompt
   ```
   The first line of output is `model=<model>`. The rest is the subagent prompt.

4. Launch a synthesis subagent (**foreground** — manual `/synthesize` always blocks so user sees results):
   ```
   Task(
     subagent_type: "general-purpose",
     model: <model from output>,
     prompt: <rest of output>
   )
   ```

5. Report the subagent's summary to the user.

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
