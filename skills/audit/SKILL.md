---
name: audit
description: Use when user wants to audit long-term memory for stale, incorrect, outdated, or duplicate entries, or correct memory entries based on new information
user-invokable: true
---

# Audit Skill

Fact-check long-term memory entries against the current codebase. Detect outdated references, incorrect claims, superseded fixes, duplicates, and contradictions. Present findings for user approval before making any changes.

## Modes

- **Full scan** (`/audit` or `/audit --all`): Check all entries in global LTM + current project LTM (or all project LTMs with `--all`)
- **Directed audit** (`/audit <context>`): Check only entries topically related to the provided context, suggest corrections

## File Discovery

1. **Global LTM:** Read `~/.claude/memory/global-long-term-memory.md`
2. **Current project LTM:** List files in `~/.claude/memory/project-memory/` matching `*-long-term-memory.md`. Find the file whose name prefix (before `-long-term-memory.md`) matches the current repository directory name (case-insensitive). If no match, skip project LTM.
3. **With `--all` flag:** Read ALL `*-long-term-memory.md` files in `~/.claude/memory/project-memory/` instead of just the current project.

Parse each file into sections using `## ` headers. Track which section each entry belongs to, noting whether the section is pinned (sections: "About Me", "Current Projects", "Technical Environment", "Patterns & Preferences", "Pinned" -- or any section with `<!-- Auto-pinned` or `<!-- Custom pinned` comments).

## Detection Categories

For each entry (lines starting with `- (` in LTM files), determine if it contains identifiable code references: file paths with extensions, function/class names (snake_case or PascalCase), CLI commands, or config keys. Skip entries with no verifiable references for the `outdated`, `incorrect`, and `superseded` categories (but still check them for `duplicate` and `contradicted`).

### outdated
Entry references files, functions, configs, or systems that no longer exist.
- **Verify:** For file paths: use `Glob` to check existence. For function/class names: use `Grep` to search the codebase. For config keys: `Read` the relevant config file.
- **Flag when:** Referenced identifier not found anywhere in the codebase.

### incorrect
Entry states something demonstrably wrong about current code or behavior.
- **Verify:** For code-reference entries: `Read` relevant code and compare the claim vs reality. For config/default claims (e.g., "default is X"): search for the config key in `settings.json`, `memory_utils.py DEFAULT_SETTINGS`, or relevant scripts.
- **Flag when:** The entry's claim contradicts what the code actually does.

### superseded
A bug fix, workaround, or gotcha that has been properly resolved.
- **Verify:** Check if the underlying issue described in the entry has been fixed in current code.
- **Flag when:** The issue no longer exists or the workaround is no longer needed.

### duplicate
Same fact or pattern recorded in both global and project LTM, or within the same file.
- **Verify:** Compare entry keywords across all loaded LTM files. Two entries are duplicates if they describe the same fact, pattern, or decision, even with different wording.
- **Flag when:** Substantively duplicate content exists across tiers or within a file.

### contradicted
Two entries say opposite things, or an entry contradicts current code.
- **Verify:** Cross-reference entries within and across files. Also verify claims against code.
- **Flag when:** Two entries make conflicting claims, or an entry contradicts verified code behavior.

## Full Scan Workflow (`/audit` or `/audit --all`)

1. Discover LTM files (see File Discovery above)
2. Read each file and parse into sections
3. For each entry with identifiable code references:
   a. Extract referenced identifiers (file paths, function names, class names, config keys, CLI commands)
   b. For file paths: `Glob` to check existence
   c. For function/class names: `Grep` in likely locations (scripts/, skills/, tests/)
   d. For config keys/values: `Read` the relevant config file and compare
   e. If reference not found or value doesn't match: record as finding
4. For ALL entries (including those without code references): check for duplicates across files and contradictions between entries
5. Present triage table (see Triage Presentation below)
6. Wait for user approval
7. Apply approved changes (see Actions below)

If no findings are discovered, report: "No findings -- N entries checked across M files."

## Directed Audit Workflow (`/audit <context>`)

