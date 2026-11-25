#!/usr/bin/env bash
# Scheduled entry point, fired by the launchd LaunchAgent (<label_prefix>.jobapply,
# generated at setup — see PRODUCT_INVENTORY.md).
#
# Cold-open (or reuse) the logged-in Chrome, ACTIVELY verify the LinkedIn session by probing
# /feed over CDP, then run the apply routine headless via `claude -p` (STEP 3).
# See claude_applicator/CLAUDE.md.
set -uo pipefail

# Keep the Mac FULLY awake for the entire run. An idle Mac here sleeps ~1 min after
# launchd fires (AC `sleep` timer is 1 min); on 2026-06-11 that let the routine thaw only
# ~1 min per 15-min DarkWake and take 1.5h for a single apply. Re-exec once under
# caffeinate (-d display, -i idle, -m disk, -s system[AC], -u user-active); the guard var
# stops an infinite re-exec loop. Covers scheduled AND manual runs.
if [[ -z "${JOBAPPLY_CAFFEINATED:-}" ]]; then
  export JOBAPPLY_CAFFEINATED=1
  exec /usr/bin/caffeinate -dimsu "$0" "$@"
fi

# Script-relative, so the same file works from the production checkout AND a test
# worktree (P1 branch) without operating on the wrong directory's state.
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# MULTI-001: per-user data root (run logs, costs.csv, health_status.json, run_config.txt).
# Defaults to this script's dir = today's behavior byte-identical when JOBAPPLY_DATA unset.
DATA_ROOT="${JOBAPPLY_DATA:-$DIR}"
# MULTI-002b: bind the engine directory to THIS user's files (repo-relative personal
# files become symlinks into the data root) before anything below reads one. Exits 1
# with a clear message when the user's rendered CLAUDE.md is missing — never continue a
# run that would use another user's (or the repo default's) files.
bash "$DIR/bind_user.sh" || exit 1
# Where the worker reads the run switches from: the user's run_config.txt when a data
# root is set (absolute), the repo-relative file exactly as before when unset.
RUN_CONFIG_PROMPT="${JOBAPPLY_DATA:+$JOBAPPLY_DATA/}run_config.txt"
# Cross-checkout guard: the mkdir-lock below can't see across checkouts. When running
# from anywhere OTHER than production, refuse to start while production's apply lock is
# held by a live process (a test run overlapping the scheduled prod run = double-apply
# risk). Derived from this script's own location (no hard-coded install path) — in a
# single-checkout product install DIR always equals PROD_APPLICATOR, so this is a no-op;
# it only bites in a dev setup with a second worktree pointed at the same DB/lock dir.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD_APPLICATOR="$SCRIPT_DIR"
if [[ "$DIR" != "$PROD_APPLICATOR" ]]; then
  PROD_PID=$(cat "$PROD_APPLICATOR/output/.apply.lock/pid" 2>/dev/null || echo "")
  if [[ -n "$PROD_PID" ]] && kill -0 "$PROD_PID" 2>/dev/null; then
    echo "[lock] PRODUCTION apply run is live (pid $PROD_PID) — refusing to run from $DIR"
    exit 0
  fi
fi
LOG_DIR="$DATA_ROOT/output"; mkdir -p "$LOG_DIR"
TS=$(date +%Y%m%d_%H%M%S)
LOG="$LOG_DIR/scheduled_${TS}.log"
PORT="${JOBAPPLY_CDP_PORT:-9222}"
exec > >(tee "$LOG") 2>&1

echo "===== scheduled run @ $(date '+%Y-%m-%d %H:%M:%S %Z') ====="

