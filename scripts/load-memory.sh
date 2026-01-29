#!/bin/bash
# SessionStart hook - loads memory context and triggers recovery
MEMORY_DIR="$HOME/.claude/memory"

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
