#!/usr/bin/env bash
# app/run_app.sh — bootstrap + start the local setup web app.
#
# Creates a private venv under app/.venv (gitignored) the first time it runs, installs the
# packages the app needs from app/requirements.txt (pinned — see the file for why) if missing,
# then starts the FastAPI app (served by uvicorn) bound to 127.0.0.1 only and opens it in
# the default browser.
#
# Works from any CWD — everything is derived from this script's own location, never
# from the caller's working directory.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PORT="${JOBAPPLY_PORT:-8765}"
HOST=127.0.0.1
export JOBAPPLY_PORT="$PORT"

# Tools installed per user must be visible no matter who starts this script. An app opened
# from Finder/Dock (the JobApply desktop app) and launchd hand us a minimal PATH
# (/usr/bin:/bin:/usr/sbin:/sbin) — without this line the app cannot find the Claude CLI in
# ~/.local/bin (its native installer), Homebrew's python3/node/uv, or an Intel Mac's
# /usr/local/bin. Same line as claude_applicator/run_scheduled.sh.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found on PATH — install Python 3 first." >&2
  exit 1
fi

# Python floor: the pinned runtime (app/requirements.txt) is proven on 3.9 and 3.15.
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then
  echo "ERROR: Python 3.9 or newer is required — \"$(command -v python3)\" is $(python3 --version 2>&1 | cut -d' ' -f2)." >&2
  echo "       Install a current Python 3 (e.g. https://www.python.org/downloads/macos/) and retry." >&2
  exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[run_app] creating venv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

PY="$VENV_DIR/bin/python3"
PIP="$VENV_DIR/bin/pip"

# Install only what's missing, quietly.
NEED_INSTALL=0
"$PY" -c "import fastapi" >/dev/null 2>&1 || NEED_INSTALL=1
"$PY" -c "import uvicorn" >/dev/null 2>&1 || NEED_INSTALL=1
"$PY" -c "import multipart" >/dev/null 2>&1 || NEED_INSTALL=1
"$PY" -c "import jinja2" >/dev/null 2>&1 || NEED_INSTALL=1
"$PY" -c "import yaml" >/dev/null 2>&1 || NEED_INSTALL=1
"$PY" -c "import pypdf" >/dev/null 2>&1 || NEED_INSTALL=1
# google-auth(-oauthlib): only the onboarding wizard's Gmail consent flow needs it
# (app/lib/gmail_connect.py); a plain "skip Gmail" install works without them.
"$PY" -c "import google.oauth2" >/dev/null 2>&1 || NEED_INSTALL=1
"$PY" -c "import google_auth_oauthlib" >/dev/null 2>&1 || NEED_INSTALL=1
if [[ "$NEED_INSTALL" -eq 1 ]]; then
  echo "[run_app] installing the pinned runtime from app/requirements.txt into the venv ..."
  "$PIP" install --quiet --upgrade pip >/dev/null 2>&1
  "$PIP" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

echo "[run_app] starting server on http://${HOST}:${PORT} ..."

# Open the browser shortly after the server starts listening. Backgrounded so it
# doesn't block server startup; a short sleep gives uvicorn time to bind the port.
# Skipped entirely when an embedding shell (the Electron tower) sets JOBAPPLY_NO_BROWSER=1.
if [[ "${JOBAPPLY_NO_BROWSER:-0}" != "1" ]]; then
  ( sleep 1; open "http://${HOST}:${PORT}" >/dev/null 2>&1 ) &
fi

cd "$REPO_ROOT"

# MULTI-002: when JOBAPPLY_DATA is already set at launch (tests, development, the old
# single-user way) nothing changes — plain exec, no login page, no loop. Otherwise this
# is the switch loop: the server ends itself with exit code 62 to ask for a restart as
# another user (or as the login page). It writes the next user (or "login") to
# <users dir>/.switch; we read it, export that user's env from their user.json and start
# the server again on the same port. Never abort on the server's exit code (no set -e
# here; keep set -uo pipefail).
if [[ -n "${JOBAPPLY_DATA:-}" ]]; then
  exec "$PY" "$SCRIPT_DIR/server.py"
fi

export JOBAPPLY_USERS_DIR="${JOBAPPLY_USERS_DIR:-${JOBAPPLY_ENGINE_HOME:-$HOME/JobApply}/users}"

while true; do
  "$PY" "$SCRIPT_DIR/server.py"
  CODE=$?
  if [[ "$CODE" -ne 62 ]]; then
    exit "$CODE"
  fi
  NEXT=""
  if [[ -f "$JOBAPPLY_USERS_DIR/.switch" ]]; then
    NEXT="$(tr -d '[:space:]' < "$JOBAPPLY_USERS_DIR/.switch")"
  fi
  if [[ -z "$NEXT" || "$NEXT" == "login" ]]; then
    unset JOBAPPLY_DATA JOBAPPLY_USER JOBAPPLY_CHROME_PROFILE JOBAPPLY_CDP_PORT
    echo "[run_app] login page"
  else
    # data_root, chrome_profile, cdp_port — one line, tab-separated (chrome_profile
    # contains spaces, so the separator must not be a plain space).
    LINE="$("$PY" -c 'import json,sys
u = json.load(open(sys.argv[1]))
print(u["data_root"] + "\t" + u["chrome_profile"] + "\t" + str(u["cdp_port"]))' "$JOBAPPLY_USERS_DIR/$NEXT/user.json")"
    IFS=$'\t' read -r DATA_ROOT CHROME_PROFILE CDP_PORT <<< "$LINE"
    export JOBAPPLY_DATA="$DATA_ROOT" JOBAPPLY_USER="$NEXT" \
           JOBAPPLY_CHROME_PROFILE="$CHROME_PROFILE" JOBAPPLY_CDP_PORT="$CDP_PORT"
    echo "[run_app] user: $NEXT"
  fi
done
