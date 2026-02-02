#!/bin/bash
# SessionStart hook - loads memory context and triggers recovery
MEMORY_DIR="$HOME/.claude/memory"
PROJECTS_INDEX="$MEMORY_DIR/projects-index.json"
SETTINGS_FILE="$MEMORY_DIR/settings.json"

# First, recover any orphaned transcripts (runs silently in background)
bash ~/.claude/scripts/recover-transcripts.sh >> ~/.claude/memory/recovery.log 2>&1 &

# Load settings with defaults
if [ -f "$SETTINGS_FILE" ]; then
    SHORT_TERM_DAYS=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('shortTermMemory',{}).get('workingDays',7))" 2>/dev/null || echo 7)
    PROJECT_DAYS=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('projectMemory',{}).get('workingDays',7))" 2>/dev/null || echo 7)
    INCLUDE_SUBDIRS=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('projectMemory',{}).get('includeSubdirectories',False))" 2>/dev/null || echo "False")
    TOTAL_BUDGET=$(python3 -c "import json; print(json.load(open('$SETTINGS_FILE')).get('totalTokenBudget',30000))" 2>/dev/null || echo 30000)
else
    SHORT_TERM_DAYS=7
    PROJECT_DAYS=7
    INCLUDE_SUBDIRS="False"
    TOTAL_BUDGET=30000
fi

echo "<memory>"

# Check for pending transcripts and auto-synthesize
PENDING_COUNT=$(find "$MEMORY_DIR/transcripts" -name "*.jsonl" 2>/dev/null | wc -l)
if [ "$PENDING_COUNT" -gt 0 ]; then
    echo "## AUTO-SYNTHESIZE REQUIRED"
    echo "There are $PENDING_COUNT unprocessed session transcripts."
    echo "**IMPORTANT**: Before responding to the user, you MUST:"
    echo "1. Read the transcript content below"
    echo "2. Create/update daily summaries in ~/.claude/memory/daily/YYYY-MM-DD.md"
    echo "3. Update ~/.claude/memory/LONG_TERM.md if there are significant patterns"
    echo "4. Delete the processed transcript files"
    echo ""
    echo "### Transcript Content to Process"
    echo '```'
    python3 ~/.claude/skills/synthesize/extract_transcripts.py 2>/dev/null || echo "(extraction failed - check ~/.claude/skills/synthesize/extract_transcripts.py)"
    echo '```'
    echo ""
fi

# Long-term memory
if [ -f "$MEMORY_DIR/LONG_TERM.md" ]; then
    echo "## Long-Term Memory"
    cat "$MEMORY_DIR/LONG_TERM.md"
    echo ""
fi

# Recent daily summaries (last N WORKING days - days with actual files)
# Track which dates we've loaded to avoid duplicates in project section
LOADED_DATES=""
DAILY_DIR="$MEMORY_DIR/daily"

