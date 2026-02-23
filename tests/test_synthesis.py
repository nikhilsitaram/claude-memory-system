#!/usr/bin/env python3
"""
Unit tests for synthesis.py

Run with: python -m pytest tests/test_synthesis.py -v
"""

from synthesis import (  # noqa: I001
    DailyFile,  # noqa: F401
    RouteEntry,  # noqa: F401
    SynthesisResult,  # noqa: F401
    parse_synthesis_output,
)


# =============================================================================
# parse_synthesis_output Tests
# =============================================================================


class TestParseSynthesisOutput:
    def test_single_daily_no_routes(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Did something
## Learnings
- [global/pattern] Learned something

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1
        assert result.dailies[0].date == "2026-02-22"
        assert "[global/implement] Did something" in result.dailies[0].content
        assert len(result.routes) == 0

    def test_multiple_dailies_with_routes(self):
        text = """===DAILY:2026-02-21===
# 2026-02-21
## Actions
- [proj/implement] Built feature

===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/pattern] A pattern

===ROUTE:global:Key Learnings===
- (2026-02-22) [pattern] A pattern

===ROUTE:proj:Key Actions===
- (2026-02-21) [implement] Built feature

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 2
        assert result.dailies[0].date == "2026-02-21"
        assert result.dailies[1].date == "2026-02-22"
        assert len(result.routes) == 2
        assert result.routes[0].scope == "global"
        assert result.routes[0].section == "Key Learnings"
        assert len(result.routes[0].entries) == 1
        assert result.routes[1].scope == "proj"

    def test_text_before_delimiters_ignored(self):
        text = """I'll now generate the synthesis output.

===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1
        assert "I'll now generate" not in result.dailies[0].content

    def test_missing_end_marker_warns(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something"""
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 1  # still parsed
        assert result.warnings  # warning about missing ===END===

    def test_no_daily_blocks_returns_empty(self):
        text = "Just some random text with no structure"
        result = parse_synthesis_output(text)
        assert len(result.dailies) == 0
        assert len(result.routes) == 0

    def test_empty_route_block_skipped(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Actions
- [global/implement] Something

===ROUTE:global:Key Learnings===

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.routes) == 0  # empty route skipped

    def test_route_entries_parsed_correctly(self):
        text = """===DAILY:2026-02-22===
# 2026-02-22
## Learnings
- [global/gotcha] A gotcha

===ROUTE:global:Key Learnings===
- (2026-02-22) [gotcha] A gotcha
- (2026-02-22) [pattern] A pattern

===END==="""
        result = parse_synthesis_output(text)
        assert len(result.routes) == 1
        assert len(result.routes[0].entries) == 2
        assert "[gotcha] A gotcha" in result.routes[0].entries[0]
