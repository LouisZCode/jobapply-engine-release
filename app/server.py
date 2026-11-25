#!/usr/bin/env python3
"""app/server.py — local setup web app for the job-application pipeline.

FastAPI app (served by uvicorn), bound to 127.0.0.1 only (see main() at the bottom —
never 0.0.0.0). No external network calls are made by this app itself; the few
subprocess calls it makes (render_templates.py, setup.sh, the Chrome launcher,
`claude -p`, launchctl, osascript) are the same commands documented in the repo's own
README/RUN.md, just triggered by a button instead of typed by hand. See app/lib/actions.py
for exactly what runs and app/lib/state.py for how the checklist statuses are computed.
The three background agent jobs (Documents-page answer bank + CV generation, and the
Profile page's "fill from my CV" extraction) are a further exception to "no side effects
beyond this app's own files" — see app/lib/jobs.py, which is to `claude -p` background
jobs what actions.py is to every other subprocess call.

Routes are grouped into six sections below (Home, Profile, Connections, Schedule,
Documents, Dashboard), each with a short comment banner — this file intentionally is NOT
split into an APIRouter per section: at this size a single file with clear sections is
easier to read than the indirection that split adds. Every route keeps its Flask-era path,
method, and handler function name — Starlette's Jinja2 `url_for` in the templates resolves
routes by function name, and route names default to the function name, so the templates
needed no route-reference changes, only the static-file helper (see layout.html).
"""
import csv
import os
import re
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

APP_DIR = os.path.dirname(os.path.abspath(__file__))
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, RedirectResponse  # noqa: E402
from starlette.concurrency import run_in_threadpool  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

from lib import actions, activity, jobs, llm, onboarding, paths, profile_io, state, usage  # noqa: E402
from lib import keys as key_store  # noqa: E402

# Auto-setup: the database used to need a manual "Initialize" button on the home page.
# It's now created silently the first time the app starts, if it isn't there yet — see
# lifespan() below. The result is only kept around so home_context() can surface a
# banner if it FAILED; on success nothing is shown to the user, just a stdout log line.
_startup_init_result = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _startup_init_result
    if not state.db_exists():
        result = actions.init_db()
        if result.get("ok"):
            print("[startup] created {} (first run).".format(paths.APPLICATIONS_DB))
        else:
            _startup_init_result = result
            print(
                "[startup] WARNING: automatic setup failed — {}".format(
                    (result.get("stderr") or result.get("message") or "unknown error").strip()
                )
            )
    yield


app = FastAPI(lifespan=lifespan)
# Local single-user app with no login system — no session/secret-key machinery is needed
# at all (Flask carried a per-process secret_key for its session/flash system, but this
# app never used flash() or the session; FastAPI/Starlette have no equivalent to opt into
# here, so it's simply gone).
templates = Jinja2Templates(directory=os.path.join(APP_DIR, "templates"))
# Cache-buster for static assets: the stylesheet's own mtime. Without this, browsers
# kept a stale cached style.css across app updates — new CSS classes (e.g. the ⓘ
# tooltips) silently never loaded and their hidden text rendered inline on the page.
templates.env.globals["static_version"] = int(
    os.stat(os.path.join(APP_DIR, "static", "style.css")).st_mtime
)
templates.env.globals["fmt_tokens"] = usage.fmt_tokens  # "1.2M" / "45k" in any template
app.mount("/static", StaticFiles(directory=os.path.join(APP_DIR, "static")), name="static")

# The 4-second poll endpoints would drown the activity log in identical lines — their
# meaningful transitions are logged as explicit events instead (linkedin_verified in
# state.py, job started/finished in jobs.py).
ACTIVITY_SKIP_PATHS = {"/jobs/status", "/prereqs", "/connections/linkedin/status"}


@app.middleware("http")
async def activity_log(request: Request, call_next):
    started = time.time()
    response = await call_next(request)
    path = request.url.path
    if not path.startswith("/static") and path not in ACTIVITY_SKIP_PATHS:
        activity.log_event(
            "request",
            method=request.method,
            path=path,
            status=response.status_code,
            ms=int((time.time() - started) * 1000),
        )
    return response


def render(request, name, ctx):
    """Every route funnels through this one helper. Starlette's Jinja2Templates signature is
    TemplateResponse(request, name, context) since 0.29; Starlette 1.x dropped the old
    (name, {"request": ...}) form — with it a fresh install died on its first page
    (ELECTRON-011 blocker, 2026-11-24). Jinja's url_for() reads the request from here."""
    return templates.TemplateResponse(request, name, ctx)


# ------------------------------------------------------------------------------
# POST -> redirect -> GET. Every form POST below ends in a 303 redirect to its page, with
# the one-time message it wants shown stashed in _FLASH until that page's GET picks it up.
# This used to render the page straight from the POST — and the pages' own "job finished,
# reload to show the result" JavaScript then re-submitted the POST on reload, so one click
# on "Let Claude fill my profile" / "Run a test now" started a second (and third...) run the
# moment the first finished (seen live 2026-08-19: three back-to-back test runs, and a
# 64-second CV read whose output was deleted by its own accidental restart). A GET page has
# nothing to re-submit. _FLASH is a plain in-process dict: this is a single-user local app
# served by one uvicorn process, and a message lost to a server restart costs nothing.
# ------------------------------------------------------------------------------

_FLASH = {}


def flash(page, **payload):
    _FLASH[page] = payload


def flash_pop(page):
    return _FLASH.pop(page, None) or {}


def redirect(page_or_route, anchor=""):
    """303 to a page — accepts either a PAGE_ROUTE_NAMES key ("profile") or a route name."""
    route_name = PAGE_ROUTE_NAMES.get(page_or_route, page_or_route)
    return RedirectResponse(url=url_path(route_name) + anchor, status_code=303)


# ------------------------------------------------------------------------------
# Shared context
# ------------------------------------------------------------------------------

def base_context(active):
    status, _st = state.compute_status()
    steps = [
        {
            "key": key,
            "label": state.STEP_LABELS[key],
            "done": status[key],
            "link": state.STEP_LINKS[key],
        }
        for key in state.STEP_ORDER
    ]
    return {
        "active": active,
        "status": status,
        "steps": steps,
        "steps_done": sum(1 for s in steps if s["done"]),
        "steps_total": len(steps),
    }


# ------------------------------------------------------------------------------
# HOME — three plain-language options: profile & CV, connect to Claude, connect to
# LinkedIn. Everything else (the database, the engine config files, the 8-step internal
# checklist state.py still computes) happens automatically or lives one click deeper, on
# the Advanced links at the bottom.
# ------------------------------------------------------------------------------

def home_context():
    ctx = base_context("home")
    status = ctx["status"]

    profile_done = status["profile_saved"]
    documents_done = status["answer_bank_created"] or status["cv_in_place"]
    if not profile_done:
        profile_cv_href, profile_cv_label = url_path("profile_page"), "Get started"
    elif not documents_done:
        profile_cv_href, profile_cv_label = url_path("documents_page"), "Continue"
    else:
        profile_cv_href, profile_cv_label = url_path("profile_page"), "Update"

    ctx["profile_cv_done"] = profile_done and documents_done
    ctx["profile_cv_href"] = profile_cv_href
    ctx["profile_cv_label"] = profile_cv_label
    ctx["claude_href"] = url_path("connections") + "#claude"
    ctx["claude_label"] = "Manage" if status["claude_connected"] else "Connect"
    ctx["linkedin_href"] = url_path("connections") + "#linkedin"
    ctx["linkedin_label"] = "Manage" if status["linkedin_confirmed"] else "Connect"
    ctx["chrome_installed"] = state.chrome_installed()
    ctx["readiness_items"], ctx["ready"] = _readiness_with_hrefs()
    ctx["keep_awake"] = actions.keep_awake_status()
    ctx["schedule_run_time"] = state.load_state().get("schedule_run_time", "05:01")
    # M3: one-glance heartbeat for the Home cards (full detail on Schedule).
    ctx["agents"] = actions.schedule_agent_state()
    ctx["health"] = actions.health_snapshot()
    ctx["job_status"] = jobs.status()
    ctx["claude_usage"] = state.claude_usage_hint()
    ctx["usage"] = usage.summary()
    flashed = flash_pop("home")
    ctx["keep_awake_result"] = flashed.get("keep_awake_result")
    ctx["test_run_banner"] = flashed.get("test_run_banner")
    ctx["stop_banner"] = flashed.get("stop_banner")

    # Only ever populated when the silent startup database setup failed — see
    # lifespan() above. None (the common case) shows nothing on the page.
    ctx["setup_warning"] = _startup_init_result
    return ctx


