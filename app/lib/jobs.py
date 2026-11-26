"""app/lib/jobs.py — background job runner (Home, Documents + Profile pages).

Every long-running background process this app kicks off lives here, in one place —
same discipline as actions.py for subprocess/system calls. Four job kinds exist:
"answer_bank" (writes qa_bank.md from profile.yaml + pasted background text),
"cv_generate" (runs the cv-builder skill to produce a CV PDF), "cv_extract" (reads an
uploaded CV PDF and writes a small facts-only YAML the Profile page pre-fills empty
fields from — see start_cv_extract_job below; the actual parsing/pre-fill of that output
lives in profile_io.py and server.py, not here, since this module deliberately knows
nothing about profile.yaml's on-disk shape beyond the flat key list it imports for the
prompt), and "test_run" (the Home page's one-off rehearsal button — see
start_test_run_job below; it launches run_scheduled.sh itself rather than a `claude -p`
prompt, so it's the one job kind that passes _start() an explicit argv/env instead of a
prompt). The first three are started the same way: build a prompt, hand it to `claude -p`
(see _claude_argv below for the exact flags), and let it run in the background,
unattended, exactly like the other headless workers in claude_applicator/ (apply_one.sh,
run_mail_agent.sh) already do for their own non-interactive runs.

`claude -p` jobs run with `--output-format stream-json --verbose`, the same trace format
claude_applicator/run_scheduled.sh already uses for its workers. The raw event stream is
written to app/jobs/<name>-<ts>.ndjson and read back through trace_summary() below, which
gives the UI three things a plain text log can't: a humanized progress feed ("Reading
CV.pdf", "Running: pdftotext ..."), the agent's FINAL answer on its own (the answer bank
document, the DONE/FAILED line, the CV filename), and the run's token usage — input,
output, cache, API-equivalent cost — which app/lib/usage.py rolls up across every run
(these jobs plus the engine's own traces under claude_applicator/output/). test_run's
log is run_scheduled.sh's own plain-text stdout and is shown as-is.

Only one job runs at a time — a global slot, not a queue. Starting a second job while one
is live is a friendly error (see is_running()/_start()). stop() ends the running job
(whole process group — the claude worker and, for test_run, run_scheduled.sh plus the
workers it spawned; Chrome is launched by launchservices and is never in that group).

Job state lives in two places on purpose:
  - the live `subprocess.Popen` handle, kept in the module-level `_HANDLE` dict — only
    valid for the lifetime of THIS server process;
  - a persisted summary in app_state.json (under the "job" key: name, pid, log_path,
    started_at, ended_at, status, ok, timeout_s, usage) — so a page reload, or a server
    restart mid-job, can still report something sane. If the server restarts, `_HANDLE`
    is empty again; status() falls back to checking whether the recorded pid is still
    alive, and resolves to "unknown" (neither ok nor failed — we have no exit code for a
    job we didn't spawn ourselves in this process) if it isn't.

APP_TEST=1 short-circuits the real spawn — `claude -p` for three job kinds, run_scheduled.sh
(and everything it would in turn spawn) for test_run — same pattern as actions.py's `_stub`
elsewhere in this app: nothing is invoked, a canned plain-text log is written instead and
the job is marked done immediately, so the rest of the Documents-page flow (start -> poll ->
show result -> save) is fully testable without spending a real call. The answer-bank job
additionally gets a canned markdown document appended to its log, so the UI's "show the
generated result" step has something real to render in tests.
"""
import json
import os
import re
import signal
import subprocess
import time

from . import actions, activity, llm, paths, profile_io, state

# Only valid within this process — reset on every server restart. status()/is_running()
# never trust this alone; they fall back to the persisted pid when it's empty.
_HANDLE = {"proc": None, "name": None}

ANSWER_BANK_TIMEOUT_S = 600   # 10 minutes — a single non-interactive text-generation call.
CV_GENERATE_TIMEOUT_S = 900   # 15 minutes — a real agentic run (skill read, HTML draft, PDF render).
CV_EXTRACT_TIMEOUT_S = 600    # 10 minutes — read one PDF, write one small YAML file.

# Server boot time: an API-brain (thread) job whose started_at is OLDER than this
# process cannot have a live thread — the app restarted mid-job (2026-11-08: a
# copy-update restart killed Luis's in-flight Zai extraction and the job hung
# 'running' until its 10-minute timeout). Resolve such jobs immediately.
_BOOT_TS = time.time()
TEST_RUN_TIMEOUT_S = 3600     # 60 minutes — a real prepare run browses LinkedIn for a while.

# Typical wall-clock per job kind, measured on real runs (cv_extract 64-88s, answer bank
# ~170s on 2026-08-19). Only used to draw the progress bar — it creeps toward 95% at this
# pace and jumps to 100% when the job actually ends, so a slow run never "finishes" early.
ESTIMATED_S = {
    "cv_extract": 90,
    "answer_bank": 180,
    "cv_generate": 600,
    "test_run": 900,
}

