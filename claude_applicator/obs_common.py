"""Shared parsing for Claude Code stream-json traces.

Used by both format_trace.py (flat readable log) and dashboard.py (rich TUI).
Each stream-json line is one event; items_from_event() turns an event into
zero or more (channel, text) tuples. Channels route to a panel + glyph.
"""
import json

GLYPH = {
    "llm": "\U0001F4AD",    # thought balloon
    "think": "\U0001F9E0",  # brain
    "tool": "\U0001F6E0",   # hammer+wrench
    "toolok": "↳",     # down-right arrow
    "toolerr": "✗",    # ballot x
    "meta": "·",       # middle dot
    "done": "✅",       # check mark button
}
LLM_CHANNELS = {"llm", "think"}
ACTION_CHANNELS = {"tool", "toolok", "toolerr", "meta", "done"}


def loads(line):
    line = line.strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except Exception:
        return None


def _short(s, n=90):
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_name(name):
    # MCP tools arrive namespaced: mcp__<server>__<tool>
    if isinstance(name, str) and name.startswith("mcp__"):
        return name.split("__", 2)[-1]
    return name or "tool"


def _tool_detail(name, inp):
    inp = inp if isinstance(inp, dict) else {}
    n = _tool_name(name).lower()

    def first(*keys):
        for k in keys:
            v = inp.get(k)
            if v:
                return v
        return None

    if "navigate" in n:
        d = first("url")
    elif n.endswith("click"):
        d = first("element", "ref", "selector")
    elif "type" in n:
        d = first("text", "element")
    elif "fill" in n:
        d = "form" if inp.get("fields") else first("text", "element")
    elif "select" in n:
        d = first("element", "values")
    elif "press" in n:
        d = first("key")
    elif "screenshot" in n:
        d = first("filename") or "screenshot"
    elif "snapshot" in n:
        d = "page snapshot"
    elif "wait" in n:
        d = first("text", "time")
    elif "query" in n:
        d = first("query")
    elif n == "bash":
        d = first("command")
    elif n in ("read", "write", "edit"):
        d = first("file_path")
    else:
        d = json.dumps(inp)[:80] if inp else None
    return d


def items_from_event(o):
    """Yield (channel, text) tuples from one decoded stream-json event."""
    if not isinstance(o, dict):
        return
    t = o.get("type")
    if t == "assistant":
        for c in o.get("message", {}).get("content", []) or []:
            ct = c.get("type")
            if ct == "text":
                tx = (c.get("text") or "").strip()
                if tx:
                    yield ("llm", tx)
            elif ct == "thinking":
                tx = (c.get("thinking") or "").strip()
                if tx:
                    yield ("think", tx)
            elif ct == "tool_use":
                name = _tool_name(c.get("name"))
                detail = _tool_detail(c.get("name"), c.get("input"))
                yield ("tool", f"{name} · {_short(detail)}" if detail else name)
    elif t == "user":
        content = o.get("message", {}).get("content", [])
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "tool_result":
                    body = c.get("content")
                    if isinstance(body, list):
                        body = " ".join(
                            b.get("text", "") for b in body if isinstance(b, dict)
                        )
                    ch = "toolerr" if c.get("is_error") else "toolok"
                    yield (ch, _short(body, 80))
    elif t == "result":
        turns = o.get("num_turns")
        cost = o.get("total_cost_usd")
        extra = []
        if turns is not None:
            extra.append(f"{turns} turns")
        if cost is not None:
            try:
                extra.append(f"${float(cost):.4f}")
            except Exception:
                pass
        sub = o.get("subtype")
        head = "complete" if sub in (None, "success") else f"ended ({sub})"
        yield ("done", head + (f" — {', '.join(extra)}" if extra else ""))
        res = o.get("result")
        if res:
            yield ("done", _short(res, 200))
    elif t == "system" and o.get("subtype") == "init":
        model = o.get("model")
        mcp = o.get("mcp_servers")
        if isinstance(mcp, list):
            mcp = len(mcp)
        yield ("meta", f"session init · model={model} · mcp={mcp}")
