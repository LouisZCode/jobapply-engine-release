"""app/lib/onboarding.py — the onboarding: the two-prompt door at /onboarding
(ONBOARD-001, 2026-11-25) plus the 9-step wizard it replaced (M2 of the tower), which
stays reachable, unlinked, at /onboarding/wizard until it is removed.

Design contract (mirrors .claude/skills/onboard/SKILL.md, the chat interview this
replaces): NEVER invent an answer the user didn't give. Every fact written at the end
comes from the draft the human typed; anything absent stays empty ("" / None / []) —
the engine skip-and-logs a missing answer rather than guessing, and so do we.

The wizard is a THIN layer over the existing machinery: the draft is stored in
app_state.json as the same flat form-field dict the Profile page posts, the finish
step assembles it through profile_io.build_profile() + validate() (one code path for
both UIs — no second schema to drift), and everything else (render, DB init, schedule
install, test run) reuses actions.py / setup.sh unchanged.

Steps are server-rendered, one form per step, so state survives a restart mid-wizard
and no JS is required to make progress. APP_TEST=1 stubs the same externals it always
has (claude probe, launchctl, Chrome) — the whole flow is curl-testable.
"""
import os
import re

from . import actions, keys, llm, paths, profile_io, state

# ------------------------------------------------------------------ step map
# (key, short title, one-line purpose). Order is the wizard order; the step index
# lives in app_state.json so a user can stop mid-wizard and land back where they were.
STEPS = [
    ("welcome", "Welcome", "What this app does, and what you'll need"),
    ("prereqs", "Your Mac", "A few one-time checks before we start"),
    ("claude", "Intelligence", "The brain behind the engine — your Claude login or an API key"),
    ("identity", "About you", "Name and contact details applications will use"),
    ("work", "Right to work & money", "Authorization, languages, notice, salary floor"),
    ("search", "What to apply for", "Roles, places, your CV, and how fresh posts should be"),
    ("story", "Your story", "Background notes the writer tool draws from"),
    ("connect", "Accounts", "LinkedIn (required) and Gmail (optional)"),
    ("finish", "Schedule & first run", "Pick a daily time, then watch a safe test run"),
]
STEP_KEYS = [k for k, _t, _s in STEPS]

# ------------------------------------------------------------------ draft I/O
# The draft is a flat {field: value} dict of Profile-page form fields (strings), plus
# list fields ({name: [values]}) for the multi-row inputs (languages, searches…).
# DraftForm adapts it to the .get()/.getlist() surface build_profile() expects.

LIST_FIELDS = (
    "lang_language", "lang_level",
    "edu_degree", "edu_school", "edu_years",
    "cert_text",
    "proj_name", "proj_desc",
    "skillyears_key", "skillyears_val",
    "sq_query", "sq_location", "sq_filters", "sq_resume",
)


class DraftForm:
    """The stored draft, dressed up as a form object for build_profile()/validate()."""

    def __init__(self, draft):
        self._scalars = dict(draft.get("fields") or {})
        self._lists = dict(draft.get("lists") or {})

    def get(self, name, default=""):
        return self._scalars.get(name, default)

    def getlist(self, name):
        return list(self._lists.get(name, []))


def load_draft():
    st = state.load_state()
    return {
        "step": st.get("onboarding_step", 0),
        "fields": st.get("onboarding_fields") or {},
        "lists": st.get("onboarding_lists") or {},
        "background": st.get("onboarding_background") or "",
        "finished": bool(st.get("onboarding_finished")),
    }


def save_draft(step=None, fields=None, lists=None, background=None):
    updates = {}
    if step is not None:
        updates["onboarding_step"] = step
    if fields is not None:
        updates["onboarding_fields"] = fields
    if lists is not None:
        updates["onboarding_lists"] = lists
    if background is not None:
        updates["onboarding_background"] = background
    state.update_state(**updates)


def absorb_form(form, draft, list_fields=LIST_FIELDS):
    """Merge one step's POST into the draft: every scalar field the step sent
    overwrites the draft value; fields the step didn't send stay untouched (later
    steps never erase earlier ones). List fields replace wholesale per step — each
    step owns the rows it edits, and an empty submission clears them honestly."""
    fields = dict(draft["fields"])
    lists = dict(draft["lists"])
    sent_lists = set()
    for key in form.keys():
        values = form.getlist(key)
        if key in list_fields:
            sent_lists.add(key)
            lists[key] = values
        elif len(values) == 1:
            fields[key] = values[0]
    for key in sent_lists:
        pass  # already stored above; kept explicit for readability
    return fields, lists


def draft_form(draft):
    return DraftForm(draft)


# ------------------------------------------------------------------ prereq checks

