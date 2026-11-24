#!/usr/bin/env bash
# writer/write.sh — ghostwrites job-application screener answers and cover letters,
# AS the configured user identity (user.env USER_FULL_NAME / USER_FIRST_NAME, generated
# by setup/render_templates.py from profile.yaml), grounded strictly in the QA bank +
# the job description.
#
# Standalone "phone a tool" CLI, same contract shape as inbox_reader/find_verification.py:
# the apply agent (a headless `claude -p` run driving a browser) shells out to this
# script the moment a form demands custom free text, reads exactly ONE JSON object back
# off stdout, and acts on `status` alone. This script never touches the browser, never
# touches applications.db, never prompts a human — it is a pure text generator with a
# hard grounding contract enforced by WRITER.md (the ghostwriter's system prompt).
#
# Usage:
#   ./write.sh --type question --question "TEXT" --jd-file PATH --company "NAME" \
#       --title "ROLE" [--company-file PATH] [--max-words N] [--lang en|de] \
#       [--qa-bank PATH] [--out-file PATH]
#   ./write.sh --type cover_letter --jd-file PATH --company "NAME" --title "ROLE" \
#       [--company-file PATH] [--max-words N] [--lang en|de] [--qa-bank PATH] [--out-file PATH]
#
# --company-file: OPTIONAL extra grounding — the company's own self-description (About blurb,
# values, mission) as captured by the caller from the job listing / company page. Grounds
# "why us?" answers and letter hooks. Facts about the user still come only from the QA bank.
#
# --out-file: where to persist the final text. If the path ends in .pdf, the writer renders a
# one-page letter PDF instead (letter_template.html + headless Chrome with a throwaway profile,
# the cv-builder pattern — never touches the Chrome-Agent session). Any other extension = plain
# text bytes.
#
# Every `answered` draft additionally goes through a DE-AI-IFY polish pass (second model call,
# contract = ~/.claude/skills/de-ai-ify/SKILL.md): edit-only, facts frozen, same language. The
# pass can only improve or no-op — any failure keeps the original draft (see `polish` field).
#
# Exit codes: 0 = answered, 2 = skip, 1 = error. Stdout is ALWAYS exactly one JSON object,
# never anything else (no progress lines) — the caller parses stdout as JSON.
#
# set -u to catch unset-var typos, pipefail so a broken pipe in a `cmd | python3` chain
# is visible. Deliberately NOT set -e: every failure path below is handled explicitly and
# must still emit a JSON envelope + the right exit code, which `set -e` would short-circuit.
set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# MULTI-001: user.env lives in the per-user data root; default = app dir = today's behavior.
DATA_ROOT="${JOBAPPLY_DATA:-$APP_DIR}"
CLAUDE_BIN="$HOME/.local/bin/claude"
MODEL="claude-sonnet-5"
CALL_TIMEOUT_SECS=180
# MULTI-001 (Phase-1 follow-up): qa_bank.md is per-user data — it resolves under the
# data root (same pattern as app/lib/paths.py's QA_BANK_MD). Default = app dir = today's path.
DEFAULT_QA_BANK="$DATA_ROOT/qa_bank.md"
DEAIIFY_SKILL="$HOME/.claude/skills/de-ai-ify/SKILL.md"
CHROME_BIN="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
LETTER_TEMPLATE="$SCRIPT_DIR/letter_template.html"
USER_ENV="$DATA_ROOT/user.env"

# --- scratch area for this invocation (prompt files, raw envelopes, stderr captures).
# Separate from the per-call cwd below — this one just holds working files, the per-call
# cwd exists purely so `claude` finds no project CLAUDE.md to auto-discover.
WORKROOT="$(mktemp -d "${TMPDIR:-/tmp}/writer_work.XXXXXX")" || {
  printf '{"status":"error","reason":"could not create scratch dir"}\n'
  exit 1
}
trap 'rm -rf "$WORKROOT"' EXIT

