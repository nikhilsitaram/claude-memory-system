---
name: recall
description: "[DEPRECATED] Search memory - use search_memories MCP tool instead"
user-invokable: true
---

# /recall - DEPRECATED

This skill has been replaced by the `search_memories` MCP tool, which Claude calls automatically when relevant.

**Migration:** Instead of typing `/recall`, Claude now proactively searches memories during conversation using the `search_memories` tool. You don't need to do anything -- relevant memories surface automatically.

**Manual search:** If you want to explicitly search, ask Claude: "Search my memories for [topic]" and Claude will use the `search_memories` tool.