# Find working days by scanning existing daily files (not calendar days)
if [ -d "$DAILY_DIR" ]; then
    WORKING_DAYS=$(ls -1 "$DAILY_DIR"/*.md 2>/dev/null | \
      sed 's|.*/||; s|\.md$||' | \
      sort -r | \
      head -n "$SHORT_TERM_DAYS")
fi

echo "## Recent Sessions"
for DATE in $WORKING_DAYS; do
    DAILY_FILE="$DAILY_DIR/$DATE.md"
    if [ -f "$DAILY_FILE" ]; then
        echo "### $DATE"
        cat "$DAILY_FILE"
        echo ""
        LOADED_DATES="$LOADED_DATES $DATE"
    fi
done

# ===== Project-specific historical context =====
# Load last N "project days" (days with work in this specific project)
# Only loads dates NOT already included in the working days window above
PROJECT_BYTES=0

if [ -f "$PROJECTS_INDEX" ]; then
    # Use inline Python for project detection and date filtering
    PROJECT_HISTORY=$(python3 - "$PWD" "$PROJECTS_INDEX" "$LOADED_DATES" "$MEMORY_DIR/daily" "$PROJECT_DAYS" "$INCLUDE_SUBDIRS" <<'PYEOF'
import sys
import json
from pathlib import Path

def main():
    if len(sys.argv) < 5:
        return

    pwd = sys.argv[1]
    index_path = sys.argv[2]
    loaded_dates_str = sys.argv[3]
    daily_dir = Path(sys.argv[4])
    project_days_limit = int(sys.argv[5]) if len(sys.argv) > 5 else 7
    include_subdirs = sys.argv[6].lower() == "true" if len(sys.argv) > 6 else False

    # Parse already-loaded dates
    loaded_dates = set(loaded_dates_str.split())

    # Load project index
    try:
        with open(index_path) as f:
            index = json.load(f)
    except (json.JSONDecodeError, IOError):
        return

    projects = index.get("projects", {})

    # Normalize PWD for lookup (lowercase)
    pwd_lower = pwd.lower()

    # Project matching logic
    project = None
    project_key = None

    if include_subdirs:
        # Match if PWD starts with any known project path (longest match wins)
        matching_project = None
        for path_key, proj in projects.items():
            if pwd_lower.startswith(path_key) or pwd_lower == path_key:
                if matching_project is None or len(path_key) > len(matching_project[0]):
                    matching_project = (path_key, proj)
        if matching_project:
            project_key, project = matching_project
    else:
        # Exact match only (default behavior)
        if pwd_lower in projects:
            project_key = pwd_lower
            project = projects[pwd_lower]

    if not project:
        return

    project_name = project.get("name", "unknown")
    work_days = project.get("workDays", [])

    if not work_days:
        return

    # Get last N project days, excluding already-loaded dates
    project_days = [d for d in sorted(work_days, reverse=True) if d not in loaded_dates][:project_days_limit]

    if not project_days:
        return

    # Output header
    print(f"## Project History: {project_name} (Last {project_days_limit} Work Days)")
    print("")

    # Track total bytes for token estimation
    total_bytes = 0

    # Output each day's summary (oldest first for chronological reading)
    for date in sorted(project_days):
        daily_file = daily_dir / f"{date}.md"
        if daily_file.exists():
            content = daily_file.read_text()
            total_bytes += len(content.encode('utf-8'))
            print(f"### {date}")
            print(content)
            print("")

    # Output bytes marker for token estimation (will be parsed by bash)
    print(f"<!-- PROJECT_BYTES:{total_bytes} -->")

if __name__ == "__main__":
    main()
PYEOF
    )

    # Only output if there's project history
    if [ -n "$PROJECT_HISTORY" ]; then
        # Extract project bytes before outputting (strip the marker from output)
        PROJECT_BYTES=$(echo "$PROJECT_HISTORY" | grep -o 'PROJECT_BYTES:[0-9]*' | cut -d: -f2 || echo 0)
        # Output without the bytes marker
        echo "$PROJECT_HISTORY" | grep -v '<!-- PROJECT_BYTES:'
    fi
fi

echo "</memory>"

# Token usage estimation (informational, after memory tag)
# Estimate: 1 token ≈ 4 characters (bytes)
TOTAL_BYTES=0

# Count LONG_TERM.md
if [ -f "$MEMORY_DIR/LONG_TERM.md" ]; then
    TOTAL_BYTES=$((TOTAL_BYTES + $(wc -c < "$MEMORY_DIR/LONG_TERM.md")))
fi

# Count loaded daily files
for DATE in $LOADED_DATES; do
    DAILY_FILE="$DAILY_DIR/$DATE.md"
    if [ -f "$DAILY_FILE" ]; then
        TOTAL_BYTES=$((TOTAL_BYTES + $(wc -c < "$DAILY_FILE")))
    fi
done

# Add project history bytes (captured from inline Python above)
if [ -n "$PROJECT_BYTES" ] && [ "$PROJECT_BYTES" -gt 0 ] 2>/dev/null; then
    TOTAL_BYTES=$((TOTAL_BYTES + PROJECT_BYTES))
fi

# Estimate tokens
ESTIMATED_TOKENS=$((TOTAL_BYTES / 4))

# Only show warning if over budget
if [ "$ESTIMATED_TOKENS" -gt "$TOTAL_BUDGET" ]; then
    echo "<!-- Memory usage: ~$ESTIMATED_TOKENS tokens (budget: $TOTAL_BUDGET) -->"
    echo "<!-- Consider running /synthesize to consolidate older sessions -->"
fi
