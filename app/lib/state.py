"""app/lib/state.py — persistent app_state.json (gitignored) + live status checks.

Design note: the setup checklist never trusts app_state.json alone for anything that
has a real, checkable filesystem/binary signal (profile.yaml existing, user.env
existing, the DB file existing, the claude CLI on PATH, Chrome installed, a launchd
plist installed). app_state.json only stores the things that CANNOT be verified by
looking at the filesystem: the human's "I signed into LinkedIn" attestation, and the
cached result of the last `claude -p` probe (re-probing on every page load would be
slow and would spend real usage on every checklist glance).
"""
import glob
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from datetime import datetime

from . import activity, paths, profile_io

DEFAULT_STATE = {
    # the 8 checklist steps (booleans; some are overridden by a live check — see
    # compute_status() below — this default only matters before the first save)
    "profile_saved": False,
    "rendered": False,
    "answer_bank_created": False,
    "cv_in_place": False,
    "db_initialized": False,
    "linkedin_confirmed": False,
    "claude_connected": False,
    "schedule_installed": False,
    # non-step settings
    "claude_probe_message": "",
    "claude_probe_checked_at": "",
    "linkedin_verified_at": "",
    "schedule_run_time": "05:01",
    "schedule_max_applies": 3,
    "schedule_mode": "prepare",
    # Documents page — the raw background text the user pasted (persisted so it survives
    # a reload); the current/last background job run (see app/lib/jobs.py), or None.
    "raw_background": "",
    "job": None,
    # Profile page — "fill my profile from my CV" (cv_extract job kind). profile_draft is
    # a flat form-field dict (same shape as profile_io.form_from_profile()'s output) used
    # only to pre-fill EMPTY fields; profile_draft_source is the uploaded PDF's filename,
    # shown in the pre-fill banner. Cleared on a successful profile save.
    "profile_draft": None,
    "profile_draft_source": "",
}