# --- output helper: always the single source of truth for what hits stdout.
# Builds the final envelope with python3/json.dumps (never hand-rolled string JSON) so
# arbitrary answer text — quotes, newlines, unicode — can never corrupt the envelope.
# Reads fields from env vars (W_*) rather than positional args: answer text can run to a
# few KB and env vars sidestep all shell quoting/escaping concerns entirely.
emit_and_exit() {
  local status="$1" code="$2"
  W_STATUS="$status" python3 - <<'PY'
import json
import os

status = os.environ["W_STATUS"]
obj = {"status": status}
if status == "answered":
    obj["answer"] = os.environ.get("W_ANSWER", "")
    grounded_raw = os.environ.get("W_GROUNDED_IN", "")
    obj["grounded_in"] = [g for g in grounded_raw.split("\x1f") if g]
    obj["word_count"] = int(os.environ.get("W_WORD_COUNT", "0"))
    if os.environ.get("W_POLISH"):
        obj["polish"] = os.environ["W_POLISH"]
    if os.environ.get("W_EM_DASH_SCRUBBED"):
        obj["em_dash_scrubbed"] = True
    if os.environ.get("W_OUT_FILE"):
        obj["out_file"] = os.environ["W_OUT_FILE"]
else:
    obj["reason"] = os.environ.get("W_REASON", "")
print(json.dumps(obj))
PY
  exit "$code"
}

error_out() {
  # $1 = reason text
  W_REASON="$1" emit_and_exit "error" 1
}

# --- arg defaults ---
TYPE=""
QUESTION=""
JD_FILE=""
COMPANY_FILE=""
COMPANY=""
TITLE=""
MAX_WORDS=""
REQ_LANG=""
QA_BANK="$DEFAULT_QA_BANK"
OUT_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --type) TYPE="${2:-}"; shift 2 ;;
    --question) QUESTION="${2:-}"; shift 2 ;;
    --jd-file) JD_FILE="${2:-}"; shift 2 ;;
    --company-file) COMPANY_FILE="${2:-}"; shift 2 ;;
    --company) COMPANY="${2:-}"; shift 2 ;;
    --title) TITLE="${2:-}"; shift 2 ;;
    --max-words) MAX_WORDS="${2:-}"; shift 2 ;;
    --lang) REQ_LANG="${2:-}"; shift 2 ;;
    --qa-bank) QA_BANK="${2:-}"; shift 2 ;;
    --out-file) OUT_FILE="${2:-}"; shift 2 ;;
    *) error_out "unknown argument: $1" ;;
  esac
done

# --- validation ---
if [[ "$TYPE" != "question" && "$TYPE" != "cover_letter" ]]; then
  error_out "--type must be 'question' or 'cover_letter'"
fi
if [[ -z "$COMPANY" ]]; then
  error_out "--company is required"
fi
if [[ -z "$TITLE" ]]; then
  error_out "--title is required"
fi
if [[ -z "$JD_FILE" || ! -s "$JD_FILE" ]]; then
  error_out "jd-file not found or empty: ${JD_FILE:-<none>}"
fi
# --company-file is optional, but if given it must exist (a typo'd path failing silently would
# just quietly degrade answer quality, which is worse than failing loudly here).
if [[ -n "$COMPANY_FILE" && ! -s "$COMPANY_FILE" ]]; then
  error_out "company-file not found or empty: $COMPANY_FILE"
fi
if [[ "$TYPE" == "question" && -z "$QUESTION" ]]; then
  error_out "--question is required for --type question"
fi
if [[ -n "$REQ_LANG" && "$REQ_LANG" != "en" && "$REQ_LANG" != "de" ]]; then
  error_out "--lang must be 'en' or 'de'"
fi
if [[ -n "$MAX_WORDS" && ! "$MAX_WORDS" =~ ^[0-9]+$ ]]; then
  error_out "--max-words must be a positive integer"
fi
# qa_bank check is spelled out by name in the contract: this is the one failure mode the
# caller (apply agent) is expected to hit routinely while the real bank is being authored.
if [[ ! -s "$QA_BANK" ]]; then
  error_out "qa_bank not found: $QA_BANK"
fi
if [[ ! -s "$SCRIPT_DIR/WRITER.md" ]]; then
  error_out "WRITER.md not found next to write.sh: $SCRIPT_DIR/WRITER.md"
fi
# user.env (name used in "in first person as X" prompts + the letter PDF header) is
# generated by setup/render_templates.py from profile.yaml — no fallback to a
# hard-coded personal name.
if [[ ! -s "$USER_ENV" ]]; then
  error_out "user.env not found: $USER_ENV (run setup/render_templates.py first)"