def url_path(route_name):
    """Plain "/path" for a route, without needing a Request (home_context has none)."""
    return app.url_path_for(route_name)


# state.readiness()'s "page" values reuse STEP_LINKS's short names (profile/documents/
# connections/home) — this is the one place that turns those into a real FastAPI route
# name, so home_context() and schedule_context()/schedule_save() below share it instead
# of each inventing their own translation.
PAGE_ROUTE_NAMES = {
    "home": "home",
    "profile": "profile_page",
    "documents": "documents_page",
    "connections": "connections",
    "schedule": "schedule_page",
    "dashboard": "dashboard_page",
    "feedback": "feedback_page",
}

# Pages that include templates/_job_panel.html (and therefore show a Stop-button flash).
JOB_PANEL_PAGES = ("home", "profile", "documents")


def _readiness_with_hrefs():
    items, ready = state.readiness()
    for item in items:
        item["href"] = url_path(PAGE_ROUTE_NAMES[item["page"]])
    return items, ready


@app.get("/health")
async def health():
    # ELECTRON-008: identity marker for the tower's port-reuse check. The tower
    # reuses an already-serving port ONLY if this route says the server is ours —
    # a foreign app on the port must never get embedded in the tower window.
    return {"status": "ok", "app": "jobapply"}


@app.get("/")
async def home(request: Request):
    # Fresh install: no profile yet, the door was never finished AND not dismissed —
    # start at the door (two prompts, ONBOARD-001 2026-11-25) instead of a mostly-empty
    # home. "Open Job Apply" / "Not now" set a flag and land here for good; Home's
    # readiness list then guides the rest (PRODUCT_INVENTORY §H: the setup app IS the
    # onboarding).
    if (
        not state.profile_exists()
        and not state.load_state().get("onboarding_finished")
        and not state.load_state().get("onboarding_dismissed")
    ):
        return redirect("onboarding_page")
    return render(request, "home.html", home_context())


@app.post("/power/keep-awake")
async def power_keep_awake(request: Request):
    """Home page's one-click keep-awake toggle. The action runs FIRST, then
    home_context() is built — so the "on/off" state and time shown right after
    submitting are read back fresh from pmset, not assumed from what was requested."""
    form = await request.form()
    turn = (form.get("turn") or "").strip()
    run_time = state.load_state().get("schedule_run_time", "05:01")
    result = actions.set_keep_awake(turn == "on", run_time)
    activity.log_event("action", name="keep_awake", ok=bool(result.get("ok")), turn=turn)
    flash("home", keep_awake_result=result)
    return redirect("home")


@app.post("/run/test")
async def run_test(request: Request):
    """Home page's "run a test now" button — one real, bounded rehearsal of the daily
    schedule (see jobs.start_test_run_job for exactly what that means and why it's safe
    to fire even while a real scheduled round could be running)."""
    ok, err = jobs.start_test_run_job()
    activity.log_event("action", name="test_run", ok=ok, message=(err or "")[:200])
    flash("home", test_run_banner={
        "ok": ok,
        "message": err or "Test run started. It finds one job and prepares one application "
                   "for review — nothing is submitted.",
    })
    return redirect("home")


@app.post("/jobs/stop")
async def jobs_stop(request: Request):
    """The Stop button on the shared job panel — ends whatever background job is running
    and sends the user back to the page they pressed it on (form field "back", one of the
    page route names; anything else falls back to Home)."""
    form = await request.form()
    back = (form.get("back") or "home").strip()
    # jobs.stop() may wait up to 5s for the process tree to go away — off the event loop,
    # so the other tab's 2-second status poll (and every other request) keeps flowing.
    ok, msg = await run_in_threadpool(jobs.stop)
    activity.log_event("action", name="job_stop", ok=ok, message=msg[:120])
    # Only the pages that carry the job panel (and read stop_banner) are valid return
    # targets; anything else lands on Home so the message is never silently lost.
    if back not in JOB_PANEL_PAGES:
        back = "home"
    flash(back, stop_banner={"ok": ok, "message": msg})
    return redirect(PAGE_ROUTE_NAMES[back])


# ------------------------------------------------------------------------------
# ONBOARDING — the door (ONBOARD-001, 2026-11-25): /onboarding is two prompts, nothing
# else — "Do you have these installed?" (verified live by onboarding.door_rows: yes →
# green "Okay", no → the official download link) and "Do you have a CV? Upload it now or
# skip." Everything else is guided from Home's readiness list. A fresh install (no
# profile.yaml, door never finished) gets Home redirected here once.
#
# The 9-step wizard it replaced (M2) keeps its routes below, reachable only at
# /onboarding/wizard and unlinked, until it is removed. All state lives in
# app_state.json's onboarding_* keys (see lib/onboarding.py) and all real writes go
# through the SAME functions the regular pages use (profile_io, actions, setup.sh) —
# onboarding is a UX layer, never a second write path.
# ------------------------------------------------------------------------------

_GMAIL_CONNECT_LOG = os.path.join(paths.JOBS_DIR, "gmail_connect.log")
_gmail_proc = {"proc": None}


def _wizard_ctx(step_key, **extra):
    draft = onboarding.load_draft()
    try:
        step_index = onboarding.STEP_KEYS.index(step_key)
    except ValueError:
        step_index = draft["step"]
        step_key = onboarding.STEP_KEYS[min(step_index, len(onboarding.STEP_KEYS) - 1)]
        step_index = onboarding.STEP_KEYS.index(step_key)
    ctx = {
        "request": None,
        "steps": onboarding.STEPS,
        "steps_total": len(onboarding.STEPS),
        "step_index": step_index,
        "step_title": dict((k, t) for k, t, _p in onboarding.STEPS)[step_key],
        "f": draft["fields"],
        "l": draft["lists"],
        "background": draft["background"],
        "cv_pdfs": state.cv_pdfs(),
    }
    ctx.update(extra)
    return ctx


def _render_wizard(request, template, ctx):
    ctx["request"] = request
    return templates.TemplateResponse(request, "onboarding/" + template, ctx)


def _wizard_draft():
    return onboarding.load_draft()


@app.get("/onboarding")
async def onboarding_page(request: Request):
    # The door. Entering it revokes a previous "Not now" dismissal.
    if state.load_state().get("onboarding_dismissed"):
        state.update_state(onboarding_dismissed=False)
    flashed = flash_pop("onboarding")
    return templates.TemplateResponse(request, "onboarding/door.html", {
        "rows": onboarding.door_rows(),
        "ai_key_names": onboarding.ai_key_names(),
        "cv_pdfs": state.cv_pdfs(),
        "cv_banner": flashed.get("cv_banner"),
        "cv_banner_ok": bool(flashed.get("cv_ok")),
    })