# Human labels for the single shared job panel (templates/_job_panel.html).
JOB_LABELS = {
    "cv_extract": "Reading your CV",
    "answer_bank": "Writing your answer bank",
    "cv_generate": "Building your CV",
    "test_run": "Test run",
}

CANNED_QA_BANK = """## 1. Identity summary

I'm Maria Musterfrau, based in Berlin, Germany. I've spent the last 5 years building backend
and data-pipeline systems in B2B SaaS, most recently focused on internal automation tooling. I'm
looking for a mid-to-senior backend or platform engineering role, ideally remote-first.

## 2. Employers

### Beispiel GmbH (2022-present)
- Title: Senior Backend Engineer
- Mode: remote
- What I actually did: built and ran the internal billing-reconciliation service, migrated a
  batch ETL job from cron scripts to an orchestrated pipeline, mentored two junior engineers.
- Numbers: cut the monthly reconciliation run from 6 hours to 40 minutes; team of 5.

### Muster & Co (2019-2022)
- Title: Backend Engineer
- Mode: hybrid
- What I actually did: owned the customer-facing REST API, wrote the first integration test
  suite for the payments module, on-call rotation for production incidents.
- Numbers: cut payments-module bug reports by roughly a third after the test suite shipped.

## 3. Projects

### Beispiel Tracker
- What it does: a small open-source CLI that diffs two JSON API responses and highlights
  schema drift.
- Stack: Python, Click.
- Real traction / status: personal project, not deployed, used internally by two former
  teammates.

## 4. Stories (Challenge -> Action -> Result)

### Story: the reconciliation rewrite
- Challenge: the monthly billing reconciliation job took 6 hours and failed silently about
  once a quarter.
- Action: I rewrote it as an idempotent, checkpointed pipeline and added alerting on partial
  failure.
- Result: run time dropped to 40 minutes and it has not failed silently since.

## 5. Technical stack — depth, not just a list

- Python: 5 years, daily driver, built the reconciliation service and the payments API above.
- PostgreSQL: 4 years, comfortable with query tuning and migrations, not a DBA.
- Docker: 3 years, used for local dev and CI, not deep into orchestration internals.

## 6. Languages & fluency

- German: native.
- English: C1, comfortable writing technical documentation and cover letters.

## 7. Availability & anything else worth stating once

Notice period is 4 weeks. Open to remote-first roles based anywhere in the EU.
"""

CANNED_CV_EXTRACT_YAML = """full_name: "Maria Musterfrau"
email: "maria.musterfrau@example.com"
phone: "+49 30 1234567"
current_city: "Berlin"
country: "Germany"
languages:
  - language: "German"
    level: "native"
  - language: "English"
    level: "C1"
education:
  - degree: "BSc Computer Science"
    school: "Musterstadt Technical University"
    years: "2015-2018"
certifications: []
experience:
  most_recent:
    company: "Beispiel GmbH"
    title: "Senior Backend Engineer"
    industry: "B2B SaaS"
    mode: "remote"
    start: "2022"
    end: "present"
    highlight: "Rewrote the monthly billing-reconciliation job as an idempotent, checkpointed pipeline, cutting the run from 6 hours to 40 minutes."
  prior_companies:
    - "Muster & Co"
  summary: "5 years of backend and data-pipeline engineering in B2B SaaS, most recently focused on internal automation tooling."
technical_stack:
  - "Python"
  - "PostgreSQL"
  - "Docker"
portfolio_projects:
  - name: "Beispiel Tracker"
    desc: "A small open-source CLI that diffs two JSON API responses and highlights schema drift."
"""

def _cv_generate_prompt():
    # MULTI-002b: name the ACTIVE user's files absolutely — the job's claude runs from
    # the repo root, where those paths are per-user symlinks only once
    # claude_applicator/bind_user.sh has run (the app can start this job before any
    # engine run did). The skill's tracked tools (template.html, render.sh) stay at
    # their repo paths either way; with JOBAPPLY_DATA unset every path below is exactly
    # today's repo-relative location.
    return (
        "Use the cv-builder skill to build a CV PDF for this profile. Read "
        + paths.SKILL_MD + " for the house style and steps, and ground every fact "
        "ONLY in " + paths.SKILL_ANSWER_BANK + " and " + paths.PROFILE_YAML + " — never "
        "invent an employer, date, metric, tool, or certification. If no role angle is specified, "
        "pick a sensible general-purpose angle from the 'Role variants' section of the answer bank. "
        "Render the CV to PDF following the skill's own steps (the tracked template.html and "
        "render.sh live in .claude/skills/cv-builder/ — copy template.html into a draft, fill "
        "it in, then render.sh), then copy the finished PDF into " + paths.CV_DATA_DIR + "/ so the "
        "job-application pipeline can pick it up. Report the final PDF filename on the last line of "
        "your output."
    )


def _job_dir():
    os.makedirs(paths.JOBS_DIR, exist_ok=True)
    return paths.JOBS_DIR


