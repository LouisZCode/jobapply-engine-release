#!/usr/bin/env bash
# Observe a live apply run with the rich two-panel dashboard.
#
# Ensures Chrome is up + logged in, then runs the SAME apply routine the
# scheduler runs (CLAUDE.md + run_config.txt), but with --output-format
# stream-json so the dashboard can show the LLM's reasoning (left) and the
# browser actions (right) live. MODE/MAX/cap/window still come from
# run_config.txt — so set MODE: prepare for a dry run (fills, stops at Submit).
#
# This DOES NOT touch run_scheduled.sh (the 05:01 production path) — but scheduled
# runs now write the same agent_*.ndjson trace, so `attach` works on them too.
#
# Usage:  ./watch.sh            launch an observed run (Ctrl-C in the dashboard STOPS it)
#         ./watch.sh prepare    same, with a one-shot MODE override (dry run)
#         ./watch.sh attach     open the dashboard on an ALREADY-RUNNING run (e.g. the
#                               05:01 scheduled one) — Ctrl-C closes the dashboard only;
#                               the run keeps going
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || { echo "cannot cd to $DIR"; exit 1; }
# MULTI-001: per-user data root (traces/logs, run_config.txt reads+override).
# Default = script dir = today's behavior byte-identical when JOBAPPLY_DATA unset.
DATA_ROOT="${JOBAPPLY_DATA:-$DIR}"
PORT="${JOBAPPLY_CDP_PORT:-9222}"
PY="$DIR/.venv-obs/bin/python"
TS=$(date +%Y%m%d_%H%M%S)
NDJSON="$DATA_ROOT/output/agent_${TS}.ndjson"
LOG="$DATA_ROOT/output/observed_${TS}.log"
ERR="$DATA_ROOT/output/observed_${TS}.err"
mkdir -p "$DATA_ROOT/output"

[[ -x "$PY" ]] || { echo "venv missing ($PY) — run: /opt/homebrew/bin/python3.11 -m venv .venv-obs && .venv-obs/bin/pip install rich"; exit 1; }

# ---- attach mode: observe an already-running run without launching anything ----
# Finds the newest agent_*.ndjson (scheduled runs write one too) and the running claude
# PID (liveness display only — dashboard never signals it). exec = no cleanup trap here,
# so closing the dashboard CANNOT kill the run.
if [[ "${1:-}" == "attach" ]]; then
  TRACE=$(ls -t "$DATA_ROOT"/output/agent_*.ndjson 2>/dev/null | head -1)
  [[ -n "$TRACE" ]] || { echo "[attach] no agent_*.ndjson trace in output/ — nothing to attach to"; exit 1; }
  RUN_PID=$(pgrep -f "claude -p Run the LinkedIn auto-apply routine" | head -1 || true)
  echo "[attach] trace: $(basename "$TRACE") · run pid: ${RUN_PID:-none (run finished — replaying trace)}"
  exec "$PY" dashboard.py --trace "$TRACE" --db "$DATA_ROOT/applications.db" --config "$DATA_ROOT/run_config.txt" ${RUN_PID:+--pid "$RUN_PID"}
fi

ORIG_MODE_LINE=""
restore_mode() {
  [[ -n "$ORIG_MODE_LINE" ]] || return 0
  sed -i "" -E "s/^[[:space:]]*MODE:.*/${ORIG_MODE_LINE}/" "$DATA_ROOT/run_config.txt"
  echo "[restore] run_config -> ${ORIG_MODE_LINE}"
}

# Optional one-shot MODE override:  ./watch.sh prepare   (auto-restores on exit)
OVERRIDE_MODE="${1:-}"
if [[ "$OVERRIDE_MODE" == "prepare" || "$OVERRIDE_MODE" == "live" ]]; then
  ORIG_MODE_LINE=$(grep -E '^[[:space:]]*MODE:' "$DATA_ROOT/run_config.txt" | head -1)
  trap restore_mode EXIT
  sed -i "" -E "s/^[[:space:]]*MODE:.*/MODE: ${OVERRIDE_MODE}/" "$DATA_ROOT/run_config.txt"
  echo "[mode] override -> MODE: ${OVERRIDE_MODE} (auto-restores to '${ORIG_MODE_LINE}' on exit)"
