#!/usr/bin/env bash
# apply_one.sh <job_url> ["context note from the user"]
#
# QUEUE-001 Proposal-1: single-URL apply entry point. The user sends a job link (from
# their phone, recruiter outreach, anywhere); this launches ONE worker invocation
# (claude or pi, per run_config.txt) that applies to exactly that job under the full
# CLAUDE.md contract (Single-URL mode section): suppression checks + caps still bind, relevance gate is skipped (the user
# picked the job), RUN_WINDOW_LOCAL does not bind (human-initiated). Mirrors
# run_scheduled.sh's invocation conventions so the worker runs with the same env, guard
# hook, trace and log shapes.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# MULTI-001: per-user data root (user.env, run logs). Default = script dir = today's behavior.
DATA_ROOT="${JOBAPPLY_DATA:-$DIR}"
# BRAIN-001a: the worker (claude|pi) is dispatched by worker_lib.sh from run_config.txt;
# it also exports JOBAPPLY_WORKER/JOBAPPLY_WORKER_MODEL for the DB stamping.
source "$DIR/worker_lib.sh"
# MULTI-002b: bind the engine directory to THIS user's files before anything reads one
# (fails with a clear message when the user's rendered CLAUDE.md is missing).
bash "$DIR/bind_user.sh" || exit 1
# Where the worker reads the run switches from: the user's run_config.txt when a data
# root is set (absolute), the repo-relative file exactly as before when unset.
RUN_CONFIG_PROMPT="${JOBAPPLY_DATA:+$JOBAPPLY_DATA/}run_config.txt"
if [[ ! -f "$DATA_ROOT/user.env" ]]; then
  echo "ERROR: $DATA_ROOT/user.env not found — run setup/render_templates.py first" >&2
  exit 2
fi
# shellcheck disable=SC1091
source "$DATA_ROOT/user.env"

if [[ -z "${1:-}" ]]; then
  echo "usage: $0 <job_url> [\"context note from $USER_FIRST_NAME\"]"
  exit 2
fi
JOB_URL="$1"
CONTEXT="${2:-}"

# Keep the Mac fully awake for the whole apply (same guard var pattern as run_scheduled.sh).
if [[ -z "${JOBAPPLY_CAFFEINATED:-}" ]]; then
  export JOBAPPLY_CAFFEINATED=1
  exec /usr/bin/caffeinate -dimsu "$0" "$@"
fi

LOG_DIR="$DATA_ROOT/output"; mkdir -p "$LOG_DIR"
TS=$(date +%Y%m%d_%H%M%S)
LOG="$LOG_DIR/manual_${TS}.log"
exec > >(tee "$LOG") 2>&1

echo "===== single-URL apply @ $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
echo "url: $JOB_URL"
[[ -n "$CONTEXT" ]] && echo "context: $CONTEXT"

notify() {
  local msg=${1//[\"\\]/}
  /usr/bin/osascript -e "display notification \"$msg\" with title \"JobApply\"" >/dev/null 2>&1 || true
}

# Same lock domain as the scheduled run (output/.apply.lock) — a single-URL apply must never
# overlap the 05:01 round (double-apply risk). Stale-pid aware, single retake attempt.
LOCKDIR="$LOG_DIR/.apply.lock"
acquire_lock() {
  if mkdir "$LOCKDIR" 2>/dev/null; then echo $$ > "$LOCKDIR/pid"; return 0; fi
  local pid; pid=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "")
  if [[ -z "$pid" ]]; then sleep 1; pid=$(cat "$LOCKDIR/pid" 2>/dev/null || echo ""); fi
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then return 1; fi
  rm -rf "$LOCKDIR"
  if mkdir "$LOCKDIR" 2>/dev/null; then echo $$ > "$LOCKDIR/pid"; return 0; fi
  return 1
}
if ! acquire_lock; then
  echo "[lock] another apply run is live (pid $(cat "$LOCKDIR/pid" 2>/dev/null)) — refusing to start"
  notify "single-URL apply refused: another apply run is live"
  exit 1
fi
trap 'rm -rf "$LOCKDIR"' EXIT