@app.post("/onboarding/answer")
async def onboarding_answer(request: Request):
    """Yes / No on an installed-check row. The next view re-runs the live check: found →
    green "Okay"; said yes but not found → says so; said no → shows the download link."""
    form = await request.form()
    key = (form.get("key") or "").strip()
    answer = (form.get("answer") or "").strip()
    if key not in {row["key"] for row in onboarding.door_rows()} or answer not in ("yes", "no"):
        return redirect("onboarding_page")
    answers = dict(state.load_state().get("onboarding_answers") or {})
    answers[key] = answer
    state.update_state(onboarding_answers=answers)
    activity.log_event("onboarding", door=key, answer=answer)
    return redirect("onboarding_page")


@app.post("/onboarding/open")
async def onboarding_open():
    """"Open Job Apply": the door is done for good — Home guides the rest."""
    state.update_state(onboarding_finished=True)
    activity.log_event("onboarding", event="door_opened")
    return redirect("home")


@app.get("/onboarding/wizard")
async def onboarding_wizard(request: Request):
    # Retired as the entry point 2026-11-25 (ONBOARD-001); unlinked, kept until removed.
    # Entering the wizard revokes a previous "Not now" dismissal.
    if state.load_state().get("onboarding_dismissed"):
        state.update_state(onboarding_dismissed=False)
    draft = _wizard_draft()
    # Allow explicit ?to= navigation only backwards or to the saved position (no skipping ahead).
    target = request.query_params.get("to")
    index = draft["step"]
    if target in onboarding.STEP_KEYS:
        target_index = onboarding.STEP_KEYS.index(target)
        if target_index <= index or draft["finished"]:
            index = target_index
    step_key = onboarding.STEP_KEYS[min(index, len(onboarding.STEP_KEYS) - 1)]

    extra = {}
    if step_key == "prereqs":
        extra["checks"] = onboarding.prereq_checks()
    elif step_key == "claude":
        st = state.load_state()
        extra.update({
            "claude_ok": bool(st.get("claude_connected")),
            "claude_cli": state.claude_cli_path() is not None,
            "probe_message": st.get("claude_probe_message", ""),
            "probe_at": st.get("claude_probe_checked_at", ""),
        })
    elif step_key == "search":
        extra.update({
            "roles_text": request.query_params.get("roles_text") or draft["fields"].get("_roles_text", ""),
            "roles_location": draft["fields"].get("_roles_location", ""),
            "cv_banner": flash_pop("onboarding").get("cv_banner"),
        })
    elif step_key == "connect":
        extra.update({
            "linkedin_ok": bool(state.load_state().get("linkedin_confirmed")),
            "gmail_ok": os.path.isfile(os.path.join(paths.ENGINE_DIR, "inbox_reader", "token.json")),
            "gmail_needs_credentials": not os.path.isfile(os.path.join(paths.ENGINE_DIR, "inbox_reader", "credentials.json")),
        })
    elif step_key == "finish":
        st = state.load_state()
        items, _ready = state.readiness()
        extra.update({
            "finished": draft["finished"] or state.profile_exists(),
            "assemble_log": st.get("onboarding_assemble_log", ""),
            "schedule_log": st.get("onboarding_schedule_log", ""),
            "readiness": items,
            "run_time": st.get("schedule_run_time", "05:01"),
        })
    return _render_wizard(request, step_key + ".html", _wizard_ctx(step_key, **extra))


@app.get("/onboarding/dismiss")
async def onboarding_dismiss():
    """"Not now — open the app": remember the dismissal so Home stops
    redirecting back here (fresh installs would otherwise bounce straight
    into the wizard again — the exact loop that made the link feel dead).
    Reopening the wizard from the sidebar/menu revokes it (onboarding_page)."""
    state.update_state(onboarding_dismissed=True)
    activity.log_event("onboarding", event="dismissed")
    return redirect("home")


@app.post("/onboarding/step")
async def onboarding_step(request: Request):
    form = await request.form()
    current = (form.get("step") or "").strip()
    if current not in onboarding.STEP_KEYS:
        return redirect("onboarding_wizard")
    index = onboarding.STEP_KEYS.index(current)

    draft = _wizard_draft()
    fields, lists = onboarding.absorb_form(form, draft)
    background = draft["background"]
    if current == "story":
        background = (form.get("background") or "").strip()

    next_index = min(index + 1, len(onboarding.STEPS) - 1)
    onboarding.save_draft(step=next_index, fields=fields, lists=lists, background=background)
    activity.log_event("onboarding", step=current, event="advanced")
    if onboarding.STEP_KEYS[next_index] == "finish" and state.profile_exists():
        # Returning to a finished setup — land on the finished view directly.
        pass
    return redirect("onboarding_wizard")


@app.post("/onboarding/prereqs/recheck")
async def onboarding_recheck(request: Request):
    # Shared by the door ("Re-check" after installing) and the retired wizard's prereq
    # page; both checks are live, so a reload is all a re-check needs.
    return redirect("onboarding_page")


@app.post("/onboarding/claude/probe")
async def onboarding_claude_probe(request: Request):
    result = actions.probe_claude()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    msg = (result.get("stdout") or result.get("stderr") or result.get("message") or "").strip()
    state.update_state(claude_connected=bool(result["ok"]), claude_probe_message=msg, claude_probe_checked_at=now)
    activity.log_event("onboarding", step="claude", event="probe", ok=bool(result["ok"]))
    return redirect("onboarding_wizard")


@app.post("/onboarding/claude/terminal")
async def onboarding_claude_terminal(request: Request):
    actions.open_terminal_for_claude_login()
    return redirect("onboarding_wizard")


@app.post("/onboarding/search/generate")
async def onboarding_search_generate(request: Request):
    form = await request.form()
    draft = _wizard_draft()
    fields, _lists = onboarding.absorb_form(form, draft, list_fields=())
    roles_text = (form.get("roles_text") or "").strip()
    location = (form.get("roles_location") or "").strip()
    remote_pref = draft["fields"].get("remote_preference", "remote")
    rows = onboarding.starter_queries(roles_text, location, remote_pref)
    if rows:
        lists = dict(draft["lists"])
        lists["sq_query"] = [r["query"] for r in rows]
        lists["sq_location"] = [r["location"] for r in rows]
        lists["sq_filters"] = [r["filters"] for r in rows]
        lists["sq_resume"] = [""] * len(rows)
        fields["_roles_text"] = roles_text
        fields["_roles_location"] = location
        onboarding.save_draft(fields=fields, lists=lists)
        activity.log_event("onboarding", step="search", event="generated", rows=len(rows))
    return redirect("onboarding_wizard")


@app.post("/onboarding/cv")
async def onboarding_cv_upload(request: Request):
    form = await request.form()
    upload = await _save_cv_upload(form.get("cv_pdf"))
    activity.log_event("onboarding", step="search", event="cv_upload", ok=upload["ok"])
    flash("onboarding", cv_banner=upload["message"], cv_ok=upload["ok"])
    return redirect("onboarding_wizard")


@app.post("/onboarding/story/from-cv")
async def onboarding_story_from_cv(request: Request):
    form = await request.form()
    filename = (form.get("cv_filename") or "").strip()
    if filename not in state.cv_pdfs():
        flash("onboarding", cv_banner="That file isn't in the CV pool anymore — refresh and try again.")
        return redirect("onboarding_wizard")
    try:
        from pypdf import PdfReader
        pdf_path = os.path.join(paths.CV_DATA_DIR, filename)
        text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    except Exception:
        text = ""
    draft = _wizard_draft()
    if len(text.strip()) < 200:
        flash("onboarding", cv_banner="Couldn't read enough text from that PDF — paste your background by hand.")
    else:
        combined = (draft["background"] + "\n\n" + text) if draft["background"].strip() else text
        onboarding.save_draft(background=combined)
        flash("onboarding", cv_banner="Added text from {} — read it through below.".format(filename))
    activity.log_event("onboarding", step="story", event="from_cv", filename=filename)
    return redirect("onboarding_wizard")


