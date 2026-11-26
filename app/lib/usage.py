"""app/lib/usage.py — token usage roll-up across every Claude run this install has made.

Claude's subscription plans expose no headless "percent of my plan left" query (the CLI only
has the interactive /usage screen), so the app can't draw a real allowance bar. What IS
measurable, exactly, is how many tokens each run consumed: every `claude -p` worker here
runs with --output-format stream-json, and the final "result" event of each trace carries
the CLI's own session totals — input, output, cache-read and cache-creation tokens, plus the
API-equivalent cost — covering every turn, tool call, MCP call and sub-agent in that run.

This module sums those across:
  - the app's own jobs  (app/jobs/*.ndjson — answer bank, CV read, CV build)
  - the engine's workers (claude_applicator/output/agent_*.ndjson — the daily apply runs)
  - the mail agent       (claude_applicator/output/mail_agent_run_*.ndjson)

Only the tail of each file is read (the result event is the last line), and the parsed
totals are cached per (path, size, mtime) so a page view stays cheap however many runs have
accumulated. Nothing here ever writes a file.
"""
import glob
import json
import os
import time

from . import paths

_CACHE = {}  # path -> (size, mtime, record)
_TAIL_BYTES = 256 * 1024


def _result_record(path):
    """The usage record of one trace, or None if the trace has no result event (a run
    that's still going, or one that was killed before it could finish)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (st.st_size, st.st_mtime)
    cached = _CACHE.get(path)
    if cached and cached[0] == key:
        return cached[1]

    record = None
    try:
        with open(path, "rb") as fh:
            if st.st_size > _TAIL_BYTES:
                fh.seek(-_TAIL_BYTES, os.SEEK_END)
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    # A pi worker run records no usage yet: its traces carry no Claude "result" event, so they are skipped here.
    for line in reversed(chunk.splitlines()):
        line = line.strip()
        if not line.startswith("{") or '"result"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "result":
            continue
        raw = ev.get("usage") or {}
        if not isinstance(raw, dict):
            raw = {}
        def num(*keys):
            for k in keys:
                v = raw.get(k)
                if isinstance(v, (int, float)):
                    return int(v)
            return 0
        record = {
            "path": path,
            "name": os.path.basename(path),
            "ended_at": st.st_mtime,
            "input": num("input_tokens", "inputTokens"),
            "output": num("output_tokens", "outputTokens"),
            "cache_read": num("cache_read_input_tokens", "cacheReadInputTokens"),
            "cache_create": num("cache_creation_input_tokens", "cacheCreationInputTokens"),
            "cost_usd": float(ev.get("total_cost_usd") or 0.0),
            "turns": int(ev.get("num_turns") or 0),
        }
        break
    _CACHE[path] = (key, record)
    return record


def _trace_paths():
    patterns = [
        os.path.join(paths.JOBS_DIR, "*.ndjson"),
        paths.engine_file("output/agent_*.ndjson"),
        paths.engine_file("output/mail_agent_run_*.ndjson"),
    ]
    found = []
    for pattern in patterns:
        found.extend(glob.glob(pattern))
    return sorted(set(found))


def _empty():
    return {"runs": 0, "input": 0, "output": 0, "cache_read": 0, "cache_create": 0,
            "cost_usd": 0.0, "total": 0}


def _add(bucket, rec):
    bucket["runs"] += 1
    for k in ("input", "output", "cache_read", "cache_create"):
        bucket[k] += rec[k]
    bucket["cost_usd"] += rec["cost_usd"]
    bucket["total"] = bucket["input"] + bucket["output"] + bucket["cache_read"] + bucket["cache_create"]


def summary():
    """{"today": bucket, "all": bucket, "last": record-or-None} where a bucket is
    {runs, input, output, cache_read, cache_create, total, cost_usd}. "today" is local
    calendar day by the trace file's end time."""
    today = _empty()
    overall = _empty()
    last = None
    day_start = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    for path in _trace_paths():
        rec = _result_record(path)
        if not rec:
            continue
        _add(overall, rec)
        if rec["ended_at"] >= day_start:
            _add(today, rec)
        if last is None or rec["ended_at"] > last["ended_at"]:
            last = rec
    overall["cost_usd"] = round(overall["cost_usd"], 2)
    today["cost_usd"] = round(today["cost_usd"], 2)
    return {"today": today, "all": overall, "last": last}


def fmt_tokens(n):
    """1234 -> '1.2k', 1234567 -> '1.2M' — for the compact usage line."""
    n = int(n or 0)
    if n >= 1_000_000:
        return "{:.1f}M".format(n / 1_000_000).replace(".0M", "M")
    if n >= 1_000:
        return "{:.1f}k".format(n / 1_000).replace(".0k", "k")
    return str(n)