def _claude_argv(prompt, max_turns=None):
    # Non-interactive (`-p`) with no TTY attached to answer a permission prompt, so this
    # flag is required — same convention every headless worker in claude_applicator/
    # already uses (apply_one.sh, run_mail_agent.sh, run_scheduled.sh, watch.sh).
    # stream-json (+ --verbose, which print mode requires for it) is what gives us the
    # progress feed and the token usage — see the module docstring / trace_summary().
    cli = state.claude_cli_path() or "claude"
    argv = [
        cli, "-p", prompt, "--model", "claude-sonnet-5",
        "--output-format", "stream-json", "--verbose",
        "--dangerously-skip-permissions",
    ]
    if max_turns is not None:
        # --max-turns doesn't appear in `claude -p --help`, but the installed CLI accepts
        # it — claude_applicator/apply_one.sh passes 100 and run_scheduled.sh passes 250
        # for the same kind of unattended, non-interactive run these jobs are.
        argv += ["--max-turns", str(max_turns)]
    return argv


# ------------------------------------------------------------------------------
# Trace reading — stream-json (.ndjson) vs plain text (.log)
# ------------------------------------------------------------------------------

def _is_trace(path):
    return bool(path) and path.endswith(".ndjson")


def _iter_events(path):
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def _short(text, n=90):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _describe_tool(name, inp):
    """One friendly line per tool call for the progress feed — never the raw arguments
    (a Write's content could be the user's whole answer bank)."""
    inp = inp or {}
    if name in ("Read", "Write", "Edit", "NotebookEdit"):
        target = inp.get("file_path") or inp.get("path") or ""
        verb = {"Read": "Reading", "Write": "Writing", "Edit": "Editing", "NotebookEdit": "Editing"}[name]
        return "{} {}".format(verb, os.path.basename(target) or "a file")
    if name == "Bash":
        return "Running: {}".format(_short(inp.get("description") or inp.get("command") or "a command", 70))
    if name in ("Glob", "Grep"):
        return "Searching files"
    if name == "WebFetch":
        return "Fetching a web page"
    if name == "WebSearch":
        return "Searching the web"
    if name == "Task":
        return "Delegating: {}".format(_short(inp.get("description") or "a sub-task", 60))
    return "Using {}".format(name)


def trace_summary(path):
    """Reads a stream-json trace once, front to back. Returns
    {lines: [progress lines], result: final text or None, usage: dict or None,
     cost_usd: float or None, subtype: result subtype or None}.
    `usage` is normalized to input/output/cache_read/cache_create token counts taken from
    the final "result" event — the CLI's own session total, which covers every turn, tool
    call and sub-agent in that run."""
    lines, result_text, usage, cost, subtype = [], None, None, None, None
    for ev in _iter_events(path):
        etype = ev.get("type")
        if etype == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                btype = block.get("type")
                if btype == "text" and (block.get("text") or "").strip():
                    lines.append(_short(block["text"], 160))
                elif btype == "tool_use":
                    lines.append("▸ " + _describe_tool(block.get("name"), block.get("input")))
        elif etype == "result":
            subtype = ev.get("subtype")
            result_text = ev.get("result") if isinstance(ev.get("result"), str) else None
            cost = ev.get("total_cost_usd")
            usage = normalize_usage(ev.get("usage"))
            if ev.get("is_error") and not result_text:
                result_text = "The run ended with an error ({}).".format(subtype or "unknown")
    return {"lines": lines, "result": result_text, "usage": usage, "cost_usd": cost, "subtype": subtype}


def normalize_usage(raw):
    """Same four counters whatever the CLI version calls them (see usage.py for the
    cross-run roll-up)."""
    if not isinstance(raw, dict):
        return None
    def num(*keys):
        for k in keys:
            v = raw.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0
    return {
        "input": num("input_tokens", "inputTokens"),
        "output": num("output_tokens", "outputTokens"),
        "cache_read": num("cache_read_input_tokens", "cacheReadInputTokens"),
        "cache_create": num("cache_creation_input_tokens", "cacheCreationInputTokens"),
    }


def _tail(log_path, n):
    """Last n lines of the job's human-readable feed: progress lines for a trace, the raw
    text for a plain log."""
    if not log_path or not os.path.isfile(log_path):
        return ""
    if _is_trace(log_path):
        summary = trace_summary(log_path)
        lines = summary["lines"]
        if summary["result"]:
            final = _short(summary["result"], 160)
            # The final answer usually repeats the last text block — show it once.
            if lines and lines[-1] == final:
                lines = lines[:-1]
            lines = lines + ["✓ " + final]
        return "\n".join(lines[-n:])
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    return "".join(lines[-n:])