@app.post("/onboarding/linkedin/launch")
async def onboarding_linkedin_launch(request: Request):
    result = actions.launch_linkedin_chrome()
    activity.log_event("onboarding", step="connect", event="linkedin_launch", ok=bool(result.get("ok")))
    return redirect("onboarding_wizard")


@app.post("/onboarding/gmail/credentials")
async def onboarding_gmail_credentials(request: Request):
    form = await request.form()
    upload = form.get("credentials")
    filename = getattr(upload, "filename", None) if upload is not None else None
    inbox = os.path.join(paths.ENGINE_DIR, "inbox_reader")
    if not filename:
        flash("onboarding", cv_banner="No file selected.")
    elif not filename.lower().endswith(".json"):
        flash("onboarding", cv_banner="That doesn't look like the key file — it should be credentials.json.")
    else:
        data = await upload.read()
        import json as _json
        try:
            parsed = _json.loads(data)
            if "installed" not in parsed and "web" not in parsed:
                raise ValueError("not an OAuth client file")
        except ValueError:
            flash("onboarding", cv_banner="That file isn't a valid Google OAuth client key.")
        else:
            os.makedirs(inbox, exist_ok=True)
            with open(os.path.join(inbox, "credentials.json"), "wb") as fh:
                fh.write(data)
            activity.log_event("onboarding", step="connect", event="gmail_credentials_added")
            flash("onboarding", cv_banner="Key file added — you can connect Gmail now.")
    return redirect("onboarding_wizard")


@app.post("/onboarding/gmail/connect")
async def onboarding_gmail_connect(request: Request):
    if _gmail_proc["proc"] is not None and _gmail_proc["proc"].poll() is None:
        return redirect("onboarding_wizard")
    os.makedirs(paths.JOBS_DIR, exist_ok=True)
    fh = open(_GMAIL_CONNECT_LOG, "w", encoding="utf-8")
    import subprocess as _sp
    import sys as _sys
    venv_py = os.path.join(paths.APP_DIR, ".venv", "bin", "python3")
    _gmail_proc["proc"] = _sp.Popen(
        [venv_py if os.path.exists(venv_py) else _sys.executable,
         os.path.join(paths.APP_DIR, "lib", "gmail_connect.py")],
        cwd=paths.REPO_ROOT, stdin=_sp.DEVNULL, stdout=fh, stderr=_sp.STDOUT,
    )
    fh.close()
    activity.log_event("onboarding", step="connect", event="gmail_connect_started")
    return redirect("onboarding_wizard")


@app.get("/onboarding/gmail/status")
async def onboarding_gmail_status():
    proc = _gmail_proc["proc"]
    running = bool(proc is not None and proc.poll() is None)
    tail = ""
    if os.path.isfile(_GMAIL_CONNECT_LOG):
        try:
            with open(_GMAIL_CONNECT_LOG, encoding="utf-8", errors="replace") as fh:
                tail = "".join(fh.readlines()[-8:])
        except OSError:
            pass
    connected = os.path.isfile(os.path.join(paths.ENGINE_DIR, "inbox_reader", "token.json"))
    return JSONResponse({"running": running, "connected": connected, "log": tail})


@app.post("/onboarding/finish")
async def onboarding_finish(request: Request):
    form = await request.form()
    draft = _wizard_draft()
    hh_mm = (form.get("run_time") or "").strip()
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", hh_mm):
        hh_mm = state.load_state().get("schedule_run_time", "05:01")

    errors, log_lines = onboarding.assemble(draft, hh_mm)
    schedule_log = ""
    if not errors:
        render_res = actions.render_schedule(hh_mm)
        install_res = actions.install_schedule()
        ok = render_res.get("ok") and install_res.get("ok")
        lines = ["$ bash setup/setup.sh --schedule " + hh_mm,
                 (render_res.get("stdout") or "").rstrip(), (render_res.get("stderr") or "").rstrip(),
                 "$ (install launch agents)",
                 (install_res.get("stdout") or install_res.get("message") or "").rstrip(),
                 (install_res.get("stderr") or "").rstrip()]
        schedule_log = "\n".join(l for l in lines if l)
        state.update_state(
            schedule_run_time=hh_mm, schedule_mode="prepare", schedule_installed=ok,
            onboarding_schedule_log=schedule_log,
        )
        onboarding.save_draft(step=len(onboarding.STEPS) - 1)
        activity.log_event("onboarding", step="finish", event="finished", schedule_ok=ok)
    else:
        state.update_state(onboarding_assemble_log="\n".join(log_lines))
        onboarding.save_draft(step=onboarding.STEP_KEYS.index("search"))
        activity.log_event("onboarding", step="finish", event="assemble_failed", errors=len(errors))

    return redirect("onboarding_wizard")


# ------------------------------------------------------------------------------
# CV UPLOAD — shared by the Profile page ("fill from my CV") and the Documents page
# (the plain CV-pool upload). One validated save path into claude_applicator/data/,
# reused rather than duplicated: extension + %PDF- magic-byte + 15MB cap, numeric-suffix
# no-clobber naming. The Documents-page route below (documents_cv_upload) is the other
# caller.
# ------------------------------------------------------------------------------

MAX_CV_BYTES = 15 * 1024 * 1024
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_pdf_filename(name):
    base = os.path.basename(name or "").strip()
    base = _SAFE_FILENAME_RE.sub("_", base)
    base = base.strip("._") or "resume"
    if not base.lower().endswith(".pdf"):
        base += ".pdf"
    return base


def _unique_path(dirpath, filename):
    """Never clobbers an existing file with the same sanitized name — appends -2, -3, ..."""
    stem, ext = os.path.splitext(filename)
    candidate = filename
    n = 2
    while os.path.exists(os.path.join(dirpath, candidate)):
        candidate = "{}-{}{}".format(stem, n, ext)
        n += 1
    return os.path.join(dirpath, candidate)


async def _save_cv_upload(cv_pdf):
    # cv_pdf is a starlette.datastructures.UploadFile when a file was actually attached,
    # or None/empty-string when the field was left blank — duck-typed on .filename rather
    # than isinstance-checked so either shape is handled the same way Flask's
    # FileStorage-or-None used to be.
    filename = getattr(cv_pdf, "filename", None) if cv_pdf is not None else None
    if not filename:
        return {"ok": False, "message": "No file selected.", "path": None}
    if not filename.lower().endswith(".pdf"):
        return {"ok": False, "message": "Only .pdf files are accepted.", "path": None}
    data = await cv_pdf.read()
    if len(data) > MAX_CV_BYTES:
        return {"ok": False, "message": "File is larger than 15MB — pick a smaller PDF.", "path": None}
    if not data.startswith(b"%PDF-"):
        return {"ok": False, "message": "That file doesn't look like a real PDF (no %PDF header).", "path": None}
    os.makedirs(paths.CV_DATA_DIR, exist_ok=True)
    dest = _unique_path(paths.CV_DATA_DIR, _sanitize_pdf_filename(filename))
    with open(dest, "wb") as fh:
        fh.write(data)
    _adopt_default_resume(os.path.basename(dest))
    return {"ok": True, "message": "Uploaded {}.".format(os.path.basename(dest)), "path": dest}