def prereq_checks():
    """The Stage-0 list, as (key, label, ok, detail) rows. Live checks only —
    re-runnable on every view, no state stored."""
    repo = paths.REPO_ROOT
    home = os.path.expanduser("~")
    tcc = any(repo.startswith(os.path.join(home, p) + os.sep) for p in ("Desktop", "Documents", "Downloads"))
    import shutil
    return [
        {
            "key": "chrome",
            "label": "Google Chrome installed",
            "ok": state.chrome_installed(),
            "detail": "The engine drives your real Chrome — install it from google.com/chrome, then re-check.",
        },
        {
            "key": "claude_cli",
            "label": "Claude CLI installed",
            "ok": state.claude_cli_path() is not None,
            "detail": "Install the Claude CLI (claude.ai/download), then sign in once. Re-check after.",
        },
        {
            "key": "location",
            "label": "Folder not on Desktop / Documents / Downloads",
            "ok": not tcc,
            "detail": "macOS blocks background schedulers from those folders. Move the whole engine folder "
                      "under your home folder (e.g. ~/GitHub/) before installing the daily schedule.",
        },
    ]


# ------------------------------------------------------------------ the door (ONBOARD-001)
# /onboarding shows two prompts and nothing else: "Do you have these installed?" — we
# verify each one ourselves (yes → a green "Okay", no → the official download link) — and
# "Do you have a CV? Upload it now or skip." Home's readiness list guides the rest. The
# checks are always live; only the visitor's yes/no answers are stored (app_state.json,
# onboarding_answers) so the page knows which message to show.

DOOR_AI_URL = "https://claude.ai/download"


def door_rows():
    """[{key, label, why, ok, status, url, link, install}] in display order. status:
    'okay' (verified on this Mac), 'ask' (not found, not answered yet), 'not_found'
    (visitor said yes, we still can't see it), 'install' (visitor said no)."""
    answers = state.load_state().get("onboarding_answers") or {}
    repo = paths.REPO_ROOT
    home = os.path.expanduser("~")
    in_tcc_folder = any(
        repo.startswith(os.path.join(home, p) + os.sep) for p in ("Desktop", "Documents", "Downloads")
    )
    rows = [
        {
            "key": "chrome",
            "label": "Google Chrome",
            "why": "Job Apply opens LinkedIn in your own Chrome.",
            "ok": state.chrome_installed(),
            "url": "https://www.google.com/chrome/",
            "link": "google.com/chrome",
            "install": "Download it, open it once, then come back and re-check.",
        },
        {
            "key": "ai",
            "label": "An AI assistant",
            # BRAIN-001b: any application AI counts — Claude, or a stored key with pi
            # installed (llm.application_options() decides, same list the Connections
            # page picks from).
            "why": "The AI reads job posts and fills in the forms. Claude, or an API key (Z.ai, OpenRouter, OpenAI).",
            "ok": any(o["available"] for o in llm.application_options()),
            "url": DOOR_AI_URL,
            "link": "claude.ai/download",
            "install": "Download Claude, install it, then sign in once inside the app.",
            "has_key": any(keys.hints()),
        },
    ]
    if in_tcc_folder:
        # Only when it fails: every packaged install lives in ~/JobApply, so a friend never
        # sees this row.
        rows.append({
            "key": "location",
            "label": "The app's folder is in a place macOS locks",
            "why": "macOS blocks background apps from folders on Desktop, Documents and Downloads.",
            "ok": False,
            "url": "",
            "link": "",
            "install": "Move the JobApply folder somewhere else in your home folder, then re-check.",
        })
    for row in rows:
        answer = answers.get(row["key"])
        if row["ok"]:
            row["status"] = "okay"
        elif answer == "yes":
            row["status"] = "not_found"
        elif answer == "no":
            row["status"] = "install"
        else:
            row["status"] = "ask"
    return rows


def ai_key_names():
    """'Zai (GLM), Codex / OpenAI, OpenRouter' — the API-key providers, for the door's
    second AI option (comma-joined: a provider label may itself contain a slash)."""
    return ", ".join(meta["label"] for meta in keys.PROVIDERS.values())


# ------------------------------------------------------------------ starter queries

def starter_queries(roles_text, location, filters_pref):
    """Deterministic starter rotation from free-text roles: one search per role
    (max 3), same location, one filters combo chosen from the remote preference.
    No invention — the role text is used exactly as typed, minus obvious filler."""
    roles = []
    for raw in re.split(r"[,;\n]+", roles_text or ""):
        role = raw.strip().lower().lstrip("-•* ").strip()
        if role and role not in roles:
            roles.append(role)
    roles = roles[:3]
    location = (location or "").strip()
    filters = "past week + remote" if filters_pref == "remote" else (
        "past week" if filters_pref == "hybrid" else "past 24 hours")
    return [
        {"query": role, "location": location, "filters": filters, "resume": ""}
        for role in roles
    ]