fi
# shellcheck disable=SC1091
source "$USER_ENV"
if [[ -z "${USER_FIRST_NAME:-}" || -z "${USER_FULL_NAME:-}" ]]; then
  error_out "$USER_ENV is missing USER_FIRST_NAME/USER_FULL_NAME (re-run setup/render_templates.py)"
fi

# --- defaults that depend on --type ---
if [[ -z "$MAX_WORDS" ]]; then
  if [[ "$TYPE" == "cover_letter" ]]; then MAX_WORDS=250; else MAX_WORDS=120; fi
fi
if [[ "$TYPE" == "question" ]]; then
  REQUEST_TEXT="Question to answer, in first person as $USER_FIRST_NAME: \"$QUESTION\""
else
  REQUEST_TEXT="Write the cover letter."
fi
if [[ -n "$REQ_LANG" ]]; then
  if [[ "$REQ_LANG" == "de" ]]; then LANG_NAME="German"; else LANG_NAME="English"; fi
  LANG_INSTRUCTION="Write the answer in $LANG_NAME ONLY. This is an explicit override of the caller — follow it even if the job description below is in a different language."
else
  LANG_INSTRUCTION="Match the job description's language exactly (English if the JD is in English, German if the JD is in German), per your contract above."
fi

# --- assemble the one prompt: WRITER.md + QA bank + JD + request + constraints.
# Each section is written with printf '%s' so bank/JD/question TEXT is never treated as a
# format string (a stray % in a job description must not corrupt printf).
PROMPT_FILE="$WORKROOT/prompt.txt"
build_prompt() {
  # $1 = extra instruction appended at the very end (used by the retry paths below), or ""
  local extra="$1"
  {
    cat "$SCRIPT_DIR/WRITER.md"
    printf '\n=== QA BANK ===\n'
    cat "$QA_BANK"
    printf '\n=== JOB DESCRIPTION (%s / %s) ===\n' "$COMPANY" "$TITLE"
    cat "$JD_FILE"
    if [[ -n "$COMPANY_FILE" ]]; then
      printf '\n=== COMPANY CONTEXT (the company'"'"'s own self-description, captured from its listing/page) ===\n'
      cat "$COMPANY_FILE"
    fi
    printf '\n=== REQUEST ===\n%s\n' "$REQUEST_TEXT"
    printf '\nConstraints: maximum %s words. %s\n' "$MAX_WORDS" "$LANG_INSTRUCTION"
    if [[ -n "$extra" ]]; then
      printf '\n%s\n' "$extra"
    fi
  }
}

# --- kill a background process (and any direct children) after CALL_TIMEOUT_SECS.
# No GNU `timeout` on stock macOS, so this is the portable background-watcher pattern:
# background the real command, background a poll-then-kill watcher, wait on the real
# command, then reap the watcher. `claude` here is a single native binary (not a shell
# wrapper spawning node), so a plain kill -9 on its pid is sufficient; the pkill -P is
# belt-and-braces in case it ever forks a helper.
#
# IMPORTANT: this function runs inside a `$(...)` command substitution upstream (the
# caller captures its stdout). ANY backgrounded process that inherits that substitution's
# pipe on its own stdout/stderr keeps the substitution blocked reading — waiting for EOF —
# until THAT process exits too, even after the real command already finished. A first
# version of this watcher used a single `sleep "$CALL_TIMEOUT_SECS"` with no redirection;
# killing the watcher's own subshell after the real call finished left the `sleep` itself
# orphaned (its true OS parent was the already-exited subshell, not the watcher pid we
# killed), so it kept running — and kept holding that pipe open — for up to 180s per call,
# making every invocation appear to hang even when the model answered in seconds. Fixed by
# (a) redirecting the ENTIRE watcher job to /dev/null so no descendant of it can ever hold
# that pipe open, and (b) polling with `kill -0` in 1s steps instead of one long `sleep`,
# so terminating the watcher's pid actually stops the wait promptly.
run_claude_call() {
  # $1 = prompt file, $2 = envelope-out file, $3 = stderr-out file
  local promptfile="$1" outfile="$2" errfile="$3"
  local workdir
  workdir="$(mktemp -d "${TMPDIR:-/tmp}/writer_call.XXXXXX")" || return 1
  (
    cd "$workdir" || exit 1
    # --tools "" : no built-in tools available to the model — the writer only ever
    # needs to emit text, and this is belt-and-braces against it trying to act.
    # cwd isolation (fresh dir OUTSIDE the repo) keeps claude_applicator/CLAUDE.md
    # (a full browser-automation routine) from being auto-discovered and leaking in.
    "$CLAUDE_BIN" -p --model "$MODEL" --output-format json --tools "" \
      < "$promptfile" > "$outfile" 2> "$errfile"
  ) &
  local cmd_pid=$!
  (
    waited=0
    while kill -0 "$cmd_pid" 2>/dev/null && [[ $waited -lt $CALL_TIMEOUT_SECS ]]; do
      sleep 1
      waited=$((waited + 1))
    done
    kill -9 "$cmd_pid" 2>/dev/null
    pkill -9 -P "$cmd_pid" 2>/dev/null
  ) >/dev/null 2>&1 &
  local watcher_pid=$!
  wait "$cmd_pid" 2>/dev/null
  local rc=$?
  kill "$watcher_pid" 2>/dev/null
  wait "$watcher_pid" 2>/dev/null
  rm -rf "$workdir"
  return $rc
}

