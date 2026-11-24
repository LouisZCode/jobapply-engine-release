#!/usr/bin/env bash
# setup/setup.sh — deterministic mechanics for onboarding this job-application pipeline.
#
# This script does the boring, deterministic parts of setup (check prerequisites,
# bootstrap the DB/output dir, render the launchd plists). The CONVERSATIONAL parts
# (interviewing the user for profile.yaml / qa_bank.md / search strategy / a first CV)
# live in .claude/skills/onboard/SKILL.md, which calls into this script at the right
# points. You can also run this script by hand — see subcommands below.
#
# Script-relative paths throughout, so this works from any cwd and on any machine
# it's checked out to (no hard-coded install path, no hard-coded username).
#
# Side effects are scoped exactly to what each subcommand documents below. In
# particular: this script NEVER runs `launchctl` and NEVER copies a plist into
# ~/Library/LaunchAgents — --schedule only renders files under setup/generated/
# and prints the commands for the user to run themselves.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$REPO_ROOT/claude_applicator"
# MULTI-001: --init artifacts (output/, applications.db) are per-user data → data root.
# Default = engine dir = today's behavior, byte-identical when JOBAPPLY_DATA is unset.
DATA_ROOT="${JOBAPPLY_DATA:-$APP_DIR}"

usage() {
  cat <<'EOF'
usage: setup/setup.sh <subcommand>

  --check              Verify prerequisites (safe, read-only). Prints PASS/WARN/FAIL/INFO
                        lines; exits non-zero if a HARD prerequisite is missing.

  --init                Create claude_applicator/output/, create applications.db from
                        schema.sql (ONLY if it doesn't already exist — an existing DB is
                        never touched), then render the engine docs/config from
                        profile.yaml if it exists yet.

  --schedule HH:MM      Render both launchd plists (jobhealth + jobapply) for a daily run
                        at the given local time into setup/generated/ (gitignored), and
                        print the exact commands to install + load them. Never runs
                        launchctl or writes to ~/Library/LaunchAgents itself.

  -h, --help            Show this message.
EOF
}