# ------------------------------------------------------------------ qa_bank seed

_SEED_HEADER = """# qa_bank.md — seeded by the onboarding wizard, {date}
#
# This is a STARTER version of your answer bank. The writer tool (which answers
# "why do you want to work here?" style questions) can ONLY use facts in this file.
# Everything below the raw notes is a guided skeleton: replace the CHANGEME lines
# whenever you like — the fuller this file, the more questions it can answer
# honestly. The Background page can also have Claude draft it with you.
"""


def build_qa_seed(draft, profile):
    """Deterministic qa_bank.md seed from the draft + assembled profile. Identity
    summary is generated FROM GIVEN FACTS ONLY (name/city/role targets); the pasted
    background lands verbatim in a Raw notes section; every other section keeps its
    guided CHANGEME placeholder. Never invents a fact to fill a gap."""
    ident = profile.get("identity") or {}
    loc = profile.get("location") or {}
    js = profile.get("job_search") or {}
    searches = js.get("search_queries") or []
    role_line = ", ".join(q.get("query", "") for q in searches[:3] if q.get("query"))
    bits = []
    bits.append(_SEED_HEADER.format(date=__import__("datetime").date.today().isoformat()))
    bits.append("## 1. Identity summary\n")
    summary = "CHANGEME — one short paragraph: who you are, where you're based, what role you want."
    if ident.get("full_name"):
        summary = "I'm {}.".format(ident["full_name"])
        if loc.get("current_city") or loc.get("country"):
            where = " ".join(x for x in (loc.get("current_city"), loc.get("country")) if x)
            summary += " I'm based in {}.".format(where)
        if role_line:
            summary += " I'm looking for {} roles.".format(role_line)
        summary += " (Expand this paragraph in your own words — add what makes your background strong.)"
    bits.append(summary + "\n")
    bits.append("## 2. Raw background notes (as pasted during setup)\n")
    raw = (draft.get("background") or "").strip()
    bits.append((raw if raw else "(Nothing pasted yet — add your background here: employers, titles, "
                                 "dates, real numbers, tools, projects.)") + "\n")
    for heading, hint in (
        ("## 3. Employers", "One block per employer: title, dates, what you actually did, real numbers."),
        ("## 4. Projects", "Name, what it does, stack, real traction or status."),
        ("## 5. Stories (Challenge → Action → Result)", "2-4 short stories with real outcomes."),
        ("## 6. Technical stack — depth, not just a list", "Tool: years, where you used it, how deep."),
        ("## 7. Languages & fluency", "Language: level."),
        ("## 8. Availability & anything else worth stating once", "Notice period, start date, constraints."),
    ):
        bits.append("{}\n\nCHANGEME — {}.\n".format(heading, hint))
    return "\n".join(bits)


def write_qa_seed(content):
    """The same two-file save the Documents page performs: qa_bank.md for the writer
    tool + the cv-builder skill's answer bank copy, so the CV generator is grounded
    from day one."""
    os.makedirs(os.path.dirname(paths.QA_BANK_MD), exist_ok=True)
    with open(paths.QA_BANK_MD, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.makedirs(paths.SKILL_DIR, exist_ok=True)
    with open(paths.SKILL_ANSWER_BANK, "w", encoding="utf-8") as fh:
        fh.write(content)


# ------------------------------------------------------------------ finish assembly

def assemble(draft, run_time):
    """The one-shot 'Finish setup': profile.yaml → render → qa_bank seed.
    Returns (errors, log_lines); errors empty == everything wrote. Schedule install
    is NOT done here — the finish step calls actions.render_schedule/install_schedule
    itself so its log output can be shown next to the button that caused it."""
    form = DraftForm(draft)
    errors = profile_io.validate(form)
    if errors:
        return errors, []
    log = []
    profile = profile_io.build_profile(form, existing=None, cv_pool=state.cv_pdfs())
    profile_io.save_profile_yaml(profile)
    log.append("$ wrote claude_applicator/profile.yaml")
    render_res = actions.render_profile()
    log.append("$ python3 setup/render_templates.py")
    log.append((render_res.get("stdout") or render_res.get("stderr") or "").strip())
    if not render_res.get("ok"):
        return ["Rendering the engine docs failed — see the log below."], log
    write_qa_seed(build_qa_seed(draft, profile))
    log.append("$ wrote claude_applicator/qa_bank.md (+ cv-builder answer bank copy)")
    state.update_state(onboarding_finished=True)
    return [], log