# --- extract the model's answer JSON out of the --output-format json envelope, and
# parse IT as JSON too (python3, not jq, so a shape problem prints a real message instead
# of jq's terser errors). Prints the parsed inner object as compact JSON on success.
# Exit codes: 0 = parsed cleanly; 1 = envelope OK but inner text isn't valid JSON
# (caller should retry once with a firmer instruction); 2 = the model call itself failed
# (envelope missing/invalid/is_error) — not something a "be more careful with JSON" retry
# can fix.
parse_envelope() {
  # $1 = envelope file
  python3 - "$1" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        envelope = json.load(fh)
except Exception as exc:
    print("envelope not valid JSON: {}".format(exc), file=sys.stderr)
    sys.exit(2)

if envelope.get("is_error"):
    print("model call reported is_error: {}".format(envelope.get("subtype")), file=sys.stderr)
    sys.exit(2)

text = envelope.get("result")
if not isinstance(text, str) or not text.strip():
    print("envelope has no usable .result text", file=sys.stderr)
    sys.exit(2)

t = text.strip()
if t.startswith("```"):
    lines = t.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    t = "\n".join(lines).strip()

try:
    obj = json.loads(t)
except Exception as exc:
    print("model text is not valid JSON: {}".format(exc), file=sys.stderr)
    sys.exit(1)

if not isinstance(obj, dict) or "status" not in obj:
    print("model JSON missing a 'status' field", file=sys.stderr)
    sys.exit(1)

print(json.dumps(obj))
PY
}

# --- like parse_envelope, but for the polish pass: the inner payload is PLAIN TEXT (the
# edited draft), not JSON. Prints the text; exit 1 on any envelope problem. Defined at top
# level like parse_envelope — a heredoc containing backtick fences must NOT sit inside a
# $(...) substitution (bash's $() scanner trips on the backticks).
parse_polish_envelope() {
  # $1 = envelope file
  python3 - "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as fh:
        envelope = json.load(fh)
except Exception:
    sys.exit(1)
if envelope.get("is_error"):
    sys.exit(1)
text = envelope.get("result")
if not isinstance(text, str) or not text.strip():
    sys.exit(1)
t = text.strip()
if t.startswith("```"):
    lines = t.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    t = "\n".join(lines).strip()
print(t, end="")
PY
}

# --- run one full call+parse attempt, with the ONE built-in retry the contract calls for
# when the model's text isn't parseable JSON. Sets RESULT_JSON on success (echoed via
# stdout capture by the caller) and returns 0/1/2 same as parse_envelope.
call_and_parse() {
  # $1 = prompt file to send
  local promptfile="$1"
  local envelope="$WORKROOT/envelope_$$_$RANDOM.json"
  local errfile="$WORKROOT/stderr_$$_$RANDOM.log"

  run_claude_call "$promptfile" "$envelope" "$errfile"
  local parsed rc
  parsed="$(parse_envelope "$envelope")"
  rc=$?
  if [[ $rc -eq 0 ]]; then
    printf '%s' "$parsed"
    return 0
  fi
  if [[ $rc -eq 2 ]]; then
    return 2
  fi

  # rc == 1: unparseable model text — ONE retry with a firmer instruction appended.
  local retry_prompt="$WORKROOT/retry_prompt_$$_$RANDOM.txt"
  {
    cat "$promptfile"
    printf '\nOutput ONLY the raw JSON object, no fences, no commentary.\n'
  } > "$retry_prompt"

  local envelope2="$WORKROOT/envelope2_$$_$RANDOM.json"
  local errfile2="$WORKROOT/stderr2_$$_$RANDOM.log"
  run_claude_call "$retry_prompt" "$envelope2" "$errfile2"
  parsed="$(parse_envelope "$envelope2")"
  rc=$?
  if [[ $rc -eq 0 ]]; then
    printf '%s' "$parsed"
    return 0
  fi
  return 1
}

