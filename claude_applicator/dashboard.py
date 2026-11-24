#!/usr/bin/env python3
"""Live two-panel dashboard for an observed apply run.

Tails a Claude Code stream-json trace (NDJSON, appended live) and reads
applications.db, rendering:
  header  — MODE, run clock, applied N/MAX, prepared, daily, idle timer
  left    — LLM logic (the model's text / thinking)
  right   — Actions / logs (tool calls + results)

Run via the venv interpreter:
  .venv-obs/bin/python dashboard.py --trace output/agent_<ts>.ndjson \
      --db applications.db --config run_config.txt [--pid <claude_pid>]
  add --once to render a single frame and exit (for testing, no TTY needed).
"""
import argparse
import os
import re
import sqlite3
import time
from collections import deque
from datetime import date

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from obs_common import loads, items_from_event, GLYPH, LLM_CHANNELS


def read_config_value(path, key, default=None):
    try:
        with open(path) as f:
            for ln in f:
                m = re.match(rf"\s*{key}:\s*(\S+)", ln)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return default


def db_snapshot(db_path):
    """(applied_job_ids, prepared_job_ids, today_applied_count)."""
    applied, prepared, today_ct = set(), set(), 0
    today = date.today().isoformat()
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        for jid, status, ts in con.execute(
            "SELECT job_id,status,status_updated_at FROM applications"
        ):
            if status == "applied":
                applied.add(jid)
                if (ts or "")[:10] == today:
                    today_ct += 1
            elif status == "prepared":
                prepared.add(jid)
        con.close()
    except Exception:
        pass
    return applied, prepared, today_ct


def pid_alive(pid):
    if not pid:
        return None
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trace", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--config", required=True)
    ap.add_argument("--pid", type=int, default=0)
    ap.add_argument("--once", action="store_true",
                    help="render one frame and exit (for testing, no TTY needed)")
    args = ap.parse_args()

    console = Console()
    MODE = read_config_value(args.config, "MODE", "?")
    MAX = read_config_value(args.config, "MAX_APPLIES_PER_RUN", "?")
    init_applied, init_prepared, _ = db_snapshot(args.db)

    llm = deque(maxlen=2000)
    actions = deque(maxlen=2000)
    start = time.time()
    state = {"model": "?", "mcp": "?", "last_action": None, "done": False, "done_line": ""}
    fh = {"f": None, "pos": 0}
    dbc = {"t": 0.0, "val": (init_applied, init_prepared, 0)}

    def db_cached():
        now = time.time()
        if now - dbc["t"] > 0.8:
            dbc["val"] = db_snapshot(args.db)
            dbc["t"] = now
        return dbc["val"]

    def open_trace():
        if fh["f"] is None and os.path.exists(args.trace):
            fh["f"] = open(args.trace, "r")

    def drain():
        f = fh["f"]
        if f is None:
            return
        f.seek(fh["pos"])
        for line in f:
            o = loads(line)
            if o is None:
                continue
            for ch, text in items_from_event(o):
                g = GLYPH.get(ch, " ")
                if ch in LLM_CHANNELS:
                    for sub in text.split("\n"):
                        sub = sub.strip()
                        if sub:
                            llm.append(f"{g} {sub}")
                else:
                    actions.append(f"{g} {text}")
                    if ch in ("tool", "toolok", "toolerr"):
                        state["last_action"] = time.time()
                    elif ch == "meta":
                        m = re.search(r"model=(\S+).*mcp=(\S+)", text)
                        if m:
                            state["model"], state["mcp"] = m.group(1), m.group(2)
                    elif ch == "done":
                        state["done"] = True
                        state["done_line"] = text
        fh["pos"] = f.tell()

    def render():
        cur_applied, cur_prepared, today_ct = db_cached()
        new_applied = len(cur_applied - init_applied)
        new_prepared = len(cur_prepared - init_prepared)
        elapsed = int(time.time() - start)
        clock = f"{elapsed // 60:02d}:{elapsed % 60:02d}"
        if state["last_action"] is None:
            timer = "warming up…"
        else:
            timer = f"{int(time.time() - state['last_action'])}s idle"

        head = Table.grid(expand=True)
        head.add_column(justify="left")
        head.add_column(justify="right")
        head.add_row(
            f"[bold]Job Apply[/] · MODE=[bold]{MODE}[/] · ⏱ {clock}",
            f"applied [bold]{new_applied}/{MAX}[/] · prep {new_prepared} · daily {today_ct} · ⏳ {timer}",
        )

        body_h = max(3, console.size.height - 8)

        def tail(dq):
            lines = list(dq)[-body_h:]
            return Text("\n".join(lines) if lines else "(waiting…)",
                        no_wrap=True, overflow="ellipsis")

        llm_panel = Panel(tail(llm), title="💭 LLM logic", border_style="cyan")
        act_panel = Panel(tail(actions), title="🛠 Actions / logs", border_style="green")

        if state["done"]:
            foot = f"[bold green]✔ {state['done_line']}[/] · Ctrl-C to close"
        elif args.pid and pid_alive(args.pid) is False:
            foot = "[yellow]run process exited[/] · Ctrl-C to close"
        else:
            src = "trace live" if fh["f"] is not None else "waiting for trace file…"
            foot = f"model={state['model']} · mcp={state['mcp']} · {src} · Ctrl-C to stop"

        root = Layout()
        root.split_column(
            Layout(Panel(head, border_style="white"), size=3),
            Layout(name="body"),
            Layout(Panel(Text.from_markup(foot)), size=3),
        )
        root["body"].split_row(Layout(llm_panel), Layout(act_panel))
        return root

    if args.once:
        open_trace()
        drain()
        console.print(render())
        return

    from rich.live import Live

    try:
        with Live(console=console, screen=True, refresh_per_second=8) as live:
            while True:
                open_trace()
                drain()
                live.update(render())
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if fh["f"]:
            fh["f"].close()
    a, _, _ = db_snapshot(args.db)
    console.print(
        f"[dim]closed · applied {len(a - init_applied)} this run · "
        f"trace {os.path.basename(args.trace)}[/]"
    )


if __name__ == "__main__":
    main()
