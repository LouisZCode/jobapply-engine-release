#!/usr/bin/env python3
"""PreToolUse guard for unattended job-pipeline workers (LOOP-003 / applicator).

Wired in <repo>/.claude/settings.json; runs before EVERY tool call of every claude
session in this repo — including `claude -p ... --dangerously-skip-permissions`
workers, which skip permission PROMPTS but still execute hooks. Armed ONLY when the
launching wrapper exports JOBAPPLY_GUARD=1 (run_scheduled.sh / run_mail_agent.sh);
interactive human sessions have no such var and pass straight through.

Policy (deterministic enforcement of the CLAUDE.md "report, don't fix" contract):
  - no edits/writes to config or code (.mcp.json, settings.json, run_config.txt,
    profile.yaml, schema.sql, CLAUDE.md, MAIL_AGENT.md, qa_bank.md, any *.sh/*.py/
    *.plist) — workers write only under output/ and /tmp
  - applications.db: SELECT and plain INSERT only — no ALTER/CREATE/DROP/UPDATE/
    DELETE/REPLACE, via the sqlite3 CLI or the sqlite MCP server; no rm/mv of the
    DB file or the backups dir
  - no printing of the secrets the worker was given (BRAIN-001c): its environment
    keys, the key store (keys.json) and the mail credentials (credentials.json,
    token*.json) — keys stay in the environment
Deny = exit 2; stderr becomes the tool-call error the worker sees.
This is a guardrail against ACCIDENTAL self-modification, not a security sandbox.
"""
import json
import os
import re
import sys

if os.environ.get("JOBAPPLY_GUARD") != "1":
    sys.exit(0)

try:
    hook = json.load(sys.stdin)
except Exception:
    sys.exit(0)

tool = hook.get("tool_name") or ""
tin = hook.get("tool_input") or {}


def deny(reason):
    sys.stderr.write(
        "GUARD BLOCKED: {}. Per the contract, do not retry another way - "
        "write an 'ISSUE:' line describing the problem to your run log and "
        "continue.\n".format(reason)
    )
    sys.exit(2)


PROTECTED = re.compile(
    r"(\.mcp\.json|settings(\.local)?\.json|run_config\.txt|profile\.yaml"
    r"|schema\.sql|CLAUDE\.md|MAIL_AGENT\.md|qa_bank\.md|MEMORY\.md"
    r"|[\w.-]+\.(sh|py|plist)\b)",
    re.IGNORECASE,
)


def path_protected(p):
    p = p or ""
    if "/output/" in p or p.startswith("output/") or p.startswith("/tmp/") or p.startswith("/private/tmp/"):
        return False
    return bool(PROTECTED.search(p))


# --- BRAIN-001c: secrets stay sealed -------------------------------------------------
# The worker's environment carries the user's API keys; no Bash command may print
# them, and neither the key store nor the mail credentials may be read back.

_SECRET_VAR_RE = re.compile(
    r"\$[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET)[A-Za-z0-9_]*"        # $ZAI_API_KEY
    r"|\$\{[A-Za-z0-9_]*(?:KEY|TOKEN|SECRET)[A-Za-z0-9_]*\}"   # ${ZAI_API_KEY}
)
_SECRET_FILE_RE = re.compile(r"keys\.json|credentials\.json|token[\w.-]*\.json")
_SECRET_READER_RE = re.compile(r"\b(?:cat|less|more|head|tail|grep|strings|python\w*)\b")


def reveals_secrets(cmd):
    """True when a Bash command could print what the worker was given:
      - `printenv` (any form prints stored values);
      - a bare `env` or `set`, or one piped into something, and `export -p` /
        `declare -p` — the environment-dumping forms (`env VAR=x utility` runs a
        utility and dumps nothing, so that one passes);
      - `echo`/`printf` in a command that references a variable whose name contains
        KEY/TOKEN/SECRET ($VAR and ${VAR} forms);
      - a file reader (cat/less/more/head/tail/grep/strings/python*) in a command
        that also names the key store (keys.json) or the mail credentials
        (credentials.json, token*.json).
    Deliberately coarse: a guard blocks first and asks no questions."""
    if re.search(r"\bprintenv\b", cmd):
        return True
    # `env`/`set` alone or piped: split on the shell separators — a segment that is
    # exactly the word is the dump form (a pipe just ends that segment early).
    for seg in re.split(r"[;|&()\n]", cmd):
        if seg.strip() in ("env", "set"):
            return True
    if re.search(r"\b(?:export|declare)\s+-p\b", cmd):
        return True
    if re.search(r"\b(?:echo|printf)\b", cmd) and _SECRET_VAR_RE.search(cmd):
        return True
    if _SECRET_READER_RE.search(cmd) and _SECRET_FILE_RE.search(cmd):
        return True
    return False


if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
    fp = tin.get("file_path") or tin.get("notebook_path") or ""
    if path_protected(fp):
        deny("editing protected config/code file '{}'".format(fp))
    sys.exit(0)

if tool == "Bash":
    cmd = tin.get("command") or ""
    if reveals_secrets(cmd):
        deny("Keys stay in the environment — this command is not allowed")
    if re.search(r"applications\.db", cmd):
        if re.search(r"\b(ALTER|CREATE|DROP|REINDEX|ATTACH|UPDATE|DELETE|REPLACE|VACUUM)\b", cmd, re.IGNORECASE):
            deny("DB mutation beyond plain INSERT/SELECT on applications.db "
                 "(ALTER/CREATE/DROP/UPDATE/DELETE/REPLACE are off-limits)")
        if re.search(r"\b(rm|mv|shred|truncate)\b[^|;&]*applications\.db", cmd):
            deny("removing/moving applications.db")
    if re.search(r"\b(rm|mv)\b[^|;&]*\bbackups/", cmd):
        deny("touching the DB backups directory")
    # writes into protected files: redirection target or mutating command + protected name
    m = re.search(r">>?\s*(\S+)", cmd)
    if m and path_protected(m.group(1)):
        deny("redirecting output into protected file '{}'".format(m.group(1)))
    if re.search(r"\b(sed\s+(-[a-zA-Z]*\s+)*-i|tee|chmod|chflags|ln)\b", cmd):
        for tok in re.findall(r"\S+", cmd):
            if path_protected(tok):
                deny("mutating protected file '{}'".format(tok))
    if re.search(r"\b(cp|mv)\b", cmd):
        toks = re.findall(r"\S+", cmd)
        if toks and path_protected(toks[-1]):
            deny("overwriting protected file '{}'".format(toks[-1]))
    sys.exit(0)

if tool.startswith("mcp__sqlite__"):
    if tool in ("mcp__sqlite__create_table",):
        deny("schema change via sqlite MCP (create_table)")
    if tool == "mcp__sqlite__write_query":
        q = (tin.get("query") or "").strip().upper()
        ok = re.match(r"^INSERT\s+INTO\s+(APPLICATIONS|APPLICATION_EVENTS)\b", q)
        if not ok or "OR REPLACE" in q or "ON CONFLICT" in q:
            deny("sqlite MCP write_query must be a plain INSERT INTO "
                 "applications/application_events (no OR REPLACE / ON CONFLICT / "
                 "UPDATE / DELETE / DDL)")
    sys.exit(0)

sys.exit(0)