def _adopt_default_resume(filename):
    """A saved profile that has no usable default resume yet takes the CV that was just
    uploaded — so "upload a CV" is all a user has to do for the run to have one. A
    profile that already names an existing PDF is left alone."""
    profile = profile_io.load_profile()
    if profile is None:
        return
    current = (profile.get("resume_file") or "").strip()
    if current and "CHANGEME" not in current and os.path.isfile(os.path.join(paths.ENGINE_DIR, current)):
        return
    resume_path = "data/{}".format(filename)
    profile["resume_file"] = resume_path
    for q in (profile.get("job_search") or {}).get("search_queries") or []:
        if not (q.get("resume") or "").strip() or "CHANGEME" in (q.get("resume") or ""):
            q["resume"] = resume_path
    profile_io.save_profile_yaml(profile)
    activity.log_event("action", name="default_resume_adopted", ok=True, filename=filename)


# ------------------------------------------------------------------------------
# PROFILE — the profile wizard, plus "fill my profile from my CV" (cv_extract job).
#
# profile_draft (app_state.json) holds a form_from_cv_extract()-shaped dict once a
# cv_extract job finishes — see _harvest_cv_extract_if_ready() below, called on every
# GET /profile so a reload right after the job completes (the polling JS in profile.html
# does exactly that) picks it up. A saved profile.yaml value always wins over a draft one
# (profile_io.merge_profile_draft); the draft itself is cleared on a successful save.
# ------------------------------------------------------------------------------

def _claude_ready():
    """Same bar the cv_extract job start actually needs to clear: a probed-and-connected
    Claude AND a real `claude` binary on PATH — either missing means pre-fill isn't
    offered (the upload itself still works either way, see profile_cv_extract)."""
    return bool(state.load_state().get("claude_connected")) and state.claude_cli_path() is not None


def _harvest_cv_extract_if_ready():
    """Called at the top of every GET /profile. Resolves a finished cv_extract job (if
    any) into app_state.json's profile_draft the first time it's seen done — the
    persisted job's own "harvested" flag guards against re-harvesting (and re-deleting an
    already-gone output file) on a later reload. A job that failed to run, or one that
    "succeeded" but left no readable output YAML, is a clear failure surfaced through the
    job's own error field — never a stored draft."""
    jobs.status()  # resolve running -> done/timeout as a side effect, same as any other poll
    job = jobs.current_job()
    if not job or job.get("name") != "cv_extract" or job.get("status") == "running":
        return
    if job.get("harvested"):
        return

    if not job.get("ok") or not os.path.isfile(paths.CV_EXTRACT_OUTPUT_YAML):
        tail = jobs.read_log_full(job.get("log_path")) or ""
        last_line = next((ln.strip() for ln in reversed(tail.splitlines()) if ln.strip()), "")
        reason = last_line or "the extraction job did not produce an output file."
        # The agent's final text is shown to the user on the page (it's their own CV being
        # discussed) but never written to the activity log — the prompt asks for a bare
        # DONE/FAILED line, nothing enforces it, and a chatty reply could quote the CV.
        activity.log_event(
            "job", name="cv_extract", event="harvest_failed",
            reason="no_output" if not last_line else ("failed_line" if last_line.upper().startswith("FAILED") else "unexpected_reply"),
        )
        state.update_state(job=dict(job, status="done", ok=False, harvested=True, harvest_error=reason))
        return

    try:
        filtered = profile_io.parse_cv_extract_yaml(paths.CV_EXTRACT_OUTPUT_YAML)
        draft = profile_io.form_from_cv_extract(filtered)
    except ValueError as exc:
        os.remove(paths.CV_EXTRACT_OUTPUT_YAML)
        activity.log_event("job", name="cv_extract", event="harvest_failed", reason="bad_yaml")
        state.update_state(job=dict(
            job, status="done", ok=False, harvested=True,
            harvest_error="Could not read the extracted data: {}".format(exc),
        ))
        return

    os.remove(paths.CV_EXTRACT_OUTPUT_YAML)
    activity.log_event(
        "job", name="cv_extract", event="draft_stored",
        fields=sum(1 for v in draft.values() if v),
    )
    state.update_state(
        profile_draft=draft,
        profile_draft_source=job.get("source_filename") or "your CV",
        job=dict(job, harvested=True),
    )


def profile_context(form, errors, render_result, saved, cv_extract_banner=None, cv_selected=None,
                    stop_banner=None):
    ctx = base_context("profile")
    ctx["form"] = form
    ctx["errors"] = errors
    ctx["render_result"] = render_result
    ctx["saved"] = saved
    ctx["edit_mode"] = profile_io.load_profile() is not None
    st = state.load_state()
    ctx["profile_draft"] = st.get("profile_draft")
    ctx["profile_draft_source"] = st.get("profile_draft_source") or ""
    ctx["claude_ready"] = _claude_ready()
    ctx["brains"] = llm.brains()
    ctx["llm_models"] = state.load_state().get("llm_models") or {}
    ctx["llm_defaults"] = llm.DEFAULT_MODELS
    ctx["job_status"] = jobs.status()
    ctx["cv_extract_banner"] = cv_extract_banner
    ctx["stop_banner"] = stop_banner
    ctx["cv_pool"] = state.cv_pdfs()
    ctx["cv_selected"] = cv_selected or (ctx["cv_pool"][-1] if ctx["cv_pool"] else None)
    return ctx


@app.get("/profile")
async def profile_page(request: Request):
    jobs.poll_api_jobs()
    _harvest_cv_extract_if_ready()
    flashed = flash_pop("profile")
    # A save that failed validation re-renders straight from the POST (the typed values
    # must survive); everything else arrives here by redirect with its message flashed.
    return render(request, "profile.html", profile_context(
        _profile_form_with_draft(), [],
        flashed.get("render_result"), bool(flashed.get("saved")),
        cv_extract_banner=flashed.get("cv_extract_banner"),
        cv_selected=flashed.get("cv_selected"),
        stop_banner=flashed.get("stop_banner"),
    ))


@app.post("/profile")
async def profile_save(request: Request):
    form = await request.form()
    errors = profile_io.validate(form)
    existing = profile_io.load_profile()
    attempted = profile_io.build_profile(form, existing=existing, cv_pool=state.cv_pdfs())

    if errors:
        activity.log_event("action", name="profile_save", ok=False, validation_errors=len(errors))
        prefill = profile_io.form_from_profile(attempted)
        return render(request, "profile.html", profile_context(prefill, errors, None, False))

    profile_io.save_profile_yaml(attempted)
    render_result = actions.render_profile()
    activity.log_event("action", name="profile_save", ok=bool(render_result["ok"]))
    # Nothing is stored to the profile until a save succeeds — this is that moment, so any
    # pending pre-fill draft has now either been accepted (copied into the saved fields
    # the human just submitted) or deliberately left out; either way it's stale now.
    state.update_state(profile_draft=None, profile_draft_source="")
    flash("profile", render_result=render_result, saved=render_result["ok"])
    return redirect("profile")


def _profile_form_with_draft():
    form_data = profile_io.form_from_profile(profile_io.load_profile())
    return profile_io.merge_profile_draft(form_data, state.load_state().get("profile_draft"))


@app.post("/profile/cv-upload")
async def profile_cv_upload(request: Request):
    """Plain upload into the CV pool — starting the Claude pre-fill is a separate,
    deliberate button (profile_cv_prefill below), per user feedback: one action each."""
    form = await request.form()
    upload = await _save_cv_upload(form.get("cv_pdf"))
    activity.log_event("action", name="cv_upload", ok=upload["ok"], message=upload["message"][:200])
    banner = {"ok": upload["ok"], "message": upload["message"]}
    selected = os.path.basename(upload["path"]) if upload.get("path") else None
    flash("profile", cv_extract_banner=banner, cv_selected=selected)
    return redirect("profile")