# ------------------------------------------------------------------------------
# --check
# ------------------------------------------------------------------------------
cmd_check() {
  local hard_fail=0

  line() { printf '%-5s %s\n' "$1" "$2"; }

  # claude CLI on PATH
  if command -v claude >/dev/null 2>&1; then
    line PASS "claude CLI on PATH ($(command -v claude))"
  else
    line FAIL "claude CLI not found on PATH — install Claude Code first (https://claude.com/claude-code), then re-open a shell"
    hard_fail=1
  fi

  # python3
  if command -v python3 >/dev/null 2>&1; then
    line PASS "python3 on PATH ($(command -v python3))"
  else
    line FAIL "python3 not found on PATH — install Python 3"
    hard_fail=1
  fi

  # PyYAML
  if command -v python3 >/dev/null 2>&1 && python3 -c "import yaml" >/dev/null 2>&1; then
    line PASS "PyYAML importable"
  else
    line FAIL "PyYAML not importable — install with: pip3 install pyyaml"
    hard_fail=1
  fi

  # sqlite3
  if command -v sqlite3 >/dev/null 2>&1; then
    line PASS "sqlite3 on PATH ($(command -v sqlite3))"
  else
    line FAIL "sqlite3 not found on PATH — install the sqlite3 CLI"
    hard_fail=1
  fi

  # Google Chrome
  if [[ -d "/Applications/Google Chrome.app" ]]; then
    line PASS "Google Chrome installed"
  else
    line FAIL "Google Chrome not found at /Applications/Google Chrome.app — install it (this pipeline drives a real, logged-in Chrome, never a headless/spoofed one)"
    hard_fail=1
  fi

  # npx (playwright MCP)
  if command -v npx >/dev/null 2>&1; then
    line PASS "npx on PATH ($(command -v npx)) — needed for the playwright MCP server"
  else
    line FAIL "npx not found on PATH — install Node.js (https://nodejs.org), which bundles npx"
    hard_fail=1
  fi

  # TCC-safe install location (warning, not a hard fail — but will break the
  # scheduled/background run if ignored, so say so plainly).
  case "$REPO_ROOT" in
    "$HOME"/Desktop|"$HOME"/Desktop/*|"$HOME"/Documents|"$HOME"/Documents/*|"$HOME"/Downloads|"$HOME"/Downloads/*)
      line WARN "repo is under Desktop/Documents/Downloads ($REPO_ROOT) — macOS blocks background (launchd) processes from those folders (TCC). Fix: move the WHOLE repo folder somewhere else under \$HOME (e.g. ~/GitHub/) before scheduling, then re-clone/re-open Claude Code there."
      ;;
    *)
      line PASS "repo location is TCC-safe ($REPO_ROOT)"
      ;;
  esac

  # profile.yaml presence — informational only, not a prerequisite (onboarding creates it)
  if [[ -f "$APP_DIR/profile.yaml" ]]; then
    line INFO "profile.yaml present ($APP_DIR/profile.yaml)"
  else
    line INFO "profile.yaml not created yet — the onboard skill (or you, by hand) creates it from claude_applicator/profile.template.yaml"
  fi

  echo
  if [[ "$hard_fail" -ne 0 ]]; then
    echo "RESULT: one or more HARD prerequisites are missing — fix the FAIL lines above, then re-run: bash setup/setup.sh --check"
    return 1
  fi
  echo "RESULT: all hard prerequisites satisfied."
  return 0
}

# ------------------------------------------------------------------------------
# --init
# ------------------------------------------------------------------------------
cmd_init() {
  mkdir -p "$DATA_ROOT/output"
  echo "[init] output/ ready: $DATA_ROOT/output"

  local db="$DATA_ROOT/applications.db"
  local schema="$APP_DIR/schema.sql"
  if [[ -f "$db" ]]; then
    echo "[init] applications.db already exists — leaving it untouched: $db"
  else
    if ! command -v sqlite3 >/dev/null 2>&1; then
      echo "[init] ERROR: sqlite3 not found on PATH — cannot create applications.db. Run: bash setup/setup.sh --check" >&2
      return 1
    fi
    if [[ ! -f "$schema" ]]; then
      echo "[init] ERROR: schema not found at $schema" >&2
      return 1
    fi
    sqlite3 "$db" < "$schema"
    echo "[init] applications.db created from schema.sql: $db"
  fi

  if [[ -f "$APP_DIR/profile.yaml" ]]; then
    echo "[init] profile.yaml found — rendering engine docs/config..."
    python3 "$SCRIPT_DIR/render_templates.py"
  else
    echo "[init] profile.yaml not found yet — skipping render_templates.py (run it once profile.yaml exists: python3 setup/render_templates.py)"
  fi

  cat <<'EOF'

[init] done. Next steps:
  1. If profile.yaml isn't finished yet, finish it (see the onboard skill, or fill in
     claude_applicator/profile.template.yaml by hand as claude_applicator/profile.yaml),
     then run: python3 setup/render_templates.py
  2. Connect LinkedIn:   bash claude_applicator/launch_chrome.sh   (then sign in once, by hand)
  3. Schedule the daily run: bash setup/setup.sh --schedule HH:MM
EOF
}

# ------------------------------------------------------------------------------
# --schedule HH:MM
# ------------------------------------------------------------------------------

# render_plist TEMPLATE OUT LABEL_PREFIX INSTALL_PATH [RUN_HOUR RUN_MINUTE]
# Substitutes {{label_prefix}}, {{install_path}}, and — when given — {{run_hour}}/
# {{run_minute}}. Refuses to write (loudly) if any `{{...}}` placeholder survives the
# substitution, so a template/script mismatch is never shipped silently.
render_plist() {
  local tmpl="$1" out="$2" label_prefix="$3" install_path="$4" run_hour="${5:-}" run_minute="${6:-}"
  python3 - "$tmpl" "$out" "$label_prefix" "$install_path" "$run_hour" "$run_minute" <<'PY'
import re
import sys

tmpl, out, label_prefix, install_path, run_hour, run_minute = sys.argv[1:7]
with open(tmpl, encoding="utf-8") as fh:
    text = fh.read()

text = text.replace("{{label_prefix}}", label_prefix)
text = text.replace("{{install_path}}", install_path)
if run_hour:
    text = text.replace("{{run_hour}}", run_hour)
if run_minute:
    text = text.replace("{{run_minute}}", run_minute)

leftover = sorted(set(re.findall(r"\{\{[^{}]*\}\}", text)))
if leftover:
    sys.exit("ERROR: {} still has unrendered placeholder(s): {}".format(tmpl, leftover))

with open(out, "w", encoding="utf-8") as fh:
    fh.write(text)
PY
}

cmd_schedule() {
  local time="${1:-}"
  if [[ -z "$time" ]]; then
    echo "usage: setup/setup.sh --schedule HH:MM   (24-hour local time, e.g. 05:01)" >&2
    return 1
  fi
  if [[ ! "$time" =~ ^([01][0-9]|2[0-3]):([0-5][0-9])$ ]]; then
    echo "ERROR: '$time' is not a valid 24-hour HH:MM time (e.g. 05:01)" >&2
    return 1
  fi

  local run_hour="${time%%:*}" run_minute="${time##*:}"
  local run_hour_i=$((10#$run_hour)) run_minute_i=$((10#$run_minute))

  local label_prefix
  label_prefix="com.$(id -un)"
  local install_path="$REPO_ROOT"

  local gen_dir="$SCRIPT_DIR/generated"
  mkdir -p "$gen_dir"

  local health_tmpl="$APP_DIR/jobhealth.plist.template"
  local apply_tmpl="$APP_DIR/jobapply.plist.template"
  local health_out="$gen_dir/${label_prefix}.jobhealth.plist"
  local apply_out="$gen_dir/${label_prefix}.jobapply.plist"

  for t in "$health_tmpl" "$apply_tmpl"; do
    [[ -f "$t" ]] || { echo "ERROR: missing template $t" >&2; return 1; }
  done

  render_plist "$health_tmpl" "$health_out" "$label_prefix" "$install_path" || return 1
  render_plist "$apply_tmpl" "$apply_out" "$label_prefix" "$install_path" "$run_hour_i" "$run_minute_i" || return 1

  echo "[schedule] rendered:"
  echo "  $health_out   (daily 09:00 heartbeat check)"
  echo "  $apply_out   (daily $time apply round)"
  echo
  echo "Review both files, then install + load them yourself — this script never runs"
  echo "launchctl or touches ~/Library/LaunchAgents on its own:"
  echo
  echo "  cp \"$health_out\" ~/Library/LaunchAgents/${label_prefix}.jobhealth.plist"
  echo "  cp \"$apply_out\" ~/Library/LaunchAgents/${label_prefix}.jobapply.plist"
  echo "  launchctl load ~/Library/LaunchAgents/${label_prefix}.jobhealth.plist"
  echo "  launchctl load ~/Library/LaunchAgents/${label_prefix}.jobapply.plist"
  echo
  echo "To re-install after an edit: launchctl unload <plist> first, then load again."
  echo

  # Optional pre-wake, one minute before the run, :30s in (mirrors the pattern this
  # was modeled on — a scheduled run needs the Mac awake, not just unlocked).
  local pw_h=$run_hour_i pw_m=$((run_minute_i - 1))
  if (( pw_m < 0 )); then pw_m=59; pw_h=$(( (run_hour_i - 1 + 24) % 24 )); fi
  printf -v pw_h2 '%02d' "$pw_h"
  printf -v pw_m2 '%02d' "$pw_m"
  echo "Optional but recommended — if this Mac sleeps overnight, the launchd job above"
  echo "won't fire while asleep. Pre-wake it (needs sudo, one-time):"
  echo
  echo "  sudo pmset repeat wakeorpoweron MTWRFSU ${pw_h2}:${pw_m2}:30"
  echo
  echo "Verify anytime with: pmset -g sched"
}

# ------------------------------------------------------------------------------
main() {
  local sub="${1:-}"
  case "$sub" in
    --check) cmd_check ;;
    --init) cmd_init ;;
    --schedule) shift; cmd_schedule "${1:-}" ;;
    -h|--help) usage ;;
    "") usage; return 1 ;;
    *) echo "unknown subcommand: $sub" >&2; usage; return 1 ;;
  esac
}

main "$@"
