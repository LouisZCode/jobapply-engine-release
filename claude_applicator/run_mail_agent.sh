#!/usr/bin/env bash
# LOOP-003 mail agent — the worker AI (claude or pi, per run_config.txt) organizes Gmail
# + reconciles the DB after the apply round.
#
# Replaces the regex brain (LOOP-002 sweep) as the DAILY organizer: run_scheduled.sh STEP 4
# chains this right after the apply round finishes, so mail is sorted before the next day.
# The deterministic sweep stays in place at 05:01 pre-run + 23:00 launchd as a fallback net.
#
# The agent's only hands are inbox_reader/mail_agent_tool.py (read + label + append-only DB
# events; NO delete/trash/send code path exists) and its program is inbox_reader/MAIL_AGENT.md.
# Validated live 2026-07-28: first pass recorded 23 events, corrected Sopra→offer,
# OMQ→interviewing, Demodesk→rejected, and left the inbox at contract state.
#
# Shares output/.sweep.lock with run_mail_sweep.sh so the agent and the regex sweep can never
# race the same Gmail state. Heartbeats to output/launchd_mail.log; health domain "sweep".
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# MULTI-001: per-user data root (heartbeat log, sweep lock, health_status.json).
# Default = script dir = today's behavior byte-identical when JOBAPPLY_DATA unset.
DATA_ROOT="${JOBAPPLY_DATA:-$DIR}"
# BRAIN-001a: the worker (claude|pi) is dispatched by worker_lib.sh from run_config.txt;
# it also exports JOBAPPLY_WORKER/JOBAPPLY_WORKER_MODEL for the DB stamping.
source "$DIR/worker_lib.sh"
# MULTI-002b: bind the engine directory to THIS user's files (MAIL_AGENT.md, user_mail.json,
# tokens, ...) before the agent reads them; fails with a clear message when the user's
# rendered CLAUDE.md is missing.
bash "$DIR/bind_user.sh" || exit 1
LOG_DIR="$DATA_ROOT/output"; mkdir -p "$LOG_DIR"
HEARTBEAT="$LOG_DIR/launchd_mail.log"
TS="$(date '+%Y%m%d_%H%M%S')"
NDJSON="$LOG_DIR/mail_agent_run_${TS}.ndjson"
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
# Guard hook (2026-07-30): arm guard_hook.py for the agent (see ../.claude/settings.json) —
# it may not edit config/code/memory or mutate the DB beyond its helper's inserts.
export JOBAPPLY_GUARD=1

notify() {
  local msg=${1//[\"\\]/}
  /usr/bin/osascript -e "display notification \"$msg\" with title \"JobApply\"" >/dev/null 2>&1 || true
}

# --- lock: same domain as run_mail_sweep.sh (mkdir-atomic; stale-pid aware) ---
LOCKDIR="$LOG_DIR/.sweep.lock"
acquire_lock() {
  local lockdir="$1"
  if mkdir "$lockdir" 2>/dev/null; then
    echo $$ > "$lockdir/pid"
    return 0
  fi
  local pid
  pid=$(cat "$lockdir/pid" 2>/dev/null || echo "")
  if [[ -z "$pid" ]]; then
    sleep 1
    pid=$(cat "$lockdir/pid" 2>/dev/null || echo "")
  fi
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    return 1
  fi
  rm -rf "$lockdir"
  if mkdir "$lockdir" 2>/dev/null; then
    echo $$ > "$lockdir/pid"
    return 0
  fi
  return 1
}

LOCKED=0
LOCK_DEADLINE=$(( $(date +%s) + 60 ))
while true; do
  if acquire_lock "$LOCKDIR"; then
    LOCKED=1
    break
  fi
  [[ $(date +%s) -ge $LOCK_DEADLINE ]] && break
  sleep 5
done

if [[ "$LOCKED" -ne 1 ]]; then
  echo "[lock] mail sweep/agent already running — skipping" | tee -a "$HEARTBEAT"
  JOBHEALTH_STATUS_PATH="$DATA_ROOT/output/health_status.json" python3 "$DIR/health_update.py" sweep 0 "skipped: mail lock held" || true
  exit 0
fi
trap 'rm -rf "$LOCKDIR"' EXIT

JOBHEALTH_STATUS_PATH="$DATA_ROOT/output/health_status.json" python3 "$DIR/health_update.py" sweep start || true
echo "===== mail-AGENT START @ $(date '+%Y-%m-%d %H:%M:%S %Z') =====" >> "$HEARTBEAT"

PROMPT="You are the LOOP-003 mail agent. Working directory: $DIR (cd there first). Read inbox_reader/MAIL_AGENT.md — it is your complete program — and execute it now: load the corpus, work the inbox, run the reconcile pass, write the report log, refresh db_report.sh, and end with the summary counts."

cd "$DIR"
# BRAIN-001a: worker_lib.sh launches the selected harness; rc=64 = launch refused
# (unknown WORKER / pi without WORKER_MODEL) — a failed run. The worker's stderr (the
# refusal reason included) goes to the heartbeat log, not just the parent's stderr.
invoke_worker "$PROMPT" 100 </dev/null >"$NDJSON" 2>>"$HEARTBEAT"
RC=$?

# cost + one-line summary out of the trace's final result event (best-effort). A pi run
# prints its final text on stdout and emits no Claude "result" event — say so instead
# of failing on the missing cost line.
python3 - "$NDJSON" >> "$HEARTBEAT" 2>/dev/null <<'PY' || true
import json, sys
cost, txt = None, ""
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if ev.get("type") == "result":
        cost = ev.get("total_cost_usd")
        txt = (ev.get("result") or "")[:300].replace("\n", " ")
if cost is not None:
    print("[mail-agent ${:.2f}] {}".format(cost, txt))
else:
    print("[mail-agent $0.00] (no cost event from this harness)")
PY

"$DIR/db_report.sh" >/dev/null 2>&1 || true
echo "===== mail-AGENT END   @ $(date '+%Y-%m-%d %H:%M:%S %Z')  rc=$RC =====" >> "$HEARTBEAT"

JOBHEALTH_STATUS_PATH="$DATA_ROOT/output/health_status.json" python3 "$DIR/health_update.py" sweep "$RC" "mail agent rc=$RC" || true
if [[ "$RC" -ne 0 ]]; then
  notify "mail agent failed rc=$RC"
fi

exit "$RC"