1. Discover LTM files (same as full scan)
2. Extract significant keywords from the context string (nouns, identifiers, technical terms -- skip stop words like "the", "is", "and", etc.)
3. Find entries whose text shares keywords with the context. Use judgment to filter out entries where keyword overlap is coincidental rather than topical.
4. For each related entry, verify whether the entry's claim is still correct given the context provided
5. Present findings with **suggested corrections** -- propose edited entry text, not just "archive"
6. Wait for user to approve corrections, provide their own edit, or dismiss each finding
7. Apply approved edits in place using the Edit tool

If no related entries are found, report: "No entries found related to: <context>"
If related entries all verify clean, report: "N entries checked related to <context> -- all verified correct."

## Triage Presentation

Present findings as a markdown table grouped by file:

```
### global-long-term-memory.md (3 findings)

| # | Section | Entry | Category | Reasoning |
|---|---------|-------|----------|-----------|
| 1 | Pinned | (2026-02-10) [pattern] Use set_memory_dir()... | outdated | set_memory_dir() not found in codebase |
| 2 | Key Learnings | (2026-01-15) [gotcha] SQLite WAL mode... | superseded | No SQLite usage in current architecture |
| 3 | Key Actions | (2026-02-20) [tip] Run vector_sync... | incorrect | vector_sync removed; no vector DB |
```

- Mark pinned entries by showing their section name (e.g., "Pinned", "About Me") so the user knows they are modifying protected content
- Truncate long entry text with `...` to keep the table readable
- For directed audit: include a "Suggested Correction" column showing the proposed replacement text

After the table, ask the user which findings to act on. List findings by number and ask the user to reply with the numbers they want to apply (e.g., "Apply 1, 3" or "Apply all" or "Dismiss all").

If there are more than 15 findings, batch them into groups of 10 and present each group separately, waiting for user response before showing the next group.

## Actions on Approved Findings

### Archive (default for outdated, superseded, incorrect)

1. Remove the entry line from its source LTM file using the Edit tool
2. Append the entry to `~/.claude/memory/.decay-archive.md` under an `## Audited YYYY-MM-DD` header (use today's date)
3. Append `[audit:category - reasoning]` tag to the archived entry
4. Add a source annotation on the next line: `  - *Source: filename.md*` (filename only, not full path)
5. If `.decay-archive.md` does not exist, create it with a `# Decay Archive` header before appending

Archive format example:
```
## Audited 2026-03-31
- (2026-02-10) [pattern] Use set_memory_dir() for path resolution [audit:outdated - function removed in v1 migration]
  - *Source: global-long-term-memory.md*
- (2026-01-15) [gotcha] SQLite WAL mode causes locking [audit:superseded - migrated to markdown, no SQLite]
  - *Source: claude-memory-system-long-term-memory.md*
```

**Important:** Use `## Audited` header (NOT `## Archived` which is used by the decay system). Audit archives are distinct from decay archives and are not subject to decay's purge cycle.

### Correct (directed mode or user-provided edit)

1. Use the Edit tool to replace the entry text in place in the source LTM file
2. Update the date prefix to today's date: `(YYYY-MM-DD)`

### Merge duplicate

1. Ask the user which tier to keep the entry in (global or project)
2. Archive the entry from the other tier (using the Archive flow above)
3. If the user wants to combine wording, edit the kept entry in place

## Rules

1. **Never modify memory files without explicit user approval** -- present all findings first, wait for user to select which to act on
2. **If user rejects all findings or there are no findings, do not modify any files**
3. **All sections are auditable** including Pinned and auto-pinned sections -- clearly indicate when a finding affects a pinned entry
4. **Entries with no verifiable code references** are skipped for outdated/incorrect/superseded detection but still checked for duplicates and contradictions
5. **Behavioral/preference entries** (e.g., "prefer X over Y") are skipped for code verification
6. **Use `## Audited` header in archive** (not `## Archived`) to distinguish audit actions from age-based decay
7. **Source annotations use filename only** (e.g., `global-long-term-memory.md`), not full paths