# Worker env — identical to run_scheduled.sh STEP 3.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
export BASH_DEFAULT_TIMEOUT_MS=300000
export JOBAPPLY_GUARD=1

cd "$DIR" || { echo "cannot cd to $DIR — aborting"; exit 1; }

RUN_LOG="$LOG_DIR/run_${TS}_manual.log"
NDJSON="$LOG_DIR/agent_${TS}_manual.ndjson"

PROMPT="SINGLE-URL MODE (see the 'Single-URL mode' section of CLAUDE.md). This unattended invocation applies to EXACTLY ONE job, hand-picked by $USER_FIRST_NAME: $JOB_URL . The relevance gate is skipped ($USER_FIRST_NAME picked it) but ALL suppression checks (dedup, cooldown, active-pipeline, blocklist, ghost, duplicate-posting) and the daily cap still bind; RUN_WINDOW_LOCAL does not. Read $RUN_CONFIG_PROMPT fresh and obey MODE for the submit gate. This run's log is output/$(basename "$RUN_LOG") — append your entries and APPLY markers there, wrap up with the 'SINGLE URL COMPLETE' marker, and end with a one-line result."
if [[ -n "$CONTEXT" ]]; then
  PROMPT="$PROMPT Context from $USER_FIRST_NAME (record in run log + DB notes; NOT answer-bank material): $CONTEXT"
fi

echo "[1] launching worker — trace=$(basename "$NDJSON")"
# BRAIN-001a: worker_lib.sh launches the selected harness and stamps
# JOBAPPLY_WORKER/JOBAPPLY_WORKER_MODEL; rc=64 = launch refused (unknown WORKER / pi
# without WORKER_MODEL) — the reason is on stderr, which the tee above puts in this log.
invoke_worker "$PROMPT" 100 </dev/null >"$NDJSON"
RC=$?
echo "[1] worker exited rc=$RC"

SUMMARY="(no final summary in trace)"
if [[ -s "$NDJSON" ]]; then
  SUMMARY=$(python3 -c 'import json,sys
res=None
for line in open(sys.argv[1]):
    try: d=json.loads(line)
    except Exception: continue
    if d.get("type")=="result": res=d
print((res or {}).get("result") or "(no final summary in trace)")' "$NDJSON")
  COST=$(python3 -c 'import json,sys
res=None
for line in open(sys.argv[1]):
    try: d=json.loads(line)
    except Exception: continue
    if d.get("type")=="result": res=d
print((res or {}).get("total_cost_usd") or "?")' "$NDJSON")
  echo "[2] cost_usd=$COST"
fi
echo "[2] result: $SUMMARY"

# Completion + orphan checks (same idea as run_scheduled.sh STEP 3b, one job's worth).
if [[ -f "$RUN_LOG" ]]; then
  if ! grep -q "SINGLE URL COMPLETE" "$RUN_LOG"; then
    echo "[3] WARNING: no 'SINGLE URL COMPLETE' marker — worker was likely cut off; check $RUN_LOG"
    notify "single-URL apply may be incomplete — check the run log"
  fi
  # grep -c prints the count (0 included) even when nothing matches — no || fallback,
  # it would append a SECOND 0 and break the arithmetic ("0\n0", seen on the smoke test).
  ORPHANS=$(grep -c "^APPLY START" "$RUN_LOG" 2>/dev/null); ORPHANS=${ORPHANS:-0}
  ENDS=$(grep -c "^APPLY END" "$RUN_LOG" 2>/dev/null); ENDS=${ENDS:-0}
  if (( ORPHANS > ENDS )); then
    echo "[3] ORPHAN APPLY: an APPLY START has no END — a half-filled form may sit in a browser tab"
    notify "single-URL apply left an unfinished form — review the browser"
  fi
else
  echo "[3] WARNING: worker wrote no run log ($RUN_LOG missing) — check the trace"
fi

./db_report.sh >/dev/null 2>&1 || true
notify "single-URL apply done: ${SUMMARY:0:120}"
echo "===== done @ $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
exit "$RC"
