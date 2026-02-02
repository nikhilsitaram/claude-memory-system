#!/bin/bash
# PreToolUse hook that auto-allows memory-related operations
# Checks tool name and input to determine if this is a memory system operation

LOG_FILE="$HOME/.claude/memory/pretooluse-debug.log"

# Read the JSON input
input=$(cat)

# Log the invocation
echo "$(date '+%Y-%m-%d %H:%M:%S') PreToolUse hook called" >> "$LOG_FILE"
echo "  Input: $input" >> "$LOG_FILE"

# Extract tool name using grep/sed (avoid jq dependency)
tool_name=$(echo "$input" | grep -o '"tool_name":"[^"]*"' | sed 's/"tool_name":"//;s/"//')

# Check for memory-related operations
should_allow=false

# 1. Direct memory path in input
if echo "$input" | grep -q '\.claude/memory'; then
    should_allow=true
    reason="memory path detected"
fi

# 2. Memory system skills
if [ "$tool_name" = "Skill" ]; then
    if echo "$input" | grep -qE '"skill"\s*:\s*"(synthesize|remember|recall|reload|settings)"'; then
        should_allow=true
        reason="memory system skill"
    fi
fi

# 3. Task tool for memory operations (check if prompt mentions memory/synthesize)
if [ "$tool_name" = "Task" ]; then
    if echo "$input" | grep -qiE '(synthesize|memory|transcript)'; then
        should_allow=true
        reason="memory-related task"
    fi
fi

# 4. Indexing script operations
if echo "$input" | grep -q 'indexing\.py'; then
    should_allow=true
    reason="indexing script"
fi

# Output decision
if [ "$should_allow" = true ]; then
    echo "  Decision: allow ($reason)" >> "$LOG_FILE"
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"Memory system operation auto-approved"}}'
else
    echo "  Decision: ask (not a memory operation)" >> "$LOG_FILE"
    # Don't output anything - let normal permission flow happen
    # Outputting "ask" would override existing permissions
fi
