#!/bin/bash
# PreToolUse hook that auto-allows memory-related operations
# Checks tool name and input to determine if this is a memory system operation

# Read the JSON input
input=$(cat)

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

# 4. Memory system scripts (indexing.py, token_usage.py, etc.)
if echo "$input" | grep -q '\.claude/scripts'; then
    should_allow=true
    reason="memory system script"
fi

# Output decision
if [ "$should_allow" = true ]; then
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","permissionDecisionReason":"Memory system operation auto-approved"}}'
fi
# For non-memory operations, output nothing - let normal permission flow happen