# --- first attempt ---
build_prompt "" > "$PROMPT_FILE"
RESULT_JSON="$(call_and_parse "$PROMPT_FILE")"
CALL_RC=$?
if [[ $CALL_RC -eq 2 ]]; then
  error_out "model call failed (see stderr capture under $WORKROOT before it's cleaned up)"
fi
if [[ $CALL_RC -eq 1 ]]; then
  error_out "unparseable model output"
fi

# --- pull fields out of the model's JSON with python3 (keeps this bash script free of
# ad-hoc JSON-in-bash parsing) ---
read_field() {
  # $1 = field name; prints the field as-is (empty string if absent/non-string)
  W_JSON="$RESULT_JSON" W_FIELD="$1" python3 - <<'PY'
import json
import os

obj = json.loads(os.environ["W_JSON"])
val = obj.get(os.environ["W_FIELD"], "")
print(val if isinstance(val, str) else "")
PY
}

MODEL_STATUS="$(read_field status)"

if [[ "$MODEL_STATUS" == "skip" ]]; then
  MODEL_REASON="$(read_field reason)"
  [[ -z "$MODEL_REASON" ]] && MODEL_REASON="model returned skip with no reason"
  W_REASON="$MODEL_REASON" emit_and_exit "skip" 2
fi

if [[ "$MODEL_STATUS" != "answered" ]]; then
  error_out "model returned unexpected status: ${MODEL_STATUS:-<empty>}"
fi

ANSWER_TEXT="$(read_field answer)"
if [[ -z "${ANSWER_TEXT// /}" ]]; then
  error_out "model returned status=answered with an empty answer"
fi

# grounded_in is an array, not a plain string field — pull it separately, joined on a
# control character that will never appear in a short pointer phrase.
GROUNDED_IN_JOINED="$(W_JSON="$RESULT_JSON" python3 - <<'PY'
import json
import os

obj = json.loads(os.environ["W_JSON"])
items = obj.get("grounded_in") or []
if isinstance(items, list):
    print("\x1f".join(str(i) for i in items), end="")
PY
)"

# --- DE-AI-IFY POLISH PASS ---
# Second model call: the de-ai-ify skill edits the draft (dash habits, antithesis,
# tricolons, hedging, connective tissue) without touching facts. Edit-only by
# construction: this call never sees the QA bank or the JD — only the draft plus the
# style contract — so it CANNOT introduce a fact the draft didn't already contain.
# Any failure keeps the original draft: the pass can improve or no-op, never block.
POLISH_STATE=""
if [[ -s "$DEAIIFY_SKILL" ]]; then
  POLISH_PROMPT="$WORKROOT/polish_prompt.txt"
  {
    printf 'You are editing a short job-application text written by %s so it reads as natural human writing. Apply this style contract:\n\n' "$USER_FULL_NAME"
    cat "$DEAIIFY_SKILL"
    printf '\n=== HARD RULES (these override the contract above wherever they conflict) ===\n'
    printf -- '- EDIT ONLY: do not add, remove, or change any fact, number, product name, employer, date, or claim.\n'
    printf -- '- ZERO em dashes (U+2014) and ZERO en dashes (U+2013) in the output. This is stricter than the contract above and absolute.\n'
    printf -- '- Keep the SAME language as the input text (a German text stays German).\n'
    printf -- '- Maximum %s words. No markdown, no bullets: plain paragraphs separated by blank lines only.\n' "$MAX_WORDS"
    printf -- '- Output ONLY the edited text. No preamble, no notes, no flags to %s, no JSON, no code fences. If the text needs no changes, output it unchanged.\n' "$USER_FIRST_NAME"
    printf '\n=== TEXT TO EDIT ===\n'
    printf '%s\n' "$ANSWER_TEXT"
  } > "$POLISH_PROMPT"
  POLISH_ENVELOPE="$WORKROOT/polish_envelope.json"
  POLISH_ERRFILE="$WORKROOT/polish_stderr.log"
  run_claude_call "$POLISH_PROMPT" "$POLISH_ENVELOPE" "$POLISH_ERRFILE"
  POLISHED_TEXT="$(parse_polish_envelope "$POLISH_ENVELOPE")"
  POLISH_RC=$?
  if [[ $POLISH_RC -eq 0 && -n "${POLISHED_TEXT// /}" ]]; then
    # Sanity: an edit pass returns roughly the same amount of text. A collapse to under
    # half the draft's words means the model commented or refused instead of editing.
    DRAFT_WORDS="$(W_TEXT="$ANSWER_TEXT" python3 -c 'import os; print(len(os.environ["W_TEXT"].split()))')"
    POLISHED_WORDS="$(W_TEXT="$POLISHED_TEXT" python3 -c 'import os; print(len(os.environ["W_TEXT"].split()))')"
    if [[ "$POLISHED_WORDS" -ge $((DRAFT_WORDS / 2)) ]]; then
      ANSWER_TEXT="$POLISHED_TEXT"
      POLISH_STATE="applied"
    else
      POLISH_STATE="rejected_collapsed"
    fi
  else
    POLISH_STATE="failed_kept_draft"
  fi