elif [[ -n "$OVERRIDE_MODE" ]]; then
  echo "[mode] ignoring unknown arg '$OVERRIDE_MODE' (use: prepare | live | attach)"
fi
MODE=$(grep -E '^[[:space:]]*MODE:' "$DATA_ROOT/run_config.txt" | head -1 | awk '{print $2}')
echo "===== observed run @ $(date '+%H:%M:%S') · MODE=${MODE:-?} ====="

# STEP 1 — ensure the logged-in Chrome is up
echo "[1] launch_chrome.sh"
"$DIR/launch_chrome.sh" || { echo "[1] chrome launch failed — aborting"; exit 1; }

# STEP 2 — actively confirm login (open /feed, see where it lands)
echo "[2] confirm login (active /feed probe)"
curl -s --max-time 8 -X PUT "http://127.0.0.1:${PORT}/json/new?https://www.linkedin.com/feed/" >/dev/null 2>&1 || true
sleep 6
STATE=$(curl -s --max-time 8 "http://127.0.0.1:${PORT}/json" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print('UNREACHABLE'); raise SystemExit
urls=[(t.get('url') or '') for t in d if t.get('type')=='page']
print('LOGGED_IN' if any('/feed' in u for u in urls) else 'LOGGED_OUT')")
echo "[2] $STATE"
[[ "$STATE" == LOGGED_IN* ]] || { echo "[2] not logged in — aborting"; exit 1; }

# STEP 3 — tip + one-shot: bring the jobs tab to front WITHIN Chrome once it opens
echo "[3] TIP: put the Chrome-Agent window beside this terminal so you see both"
( sleep 25
  ID=$(curl -s --max-time 6 "http://127.0.0.1:${PORT}/json" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: raise SystemExit
j=[t for t in d if t.get('type')=='page' and '/jobs/' in (t.get('url') or '')]
print(j[0]['id'] if j else '')" 2>/dev/null)
  [[ -n "$ID" ]] && curl -s --max-time 6 "http://127.0.0.1:${PORT}/json/activate/$ID" >/dev/null 2>&1
) &

# STEP 4 — run the routine headless with stream-json (claude writes the NDJSON directly)
echo "[4] start routine (stream-json) -> $(basename "$NDJSON")"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
"$HOME/.local/bin/claude" -p "Run the LinkedIn auto-apply routine exactly per CLAUDE.md in this directory. This is an OBSERVED run: read run_config.txt fresh and obey MODE, MAX_APPLIES_PER_RUN, DAILY_APPLY_CAP, and RUN_WINDOW_LOCAL. Briefly narrate each decision as you go (which card, the gate verdict, why apply/skip) so a watcher can follow. Record successes in applications.db." \
  --model claude-sonnet-5 \
  --output-format stream-json --verbose --dangerously-skip-permissions \
  </dev/null >"$NDJSON" 2>"$ERR" &
CLAUDE_PID=$!

# STEP 5 — live dashboard in the foreground (Ctrl-C to close)
echo "[5] dashboard starting… (Ctrl-C to close)"
sleep 1
"$PY" dashboard.py --trace "$NDJSON" --db "$DATA_ROOT/applications.db" --config "$DATA_ROOT/run_config.txt" --pid "$CLAUDE_PID"

# cleanup — if the run is still going when the dashboard closes, stop it
if kill -0 "$CLAUDE_PID" 2>/dev/null; then
  echo "stopping run (dashboard closed before completion)…"
  kill "$CLAUDE_PID" 2>/dev/null
fi
wait "$CLAUDE_PID" 2>/dev/null
RC=$?

# render the readable flat log from the trace
"$PY" format_trace.py < "$NDJSON" > "$LOG" 2>&1 || true
echo "done (rc=$RC) · trace: $(basename "$NDJSON") · log: $(basename "$LOG")"