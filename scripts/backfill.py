#!/usr/bin/env python3
"""DEPRECATED: Entity backfill for chunks table.

Entity extraction is now handled by:
- synthesis.py _apply_add_v3 (creates entities during synthesis)
- memory_server.py write_memory (creates entities on manual write)

This script targeted the v2 chunks table which no longer exists.
"""
raise DeprecationWarning(
    "backfill.py is deprecated. Entity extraction is handled by synthesis and write_memory."
)
