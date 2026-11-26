#!/usr/bin/env bash
# worker_lib.sh — BRAIN-001a (port of WORKER-001): pluggable worker invocation.
#
# ONE dispatch point for every wrapper that spawns an agent CLI. Selected by two knobs
# in run_config.txt, read FRESH from disk on every call — so the AI that runs
# applications is a setting (the app's Connections page writes it), switched between
# rounds without touching code:
#
#   WORKER: claude        claude (default) | pi
#   WORKER_MODEL:         model id passed to the harness (claude default
#                         claude-haiku-5-5; pi has NO default — a nameless pi run is
#                         refused, the model that ran must be recorded)
#
# API:
#   worker_name             echo the selected worker (claude|pi); rc=64 + stderr on an
#                           unknown value (fail CLOSED: a typo must never silently run 50
#                           applies on the wrong harness)
#   worker_model <default>  echo WORKER_MODEL from run_config.txt, or <default> if unset
#   invoke_worker <prompt> <max_turns>
#                           run the selected worker with the prompt; stdin/stdout/stderr
#                           and the exit code pass through untouched (callers keep their
#                           own `</dev/null >"$NDJSON"` redirections and `RC=$?` reads).
#                           rc=64 = launch REFUSED — the stderr line says why (unknown
#                           WORKER, or pi without WORKER_MODEL); treat it as a failed run.
#
# Both branches stamp what ACTUALLY ran by exporting JOBAPPLY_WORKER/JOBAPPLY_WORKER_MODEL
# (the routine echoes them into the applications.worker/model columns; the model must
# never self-report its identity).
#
# Env overrides (production behavior identical when unset — for tests and productized
# copies only):
#   WORKER_LIB_CONFIG   path to the knobs file (default: ${JOBAPPLY_DATA:+$JOBAPPLY_DATA/}run_config.txt
#                       — the user's file when a data root is set, else the repo-relative
#                       run_config.txt of the wrappers' working directory)
#   WORKER_CLAUDE_BIN   claude branch binary     (default: $HOME/.local/bin/claude)
#   WORKER_PI_BIN       pi branch binary         (default: pi on PATH, else $HOME/.pi/agent/bin/pi)
#   WORKER_LIB_DRY_RUN=1  print the argv (one token per line) instead of executing
#
# Tests: app/tests/test_worker_lib.py (runs this library under WORKER_LIB_DRY_RUN=1).
# Pi rails: the pi branch loads .pi/extensions/jobapply-guard.ts — the same armed
# guard_hook.py brains Claude gets (it only enforces when the wrapper exported
# JOBAPPLY_GUARD=1, exactly like the Claude wiring), and that extension registers the
# playwright/sqlite MCP servers from the JOBAPPLY_* environment, so nothing is
# hard-coded per user. Print mode stays pi's default (final text on stdout) so the
# wrappers' result-line greps keep working.

WORKER_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKER_CONFIG_PATH="${WORKER_LIB_CONFIG:-${JOBAPPLY_DATA:+$JOBAPPLY_DATA/}run_config.txt}"

# pi's own install location is not on PATH by default — add it when it exists so the pi
# branch can launch without an absolute binary path.
[[ -d "$HOME/.pi/agent/bin" ]] && export PATH="$HOME/.pi/agent/bin:$PATH"

