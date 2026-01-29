#!/bin/bash
# SessionEnd hook - saves transcript to memory system
INPUT=$(cat)
TRANSCRIPT=$(echo "$INPUT" | jq -r '.transcript_path')
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id')

# Handle tilde expansion
TRANSCRIPT="${TRANSCRIPT/#\~/$HOME}"

TODAY=$(date +%Y-%m-%d)
TIME=$(date +%H-%M-%S)
DEST_DIR="$HOME/.claude/memory/transcripts/$TODAY"
DEST_FILE="$DEST_DIR/$TIME.jsonl"
CAPTURED_FILE="$HOME/.claude/memory/.captured"

mkdir -p "$DEST_DIR"

if [ -f "$TRANSCRIPT" ]; then
    cp "$TRANSCRIPT" "$DEST_FILE"
    # Record this session as captured
    echo "$SESSION_ID" >> "$CAPTURED_FILE"
fi
