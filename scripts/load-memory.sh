#!/bin/bash
# SessionStart hook - loads memory context and triggers recovery
MEMORY_DIR="$HOME/.claude/memory"
PROJECTS_INDEX="$MEMORY_DIR/projects-index.json"

# First, recover any orphaned transcripts (runs silently in background)
bash ~/.claude/scripts/recover-transcripts.sh >> ~/.claude/memory/recovery.log 2>&1 &

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

# Recent daily summaries (last 7 days)
# Track which dates we've loaded to avoid duplicates in project section
LOADED_DATES=""
echo "## Recent Sessions"
for i in $(seq 0 6); do
    # Cross-platform date calculation
    if [[ "$OSTYPE" == "darwin"* ]]; then
        DATE=$(date -v-${i}d +%Y-%m-%d)
    else
        DATE=$(date -d "$i days ago" +%Y-%m-%d)
    fi
    DAILY_FILE="$MEMORY_DIR/daily/$DATE.md"
    if [ -f "$DAILY_FILE" ]; then
        echo "### $DATE"
        cat "$DAILY_FILE"
        echo ""
        LOADED_DATES="$LOADED_DATES $DATE"
    fi
done

# ===== Project-specific historical context =====
# Load last 14 "project days" (days with work in this specific project)
# Only loads dates NOT already included in the 7-day window above

if [ -f "$PROJECTS_INDEX" ]; then
    # Use inline Python for project detection and date filtering
    PROJECT_HISTORY=$(python3 - "$PWD" "$PROJECTS_INDEX" "$LOADED_DATES" "$MEMORY_DIR/daily" <<'PYEOF'
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

    # Exact match only - no subdirectory inheritance
    if pwd_lower not in projects:
        return

    project = projects[pwd_lower]
    project_name = project.get("name", "unknown")
    work_days = project.get("workDays", [])

    if not work_days:
        return

    # Get last 14 project days, excluding already-loaded dates
    project_days = [d for d in sorted(work_days, reverse=True) if d not in loaded_dates][:14]

    if not project_days:
        return

    # Output header
    print(f"## Project History: {project_name} (Last 14 Work Days)")
    print("")

    # Output each day's summary (oldest first for chronological reading)
    for date in sorted(project_days):
        daily_file = daily_dir / f"{date}.md"
        if daily_file.exists():
            print(f"### {date}")
            print(daily_file.read_text())
            print("")

if __name__ == "__main__":
    main()
PYEOF
    )

    # Only output if there's project history
    if [ -n "$PROJECT_HISTORY" ]; then
        echo "$PROJECT_HISTORY"
    fi
fi

echo "</memory>"