# Read one `KEY: value` from the knobs file (same sed idiom the wrappers use for
# MODE/DAILY_APPLY_CAP), strip inline comments and surrounding whitespace, fresh per call.
# ALWAYS returns 0 — a missing/unreadable knobs file must default, never kill a caller
# that runs under set -e/pipefail.
worker_knob() {
  local v=""
  v="$(sed -n "s/^$1:[[:space:]]*\([^#]*\).*/\1/p" "$WORKER_CONFIG_PATH" 2>/dev/null | head -1)" || true
  v="${v%"${v##*[![:space:]]}"}"   # trim trailing whitespace
  v="${v#"${v%%[![:space:]]*}"}"   # trim leading whitespace
  printf '%s\n' "$v"
  return 0
}

worker_name() {
  local w
  w="$(worker_knob WORKER)"
  w="${w:-claude}"
  case "$w" in
    claude|pi) printf '%s\n' "$w" ;;
    *)
      printf 'worker_lib: unknown WORKER "%s" in %s (expected claude|pi) — refusing to launch\n' \
        "$w" "$WORKER_CONFIG_PATH" >&2
      return 64
      ;;
  esac
}

worker_model() {
  local m
  m="$(worker_knob WORKER_MODEL)"
  printf '%s\n' "${m:-$1}"
}

invoke_worker() {
  # $1 = prompt, $2 = max turns (claude branch only; pi has no turn cap — its "final
  # message with no tool call ends the run" contract plays the same role).
  local prompt="$1" max_turns="$2"
  local worker model argv=()

  worker="$(worker_name)" || return $?

  case "$worker" in
    claude)
      # The knob IS honored on this branch (BRAIN-001a): WORKER_MODEL pins the claude
      # model too, default claude-haiku-5-5.
      model="$(worker_model claude-haiku-5-5)"
      export JOBAPPLY_WORKER="claude"
      export JOBAPPLY_WORKER_MODEL="$model"
      argv=("${WORKER_CLAUDE_BIN:-$HOME/.local/bin/claude}" -p "$prompt"
            --model "$model"
            --max-turns "$max_turns"
            --output-format stream-json --verbose
            --dangerously-skip-permissions)
      ;;
    pi)
      # No default model: the model that ran must be recorded, so a nameless pi run is
      # refused here (never a "pi-default" stamp of unknown identity).
      model="$(worker_model "")"
      if [[ -z "$model" ]]; then
        printf 'worker_lib: WORKER_MODEL is not set — the model that ran must be recorded; set it on the Connections page\n' >&2
        return 64
      fi
      local pi_bin
      pi_bin="${WORKER_PI_BIN:-$(command -v pi || echo "$HOME/.pi/agent/bin/pi")}"
      export JOBAPPLY_WORKER="pi"
      export JOBAPPLY_WORKER_MODEL="$model"
      # Guard/pace rails arrive via the repo adapter (-ne keeps discovery off so ONLY
      # that extension loads); -e builtin:mcp re-includes the built-in MCP support the
      # adapter's registered playwright/sqlite servers need (--approve trusts
      # project-local files for THIS run only, so a headless pi -p can load them).
      # The worker gets an EMPTY pi of its own (PI_CODING_AGENT_DIR = a fresh temp dir):
      # no personal MCP servers, skills, extensions, prompts or stored logins from the
      # person at the keyboard. Credentials come from the product's key store as
      # environment variables (ZAI_API_KEY, OPENROUTER_API_KEY, …), which pi's built-in
      # providers read. Nothing is kept: no session file, the temp dir is removed.
      local agent_dir
      agent_dir="$(mktemp -d "${TMPDIR:-/tmp}/jobapply-pi.XXXXXX")"
      export PI_CODING_AGENT_DIR="$agent_dir"
      argv=("$pi_bin" -p "$prompt" --model "$model" --no-session
            --approve -ne -ns -np --no-themes
            -e "$WORKER_LIB_DIR/../.pi/extensions/jobapply-guard.ts"
            -e builtin:mcp)
      # The CV builder skill lives with the user's data when a data root is set, else
      # with the repo checkout.
      local skill_dir="${JOBAPPLY_DATA:-$WORKER_LIB_DIR/..}/.claude/skills/cv-builder"
      [[ -d "$skill_dir" ]] && argv+=(--skill "$skill_dir")
      ;;
  esac

  if [[ "${WORKER_LIB_DRY_RUN:-0}" == "1" ]]; then
    printf '%s\n' "${argv[@]}"
    return 0
  fi
  if [[ -n "${agent_dir:-}" ]]; then
    "${argv[@]}"
    local rc=$?
    rm -rf "$agent_dir"
    return $rc
  fi
  "${argv[@]}"
}