# --- RB-1 notify helper: best-effort macOS notification, never fails the script ---
notify() {
  local msg=${1//[\"\\]/}   # strip quotes/backslashes — $1 is interpolated into AppleScript
  /usr/bin/osascript -e "display notification \"$msg\" with title \"JobApply\"" >/dev/null 2>&1 || true
}

# --- RB-2 lock: mkdir-based (macOS has no flock); stale-pid aware. Guards against two
# apply runs overlapping (double-submit risk) — a manual run started while the 05:01
# launchd run is still going, for example. Deliberately its OWN lock domain
# (output/.apply.lock), separate from the sweep's output/.sweep.lock: this script calls
# into run_mail_sweep.sh below, and a shared lock would deadlock on itself. Acquired here,
# AFTER the caffeinate re-exec guard above, so the exec never drops a held lock. ---
LOCKDIR="$LOG_DIR/.apply.lock"
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

if ! acquire_lock "$LOCKDIR"; then
  HELD_PID=$(cat "$LOCKDIR/pid" 2>/dev/null || echo "?")
  echo "[lock] another apply run is live (pid $HELD_PID) — skipping this run"
  notify "apply run skipped: another run is live (pid $HELD_PID)"
  JOBHEALTH_STATUS_PATH="$DATA_ROOT/output/health_status.json" python3 "$DIR/health_update.py" apply 0 "skipped: lock held" || true
  exit 0
fi
trap 'rm -rf "$LOCKDIR"' EXIT

JOBHEALTH_STATUS_PATH="$DATA_ROOT/output/health_status.json" python3 "$DIR/health_update.py" apply start || true

# STEP 0 — nightly mail sweep, run before AND after the apply round (see STEP 4 below) so
# response mail filed overnight (or just now, mid-run) is reflected before/after applying.
# Product guard (2026-08-19): the Gmail reader is optional. Without its venv + a stored
# OAuth token the sweep can only fail (rc=127) and the step-4 agent only burns tokens
# hunting for a venv that isn't there (and its `find /` walks trip macOS folder-access
# prompts), so both mail steps are skipped outright until Gmail is actually set up.
MAIL_READY=0
if [[ -x "$DIR/inbox_reader/.venv/bin/python" && -f "$DIR/inbox_reader/token_modify.json" ]]; then
  MAIL_READY=1
fi
if [[ -n "${JOBAPPLY_SKIP_SWEEPS:-}" ]]; then
  echo "[0] pre-run mail sweep SKIPPED (JOBAPPLY_SKIP_SWEEPS set — test run)"
elif [[ "$MAIL_READY" -ne 1 ]]; then
  echo "[0] pre-run mail sweep SKIPPED (Gmail reader not set up on this Mac)"
else
  echo "[0] pre-run mail sweep"
  "$DIR/run_mail_sweep.sh" || true
fi

# STEP 0b — periodic DB backup (2026-07-30): applications.db is weeks of irreplaceable
# outcome data. Take a .backup snapshot when the newest one is 3+ days old; keep the
# last 10 (~a month of history). *.db is gitignored, so backups never reach the repo.
BK_DIR="$DATA_ROOT/backups"; mkdir -p "$BK_DIR"   # MULTI-001: backups are per-user data → data root (default = $DIR/backups, unchanged unset)
NEWEST_BK=$(ls -t "$BK_DIR"/applications_*.db 2>/dev/null | head -1)
if [[ -z "$NEWEST_BK" || $(( $(date +%s) - $(stat -f %m "$NEWEST_BK") )) -ge $(( 3*86400 )) ]]; then
  if sqlite3 "$DATA_ROOT/applications.db" ".backup '$BK_DIR/applications_$(date +%Y%m%d).db'"; then
    echo "[0b] DB backup written: backups/applications_$(date +%Y%m%d).db"
    # portable CSV copies alongside the snapshot (freshened post-round by db_to_csv.sh)
    for f in "${JOBAPPLY_DATA:-$DIR/..}/applications.csv" "${JOBAPPLY_DATA:-$DIR/..}/application_events.csv"; do
      [[ -f "$f" ]] && cp "$f" "$BK_DIR/$(basename "${f%.csv}")_$(date +%Y%m%d).csv"
    done
    ls -t "$BK_DIR"/applications_*.db 2>/dev/null | tail -n +11 | xargs rm -f 2>/dev/null || true
    ls -t "$BK_DIR"/*_2*.csv 2>/dev/null | tail -n +21 | xargs rm -f 2>/dev/null || true
  else
    echo "[0b] WARN: DB backup FAILED — continuing the run"
  fi
else
  echo "[0b] DB backup fresh enough ($(basename "$NEWEST_BK"))"
fi

# STEP 1 — cold-open the logged-in Chrome (the everyday first step)
echo "[1] launch_chrome.sh"
"$DIR/launch_chrome.sh" || {
  echo "[1] FAILED — aborting scheduled run"
  JOBHEALTH_STATUS_PATH="$DATA_ROOT/output/health_status.json" python3 "$DIR/health_update.py" apply 1 "launch_chrome.sh failed" || true
  notify "apply run FAILED: launch_chrome.sh"
  exit 1
}

# STEP 2 — ACTIVELY verify login: open a /feed tab over CDP and see where it lands.
# A passive read of the first tab misfires when a stale Chrome has 0 tabs or the front tab
# isn't the feed — that exact false-negative skipped the 2026-06-09 run. So we open /feed
# ourselves, then pass only if a tab actually sits on /feed (fail on login/authwall/checkpoint).
echo "[2] verify CDP + login (active /feed probe)"
curl -s --max-time 8 -X PUT "http://127.0.0.1:${PORT}/json/new?https://www.linkedin.com/feed/" >/dev/null 2>&1 \
  || echo "[2] WARN: /json/new probe failed; reading existing tabs anyway"
sleep 7   # let /feed render or redirect to login
STATE=$(curl -s --max-time 8 "http://127.0.0.1:${PORT}/json" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print('UNREACHABLE'); raise SystemExit
urls=[(t.get('url') or '') for t in d if t.get('type')=='page']
feed=[u for u in urls if '/feed' in u]
out =[u for u in urls if any(x in u for x in ('login','authwall','uas/login','checkpoint'))]
if   feed: print('LOGGED_IN  '+feed[0])
elif out:  print('LOGGED_OUT '+out[0])
else:      print('LOGGED_OUT (no /feed tab; urls='+(','.join(urls) or 'none')+')')")
echo "[2] $STATE"

# STEP 3 — run the apply routine headless. The routine self-gates on MODE / daily cap /
# run window (read fresh from run_config.txt), so a fixed 07:01 cadence is safe. Needs
# /opt/homebrew/bin on PATH so claude can spawn the npx (playwright) + uvx (sqlite) MCP
# servers, and ~/.local/bin for the claude binary (native installer);
# --dangerously-skip-permissions because -p is non-interactive (no prompt to answer).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:$PATH"
# Foreground-Bash timeout backstop: the harness does NOT block past a foreground command's
# timeout (default 120s) — it moves the still-running command to the BACKGROUND, which
# strands the agent mid-gap and its "waiting..." message then terminates the -p run (that is
# how the 2026-07-22 run died at 1 card / 0 applies on a >120s APPLY_GAP sleep). Raise the
# default so the 90-240s gap sleeps always block in the foreground.
export BASH_DEFAULT_TIMEOUT_MS=300000
# Guard hook (2026-07-30): arm guard_hook.py (wired in ../.claude/settings.json) for every
# claude worker this wrapper launches (incl. the step-4 mail agent, which inherits the env) —
# workers may not edit config/code or mutate the DB beyond SELECT + plain INSERT; denied
# attempts surface as ISSUE: lines in the run log instead.
export JOBAPPLY_GUARD=1
# MULTI-002b: .mcp.json's playwright args expand ${JOBAPPLY_CDP_PORT:-9222} and
# ${JOBAPPLY_DATA:-.}/output from the worker's environment — make sure both are exported
# for the claude invocations below (JOBAPPLY_DATA only when actually set: exporting an
# empty default here would push "user mode" into the child scripts, e.g. bind_user.sh).
export JOBAPPLY_CDP_PORT="${JOBAPPLY_CDP_PORT:-$PORT}"
if [[ -n "${JOBAPPLY_DATA:-}" ]]; then export JOBAPPLY_DATA; fi
# DATA-001: the worker model is defined ONCE here. The `claude -p` invocation below and the
# DB stamp the worker writes (worker/model columns, CLAUDE.md step 8) both read it, so the
# recorded model can never drift from what actually ran. Exported env (not a config file)
# because the sqlite MCP server inherits the environment (.mcp.json has no env block).
WORKER_MODEL=claude-sonnet-5
export JOBAPPLY_WORKER=claude
export JOBAPPLY_WORKER_MODEL="$WORKER_MODEL"
RC=0
SUMMARY=""
if [[ "$STATE" == LOGGED_IN* ]]; then
  # --- P1 context resets: ONE `claude -p` INVOCATION PER SEARCH QUERY -----------------
  # A single all-day session re-reads its whole growing history every turn — ~90% of the
  # 2026-07-22 run's $98 was cache-reads by turn 663. Per-query invocations restart from
  # a small fresh context instead. All real state already lives OUTSIDE the context:
  # dedup in applications.db, switches in run_config.txt, Chrome keeps its tabs between
  # invocations, and this round's evaluations accumulate in ONE shared run log every
  # invocation reads first and appends to. A crash now costs one query, not the day.
  # Wrapper-side switches (the routine still re-reads run_config.txt fresh itself —
  # these copies are only for the loop's cap arithmetic):
  MODE=$(sed -n 's/^MODE:[[:space:]]*\([a-z]*\).*/\1/p' "$DATA_ROOT/run_config.txt" | head -1)
  DAILY_CAP=$(sed -n 's/^DAILY_APPLY_CAP:[[:space:]]*\([0-9]*\).*/\1/p' "$DATA_ROOT/run_config.txt" | head -1)
  MAX_PER_RUN=$(sed -n 's/^MAX_APPLIES_PER_RUN:[[:space:]]*\([0-9]*\).*/\1/p' "$DATA_ROOT/run_config.txt" | head -1)
  MAX_PER_RUN="${JOBAPPLY_MAX_APPLIES:-$MAX_PER_RUN}"   # test knob: cap applies for a one-off test run
  QUERY_COUNT="${JOBAPPLY_QUERY_COUNT:-9}"   # test knob: cover only the first N queries
  SHARED_RUN_LOG="$LOG_DIR/run_${TS}.log"
  # Rolling-24h usage, mirroring the routine's own cap query (prepare counts prepared).
  count_used() {
    if [[ "$MODE" == "prepare" ]]; then
      sqlite3 "$DATA_ROOT/applications.db" "SELECT COUNT(*) FROM applications WHERE status='prepared' AND status_updated_at >= datetime('now','-24 hours');"
    else
      sqlite3 "$DATA_ROOT/applications.db" "SELECT COUNT(*) FROM applications WHERE status='applied' AND applied_at >= datetime('now','-24 hours');"
    fi
  }
  cd "$DIR" || { echo "[3] cannot cd to $DIR — aborting"; exit 1; }
  # Optional round budget (test knob): cap the number of NEW applications THIS ROUND adds,
  # independent of the rolling-24h DAILY_APPLY_CAP. Needed when the DB is shared with
  # production (live tests): the daily cap alone would let a test round keep applying up
  # to 13 on top of the morning production run. Unset = no extra limit (prod behavior).
  ROUND_BUDGET="${JOBAPPLY_ROUND_BUDGET:-}"
  USED0=$(count_used 2>/dev/null || echo 0)
  ROUND_DONE=0
  for Q in $(seq 1 "$QUERY_COUNT"); do
    # Red-flag stop: a CAPTCHA/verify/unusual-activity halt written by ANY invocation
    # stops the WHOLE round — do not keep hammering LinkedIn with the later queries.
    if [[ -f "$SHARED_RUN_LOG" ]] && grep -q "^RED FLAG" "$SHARED_RUN_LOG"; then
      echo "[3] RED FLAG found in $(basename "$SHARED_RUN_LOG") — stopping the round before query #$Q"
      notify "apply run STOPPED on red flag — check the run log"
      RC=1
      break
    fi
    USED=$(count_used 2>/dev/null || echo 0)
    if (( USED >= DAILY_CAP )); then
      echo "[3] daily cap reached ($USED/$DAILY_CAP) — stopping before query #$Q"
      break
    fi
    # --- Whale mitigation: hard turn cap per invocation + continuation attempts. -------
    # The 2026-07-23 benchmark put 66% of round cost in two 290+-turn invocations (the
    # discovery query and the week-wide backfill) — context snowballs WITHIN a big query
    # exactly like it used to across the whole day. So: cap every invocation at 150
    # turns, and when one ends without writing its "QUERY #N COMPLETE" marker to the
    # shared log (turn-capped or crashed), relaunch the SAME query as a CONTINUATION —
    # a fresh context that reads the log's "QUERY #N PAGE P DONE" markers and resumes
    # at the first unfinished page. Up to 3 attempts per query.
    ATTEMPT=1
    while :; do
      USED=$(count_used 2>/dev/null || echo 0)
      if (( USED >= DAILY_CAP )); then
        echo "[3] daily cap reached ($USED/$DAILY_CAP) — ending query #$Q attempts"
        ROUND_DONE=1
        break
      fi
      if [[ -n "$ROUND_BUDGET" ]] && (( USED - USED0 >= ROUND_BUDGET )); then
        echo "[3] round budget reached ($(( USED - USED0 ))/$ROUND_BUDGET new applications this round) — ending the round"
        ROUND_DONE=1
        break
      fi
      SLOTS=$(( DAILY_CAP - USED ))
      (( SLOTS > MAX_PER_RUN )) && SLOTS=$MAX_PER_RUN
      if [[ -n "$ROUND_BUDGET" ]]; then
        REMAIN_ROUND=$(( ROUND_BUDGET - (USED - USED0) ))
        (( SLOTS > REMAIN_ROUND )) && SLOTS=$REMAIN_ROUND
      fi
      NDJSON="$LOG_DIR/agent_${TS}_q${Q}_a${ATTEMPT}.ndjson"
      BASE_PROMPT="SINGLE-QUERY MODE (see the 'Single-query mode' section of CLAUDE.md). This unattended invocation covers ONLY search query #$Q from profile.yaml job_search.search_queries — never any other query. Apply at most $SLOTS times in this invocation (the daily cap is folded into that number, but still run the per-apply DB cap check as the routine requires). Today's shared run log is output/$(basename "$SHARED_RUN_LOG"): read it in preflight and skip on sight anything an earlier invocation already evaluated, then APPEND your own entries to it. Read $RUN_CONFIG_PROMPT fresh and obey MODE and RUN_WINDOW_LOCAL. Briefly narrate each decision as you go. Do NOT run db_report.sh (the wrapper runs it once after all queries)."
      if (( ATTEMPT == 1 )); then
        PROMPT="$BASE_PROMPT"
      else
        PROMPT="$BASE_PROMPT CONTINUATION (attempt $ATTEMPT): a previous invocation of query #$Q was cut off mid-query (turn cap or error). Read the shared run log FIRST: results pages marked 'QUERY #$Q PAGE <P> DONE' are finished — re-open the query's filtered search URL (CLAUDE.md step 3), paginate directly to the first page of query #$Q WITHOUT a DONE marker, and continue from there. Do not re-evaluate anything already in the log."
      fi
      echo "[3] query #$Q/$QUERY_COUNT attempt $ATTEMPT (cap $USED/$DAILY_CAP used; up to $SLOTS) — trace=$(basename "$NDJSON")"
      "$HOME/.local/bin/claude" -p "$PROMPT" \
        --model "$WORKER_MODEL" \
        --max-turns 250 \
        --output-format stream-json --verbose \
        --dangerously-skip-permissions </dev/null >"$NDJSON"
      QRC=$?
      echo "[3] query #$Q attempt $ATTEMPT claude exited rc=$QRC"
      # NOTE: an attempt's rc is deliberately NOT folded into the round's RC here. A
      # turn-capped attempt exits rc=1 (error_max_turns) by design and its continuation
      # usually finishes the query — the query's real verdict is the COMPLETE-marker
      # check after this loop. Folding rc=1 in here made healthy rounds notify as
      # failures (2026-07-24 benchmark: 3/3 applies, rc=1 from two expected turn-caps).
      if [[ -s "$NDJSON" ]]; then
        QSUM=$(python3 -c 'import json,sys
res=None
for line in open(sys.argv[1]):
    try: d=json.loads(line)
    except Exception: continue
    if d.get("type")=="result": res=d
print((res or {}).get("result") or "(no final summary in trace)")' "$NDJSON")
        QCOST=$(python3 -c 'import json,sys
res=None
for line in open(sys.argv[1]):
    try: d=json.loads(line)
    except Exception: continue
    if d.get("type")=="result": res=d
print(round((res or {}).get("total_cost_usd") or 0,2))' "$NDJSON")
        echo "[q$Q.a$ATTEMPT \$${QCOST}] $QSUM"
        SUMMARY="${SUMMARY}[q$Q.a$ATTEMPT] ${QSUM} "
        PY="$DIR/.venv-obs/bin/python"
        [[ -x "$PY" ]] && "$PY" "$DIR/format_trace.py" < "$NDJSON" > "$LOG_DIR/observed_${TS}_q${Q}_a${ATTEMPT}.log" 2>&1
      fi
      # Done with this query? The agent's wrap-up writes an explicit greppable marker.
      if [[ -f "$SHARED_RUN_LOG" ]] && grep -q "QUERY #$Q COMPLETE" "$SHARED_RUN_LOG"; then
        break
      fi
      # A red flag ends the attempts too (the outer loop's check then ends the round).
      if [[ -f "$SHARED_RUN_LOG" ]] && grep -q "^RED FLAG" "$SHARED_RUN_LOG"; then
        break
      fi
      if (( ATTEMPT >= 3 )); then
        echo "[3] query #$Q still incomplete after $ATTEMPT attempts — moving on"
        break
      fi
      ATTEMPT=$(( ATTEMPT + 1 ))
    done
    if (( ROUND_DONE )); then
      break   # cap or round budget reached mid-query — a successful end, no marker needed
    fi
    # The query's verdict: COMPLETE marker written -> success regardless of how many
    # attempts were turn-capped along the way. No marker after all attempts -> the query
    # genuinely failed (crash loop, unrecoverable error, red flag) -> degrade the round.
    if [[ ! -f "$SHARED_RUN_LOG" ]] || ! grep -q "QUERY #$Q COMPLETE" "$SHARED_RUN_LOG"; then
      echo "[3] query #$Q ended without a COMPLETE marker after $ATTEMPT attempt(s) (last rc=$QRC) — flagging the round"
      RC=1
    fi
  done
  # Refresh the human-readable DB mirror ONCE for the whole round (the single-query
  # invocations are told to skip it — saves a turn per query), plus the portable CSV
  # exports (applications.csv + application_events.csv — repo root, or $JOBAPPLY_DATA when set).
  ./db_report.sh >/dev/null 2>&1 || true
  ./db_to_csv.sh --no-open >/dev/null 2>&1 || true
  echo "[3] round complete — shared run log: $(basename "$SHARED_RUN_LOG")"

  # STEP 3b — orphan-apply check + stray-tab cleanup (Proposal-1, added 2026-08-01).
  # A turn-capped worker dies silently. If it died MID-FORM, its 'APPLY START' marker has no
  # matching 'APPLY END': that application is NOT in the DB and may sit half-filled in a
  # browser tab — surface it loudly for human review. Then close every leftover tab over CDP
  # (workers keep tabs between invocations by design; post-round they are dead weight), keeping
  # one /feed tab so Chrome stays up and logged in. Deterministic — no claude invocation.
  echo "[3b] orphan-apply check + stray-tab cleanup"
  if [[ -f "$SHARED_RUN_LOG" ]]; then
    ORPHANS=$(python3 - "$SHARED_RUN_LOG" <<'PYEOF'
import re, sys
starts, ends = {}, set()
for line in open(sys.argv[1], errors="replace"):
    m = re.search(r'APPLY START:\s*job_id=(\S+?)\s*\|\s*(.*)', line)
    if m: starts[m.group(1)] = m.group(2).strip()
    m = re.search(r'APPLY END:\s*job_id=(\S+)', line)
    if m: ends.add(m.group(1))
for jid, rest in starts.items():
    if jid not in ends:
        print(f"job_id={jid} | {rest}")
PYEOF
)
    if [[ -n "$ORPHANS" ]]; then
      echo "[3b] ORPHANED APPLY — started but never finished; NOT in the DB, check by hand:"
      printf '%s\n' "$ORPHANS" | sed 's/^/[3b]   /'
      while IFS= read -r OLINE; do
        echo "ORPHAN APPLY: $OLINE" >> "$SHARED_RUN_LOG"
      done <<< "$ORPHANS"
      notify "apply run: orphaned application(s) need a manual check — see the run log"
    else
      echo "[3b] no orphaned applies (every APPLY START has an APPLY END)"
    fi
  fi
  CLOSE_IDS=$(curl -s --max-time 8 "http://127.0.0.1:${PORT}/json" | python3 -c '
import json, sys
try:
    targets = json.load(sys.stdin)
except Exception:
    sys.exit(0)   # CDP unreachable — nothing to clean
pages = [t for t in targets if t.get("type") == "page"]
keep = next((t["id"] for t in pages if "linkedin.com/feed" in (t.get("url") or "")), None)
if keep is None and pages:
    keep = pages[0]["id"]   # never close the last tab — Chrome would quit and drop the session
for t in pages:
    if t["id"] != keep:
        print(t["id"])')
  TABS_CLOSED=0
  for TID in $CLOSE_IDS; do
    curl -s --max-time 5 "http://127.0.0.1:${PORT}/json/close/$TID" >/dev/null 2>&1 && TABS_CLOSED=$((TABS_CLOSED+1))
  done
  echo "[3b] stray tabs closed: ${TABS_CLOSED}"
else
  echo "[3] SKIPPED routine — browser not logged in ($STATE)"
  SUMMARY="skipped: browser not logged in ($STATE)"
fi

# STEP 4 — post-run MAIL AGENT (LOOP-003): the Sonnet agent organizes Gmail + reconciles the
# DB right after the apply round, so mail is sorted before the next day. Falls back to the
# deterministic regex sweep if the agent run fails (rc!=0) — both are msg_id-idempotent.
if [[ -n "${JOBAPPLY_SKIP_SWEEPS:-}" ]]; then
  echo "[4] post-run mail agent SKIPPED (JOBAPPLY_SKIP_SWEEPS set — test run)"
elif [[ "$MAIL_READY" -ne 1 ]]; then
  echo "[4] post-run mail agent SKIPPED (Gmail reader not set up on this Mac)"
else
  echo "[4] post-run mail agent"
  "$DIR/run_mail_agent.sh" || { echo "[4] mail agent failed — falling back to regex sweep"; "$DIR/run_mail_sweep.sh" || true; }
fi

# Cost ledger (2026-07-30): one CSV row per round so config/behavior changes can be
# compared run-over-run. Costs are the API-equivalent meters from the worker traces
# (usage runs on the subscription — this is a comparison series, not a bill).
APPLIES_TODAY=$(sqlite3 "$DATA_ROOT/applications.db" "SELECT COUNT(*) FROM applications WHERE date(applied_at)=date('now');" 2>/dev/null || echo "")
ROUND_COST=$(grep -oE '^\[q[0-9]+\.a[0-9]+ \$[0-9.]+' "$LOG" 2>/dev/null | awk -F'$' '{s+=$2} END {printf "%.2f", s+0}')
MAIL_COST=$(tail -60 "$LOG_DIR/launchd_mail.log" 2>/dev/null | grep -oE '^\[mail-agent \$[0-9.]+' | tail -1 | awk -F'$' '{printf "%.2f", $2+0}')
CSV="$LOG_DIR/costs.csv"
[[ -f "$CSV" ]] || echo "date,applies_today,round_cost_usd,mail_agent_cost_usd,round_rc" > "$CSV"
echo "$(date +%Y-%m-%d),${APPLIES_TODAY},${ROUND_COST:-0},${MAIL_COST:-0},$RC" >> "$CSV"
echo "[cost] round \$${ROUND_COST:-0} + mail \$${MAIL_COST:-0} | applies today: ${APPLIES_TODAY} | ledger: output/costs.csv"

echo "[done] scheduled run complete @ $(date '+%H:%M:%S')"

# RB-1: one-line summary reused from the claude trace's final result when available.
ONELINE=$(printf '%s' "$SUMMARY" | tr '\n' ' ' | cut -c1-300)
JOBHEALTH_STATUS_PATH="$DATA_ROOT/output/health_status.json" python3 "$DIR/health_update.py" apply "$RC" "$ONELINE" || true
if [[ "$RC" -ne 0 ]]; then
  notify "apply run finished with rc=$RC"
fi
