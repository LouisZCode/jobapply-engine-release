#!/usr/bin/env bash
# Launch the dedicated, logged-in Chrome-Agent profile with the DevTools port so the
# claude_applicator routine (Playwright MCP over CDP) can attach to it.
# Run this FIRST, then start the routine. Never let Playwright spawn its own Chrome:
# a direct-spawned Chrome can't reach the macOS Keychain, lands logged-out, and has
# deleted the li_at cookie before. See claude_applicator/CLAUDE.md.
set -uo pipefail

# MULTI-001: per-user overrides. Defaults keep today's behavior byte-identical.
PROFILE="${JOBAPPLY_CHROME_PROFILE:-$HOME/Library/Application Support/Google/Chrome-Agent}"
PORT="${JOBAPPLY_CDP_PORT:-9222}"
URL="https://www.linkedin.com/feed/"

if curl -s "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1; then
  echo "Chrome already up on :${PORT} — reusing it (not relaunching)."
else
  LOCK="$PROFILE/SingletonLock"
  if [ -L "$LOCK" ]; then
    PID="$(readlink "$LOCK" | sed 's/.*-//')"
    if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
      echo "clearing stale SingletonLock"; rm -f "$LOCK"
    else
      echo "A Chrome is already running on this profile (PID $PID) WITHOUT the debug port."
      echo "Quit that window, then re-run this script."; exit 1
    fi
  fi
  echo "launching Chrome (profile: ${PROFILE}, port: ${PORT}) ..."
  open -na "Google Chrome" --args \
    --user-data-dir="$PROFILE" \
    --remote-debugging-port="${PORT}" \
    "$URL"
fi

# Wait for the DevTools endpoint, then report tab + login state.
for _ in $(seq 1 20); do
  curl -s "http://127.0.0.1:${PORT}/json/version" >/dev/null 2>&1 && break
  sleep 1
done

curl -s "http://127.0.0.1:${PORT}/json" | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: print('CDP endpoint not reachable — launch may have failed'); raise SystemExit(1)
pages=[t for t in d if t.get('type')=='page']
for t in pages: print('tab:', t.get('url'),'||',t.get('title'))
print('OK: logged in' if any('/feed' in (t.get('url') or '') for t in pages)
      else 'NOTE: not on /feed — if it shows the login page, log in once in this window')"
