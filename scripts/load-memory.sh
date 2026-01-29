#!/bin/bash
# SessionStart hook - loads memory context and triggers recovery
MEMORY_DIR="$HOME/.claude/memory"

# First, recover any orphaned transcripts (runs silently in background)
bash ~/.claude/scripts/recover-transcripts.sh >> ~/.claude/memory/recovery.log 2>&1 &

echo "<memory>"

# Check for pending transcripts
PENDING_COUNT=$(find "$MEMORY_DIR/transcripts" -name "*.jsonl" 2>/dev/null | wc -l)
if [ "$PENDING_COUNT" -gt 0 ]; then
    echo "## Pending Synthesis"
    echo "There are $PENDING_COUNT unprocessed session transcripts."
    echo "Run /synthesize to process them into daily summaries."
    echo ""
fi

# Long-term memory
if [ -f "$MEMORY_DIR/LONG_TERM.md" ]; then
    echo "## Long-Term Memory"
    cat "$MEMORY_DIR/LONG_TERM.md"
    echo ""
fi

# Recent daily summaries (last 7 days)
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
    fi
done

echo "</memory>"