else
  POLISH_STATE="skipped_no_skill"
fi

# --- EM-DASH / EN-DASH GUARD ---
# Checked with python3 on the actual unicode codepoints (U+2014, U+2013) rather than a
# grep byte-match, since macOS grep has no reliable \x-style unicode class support.
has_dashes() {
  W_TEXT="$1" python3 -c '
import os
t = os.environ["W_TEXT"]
print("yes" if ("—" in t or "–" in t) else "no")
'
}

EM_DASH_SCRUBBED=""
if [[ "$(has_dashes "$ANSWER_TEXT")" == "yes" ]]; then
  RETRY_PROMPT_FILE="$WORKROOT/em_dash_retry_prompt.txt"
  build_prompt "Your previous draft contained em/en dashes. Rewrite it with zero em dashes and zero en dashes." > "$RETRY_PROMPT_FILE"
  RETRY_RESULT_JSON="$(call_and_parse "$RETRY_PROMPT_FILE")"
  RETRY_RC=$?
  if [[ $RETRY_RC -eq 0 ]]; then
    RETRY_STATUS="$(W_JSON="$RETRY_RESULT_JSON" python3 -c 'import json,os; print(json.loads(os.environ["W_JSON"]).get("status",""))')"
    if [[ "$RETRY_STATUS" == "answered" ]]; then
      RETRY_ANSWER="$(W_JSON="$RETRY_RESULT_JSON" python3 -c 'import json,os; a=json.loads(os.environ["W_JSON"]).get("answer",""); print(a if isinstance(a,str) else "")')"
      if [[ -n "${RETRY_ANSWER// /}" ]]; then
        ANSWER_TEXT="$RETRY_ANSWER"
        GROUNDED_IN_JOINED="$(W_JSON="$RETRY_RESULT_JSON" python3 - <<'PY'
import json
import os

obj = json.loads(os.environ["W_JSON"])
items = obj.get("grounded_in") or []
if isinstance(items, list):
    print("\x1f".join(str(i) for i in items), end="")
PY
)"
      fi
    fi
  fi
  # still present after the retry (or the retry itself failed/skip'd) → mechanical scrub.
  if [[ "$(has_dashes "$ANSWER_TEXT")" == "yes" ]]; then
    # \s*—\s* -> ", " (not a bare replace: "x — y" must become "x, y", never "x ,  y");
    # en dash collapses to a plain hyphen so ranges like "2019 – 2022" stay ranges.
    ANSWER_TEXT="$(W_TEXT="$ANSWER_TEXT" python3 -c '
import os
import re
t = os.environ["W_TEXT"]
t = re.sub(r"\s*—\s*", ", ", t)
t = re.sub(r"\s*–\s*", "-", t)
print(t, end="")
')"
    EM_DASH_SCRUBBED="1"
  fi
