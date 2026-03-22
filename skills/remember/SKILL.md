---
name: remember
description: "[DEPRECATED] Save to memory - use write_memory MCP tool instead"
user-invokable: true
---

# /remember - DEPRECATED

This skill has been replaced by the `write_memory` MCP tool, which Claude calls automatically when it detects important facts.

**Migration:** Instead of typing `/remember`, Claude now proactively writes memories during conversation using the `write_memory` tool. Important facts, decisions, and learnings are captured automatically.

**Manual save:** If you want to explicitly save something, tell Claude: "Remember that [fact]" and Claude will use the `write_memory` tool.