def read_log_full(log_path):
    """The job's RESULT: for a trace, the agent's final answer (the generated document,
    the DONE/FAILED line, ...); for a plain log, the whole file."""
    if not log_path or not os.path.isfile(log_path):
        return ""
    if _is_trace(log_path):
        summary = trace_summary(log_path)
        return summary["result"] or ""
    with open(log_path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def current_job():
    return state.load_state().get("job")


def _finish(job, status_value, ok, event, **event_fields):
    """Persists a job's end state and records its token usage (from the trace) on the
    job itself so the UI can show "this run used N tokens" without re-reading the file."""
    job = dict(job, status=status_value, ok=ok, ended_at=time.time())
    if _is_trace(job.get("log_path")):
        summary = trace_summary(job["log_path"])
        job["usage"] = summary["usage"]
        job["cost_usd"] = summary["cost_usd"]
        # A clean exit code but an error-typed result (e.g. the max-turns cap) is still a
        # failure for the user's purposes.
        if ok and summary["subtype"] and summary["subtype"] != "success":
            job["ok"] = False
            job["error"] = "The run stopped early ({}).".format(summary["subtype"])
    state.update_state(job=job)
    activity.log_event("job", name=job.get("name"), event=event, ok=job.get("ok"), **event_fields)
    return job


def status():
    """The single source of truth for GET /jobs/status: {running, name, label, elapsed,
    estimate_s, progress, log_tail, done, ok, error, usage}. Also resolves a
    finished/timed-out/dead job's persisted status as a side effect — this function IS the
    poll, both for the JSON endpoint and for any page render that needs a fresh view of the
    current job. "error" is empty for the common case; it only carries a message when a
    caller (e.g. server.py's cv_extract harvest) has recorded a "harvest_error" on the
    persisted job, or the trace ended with a non-success subtype — the job process itself
    can exit 0 yet still have produced nothing usable, which isn't something a bare exit
    code alone can say."""
    job = current_job()
    if not job:
        return {
            "running": False, "name": None, "label": "", "elapsed": 0, "estimate_s": 0,
            "progress": 0, "log_tail": "", "done": True, "ok": None, "error": "", "usage": None,
        }

    name = job.get("name")
    log_path = job.get("log_path")

    if job.get("status") == "running":
        # Thread (API-brain) jobs: consume the worker thread's result FIRST — a
        # finished-but-not-yet-polled result must always beat timeout/bootout checks
        # (2026-11-08: a real run flipped to 'timeout' at exactly 600s although its
        # log ended in DONE, because completion only registered on a page reload;
        # the 4-second job-panel poll now resolves it the moment it lands).
        if job.get("brain") and job.get("name") in _API_DONE:
            result = _API_DONE.pop(job["name"])
            elapsed = time.time() - job.get("started_at", time.time())
            job = _finish(job, "done", bool(result["ok"]),
                          "finished_api" if result["ok"] else "api_failed",
                          elapsed_s=round(elapsed, 1))
            state.update_state(job=job)
            activity.log_event("job", name=job.get("name"), event="finished_api",
                               ok=bool(result["ok"]))
        else:
            proc = _HANDLE.get("proc") if _HANDLE.get("name") == name else None
            elapsed_now = time.time() - job.get("started_at", time.time())
            if proc is not None:
                rc = proc.poll()
                if rc is not None:
                    job = _finish(job, "done", rc == 0, "finished", elapsed_s=round(elapsed_now, 1))
                elif job.get("timeout_s") and elapsed_now > job["timeout_s"]:
                    _kill_group(proc.pid)
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        _kill_group(proc.pid, hard=True)
                    job = _finish(job, "timeout", False, "timeout", timeout_s=job.get("timeout_s"))
            else:
                # No in-process handle — either a different process/reload, or the
                # server restarted mid-job. Fall back to a pid-liveness check; we have
                # no exit code either way, so a dead pid resolves to "unknown", never
                # a fabricated ok/fail. EXCEPTION: API-brain jobs (llm.py thread, no
                # pid, "brain" set) resolve ONLY via _API_DONE above or the guards
                # below — the generic pid check must never kill a live thread job
                # (2026-11-08: "did not produce an output file").
                if job.get("pid") is None and job.get("brain"):
                    if job.get("started_at", 0) < _BOOT_TS:
                        job = _finish(job, "unknown", False, "app_restarted",
                                      harvest_error="The app restarted mid-read — run it again.")
                    elif job.get("timeout_s") and elapsed_now > job["timeout_s"]:
                        job = _finish(job, "timeout", False, "timeout", timeout_s=job.get("timeout_s"))
                elif not _pid_is_ours(job.get("pid")):
                    job = _finish(job, "unknown", None, "resolved_unknown")

    running = job.get("status") == "running"
    ended_at = job.get("ended_at") or time.time()
    elapsed = ended_at - job.get("started_at", ended_at)
    estimate = ESTIMATED_S.get(name, 300)
    if running:
        progress = min(95, int(100 * elapsed / estimate)) if estimate else 0
    else:
        progress = 100
    error = job.get("harvest_error") or job.get("error") or ""
    if job.get("status") == "stopped" and not error:
        error = "Stopped by you."
    elif job.get("status") == "timeout" and not error:
        error = "Stopped after the time limit."
    elif job.get("status") == "unknown" and not error:
        error = "The app was restarted while this was running — no result was recorded."
    return {
        "running": running,
        "name": name,
        "label": JOB_LABELS.get(name, name or ""),
        "elapsed": round(elapsed, 1),
        "estimate_s": estimate,
        "progress": progress,
        "log_tail": _tail(log_path, 12),
        "done": not running,
        "ok": job.get("ok"),
        "error": error,
        "usage": job.get("usage"),
    }


def is_running():
    return status()["running"]


def _pid_is_ours(pid):
    """Liveness + identity for a persisted job pid. Jobs are spawned with
    start_new_session=True, so a live job is its own session/process-group leader
    (getpgid(pid) == pid). A pid that the OS has since handed to some unrelated process
    almost never is — so this refuses to treat a recycled pid as "our job still running",
    which is also what keeps _kill_group() from ever signalling a stranger."""
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return os.getpgid(pid) == pid
    except (OSError, ProcessLookupError):
        return False


def _kill_group(pid, hard=False):
    """Jobs are spawned as their own process group (start_new_session=True), so the group
    id IS the pid — the whole tree (run_scheduled.sh + caffeinate + its claude workers)
    goes together. Chrome is launched via `open -a` (launchservices) and is never in it.
    Only ever signals a pid that still passes _pid_is_ours()."""
    if not _pid_is_ours(pid):
        return
    sig = signal.SIGKILL if hard else signal.SIGTERM
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def stop():
    """Ends the running job, if any — the Stop button. Returns (ok, message). Resolves the
    job through status() FIRST: a job whose process already exited (but that nobody has
    polled since) is reported as finished, never overwritten as "stopped" — its real
    result (e.g. a finished CV read waiting to be harvested) stays intact."""
    resolved = status()
    if not resolved["running"]:
        if resolved["name"] and resolved["done"] and resolved["ok"]:
            return False, "Nothing to stop — the last job had already finished."
        return False, "Nothing is running."
    job = current_job()
    if actions.test_mode() and not job.get("pid"):
        _finish(job, "stopped", False, "stopped")
        return True, "Stopped."
    pid = job.get("pid")
    proc = _HANDLE.get("proc") if _HANDLE.get("name") == job.get("name") else None
    # Last look before signalling anything: if the process exited on its own in the
    # meantime, let status() record its real exit code instead of calling it "stopped".
    if proc is not None and proc.poll() is not None:
        status()
        return False, "It finished on its own just now — nothing was stopped."
    if pid:
        _kill_group(pid)
        deadline = time.time() + 5
        while time.time() < deadline:
            if proc is not None:
                if proc.poll() is not None:
                    break
            elif not _pid_is_ours(pid):
                break
            time.sleep(0.2)
        else:
            _kill_group(pid, hard=True)
    _finish(job, "stopped", False, "stopped")
    return True, "Stopped."


def _start(name, prompt, timeout_s, canned_output=None, max_turns=None, extra=None, argv=None, env=None):
    # extra: optional dict merged into the persisted job record — e.g. cv_extract stashes
    # the source PDF's filename there so the harvest step (server.py) can later name it in
    # the pre-fill banner without having to re-derive it from anywhere else.
    # argv: override the spawned command instead of building a `claude -p` invocation from
    # prompt — used by start_test_run_job() below, which launches run_scheduled.sh (prompt
    # is then just "" — it's still logged/persisted, but nothing reads it in that path).
    # Its output is plain text, so it gets a .log; claude jobs get a .ndjson trace.
    # env: extra environment variables layered onto this process's own os.environ for the
    # spawned child only — also start_test_run_job()'s doing (its JOBAPPLY_* test knobs).
    if is_running():
        running_name = (current_job() or {}).get("name")
        return False, "A job is already running ({}). Wait for it to finish before starting another.".format(
            JOB_LABELS.get(running_name, running_name)
        )

    # Engine jobs (argv-style) still hard-require the Claude CLI (kept deliberately,
    # BRAIN-001b: only the refusal text changed). App-side cv_extract for a key-brain
    # never comes through _start (it runs the llm.py thread path instead).
    if not actions.test_mode() and argv is not None and state.claude_cli_path() is None:
        return False, "This job needs Claude. On this page you can add your CV and your answers by hand, or connect Claude under Connections."

    is_claude_job = argv is None
    argv = argv if argv is not None else _claude_argv(prompt, max_turns=max_turns)
    ts = time.strftime("%Y%m%d-%H%M%S")
    started_at = time.time()

    if actions.test_mode():
        log_path = os.path.join(_job_dir(), "{}-{}.log".format(name, ts))
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("[APP_TEST] would run: {}\n".format(" ".join(argv)))
            if canned_output:
                fh.write(canned_output.rstrip("\n") + "\n")
        job = {
            "name": name, "pid": None, "log_path": log_path,
            "started_at": started_at, "ended_at": time.time(),
            "status": "done", "ok": True, "timeout_s": timeout_s,
        }
        job.update(extra or {})
        state.update_state(job=job)
        activity.log_event("job", name=name, event="started", test_mode=True)
        _HANDLE["proc"] = None
        _HANDLE["name"] = None
        return True, None

    ext = "ndjson" if is_claude_job else "log"
    log_path = os.path.join(_job_dir(), "{}-{}.{}".format(name, ts, ext))
    fh = open(log_path, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            argv, cwd=paths.REPO_ROOT, stdin=subprocess.DEVNULL,
            stdout=fh, stderr=subprocess.STDOUT,
            env={**os.environ, **env} if env else None,
            start_new_session=True,
        )
    except OSError as exc:
        return False, "Could not start the job: {}".format(exc)
    finally:
        fh.close()

    job = {
        "name": name, "pid": proc.pid, "log_path": log_path,
        "started_at": started_at, "ended_at": None,
        "status": "running", "ok": None, "timeout_s": timeout_s,
    }
    job.update(extra or {})
    state.update_state(job=job)
    activity.log_event("job", name=name, event="started", pid=proc.pid)
    _HANDLE["proc"] = proc
    _HANDLE["name"] = name
    return True, None


def _answer_bank_prompt(template_text, profile_text, background_text):
    # Plain concatenation, deliberately not str.format() — profile.yaml / pasted CV text
    # can legitimately contain literal `{`/`}` characters, which would break .format().
    return (
        "You are filling in a personal answer-bank document that a job-application assistant "
        "will later treat as ground truth. Produce a complete qa_bank.md document that follows "
        "the SAME section structure (same section numbers, same section headers, same order) as "
        "the template shown below, with every CHANGEME placeholder replaced by real content — but "
        "ONLY where that content is actually present in the two source materials given further "
        "below (the saved profile.yaml and the pasted background text).\n\n"
        "HARD RULE — do not break this: you are grounded ONLY in the material given below. Never "
        "invent an employer, a job title, a date, a metric, a tool, a certification, a story, or "
        "any other fact that is not stated or directly inferable from the two sources. Where the "
        "sources do not cover something the template asks for, leave that section's own CHANGEME "
        "placeholder line in place rather than making something up.\n\n"
        "Output ONLY the finished markdown document — no code fence, no preamble, no explanation "
        "of what you did, nothing before or after it.\n\n"
        "=== TEMPLATE STRUCTURE TO FOLLOW (claude_applicator/qa_bank.template.md) ===\n"
        + template_text + "\n\n"
        "=== SOURCE 1: profile.yaml ===\n"
        + profile_text + "\n\n"
        "=== SOURCE 2: pasted background / CV text ===\n"
        + background_text + "\n"
    )


def start_answer_bank_job(raw_background):
    if not os.path.isfile(paths.PROFILE_YAML):
        return False, "No profile.yaml yet — fill in the Profile page first."
    if len((raw_background or "").strip()) < 200:
        return False, "Paste at least a couple hundred characters of background before generating."

    with open(paths.QA_BANK_TEMPLATE, encoding="utf-8") as fh:
        template_text = fh.read()
    with open(paths.PROFILE_YAML, encoding="utf-8") as fh:
        profile_text = fh.read()

    prompt = _answer_bank_prompt(template_text, profile_text, raw_background)
    return _start("answer_bank", prompt, ANSWER_BANK_TIMEOUT_S, canned_output=CANNED_QA_BANK, max_turns=30)


def start_cv_generate_job():
    if not os.path.isfile(paths.SKILL_ANSWER_BANK):
        return False, "No CV answer bank yet — generate and save one in the Answer bank section above first."
    if not os.path.isfile(paths.SKILL_MD):
        return False, "Profile isn't rendered yet — save it on the Profile page first."
    return _start("cv_generate", _cv_generate_prompt(), CV_GENERATE_TIMEOUT_S, max_turns=100)


def _cv_extract_prompt(pdf_path):
    key_list = "\n".join("  - {}".format(k) for k in profile_io.CV_EXTRACT_ALLOWED_KEYS)
    return (
        "Read the PDF at {pdf} and extract ONLY facts that literally appear in it — names, "
        "dates, employers, titles, technologies, degrees, projects, contact details, exactly "
        "as written.\n\n"
        "HARD RULE — do not break this: you are grounded ONLY in the PDF's own text. Never "
        "invent, never embellish, never infer a fact beyond what's stated. Where the PDF "
        "doesn't cover something below, leave that key out of the file entirely rather than "
        "guess or leave a placeholder.\n\n"
        "Write a YAML file to {out} containing ONLY these top-level keys (they mirror "
        "claude_applicator/profile.yaml's own field names and shapes, flattened one level — "
        "read that file if you need more context on what a field means — never add any other "
        "key, and omit any key you found no grounded data for):\n\n"
        "{keys}\n\n"
        "Shapes for the non-scalar keys:\n"
        "  languages: [{{language: string, level: string}}, ...]\n"
        "  education: [{{degree: string, school: string, years: string}}, ...]\n"
        "  certifications: [string, ...]\n"
        "  experience:\n"
        "    most_recent: {{company: string, title: string, industry: string, mode: string, "
        "start: string, end: string, highlight: string}}\n"
        "    prior_companies: [string, ...]\n"
        "    summary: string\n"
        "  technical_stack: [string, ...]\n"
        "  portfolio_projects: [{{name: string, desc: string}}, ...]\n\n"
        "Write valid YAML only — no markdown code fence, no commentary inside the file. Your "
        "FINAL text reply (after writing the file) must be exactly one line: DONE if the file "
        "was written successfully, or FAILED <short reason> if it could not be (e.g. the PDF "
        "was empty, unreadable, or contained no usable facts)."
    ).format(pdf=pdf_path, out=paths.CV_EXTRACT_OUTPUT_YAML, keys=key_list)


def _cv_extract_api_prompt(cv_text):
    """Same contract as _cv_extract_prompt, but for a chat API: the CV text is inlined
    (no filesystem), and the YAML comes back in the reply body instead of being written
    by the model."""
    key_list = "\n".join("  - {}".format(k) for k in profile_io.CV_EXTRACT_ALLOWED_KEYS)
    return (
        "Extract ONLY facts that literally appear in the CV text below — names, dates, "
        "employers, titles, technologies, degrees, projects, contact details, exactly "
        "as written.\n\n"
        "HARD RULE — do not break this: you are grounded ONLY in the CV's own text. Never "
        "invent, never embellish, never infer a fact beyond what's stated. Where the CV "
        "doesn't cover something, leave that key out entirely rather than guess or leave "
        "a placeholder.\n\n"
        "Reply with a YAML document containing ONLY these top-level keys (they mirror "
        "claude_applicator/profile.yaml's own field names and shapes, flattened one "
        "level; omit any key you found no grounded data for):\n\n"
        "{keys}\n\n"
        "Shapes for the non-scalar keys:\n"
        "  languages: [{{language: string, level: string}}, ...]\n"
        "  education: [{{degree: string, school: string, years: string}}, ...]\n"
        "  certifications: [string, ...]\n"
        "  experience:\n"
        "    most_recent: {{company: string, title: string, industry: string, mode: string, "
        "start: string, end: string, highlight: string}}\n"
        "    prior_companies: [string, ...]\n"
        "    summary: string\n"
        "  technical_stack: [string, ...]\n"
        "  portfolio_projects: [{{name: string, desc: string}}, ...]\n\n"
        "Reply with the YAML only — no markdown code fence, no commentary.\n\n"
        "===== CV TEXT =====\n{cv}"
    ).format(keys=key_list, cv=cv_text)


# Finished API-brain jobs report back through this dict (written by the worker thread,
# consumed + finalized by poll_api_jobs() on the next page GET) — state.json is only
# ever written from the server thread, same as for subprocess jobs.
_API_DONE = {}
_API_PDF = {"path": None}


def poll_api_jobs():
    """Called at the top of GET /profile (alongside the harvest): finalizes a finished
    API-brain cv_extract job into the persisted job record."""
    if not _API_DONE:
        return
    for name, result in _API_DONE.items():
        job = current_job()
        if job and job.get("name") == name and job.get("status") == "running":
            job.update({"status": "done", "ok": bool(result["ok"]),
                        "ended_at": result["ended_at"]})
            state.update_state(job=job)
            activity.log_event("job", name=name, event="finished_api", ok=bool(result["ok"]))
    _API_DONE.clear()


def _api_extract_thread(provider, model, log_path, timeout):
    """Runs the CV extraction over llm.chat() and writes the output YAML the same
    harvest step already consumes. The thread writes files + _API_DONE only — never
    state.json."""
    from pypdf import PdfReader  # same reader the onboarding story flow uses
    result_ok, note = False, ""
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("[{}] reading CV via {} ({})...\n".format(
                time.strftime("%H:%M:%S"), provider, model))
            try:
                text = "\n".join(p.extract_text() or "" for p in PdfReader(_API_PDF["path"]).pages)
            except Exception as exc:
                text = ""
                note = "PDF unreadable: {}".format(exc)
            if len(text.strip()) < 200:
                note = note or "couldn't read enough text from that PDF"
                fh.write("FAILED " + note + "\n")
            else:
                reply = llm.chat(
                    provider,
                    system="You extract structured facts from CVs exactly as written, YAML only.",
                    user=_cv_extract_api_prompt(text), model=model, timeout=timeout)
                if not reply["ok"]:
                    note = reply["message"]
                    fh.write("FAILED " + note + "\n")
                else:
                    yaml_text = reply["text"].strip()
                    if yaml_text.startswith("```"):
                        yaml_text = yaml_text.strip("`").lstrip("yaml").strip()
                    import yaml as _yaml
                    parsed = _yaml.safe_load(yaml_text)
                    if not isinstance(parsed, dict) or not parsed:
                        note = "the model's answer wasn't usable YAML"
                        fh.write("FAILED " + note + "\n")
                    else:
                        with open(paths.CV_EXTRACT_OUTPUT_YAML, "w", encoding="utf-8") as yfh:
                            yfh.write(_yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False))
                        fh.write("DONE\n")
                        result_ok = True
    finally:
        _API_DONE["cv_extract"] = {"ok": result_ok, "ended_at": time.time(), "note": note}


