#!/bin/bash
# SessionEnd/PreCompact hook - saves transcript to memory system
INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')

# Handle tilde expansion
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

TODAY=$(date +%Y-%m-%d)
DEST_DIR="$HOME/.claude/memory/transcripts/$TODAY"
DEST_FILE="$DEST_DIR/$SESSION_ID.jsonl"
CAPTURED_FILE="$HOME/.claude/memory/.captured"

mkdir -p "$DEST_DIR"

if [ -f "$TRANSCRIPT" ]; then
    # Always copy (overwrites if exists - gets latest version)
    cp "$TRANSCRIPT" "$DEST_FILE"
    # Record session_id if not already captured
    if ! grep -q "^$SESSION_ID$" "$CAPTURED_FILE" 2>/dev/null; then
        echo "$SESSION_ID" >> "$CAPTURED_FILE"
    fi
fi