def load_state():
    if not os.path.isfile(paths.APP_STATE_FILE):
        return dict(DEFAULT_STATE)
    try:
        with open(paths.APP_STATE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return dict(DEFAULT_STATE)
    merged = dict(DEFAULT_STATE)
    merged.update(data or {})
    return merged


def save_state(state):
    os.makedirs(os.path.dirname(paths.APP_STATE_FILE), exist_ok=True)
    with open(paths.APP_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


def update_state(**kwargs):
    state = load_state()
    state.update(kwargs)
    save_state(state)
    return state


# ------------------------------------------------------------------------------
# Live checks (real filesystem / PATH state — not app_state.json)
# ------------------------------------------------------------------------------

def profile_exists():
    return os.path.isfile(paths.PROFILE_YAML)


def rendered_exists():
    return os.path.isfile(paths.USER_ENV)


def db_exists():
    return os.path.isfile(paths.APPLICATIONS_DB)


def claude_cli_path():
    return shutil.which("claude")


_USAGE_LIMIT_RE = re.compile(r"limit reached|usage limit|rate limit|out of usage", re.I)
_USAGE_RESET_RE = re.compile(r"(?:resets?|try again)[^.]*?(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)", re.I)

# MULTI-001: mirror of actions.CDP_PORT (importing actions from here would be circular —
# actions imports state). Unset env = the same 9222 as always.
_CDP_PORT = int(os.environ.get("JOBAPPLY_CDP_PORT", "9222"))


def claude_usage_hint():
    """Best-effort, honest signal about the user's Claude plan headroom. The CLI has no
    headless usage/allowance query, so this only reports what the app has actually
    observed: the most recent probe/job failure that looks like a plan-limit message.
    Deliberately reads ONLY claude_probe_message (not any job's log tail) — jobs.py
    imports this module, so importing jobs here to inspect a running job would be
    circular; the probe message alone is signal enough and keeps this simple."""
    st = load_state()
    msg = st.get("claude_probe_message", "") or ""
    if _USAGE_LIMIT_RE.search(msg):
        resets_match = _USAGE_RESET_RE.search(msg)
        return {
            "state": "limited",
            "detail": msg[:120],
            "resets": resets_match.group(1) if resets_match else "",
        }
    if st.get("claude_connected"):
        return {"state": "ok", "detail": "", "resets": ""}
    return {"state": "unknown", "detail": "", "resets": ""}


def chrome_installed():
    """Live check, re-run on every page view — so the moment the user installs Chrome,
    the next poll/refresh sees it and the app "continues" on its own (no restart).
    APP_TEST_NO_CHROME=1 fakes the not-installed state so the invite UI can be
    exercised on a machine that does have Chrome (test-only, same spirit as APP_TEST)."""
    if os.environ.get("APP_TEST_NO_CHROME") == "1":
        return False
    return any(os.path.isdir(p) for p in paths.CHROME_APP_CANDIDATES)


def linkedin_login_status(timeout=1.0):
    """Three-state read of the pipeline's dedicated Chrome-Agent profile (CDP port, see
    _CDP_PORT above):
    "logged_in" (some page tab's URL starts with the LinkedIn feed — a logged-out visit
    to /feed redirects to a login page instead, so this is a reliable signed-in signal),
    "chrome_up" (the debug port answers but no feed tab yet), or "no_chrome" (port
    unreachable). The first time "logged_in" is observed, persists linkedin_confirmed so
    the badge stays green even after Chrome-Agent is later closed (the li_at cookie
    itself lives on in the Chrome profile regardless)."""
    if os.environ.get("APP_TEST") == "1":
        result = os.environ.get("APP_TEST_LINKEDIN", "no_chrome")
    else:
        try:
            with urllib.request.urlopen("http://127.0.0.1:{}/json".format(_CDP_PORT), timeout=timeout) as resp:
                tabs = json.load(resp)
        except (urllib.error.URLError, OSError, ValueError):
            return "no_chrome"
        pages = [t for t in tabs if t.get("type") == "page"]
        feed_open = any((t.get("url") or "").startswith("https://www.linkedin.com/feed") for t in pages)
        result = "logged_in" if feed_open else "chrome_up"

    if result == "logged_in":
        first_time = not bool(load_state().get("linkedin_confirmed"))
        update_state(
            linkedin_confirmed=True,
            linkedin_verified_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        )
        if first_time:
            activity.log_event("linkedin", event="verified")
    return result


def qa_bank_exists():
    return os.path.isfile(paths.QA_BANK_MD)


def cv_pdfs():
    """Sorted list of PDF filenames currently in claude_applicator/data/ (empty if the
    directory doesn't exist yet — it's created on first upload/generation, not up front)."""
    if not os.path.isdir(paths.CV_DATA_DIR):
        return []
    return sorted(f for f in os.listdir(paths.CV_DATA_DIR) if f.lower().endswith(".pdf"))


def cv_pdf_exists():
    return bool(cv_pdfs())


def installed_launch_agent_plists():
    """launchd plists installed under ~/Library/LaunchAgents that look like this
    pipeline's (com.<user>.jobapply*.plist / com.<user>.jobhealth*.plist)."""
    if not os.path.isdir(paths.LAUNCH_AGENTS_DIR):
        return []
    hits = []
    for pattern in ("com.*.jobapply.plist", "com.*.jobhealth.plist"):
        hits.extend(glob.glob(os.path.join(paths.LAUNCH_AGENTS_DIR, pattern)))
    return sorted(hits)


def compute_status():
    """Returns (status_dict, state_dict). status_dict has the 8 checklist keys, each
    a bool, computed by preferring a live check where one exists."""
    state = load_state()
    schedule_live = bool(installed_launch_agent_plists())
    status = {
        "profile_saved": profile_exists(),
        "rendered": rendered_exists(),
        "answer_bank_created": qa_bank_exists(),
        "cv_in_place": cv_pdf_exists(),
        "db_initialized": db_exists(),
        "linkedin_confirmed": bool(state.get("linkedin_confirmed")),
        "claude_connected": bool(state.get("claude_connected")),
        "schedule_installed": schedule_live or bool(state.get("schedule_installed")),
    }
    return status, state


STEP_ORDER = [
    "profile_saved",
    "rendered",
    "answer_bank_created",
    "cv_in_place",
    "db_initialized",
    "linkedin_confirmed",
    "claude_connected",
    "schedule_installed",
]

STEP_LABELS = {
    "profile_saved": "Your info filled in",
    "rendered": "Instructions for the AI prepared from your info",
    "answer_bank_created": "Answer bank created (your answers, ready to type)",
    "cv_in_place": "CV in place (a PDF ready to send)",
    "db_initialized": "Application list initialized",
    "linkedin_confirmed": "LinkedIn signed in once",
    "claude_connected": "An AI connected (Claude signed in, or an API key that passed its test)",
    "schedule_installed": "Daily schedule installed",
}

STEP_LINKS = {
    "profile_saved": "profile",
    "rendered": "profile",
    "answer_bank_created": "documents",
    "cv_in_place": "documents",
    "db_initialized": "home",
    "linkedin_confirmed": "connections",
    "claude_connected": "connections",
    "schedule_installed": "schedule",
}


def readiness():
    """Ordered list of {key, label, ok, page} covering everything a REAL scheduled run
    needs — stricter than compute_status()'s checklist, which only checks that
    profile.yaml exists at all rather than that every required field actually has a
    value. "page" reuses STEP_LINKS's own short route names rather than a second
    mapping. schedule_installed itself is deliberately left out: this list is what
    gates installing the schedule, so it can't also depend on the schedule already being
    installed. Returns (items, ready) where ready = all(item["ok"] for item in items)."""
    st = load_state()
    profile = profile_io.load_profile()
    form = profile_io.form_from_profile(profile) if profile is not None else {}
    profile_complete = profile is not None and all(
        str(form.get(field) or "").strip() for field, _label in profile_io.REQUIRED_FIELDS
    ) and any(
        (q.get("query") or "").strip() and (q.get("location") or "").strip()
        for q in form.get("search_queries") or []
    )
    resume_rel = (form.get("resume_file") or "").strip()
    resume_ok = bool(resume_rel) and os.path.isfile(os.path.join(paths.ENGINE_DIR, resume_rel))

    # Display order = the path a new user walks (ONBOARD-001, 2026-11-25): AI → your
    # info → CV → what a run prepares → LinkedIn last. `ready` is still "every item ok".
    items = [
        {
            # Intelligence gate (Luis, 2026-11-08): a working provider API key that passed
            # its test replaces the Claude-connection requirement.
            "key": "claude_connected",
            "label": STEP_LABELS["claude_connected"],
            "ok": (bool(st.get("claude_connected")) and claude_cli_path() is not None)
                  or any((v or {}).get("ok") for v in (st.get("key_test_results") or {}).values()),
            "page": STEP_LINKS["claude_connected"],
        },
        {
            "key": "profile_saved",
            "label": "Your info: name, email, salary number and at least one job search",
            "ok": profile_complete,
            "page": STEP_LINKS["profile_saved"],
        },
        {
            "key": "resume_set",
            "label": "A CV chosen as the one to send",
            "ok": resume_ok,
            "page": STEP_LINKS["profile_saved"],
        },
        {
            "key": "rendered",
            "label": STEP_LABELS["rendered"],
            "ok": rendered_exists(),
            "page": STEP_LINKS["rendered"],
        },
        {
            "key": "answer_bank_created",
            "label": STEP_LABELS["answer_bank_created"],
            "ok": qa_bank_exists(),
            "page": STEP_LINKS["answer_bank_created"],
        },
        {
            "key": "cv_in_place",
            "label": STEP_LABELS["cv_in_place"],
            "ok": cv_pdf_exists(),
            "page": STEP_LINKS["cv_in_place"],
        },
        {
            "key": "db_initialized",
            "label": STEP_LABELS["db_initialized"],
            "ok": db_exists(),
            "page": STEP_LINKS["db_initialized"],
        },
        {
            "key": "linkedin_confirmed",
            "label": STEP_LABELS["linkedin_confirmed"],
            "ok": bool(st.get("linkedin_confirmed")),
            "page": STEP_LINKS["linkedin_confirmed"],
        },
    ]
    ready = all(item["ok"] for item in items)
    return items, ready
