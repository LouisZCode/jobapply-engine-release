#!/usr/bin/env bash
# RB-1 health check — launchd (<label_prefix>.jobhealth, 09:00 daily) reads
# output/health_status.json (written by health_update.py from run_scheduled.sh /
# run_mail_sweep.sh) and alerts via a macOS notification when either scheduled
# job looks unhealthy:
#   (a) the apply run's last_ok_end isn't today (05:01 run failed or the Mac
#       never woke to fire it — this script runs at 09:00, well after it should
#       have finished)
#   (b) the sweep's last_ok_end is more than 26h old
#   (c) either job's last_rc is nonzero
# Always appends one status line to output/health_check.log; always exits 0
# (a health check must never itself become a thing that needs checking).
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# MULTI-001: per-user data root (defaults to this script's dir = today's location).
DATA_ROOT="${JOBAPPLY_DATA:-$DIR}"
STATUS="$DATA_ROOT/output/health_status.json"
LOG="$DATA_ROOT/output/health_check.log"
mkdir -p "$DATA_ROOT/output"

notify() {
  local msg=${1//[\"\\]/}   # strip quotes/backslashes — $1 is interpolated into AppleScript
  /usr/bin/osascript -e "display notification \"$msg\" with title \"JobApply\"" >/dev/null 2>&1 || true
}

RESULT=$(python3 - "$STATUS" <<'PYEOF'
import datetime
import json
import sys

path = sys.argv[1]
try:
    with open(path) as f:
        state = json.load(f)
except Exception:
    state = {}

today = datetime.date.today().isoformat()
now = datetime.datetime.now()
alerts = []

apply = state.get("apply", {})
sweep = state.get("sweep", {})

apply_ok = (apply.get("last_ok_end") or "")[:10]
if apply_ok != today:
    alerts.append("apply run did not complete OK today (last_ok_end={})".format(
        apply.get("last_ok_end") or "never"))

sweep_ok_raw = sweep.get("last_ok_end")
sweep_stale = True
if sweep_ok_raw:
    try:
        sweep_ok_dt = datetime.datetime.fromisoformat(sweep_ok_raw)
        sweep_stale = (now - sweep_ok_dt) > datetime.timedelta(hours=26)
    except Exception:
        sweep_stale = True
if sweep_stale:
    alerts.append("mail sweep last_ok_end older than 26h (last_ok_end={})".format(
        sweep_ok_raw or "never"))

for job_name, entry in (("apply", apply), ("sweep", sweep)):
    rc = entry.get("last_rc")
    if rc not in (None, 0):
        alerts.append("{} last_rc={}".format(job_name, rc))

if alerts:
    print("ALERT: " + " | ".join(alerts))
else:
    print("OK: apply and sweep healthy")
PYEOF
)

echo "$(date '+%Y-%m-%d %H:%M:%S') $RESULT" >> "$LOG"

if [[ "$RESULT" == ALERT:* ]]; then
  notify "${RESULT#ALERT: }"
fi

exit 0
