#!/usr/bin/env bash
# Double-clickable entry point (Finder runs .command files in Terminal). Just execs
# app/run_app.sh — all real logic lives there.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/app/run_app.sh"
