#!/bin/bash
# Recovery script for orphaned transcripts from ungraceful exits
MEMORY_DIR="$HOME/.claude/memory"
PROJECTS_DIR="$HOME/.claude/projects"
CAPTURED_FILE="$MEMORY_DIR/.captured"
LOCKFILE="$MEMORY_DIR/.recovery.lock"

mkdir -p "$MEMORY_DIR/transcripts"
touch "$CAPTURED_FILE"

# File locking to prevent race conditions
exec 200>"$LOCKFILE"
flock -n 200 || { echo "Recovery already running"; exit 0; }

# Detect OS for date/stat commands
if [[ "$OSTYPE" == "darwin"* ]]; then
    IS_MAC=true
else
    IS_MAC=false
fi

# Find all session transcripts older than 30 minutes
find "$PROJECTS_DIR" -name "*.jsonl" -type f -mmin +30 2>/dev/null | while read -r file; do
    # Extract session ID from filename
    SESSION_ID=$(basename "$file" .jsonl)

    # Skip if already captured
    if grep -q "^$SESSION_ID$" "$CAPTURED_FILE" 2>/dev/null; then
        continue
    fi

    # Skip subagent files
    if [[ "$file" == *"/subagents/"* ]]; then
        continue
    fi

    # Get file date for organizing (cross-platform)
    if $IS_MAC; then
        FILE_DATE=$(stat -f %m "$file")
        DATE=$(date -r "$FILE_DATE" +%Y-%m-%d)
        TIME=$(date -r "$FILE_DATE" +%H-%M-%S)
    else
        FILE_DATE=$(stat -c %Y "$file")
        DATE=$(date -d "@$FILE_DATE" +%Y-%m-%d)
        TIME=$(date -d "@$FILE_DATE" +%H-%M-%S)
    fi

    DEST_DIR="$MEMORY_DIR/transcripts/$DATE"
    DEST_FILE="$DEST_DIR/${TIME}_recovered.jsonl"

    mkdir -p "$DEST_DIR"
    cp "$file" "$DEST_FILE"
    echo "$SESSION_ID" >> "$CAPTURED_FILE"

    echo "$(date '+%Y-%m-%d %H:%M:%S') Recovered: $SESSION_ID -> $DEST_FILE"
done