@app.post("/profile/cv-prefill")
async def profile_cv_prefill(request: Request):
    form = await request.form()
    name = (form.get("cv_filename") or "").strip()
    # basename only, and only a PDF that really sits in the pool — the filename comes
    # from a <select> in the happy path, but a hand-crafted POST must not reach outside
    # claude_applicator/data/.
    bad_name = not name or "/" in name or "\\" in name or ".." in name or not name.lower().endswith(".pdf")
    pdf_path = None if bad_name else os.path.join(paths.CV_DATA_DIR, name)
    brain = ((form.get("brain") or "claude")).strip()
    model = ((form.get("model") or "")).strip()
    brain_ids = [b["id"] for b in llm.brains()]

    if bad_name or not os.path.isfile(pdf_path):
        activity.log_event("action", name="cv_prefill", ok=False, message="invalid or missing filename")
        banner = {"ok": False, "message": "Pick one of the CVs in the list."}
    elif brain not in brain_ids:
        # The disabled button is not a gate — re-check server-side.
        activity.log_event("action", name="cv_prefill", ok=False, message="no brain connected")
        banner = {"ok": False, "message": "Pre-fill needs an AI — connect one or add an API key on the Connections page first."}
    else:
        ok, err = jobs.start_cv_extract_job(pdf_path, brain=brain, model=model)
        activity.log_event("action", name="cv_prefill", ok=ok, brain=brain, message=(err or "")[:200])
        # No "started" banner on success — the job panel itself is the feedback.
        banner = None if ok else {"ok": False, "message": "Pre-fill didn't start: {}".format(err)}

    flash("profile", cv_extract_banner=banner, cv_selected=name or None)
    return redirect("profile")


# ------------------------------------------------------------------------------
# CONNECTIONS — LinkedIn (Chrome/CDP) + Claude, no credentials ever touched
# ------------------------------------------------------------------------------

def connections_context(action_name=None, action_result=None):
    ctx = base_context("connections")
    _status, st = state.compute_status()
    ctx["claude_cli"] = state.claude_cli_path()
    ctx["chrome_installed"] = state.chrome_installed()
    ctx["claude_probe_message"] = st.get("claude_probe_message", "")
    ctx["claude_probe_checked_at"] = st.get("claude_probe_checked_at", "")
    ctx["linkedin_verified_at"] = st.get("linkedin_verified_at", "")
    ctx["test_mode"] = actions.test_mode()
    ctx["action_name"] = action_name
    ctx["action_result"] = action_result
    # Intelligence: provider API keys (Zai / Codex / OpenRouter) — hints only.
    ctx["key_providers"] = key_store.PROVIDERS
    ctx["key_hints"] = key_store.hints()
    ctx["key_tests"] = state.load_state().get("key_test_results") or {}
    return ctx


@app.get("/connections")
async def connections(request: Request):
    flashed = flash_pop("connections")
    return render(request, "connections.html",
                  connections_context(flashed.get("action_name"), flashed.get("action_result")))


@app.post("/connections/linkedin/launch")
async def connections_linkedin_launch(request: Request):
    result = actions.launch_linkedin_chrome()
    activity.log_event(
        "action", name="linkedin_launch", ok=bool(result.get("ok")),
        message=(result.get("message") or result.get("stderr") or "")[:200],
    )
    flash("connections", action_name="linkedin_launch", action_result=result)
    return redirect("connections", "#linkedin")


@app.get("/connections/linkedin/status")
async def linkedin_status():
    login_status = state.linkedin_login_status()
    confirmed = bool(state.load_state().get("linkedin_confirmed"))
    return JSONResponse({"status": login_status, "confirmed": confirmed})


@app.post("/connections/claude/probe")
async def connections_claude_probe(request: Request):
    result = actions.probe_claude()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    msg = (result.get("stdout") or "").strip() or (result.get("stderr") or "").strip() or result.get("message", "")
    state.update_state(
        claude_connected=bool(result["ok"]),
        claude_probe_message=msg,
        claude_probe_checked_at=now,
    )
    activity.log_event("action", name="claude_probe", ok=bool(result["ok"]), message=msg[:200])
    flash("connections", action_name="claude_probe", action_result=result)
    return redirect("connections", "#claude")


@app.post("/connections/claude/open-terminal")
async def connections_claude_terminal(request: Request):
    result = actions.open_terminal_for_claude_login()
    flash("connections", action_name="claude_terminal", action_result=result)
    return redirect("connections", "#claude")


# --- Intelligence: API keys (Zai / Codex / OpenRouter) -------------------------

@app.post("/connections/keys/save")
async def connections_keys_save(request: Request):
    form = await request.form()
    provider = (form.get("provider") or "").strip()
    previous = key_store.get_entry(provider)  # for rollback if the test fails
    ok, payload = key_store.save_key(provider, form.get("key"))
    result = {"ok": bool(ok)}
    if ok:
        # Save = validate = persist ONLY if the provider accepts the key (Luis,
        # 2026-11-08: "if failed, why saved?"). A rejected key is rolled back to
        # whatever was stored before (possibly nothing) — the store only ever holds
        # keys that passed their test.
        test = await run_in_threadpool(key_store.test_key, provider)
        if provider in key_store.PROVIDERS:
            results = state.load_state().get("key_test_results") or {}
            results[provider] = {"ok": bool(test["ok"]),
                                 "at": datetime.now().strftime("%Y-%m-%d %H:%M")}
            state.update_state(key_test_results=results)
        activity.log_event("action", name="key_test", ok=bool(test["ok"]), provider=provider)
        if test["ok"]:
            result["message"] = "{} Key saved — it rides into the engine when the app " \
                                "starts a run (restart the tower if it's already running).".format(test["message"])
        else:
            key_store.restore_entry(provider, previous)
            result = {"ok": False,
                      "message": "{} Nothing was saved{}.".format(
                          test["message"],
                          "; your previous key is still in place" if previous else "")}
        # one key_save event carrying the FINAL outcome (persisted or rolled back)
        # — never the intermediate mechanical write (CM review, 2026-11-08).
        activity.log_event("action", name="key_save", ok=bool(result["ok"]), provider=provider,
                           tested=bool(test["ok"]))
    else:
        result["message"] = payload if isinstance(payload, str) else "Could not save that key."
        activity.log_event("action", name="key_save", ok=False, provider=provider)
    flash("connections", action_name="key_save", action_result=result)
    return redirect("connections", "#intelligence")


@app.post("/connections/keys/remove")
async def connections_keys_remove(request: Request):
    form = await request.form()
    provider = (form.get("provider") or "").strip()
    key_store.remove_key(provider)
    activity.log_event("action", name="key_remove", ok=True, provider=provider)
    flash("connections", action_name="key_remove", action_result={"ok": True, "message": "Key removed."})
    return redirect("connections", "#intelligence")


# ------------------------------------------------------------------------------
# SCHEDULE — run_config.txt + launchd
# ------------------------------------------------------------------------------

def schedule_context(run_time=None, max_applies=None, mode=None, result=None, toggle_result=None):
    ctx = base_context("schedule")
    _status, st = state.compute_status()
    ctx["run_time"] = run_time if run_time is not None else st.get("schedule_run_time", "05:01")
    ctx["max_applies"] = max_applies if max_applies is not None else st.get("schedule_max_applies", 3)
    ctx["mode"] = mode if mode is not None else st.get("schedule_mode", "prepare")
    ctx["installed_plists"] = state.installed_launch_agent_plists()
    ctx["test_mode"] = actions.test_mode()
    ctx["readiness_items"], ctx["ready"] = _readiness_with_hrefs()
    ctx["result"] = result
    # M3: live agent state (install + load) + read-only heartbeat, same source
    # of truth check_health.sh uses (see actions.health_snapshot).
    ctx["agents"] = actions.schedule_agent_state()
    ctx["health"] = actions.health_snapshot()
    ctx["toggle_result"] = toggle_result
    return ctx


