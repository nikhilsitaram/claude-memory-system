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
   uv run $HOME/.claude/scripts/load_memory.py --synthesis-prompt
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
