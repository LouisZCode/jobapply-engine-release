#!/usr/bin/env python3
"""Read a Claude Code stream-json stream from stdin, print readable lines.

Usage:
  claude -p ... --output-format stream-json --verbose | python3 format_trace.py
  tail -f output/agent_<ts>.ndjson | python3 format_trace.py
"""
import sys

from obs_common import loads, items_from_event, GLYPH


def main():
    for line in sys.stdin:
        o = loads(line)
        if o is None:
            continue
        for channel, text in items_from_event(o):
            sys.stdout.write(f"{GLYPH.get(channel, ' ')} {text}\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