@app.get("/schedule")
async def schedule_page(request: Request):
    flashed = flash_pop("schedule")
    return render(request, "schedule.html", schedule_context(
        flashed.get("run_time"), flashed.get("max_applies"), flashed.get("mode"), flashed.get("result"),
        flashed.get("toggle_result"),
    ))


@app.post("/schedule/toggle")
async def schedule_toggle(request: Request):
    """M3 pause/resume: bootstrap/bootout the daily-run agent WITHOUT uninstalling
    it (same launchd verbs, same path-collision guard as install). The health
    watchdog is deliberately left alone — it keeps watching while paused."""
    form = await request.form()
    enabled = (form.get("turn") or "").strip().lower() == "on"
    result = actions.schedule_toggle(enabled)
    activity.log_event("action", name="schedule_toggle", ok=bool(result.get("ok")), turn="on" if enabled else "off")
    flash("schedule", toggle_result=result)
    return redirect("schedule_page")


@app.post("/schedule")
async def schedule_save(request: Request):
    form = await request.form()
    hh_mm = (form.get("run_time") or "").strip()
    max_applies_raw = (form.get("max_applies") or "").strip()
    mode = (form.get("mode") or "prepare").strip()

    errors = []
    # A disabled button on the page is not a gate by itself (a direct POST would still
    # reach this route) — re-check readiness server-side and refuse to install for real
    # if anything's still missing. Same list the page shows next to the disabled button.
    _readiness_items, ready = state.readiness()
    if not ready:
        errors.append(
            "The schedule needs everything in place before it can run for real — see "
            "the checklist below."
        )
    if not re.match(r"^([01]\d|2[0-3]):[0-5]\d$", hh_mm):
        errors.append("Run time must be 24-hour HH:MM, e.g. 05:01.")
    if mode not in ("prepare", "live"):
        errors.append("Mode must be either prepare or live.")
    try:
        max_applies = int(max_applies_raw)
        if max_applies < 1:
            errors.append("Max applies per run must be at least 1.")
    except ValueError:
        errors.append("Max applies per run must be a whole number.")
        max_applies = 3

    if errors:
        activity.log_event(
            "action", name="schedule_save", ok=False,
            message="; ".join(errors)[:300], refused_not_ready=not ready,
        )
        flash("schedule", run_time=hh_mm, max_applies=max_applies, mode=mode,
              result={"ok": False, "errors": errors, "log": ""})
        return redirect("schedule")

    log_lines = []

    updated = actions.update_run_config(mode, max_applies)
    log_lines.append(
        "$ (edited run_config.txt) MODE={}, MAX_APPLIES_PER_RUN={}  [{}]".format(
            mode, max_applies, "ok" if updated else "WARNING: a key was not found in run_config.txt"
        )
    )

    render_res = actions.render_schedule(hh_mm)
    log_lines.append("$ bash setup/setup.sh --schedule {}".format(hh_mm))
    log_lines.append(render_res["stdout"].rstrip())
    if render_res["stderr"]:
        log_lines.append(render_res["stderr"].rstrip())

    install_res = actions.install_schedule()
    log_lines.append("$ (install launch agents)")
    log_lines.append((install_res.get("stdout") or install_res.get("message") or "").rstrip())
    if install_res.get("stderr"):
        log_lines.append(install_res["stderr"].rstrip())

    ok = render_res["ok"] and install_res["ok"]
    activity.log_event("action", name="schedule_save", ok=ok, mode=mode, run_time=hh_mm)
    state.update_state(
        schedule_run_time=hh_mm,
        schedule_max_applies=max_applies,
        schedule_mode=mode,
        schedule_installed=ok,
    )

    result = {"ok": ok, "errors": [], "log": "\n".join(l for l in log_lines if l is not None)}
    flash("schedule", run_time=hh_mm, max_applies=max_applies, mode=mode, result=result)
    return redirect("schedule")


# ------------------------------------------------------------------------------
# DOCUMENTS — answer bank (qa_bank.md) + CV, both built by background `claude -p` jobs.
# See app/lib/jobs.py for the job runner itself (one job at a time; APP_TEST=1 stubs the
# real spawn); this section builds context/banners. CV upload validation itself
# (_save_cv_upload and friends) lives just above the PROFILE section now — it's shared
# with that page's "fill from my CV" upload.
# ------------------------------------------------------------------------------

def documents_context(banner=None, show_home_link=False, stop_banner=None):
    ctx = base_context("documents")
    ctx["stop_banner"] = stop_banner
    st = state.load_state()
    ctx["raw_background"] = st.get("raw_background", "")
    profile = profile_io.load_profile()
    ctx["profile_ready"] = profile is not None
    ctx["default_resume"] = ((profile or {}).get("resume_file") or "").strip()

    qa_bank_exists = os.path.isfile(paths.QA_BANK_MD)
    saved_content = ""
    if qa_bank_exists:
        with open(paths.QA_BANK_MD, encoding="utf-8") as fh:
            saved_content = fh.read()

    job_status = jobs.status()
    job = jobs.current_job()
    editor_initial = saved_content
    if job and job.get("name") == "answer_bank" and job.get("status") == "done" and job.get("ok"):
        # A completed regeneration supersedes the saved file in the editor — the file on
        # disk itself is untouched until "Save answer bank" is pressed (see the route).
        editor_initial = jobs.read_log_full(job.get("log_path")) or saved_content

    ctx["qa_bank_exists"] = qa_bank_exists
    ctx["qa_bank_editor_initial"] = editor_initial
    ctx["skill_answer_bank_exists"] = os.path.isfile(paths.SKILL_ANSWER_BANK)
    ctx["skill_rendered"] = os.path.isfile(paths.SKILL_MD)
    ctx["cv_pdfs"] = state.cv_pdfs()
    ctx["job_status"] = job_status
    ctx["banner"] = banner
    ctx["show_home_link"] = show_home_link
    return ctx


@app.get("/documents")
async def documents_page(request: Request):
    flashed = flash_pop("documents")
    return render(request, "documents.html", documents_context(
        banner=flashed.get("banner"), show_home_link=bool(flashed.get("show_home_link")),
        stop_banner=flashed.get("stop_banner"),
    ))


def _documents_redirect(banner, show_home_link=False):
    flash("documents", banner=banner, show_home_link=show_home_link)
    return redirect("documents")


@app.post("/documents/background")
async def documents_save_background(request: Request):
    form = await request.form()
    state.update_state(raw_background=form.get("raw_background", ""))
    return _documents_redirect({"ok": True, "message": "Draft text saved."})


@app.post("/documents/background/from-cv")
async def documents_background_from_cv(request: Request):
    """Seeds the background textarea from a CV already in claude_applicator/data/ —
    pypdf text extraction, appended (never overwriting) whatever the user already typed.
    pypdf is imported lazily so the app still boots if the package isn't installed yet."""
    form = await request.form()
    filename = (form.get("cv_filename") or "").strip()

    if filename not in state.cv_pdfs():
        activity.log_event("action", name="background_from_cv", ok=False, message="invalid or missing filename")
        return _documents_redirect({"ok": False, "message": "That file isn't in the CV pool anymore — refresh and try again."})

    try:
        from pypdf import PdfReader
    except ImportError:
        activity.log_event("action", name="background_from_cv", ok=False, message="pypdf not installed")
        return _documents_redirect({"ok": False, "message": "PDF text extraction isn't available — restart the app "
                                    "once to install it, or paste your background by hand."})

    pdf_path = os.path.join(paths.CV_DATA_DIR, filename)
    try:
        text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    except Exception:
        activity.log_event("action", name="background_from_cv", ok=False, message="extraction raised an exception")
        return _documents_redirect({"ok": False, "message": "Couldn't read text from that PDF — paste your background by hand instead."})

    if len(text.strip()) < 200:
        activity.log_event("action", name="background_from_cv", ok=False, message="extracted text too short")
        return _documents_redirect({"ok": False, "message": "That PDF has too little readable text (it may be "
                                    "image-based) — paste your background by hand instead."})

    existing = (state.load_state().get("raw_background") or "")
    combined = (existing + "\n\n" + text) if existing.strip() else text
    state.update_state(raw_background=combined)
    activity.log_event("action", name="background_from_cv", ok=True, chars=len(text))
    return _documents_redirect({"ok": True, "message": "Added the text from {} below — read it, fix anything odd "
                                "(PDF extraction is imperfect), then Generate.".format(filename)})