fi

WORD_COUNT="$(W_TEXT="$ANSWER_TEXT" python3 -c 'import os; print(len(os.environ["W_TEXT"].split()))')"

# --- letter PDF renderer (cv-builder's proven pattern: headless Chrome with a throwaway
# temp profile — no CDP, so it can never collide with the Chrome-Agent session — plus the
# same poll-then-kill watchdog shape as run_claude_call, because headless Chrome does NOT
# self-exit after --print-to-pdf and an orphaned watcher must never hold stdout open).
render_letter_pdf() {
  # $1 = destination .pdf; renders $ANSWER_TEXT through letter_template.html, filling
  # {{user_full_name}} / {{contact_line}} from user.env. CONTACT_LINE is optional
  # (only written when profile.yaml has a location — see setup/render_templates.py);
  # an empty value just renders an empty sub-line under the name.
  local pdf="$1"
  [[ -s "$LETTER_TEMPLATE" ]] || { echo "letter_template.html missing" >&2; return 1; }
  [[ -x "$CHROME_BIN" ]] || { echo "Chrome not found: $CHROME_BIN" >&2; return 1; }
  local html="$WORKROOT/letter.html"
  W_TEXT="$ANSWER_TEXT" W_TEMPLATE="$LETTER_TEMPLATE" W_HTML="$html" \
  W_NAME="$USER_FULL_NAME" W_CONTACT="${CONTACT_LINE:-}" python3 - <<'PY' || return 1
import html as H
import os

text = os.environ["W_TEXT"]
with open(os.environ["W_TEMPLATE"], encoding="utf-8") as fh:
    tpl = fh.read()
paras = [p.strip() for p in text.split("\n\n") if p.strip()]
body = "\n".join("<p>{}</p>".format(H.escape(p).replace("\n", "<br>")) for p in paras)
tpl = tpl.replace("{{user_full_name}}", H.escape(os.environ["W_NAME"]))
tpl = tpl.replace("{{contact_line}}", H.escape(os.environ["W_CONTACT"]))
with open(os.environ["W_HTML"], "w", encoding="utf-8") as fh:
    fh.write(tpl.replace("{{BODY}}", body))
PY
  rm -f "$pdf"
  "$CHROME_BIN" --headless --disable-gpu --no-pdf-header-footer --no-first-run \
    --no-default-browser-check --hide-scrollbars --user-data-dir="$WORKROOT/chrome_profile" \
    --print-to-pdf="$pdf" "file://$html" >"$WORKROOT/chrome_render.log" 2>&1 &
  local cpid=$!
  (
    waited=0
    while kill -0 "$cpid" 2>/dev/null && [[ $waited -lt 25 ]]; do
      sleep 1
      waited=$((waited + 1))
    done
    kill -9 "$cpid" 2>/dev/null
  ) >/dev/null 2>&1 &
  local kpid=$!
  wait "$cpid" 2>/dev/null
  kill "$kpid" 2>/dev/null
  wait "$kpid" 2>/dev/null
  [[ -s "$pdf" ]]
}

OUT_FILE_WRITTEN=""
if [[ -n "$OUT_FILE" ]]; then
  OUT_DIR="$(dirname "$OUT_FILE")"
  mkdir -p "$OUT_DIR" || error_out "could not create out-file dir: $OUT_DIR"
  if [[ "$OUT_FILE" == *.pdf ]]; then
    if render_letter_pdf "$OUT_FILE"; then
      OUT_FILE_WRITTEN="$OUT_FILE"
    else
      error_out "pdf render failed: $OUT_FILE (see $WORKROOT/chrome_render.log before cleanup)"
    fi
  elif printf '%s' "$ANSWER_TEXT" > "$OUT_FILE"; then
    OUT_FILE_WRITTEN="$OUT_FILE"
  else
    error_out "could not write --out-file: $OUT_FILE"
  fi
fi

W_ANSWER="$ANSWER_TEXT" \
W_GROUNDED_IN="$GROUNDED_IN_JOINED" \
W_WORD_COUNT="$WORD_COUNT" \
W_POLISH="$POLISH_STATE" \
W_EM_DASH_SCRUBBED="$EM_DASH_SCRUBBED" \
W_OUT_FILE="$OUT_FILE_WRITTEN" \
emit_and_exit "answered" 0
