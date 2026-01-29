#!/usr/bin/env python3
"""
Extract conversation content from JSONL transcript files.
Outputs structured data for creating daily summaries.

Usage:
    python extract_transcripts.py                    # Extract all days
    python extract_transcripts.py 2026-01-22        # Extract specific day
    python extract_transcripts.py --output /tmp/out.txt  # Save to file
"""

import os
import sys
import json
import argparse
from pathlib import Path
from collections import defaultdict
from datetime import datetime


def extract_text_content(content):
    """Extract text from message content (handles string or list format)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get('type') == 'text':
                text_parts.append(item.get('text', ''))
        return '\n'.join(text_parts)
    return ''


def parse_jsonl_file(filepath):
    """Parse a JSONL transcript file and extract messages."""
    messages = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)

                # Handle message objects - top level type is "user" or "assistant"
                obj_type = obj.get('type')
                if obj_type in ('user', 'assistant'):
                    msg = obj.get('message', {})
                    role = msg.get('role', obj_type)
                    content = extract_text_content(msg.get('content', ''))

                    if content:
                        # Skip tool results and system content for user messages
                        if role == 'user' and content.startswith('<'):
                            continue
                        messages.append({
                            'role': role,
                            'content': content
                        })

            except json.JSONDecodeError as e:
                print(f"Warning: JSON parse error in {filepath} line {line_num}: {e}", file=sys.stderr)
                continue

    return messages


def extract_transcripts(base_path, specific_day=None):
    """Extract all transcripts, organized by day and session."""
    base = Path(base_path)
    daily_data = defaultdict(list)

    if not base.exists():
        print(f"Transcript directory not found: {base}", file=sys.stderr)
        return daily_data

    for day_dir in sorted(base.iterdir()):
        if not day_dir.is_dir():
            continue

        day = day_dir.name

        # Skip if filtering for specific day
        if specific_day and day != specific_day:
            continue

        for jsonl_file in sorted(day_dir.glob("*.jsonl")):
            messages = parse_jsonl_file(jsonl_file)

            if messages:
                daily_data[day].append({
                    'session_id': jsonl_file.stem,
                    'filepath': str(jsonl_file),
                    'message_count': len(messages),
                    'messages': messages
                })

    return daily_data


def print_daily_summary(daily_data, output_file=None):
    """Print extracted data in a readable format."""
    output = []

    for day in sorted(daily_data.keys()):
        sessions = daily_data[day]
        total_messages = sum(s['message_count'] for s in sessions)
        output.append(f"\n{'='*70}")
        output.append(f"DAY: {day} ({len(sessions)} sessions, {total_messages} messages)")
        output.append(f"{'='*70}")

        for session in sessions:
            output.append(f"\n{'─'*70}")
            output.append(f"Session: {session['session_id']}")
            output.append(f"{'─'*70}")

            for msg in session['messages']:
                role_label = "USER" if msg['role'] == 'user' else "CLAUDE"
                output.append(f"\n[{role_label}]")
                output.append(msg['content'])

    text = '\n'.join(output)

    if output_file:
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(text)
        print(f"Output written to: {output_file}", file=sys.stderr)
    else:
        print(text)

    return text


def main():
    parser = argparse.ArgumentParser(description='Extract conversation content from transcripts')
    parser.add_argument('day', nargs='?', help='Specific day to extract (YYYY-MM-DD)')
    parser.add_argument('--output', '-o', help='Output file path')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--base', default=str(Path.home() / '.claude/memory/transcripts'),
                        help='Base transcript directory')

    args = parser.parse_args()

    daily_data = extract_transcripts(args.base, args.day)

    if not daily_data:
        print("No transcripts found.", file=sys.stderr)
        sys.exit(1)

    if args.json:
        if args.output:
            with open(args.output, 'w') as f:
                json.dump(dict(daily_data), f, indent=2)
        else:
            print(json.dumps(dict(daily_data), indent=2))
    else:
        print_daily_summary(daily_data, args.output)


if __name__ == '__main__':
    main()