@app.post("/documents/answer-bank/generate")
async def documents_answer_bank_generate(request: Request):
    form = await request.form()
    raw_background = form.get("raw_background", "")
    state.update_state(raw_background=raw_background)  # persist whatever was typed either way
    ok, err = jobs.start_answer_bank_job(raw_background)
    return _documents_redirect(None if ok else {"ok": False, "message": err})


@app.post("/documents/answer-bank/save")
async def documents_answer_bank_save(request: Request):
    form = await request.form()
    content = form.get("qa_bank_editor", "")
    if not content.strip():
        return _documents_redirect({"ok": False, "message": "Nothing to save — the editor is empty."})

    os.makedirs(os.path.dirname(paths.QA_BANK_MD), exist_ok=True)
    with open(paths.QA_BANK_MD, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.makedirs(paths.SKILL_DIR, exist_ok=True)
    with open(paths.SKILL_ANSWER_BANK, "w", encoding="utf-8") as fh:
        fh.write(content)
    # The job that produced this content (if any) is now superseded by the saved
    # file on disk; clear it so a later page view reads qa_bank.md, not stale output.
    job = jobs.current_job()
    if job and job.get("name") == "answer_bank":
        state.update_state(job=None)

    return _documents_redirect({"ok": True, "message": "Answer bank saved."}, show_home_link=True)


@app.get("/documents/answer-bank/result")
async def documents_answer_bank_result():
    job = jobs.current_job()
    text = ""
    if job and job.get("name") == "answer_bank":
        text = jobs.read_log_full(job.get("log_path"))
    return {"text": text}


@app.post("/documents/cv/upload")
async def documents_cv_upload(request: Request):
    form = await request.form()
    upload = await _save_cv_upload(form.get("cv_pdf"))
    return _documents_redirect({"ok": upload["ok"], "message": upload["message"]}, show_home_link=upload["ok"])


@app.post("/documents/cv/use-default")
async def documents_cv_use_default(request: Request):
    form = await request.form()
    filename = (form.get("filename") or "").strip()
    if filename not in state.cv_pdfs():
        return _documents_redirect({"ok": False, "message": "That file isn't in the CV pool anymore — refresh and try again."})

    profile = profile_io.load_profile()
    if profile is None:
        return _documents_redirect({"ok": False, "message": "No profile yet — fill in the Profile page first."})

    resume_path = "data/{}".format(filename)
    profile["resume_file"] = resume_path
    filled = 0
    for q in (profile.get("job_search") or {}).get("search_queries") or []:
        current = (q.get("resume") or "").strip()
        if not current or "CHANGEME" in current:
            q["resume"] = resume_path
            filled += 1
    profile_io.save_profile_yaml(profile)

    return _documents_redirect({"ok": True, "message": "{} is now the default resume{}.".format(
        filename, " (also used by {} job search{})".format(filled, "" if filled == 1 else "es") if filled else "")})


@app.post("/documents/cv/generate")
async def documents_cv_generate(request: Request):
    ok, err = jobs.start_cv_generate_job()
    return _documents_redirect(None if ok else {"ok": False, "message": err})


@app.get("/jobs/status")
async def jobs_status():
    return JSONResponse(jobs.status())


@app.get("/prereqs")
async def prereqs():
    """Live prerequisite probe, polled by pages that are waiting for the user to install
    something (e.g. the Chrome invite on the Connections page) so they can unlock
    themselves the moment it appears — both checks are cheap filesystem/PATH lookups."""
    return JSONResponse({
        "chrome": state.chrome_installed(),
        "claude": state.claude_cli_path() is not None,
    })


# ------------------------------------------------------------------------------
# DASHBOARD — read-only view of claude_applicator/applications.db. Never writes to the
# DB; opened in SQLite read-only URI mode so a bug here can't corrupt real application
# history.
# ------------------------------------------------------------------------------

def _dashboard_rows():
    con = sqlite3.connect("file:{}?mode=ro".format(paths.APPLICATIONS_DB), uri=True)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(
            "SELECT job_id, company, title, status, outcome, apply_email, applied_at, "
            "status_updated_at FROM applications ORDER BY status_updated_at DESC LIMIT 50"
        )
        rows = [dict(r) for r in cur.fetchall()]
        total = con.execute("SELECT COUNT(*) FROM applications").fetchone()[0]
        this_week = con.execute(
            "SELECT COUNT(*) FROM applications WHERE "
            "date(COALESCE(applied_at, status_updated_at)) >= date('now', '-7 days')"
        ).fetchone()[0]
        companies = con.execute(
            "SELECT COUNT(DISTINCT company) FROM applications WHERE company IS NOT NULL AND company != ''"
        ).fetchone()[0]
    finally:
        con.close()
    return rows, {"total": total, "this_week": this_week, "companies": companies}


def _costs_total():
    """Sum every *_cost_usd column in claude_applicator/output/costs.csv, or None if the
    file doesn't exist yet (no run has happened) — the dashboard skips that tile then."""
    if not os.path.isfile(paths.COSTS_CSV):
        return None
    total = 0.0
    with open(paths.COSTS_CSV, encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        cost_cols = [c for c in (reader.fieldnames or []) if c.endswith("_cost_usd")]
        for row in reader:
            for col in cost_cols:
                try:
                    total += float(row.get(col) or 0)
                except ValueError:
                    pass
    return round(total, 2)


# ------------------------------------------------------------------------------
# FEEDBACK — placeholder page for the research/feedback area (salary research for the
# target role + location, suggested search queries, and later what the applications
# themselves teach us). Nothing runs yet; see todo_list.md PROD-010.
# ------------------------------------------------------------------------------

@app.get("/feedback")
async def feedback_page(request: Request):
    ctx = base_context("feedback")
    profile = profile_io.load_profile() or {}
    queries = ((profile.get("job_search") or {}).get("search_queries") or [])
    ctx["target_queries"] = [q for q in queries if (q.get("query") or "").strip()][:6]
    ctx["stop_banner"] = flash_pop("feedback").get("stop_banner")
    return render(request, "feedback.html", ctx)


@app.get("/dashboard")
async def dashboard_page(request: Request):
    ctx = base_context("dashboard")
    ctx["usage"] = usage.summary()
    ctx["stop_banner"] = flash_pop("dashboard").get("stop_banner")
    ctx["db_exists"] = state.db_exists()
    ctx["rows"] = []
    ctx["stats"] = {"total": 0, "this_week": 0, "companies": 0}
    ctx["db_error"] = None
    if ctx["db_exists"]:
        try:
            ctx["rows"], ctx["stats"] = _dashboard_rows()
        except sqlite3.Error as exc:
            ctx["db_error"] = str(exc)
    ctx["costs_total"] = _costs_total()
    return render(request, "dashboard.html", ctx)


# ------------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    # 127.0.0.1 only — this app must never be reachable from the network.
    # Port: 8765 by default; the Electron tower overrides via JOBAPPLY_PORT
    # (app/run_app.sh exports it — plain `bash app/run_app.sh` users never see a change).
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("JOBAPPLY_PORT", "8765")))