def start_cv_extract_job(pdf_path, brain="claude", model=None):
    """pdf_path: absolute path to the just-uploaded CV PDF (already saved into
    claude_applicator/data/ by the caller — see server.py's shared _save_cv_upload). Any
    stale output from a previous run is removed up front so a crashed/killed job can never
    leave behind a file a later harvest mistakes for this run's output.

    Guard (2026-08-19): a finished-but-not-yet-harvested cv_extract is refused rather than
    silently restarted — restarting would delete that finished run's output file before the
    Profile page ever read it (exactly what happened when a page reload re-submitted the
    form: 64 seconds of extraction thrown away)."""
    job = current_job()
    if (job and job.get("name") == "cv_extract" and job.get("status") != "running"
            and not job.get("harvested") and os.path.isfile(paths.CV_EXTRACT_OUTPUT_YAML)):
        return False, "The last CV read is finished — reload the Profile page to see its suggestions first."

    if os.path.isfile(paths.CV_EXTRACT_OUTPUT_YAML):
        os.remove(paths.CV_EXTRACT_OUTPUT_YAML)

    prompt = _cv_extract_prompt(pdf_path)
    extra = {"source_filename": os.path.basename(pdf_path), "harvested": False}

    if actions.test_mode():
        _job_dir()  # ensure app/jobs/ exists before writing the fixture into it
        with open(paths.CV_EXTRACT_OUTPUT_YAML, "w", encoding="utf-8") as fh:
            fh.write(CANNED_CV_EXTRACT_YAML)

    # --- key-brain path: thread + chat API, same job record shape on finish ------------
    if brain != "claude":
        if is_running():
            return False, "A job is already running — wait for it to finish before starting another."
        model = llm.model_for(brain, model)
        if not model:
            return False, "Set a model id for that brain first (e.g. glm-5.3-flash for Zai)."
        llm.remember_model(brain, model)
        ts = time.strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(_job_dir(), "cv_extract-{}.log".format(ts))
        job_rec = {"name": "cv_extract", "pid": None, "log_path": log_path,
                   "started_at": time.time(), "status": "running", "ok": None,
                   "timeout_s": CV_EXTRACT_TIMEOUT_S, "brain": brain, "model": model}
        job_rec.update(extra)
        state.update_state(job=job_rec)
        activity.log_event("job", name="cv_extract", event="started", brain=brain, model=model)
        if actions.test_mode():
            # canned: instant DONE so the APP_TEST walkthrough exercises the harvest
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write("[APP_TEST] would call {} ({}).\nDONE\n".format(brain, model))
            state.update_state(job={**job_rec, "status": "done", "ok": True,
                                    "ended_at": time.time()})
            return True, None
        _API_PDF["path"] = pdf_path
        import threading
        threading.Thread(target=_api_extract_thread,
                         args=(brain, model, log_path, 120), daemon=True).start()
        return True, None

    return _start("cv_extract", prompt, CV_EXTRACT_TIMEOUT_S, canned_output="DONE", max_turns=30, extra=extra)


