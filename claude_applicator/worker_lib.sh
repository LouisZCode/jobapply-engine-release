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
#                           Before building the argv it calls _load_user_keys() below, so
#                           every launch (both branches) inherits the user's stored keys.
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

# BRAIN-001c: put the user's stored API keys into this shell's environment, so whatever
# invoke_worker launches inherits them (pi's built-in providers read ZAI_API_KEY,
# ZAI_CODING_CN_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY). The tower's one-time
# spawn injection only ever saw the old single-user file — a key saved after the app
# started, or any per-user key, never reached the worker.
#
# Key file: the per-user store at $JOBAPPLY_DATA/app/keys.json, or — when no data root
# is set — the old single-user layout at app/keys.json next to the repo. A missing file
# means nothing to do. One python3 call turns the JSON into `ENV_NAME=value` lines
# (mapping fixed below; empty values skipped); the shell reads them with
# IFS='=' read -r and exports each. The file is what the user saved LAST, so it wins
# over a value already in the environment; a provider absent from the file leaves the
# environment alone. Values are NEVER echoed — only names: the log line
# "[worker_lib] keys loaded: <providers>", plus in WORKER_LIB_DRY_RUN=1 mode one
# `key-env: <ENV_NAME>` line per exported variable.
#
# The python half of _load_user_keys: prints `ENV_NAME=value` lines for every key
# stored in the key file. Kept in its own function so the heredoc stays at shell top
# level — bash 3.2 (macOS's /bin/bash) mis-parses a heredoc nested inside a command
# substitution. Empty/unreadable store: prints nothing, exits 0.
_keys_env_lines() {
  python3 - "$1" <<'PY'
import json
import sys

# provider id -> env var, the same mapping lib/keys.py's PROVIDERS carry
MAPPING = (
    ("zai", "ZAI_API_KEY"),
    ("zai_cn", "ZAI_CODING_CN_API_KEY"),
    ("codex", "OPENAI_API_KEY"),
    ("openrouter", "OPENROUTER_API_KEY"),
)
try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        data = json.load(fh)
except Exception:
    raise SystemExit(0)  # unreadable store: nothing to inject, never an error
for provider, env_name in MAPPING:
    key = (data.get(provider) or {}).get("key") or ""
    if key:  # skip empty values
        print("%s=%s" % (env_name, key))
PY
}

_load_user_keys() {
  local key_file="${JOBAPPLY_DATA:+$JOBAPPLY_DATA/app/}keys.json"
  if [[ -z "${JOBAPPLY_DATA:-}" ]]; then
    key_file="$WORKER_LIB_DIR/../app/keys.json"
  fi
  [[ -f "$key_file" ]] || return 0
  local name value env_name joined="" py_out=""
  local loaded=() names=()
  # One python3 call prints `ENV_NAME=value` lines (captured, not piped — a pipe
  # would run this loop in a subshell and lose the exports); the shell reads them
  # with IFS='=' read -r and exports each below.
  py_out="$(_keys_env_lines "$key_file")" || true
  while IFS='=' read -r name value; do
    [[ -n "$name" ]] || continue
    export "$name=$value"
    loaded+=("$name")
    case "$name" in
      ZAI_API_KEY)              names+=(zai) ;;
      ZAI_CODING_CN_API_KEY)    names+=(zai_cn) ;;
      OPENAI_API_KEY)           names+=(codex) ;;
      OPENROUTER_API_KEY)       names+=(openrouter) ;;
    esac
  done <<< "$py_out"
  [[ ${#names[@]} -gt 0 ]] || return 0
  joined="${names[0]}"
  for ((i = 1; i < ${#names[@]}; i++)); do
    joined="$joined, ${names[i]}"
  done
  # dry run prints to stdout (part of the reported argv); a real run logs to stderr, which
  # the wrappers tee into the run log — stdout is the worker's trace and stays JSON-only.
  if [[ "${WORKER_LIB_DRY_RUN:-0}" == "1" ]]; then
    printf '[worker_lib] keys loaded: %s\n' "$joined"
  else
    printf '[worker_lib] keys loaded: %s\n' "$joined" >&2
  fi
  if [[ "${WORKER_LIB_DRY_RUN:-0}" == "1" ]]; then
    for env_name in "${loaded[@]}"; do
      printf 'key-env: %s\n' "$env_name"
    done
  fi
}

invoke_worker() {
  # $1 = prompt, $2 = max turns (claude branch only; pi has no turn cap — its "final
  # message with no tool call ends the run" contract plays the same role).
  local prompt="$1" max_turns="$2"
  local worker model argv=()

  worker="$(worker_name)" || return $?

  # BRAIN-001c: before any launch — both branches below — the user's stored keys go
  # into the environment the worker inherits.
  _load_user_keys

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
