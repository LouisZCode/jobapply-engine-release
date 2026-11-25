#!/usr/bin/env bash
# LOOP-002 nightly Gmail response sweep — launchd entry point (<label_prefix>.jobmail,
# 23:00 daily; label prefix generated at setup).
#
# Thin + deterministic (F-4 Part E): run the read+label Gmail sweep — it writes
# application_events, recomputes outcome rollups, and files each processed mail under the
# user's configured sweep label (inbox_reader/user_mail.json — removing INBOX for
# acks/rejections, keeping actionable mail) — then refresh the DB report mirror. No Chrome,
# no caffeinate, no claude: seconds of Gmail API work. If the Mac is asleep at 23:00 the job
# fires on next wake, which is fine — the sweep is idempotent on msg_id (INSERT OR IGNORE)
# and scans a 14-day inbox window, so nothing is missed.
#
# Appends a START / END / rc heartbeat to output/launchd_mail.log so a run that dies BEFORE
# Python writes its own output/mail_sweep_<ts>.log still leaves a trace (the OPS-001 gap).
#
# RB-2 lock: run_scheduled.sh ALSO invokes this script (pre + post apply round), and the 23:00
# launchd job can fire independently — output/.sweep.lock/ (separate domain from the apply lock
# in output/.apply.lock/, so the apply run calling into this script can never deadlock on its
# own lock) makes overlapping sweeps a no-op instead of two processes racing the same Gmail
# state. macOS has no `flock`/`timeout`; mkdir is the atomic primitive.
# RB-1: health_update.py records start/end/rc so check_health.sh (09:00) can see this job ran.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# MULTI-001: per-user data root (heartbeat log, sweep lock, health_status.json).
# Default = script dir = today's behavior byte-identical when JOBAPPLY_DATA unset.
DATA_ROOT="${JOBAPPLY_DATA:-$DIR}"
# MULTI-002b: bind the engine directory to THIS user's files (user_mail.json, tokens,
# credentials, applications.db) before the sweep reads them; fails with a clear message
# when the user's rendered CLAUDE.md is missing.
bash "$DIR/bind_user.sh" || exit 1
PY="$DIR/inbox_reader/.venv/bin/python"
SWEEP="$DIR/inbox_reader/sweep_responses.py"
LOG_DIR="$DATA_ROOT/output"; mkdir -p "$LOG_DIR"
HEARTBEAT="$LOG_DIR/launchd_mail.log"

notify() {
  local msg=${1//[\"\\]/}   # strip quotes/backslashes — $1 is interpolated into AppleScript
  /usr/bin/osascript -e "display notification \"$msg\" with title \"JobApply\"" >/dev/null 2>&1 || true
}

# --- RB-2 lock: mkdir-based (macOS has no flock); stale-pid aware ---
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
    sleep 1   # holder may be between mkdir and pid-write — re-read before calling it stale
    pid=$(cat "$lockdir/pid" 2>/dev/null || echo "")
  fi
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    return 1   # genuinely held by a live process
  fi
  rm -rf "$lockdir"   # stale (holder is dead) — clear and retake once
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
  echo "[lock] sweep already running — skipping this run" | tee -a "$HEARTBEAT"
  JOBHEALTH_STATUS_PATH="$DATA_ROOT/output/health_status.json" python3 "$DIR/health_update.py" sweep 0 "skipped: sweep already running" || true
  exit 0
fi
trap 'rm -rf "$LOCKDIR"' EXIT

JOBHEALTH_STATUS_PATH="$DATA_ROOT/output/health_status.json" python3 "$DIR/health_update.py" sweep start || true

echo "===== mail-sweep START @ $(date '+%Y-%m-%d %H:%M:%S %Z') =====" >> "$HEARTBEAT"
"$PY" "$SWEEP" >> "$HEARTBEAT" 2>&1
RC=$?
"$DIR/db_report.sh" >/dev/null 2>&1 || true   # refresh applications_report.txt at the repo root
echo "===== mail-sweep END   @ $(date '+%Y-%m-%d %H:%M:%S %Z')  rc=$RC =====" >> "$HEARTBEAT"

JOBHEALTH_STATUS_PATH="$DATA_ROOT/output/health_status.json" python3 "$DIR/health_update.py" sweep "$RC" "mail sweep rc=$RC" || true
if [[ "$RC" -ne 0 ]]; then
  notify "mail sweep failed rc=$RC"
fi

exit "$RC"