_MODE_LINE_RE = re.compile(r"^MODE:\s*([a-z]+)")

CANNED_TEST_RUN_LOG = (
    "===== scheduled run (test) =====\n"
    "[3] query #1/1 — up to 1 apply, prepare mode\n"
    "Test run complete.\n"
)


def start_test_run_job():
    """The Home page's "run a test now" button. This is NOT a separate rehearsal script —
    it IS the real daily entry point (claude_applicator/run_scheduled.sh, the same file the
    launchd schedule fires every morning), just started by hand with env overrides that
    constrain it to one search query and up to one application (JOBAPPLY_QUERY_COUNT /
    JOBAPPLY_MAX_APPLIES, read as test knobs in run_scheduled.sh) and skip the mail
    sweep/agent steps (JOBAPPLY_SKIP_SWEEPS — a rehearsal is about the applying, and on a
    Mac without the Gmail reader set up the mail agent would only spend tokens hunting for
    a venv that isn't there). run_scheduled.sh takes its own cross-checkout overlap lock, so
    this button can never double-run alongside a live scheduled round — worst case it waits
    for the same lock a second launchd trigger would.

    Refuses to start unless run_config.txt's MODE is "prepare" — a "live" mode test run would
    actually submit a real application, which a one-off rehearsal button must never risk by
    surprise."""
    try:
        with open(paths.RUN_CONFIG, encoding="utf-8") as fh:
            run_config_lines = fh.readlines()
    except OSError:
        return False, "run_config.txt not found — set up the Schedule page first."

    mode = None
    for line in run_config_lines:
        m = _MODE_LINE_RE.match(line)
        if m:
            mode = m.group(1)
            break
    if mode != "prepare":
        return False, ("Test runs only happen in prepare mode — switch Mode to prepare on "
                        "the Schedule page first.")

    argv = ["bash", paths.RUN_SCHEDULED_SH]
    env = {"JOBAPPLY_QUERY_COUNT": "1", "JOBAPPLY_MAX_APPLIES": "1", "JOBAPPLY_SKIP_SWEEPS": "1"}
    return _start(
        "test_run", "", TEST_RUN_TIMEOUT_S,
        canned_output=CANNED_TEST_RUN_LOG, argv=argv, env=env,
    )
