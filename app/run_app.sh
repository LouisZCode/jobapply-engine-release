#!/usr/bin/env bash
# app/run_app.sh — bootstrap + start the local setup web app.
#
# Creates a private venv under app/.venv (gitignored) the first time it runs, installs
# the packages the app needs (fastapi, uvicorn, jinja2, python-multipart, pyyaml) into it
# if missing, then starts the FastAPI app (served by uvicorn) bound to 127.0.0.1 only and
# opens it in the default browser.
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

if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found on PATH — install Python 3 first." >&2
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
  echo "[run_app] installing fastapi + uvicorn + jinja2 + python-multipart + pyyaml + pypdf + google-auth into the venv ..."
  "$PIP" install --quiet --upgrade pip >/dev/null 2>&1
  "$PIP" install --quiet fastapi uvicorn jinja2 python-multipart pyyaml pypdf google-auth google-auth-oauthlib
fi

echo "[run_app] starting server on http://${HOST}:${PORT} ..."

# Open the browser shortly after the server starts listening. Backgrounded so it
# doesn't block server startup; a short sleep gives uvicorn time to bind the port.
# Skipped entirely when an embedding shell (the Electron tower) sets JOBAPPLY_NO_BROWSER=1.
if [[ "${JOBAPPLY_NO_BROWSER:-0}" != "1" ]]; then
  ( sleep 1; open "http://${HOST}:${PORT}" >/dev/null 2>&1 ) &
fi

cd "$REPO_ROOT"
exec "$PY" "$SCRIPT_DIR/server.py"
