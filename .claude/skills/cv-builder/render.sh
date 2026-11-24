#!/usr/bin/env bash
# Render a CV HTML -> PDF in the configured house style, via headless Chrome.
# Isolated throwaway profile + headless + no CDP => never touches the applicator's Chrome-Agent session.
# Bounded with a watchdog kill because headless Chrome does NOT self-exit after --print-to-pdf.
#
# Usage:  bash render.sh <input.html> [output.pdf]
#   default output = <input>.pdf next to the HTML.
set -uo pipefail

HTML="${1:?usage: render.sh <input.html> [output.pdf]}"
[ -f "$HTML" ] || { echo "no such file: $HTML"; exit 1; }
# absolutize (Chrome needs an absolute file:// URL)
HTML="$(cd "$(dirname "$HTML")" && pwd)/$(basename "$HTML")"
PDF="${2:-${HTML%.html}.pdf}"

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
[ -x "$CHROME" ] || { echo "Chrome not found at: $CHROME"; exit 1; }

rm -f "$PDF"
PROFILE="$(mktemp -d /tmp/chrome-cv.XXXXXX)"
"$CHROME" --headless --disable-gpu --no-pdf-header-footer --no-first-run \
  --no-default-browser-check --hide-scrollbars --user-data-dir="$PROFILE" \
  --print-to-pdf="$PDF" "file://$HTML" >/tmp/cv_render.log 2>&1 &
CPID=$!
( sleep 12; kill "$CPID" 2>/dev/null ) 2>/dev/null & KPID=$!
wait "$CPID" 2>/dev/null
kill "$KPID" 2>/dev/null
rm -rf "$PROFILE"

if [ -s "$PDF" ]; then
  echo "OK  ->  $PDF  ($(ls -la "$PDF" | awk '{print $5}') bytes)"
  /usr/bin/python3 -c "from pypdf import PdfReader; print('    pages:', len(PdfReader('$PDF').pages))" 2>/dev/null || true
  [ "$(uname)" = "Darwin" ] && open "$PDF"
else
  echo "RENDER FAILED — Chrome log tail:"; tail -5 /tmp/cv_render.log; exit 1
fi
