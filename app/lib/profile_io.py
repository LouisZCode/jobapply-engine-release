"""app/lib/profile_io.py — profile.yaml <-> HTML form, both directions.

The on-disk shape written here mirrors claude_applicator/profile.template.yaml
section-for-section and key-for-key (see that file for the authoritative schema and
the policy explanations behind each field). This module only handles translating
between that YAML shape and the flat field names used by app/templates/profile.html;
it never talks to the network or shells out (see server.py for the render_templates.py
subprocess call).

Advanced job_search knobs that the MVP wizard does not expose a UI for (cv_by_title,
company_blocklist, titles/locations hints, remote_first, workplace_type,
posted_within_hours, exclude_title_keywords) are written with the same sensible
defaults profile.template.yaml ships with, or preserved as-is from an existing
profile.yaml when editing (see load_profile()/build_profile()) so re-saving the form
never silently drops hand-edited values in those knobs.
"""
import os

import yaml

from . import paths

MAX_SEARCH_QUERIES = 6

TRI_STATE_TRUE = "yes"
TRI_STATE_FALSE = "no"
TRI_STATE_NULL = ""

# "Fill my profile from my CV" (cv_extract job kind, see app/lib/jobs.py) — the ONLY
# top-level keys its output YAML is allowed to contain. Mirrors profile.yaml's own field
# names/shapes but flattened one level (no identity:/location:/experience: nesting for
# the scalar fields) since a CV extraction has nothing to say about the rest of
# profile.yaml (work authorization, compensation, screening, job search, ...). Used both
# to build the extraction prompt (jobs.py) and to filter the agent's output before it
# ever reaches app_state.json — an agent writing an extra key is silently ignored, never
# trusted.
CV_EXTRACT_ALLOWED_KEYS = (
    "full_name", "email", "phone", "current_city", "country",
    "languages", "education", "certifications", "experience",
    "technical_stack", "portfolio_projects",
)


def tri_to_yaml(val):
    if val == TRI_STATE_TRUE:
        return True
    if val == TRI_STATE_FALSE:
        return False
    return None


def tri_from_yaml(val):
    if val is True:
        return TRI_STATE_TRUE
    if val is False:
        return TRI_STATE_FALSE
    return TRI_STATE_NULL


def csv_to_list(s):
    if not s:
        return []
    return [p.strip() for p in s.split(",") if p.strip()]


def list_to_csv(lst):
    return ", ".join(lst or [])


def to_number(s, default=0):
    s = (s or "").strip()
    if not s:
        return default
    try:
        f = float(s)
    except ValueError:
        return default
    return int(f) if f.is_integer() else f


def load_profile():
    """Returns the parsed profile.yaml dict, or None if it doesn't exist yet."""
    if not os.path.isfile(paths.PROFILE_YAML):
        return None
    with open(paths.PROFILE_YAML, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data or {}


def save_profile_yaml(data):
    os.makedirs(os.path.dirname(paths.PROFILE_YAML), exist_ok=True)
    with open(paths.PROFILE_YAML, "w", encoding="utf-8") as fh:
        yaml.dump(data, fh, sort_keys=False, allow_unicode=True, default_flow_style=False)


def parse_cv_extract_yaml(path):
    """Reads and validates a cv_extract job's output YAML (see app/lib/jobs.py). Returns a
    dict filtered down to CV_EXTRACT_ALLOWED_KEYS on success. Raises ValueError with a
    human-readable reason on anything malformed — bad YAML syntax, or a top level that
    isn't a mapping at all — so the caller can surface a clear job failure instead of
    storing a bogus draft."""
    with open(path, encoding="utf-8") as fh:
        try:
            raw = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            # Only PyYAML's short "problem" phrase — never str(exc), whose problem_mark
            # context quotes the offending line (i.e. extracted personal data).
            raise ValueError("not valid YAML ({})".format(getattr(exc, "problem", None) or "syntax error"))
    if not isinstance(raw, dict):
        raise ValueError("top level of the file isn't a mapping of keys to values")
    return {k: v for k, v in raw.items() if k in CV_EXTRACT_ALLOWED_KEYS}


# ------------------------------------------------------------------------------
# YAML -> flat form dict (for pre-filling the edit form)
# ------------------------------------------------------------------------------

def _clean(value):
    """A template placeholder is shown as an empty field, never as literal CHANGEME."""
    value = value or ""
    return "" if (isinstance(value, str) and "CHANGEME" in value) else value


def form_from_profile(profile):
    """profile: parsed profile.yaml dict (or {} for a blank/new form). Returns a flat
    dict of form-field-name -> string/bool, matching the <input name="..."> values used
    in app/templates/profile.html."""
    profile = profile or {}
    identity = profile.get("identity") or {}
    acct = profile.get("account_credentials") or {}
    location = profile.get("location") or {}
    auth = profile.get("work_authorization") or {}
    comp = profile.get("compensation") or {}
    experience = profile.get("experience") or {}
    most_recent = experience.get("most_recent") or {}
    screening = profile.get("screening") or {}
    job_search = profile.get("job_search") or {}

    f = {
        "full_name": identity.get("full_name") or "",
        "email": identity.get("email") or "",
        "email_external": identity.get("email_external") or "",
        "phone": identity.get("phone") or "",
        "portfolio": identity.get("portfolio") or "",
        "github": identity.get("github") or "",
        "linkedin": identity.get("linkedin") or "",
        "acc_username": acct.get("username") or "",
        "acc_password": acct.get("password") or "",
        "current_city": location.get("current_city") or "",
        "street_address": location.get("street_address") or "",
        "postal_code": location.get("postal_code") or "",
        "country": location.get("country") or "",
        "relocating_to": location.get("relocating_to") or "",
        "relocation_date": location.get("relocation_date") or "",
        "remote_preference": location.get("remote_preference") or "",
        "travel": location.get("travel") or "",
        "location_note": location.get("note") or "",
        "must_be_based_in": location.get("must_be_based_in") or "",
        "onsite_hybrid_cities": list_to_csv(location.get("onsite_hybrid_cities") or []),
        "willing_to_relocate": bool(location.get("willing_to_relocate")),
        "authorized_in": list_to_csv(auth.get("authorized_in") or []),
        "visa_sponsorship_required": tri_from_yaml(auth.get("visa_sponsorship_required")),
        "nationality": _clean(auth.get("nationality")),
        "eu_citizen": tri_from_yaml(auth.get("eu_citizen")),
        "right_to_work_eu": tri_from_yaml(auth.get("right_to_work_eu")),
        "work_permit_type": auth.get("work_permit_type") or "",
        "work_permit_note": auth.get("work_permit_note") or "",
        "citizenship_gate_pick_citizen": bool(auth.get("citizenship_gate_pick_citizen")),
        "currency": _clean(comp.get("currency")),
        "comp_floor": comp.get("floor") if comp.get("floor") is not None else "",
        "comp_target": comp.get("target") if comp.get("target") is not None else "",
        "comp_range_low": comp.get("range_low") if comp.get("range_low") is not None else "",
        "comp_range_high": comp.get("range_high") if comp.get("range_high") is not None else "",
        "comp_forced_single_number": comp.get("forced_single_number")
        if comp.get("forced_single_number") is not None else "",
        "comp_range_or_text_field": _clean(comp.get("range_or_text_field")),
        "comp_strategy_note": comp.get("strategy_note") or "",
        "resume_file": _clean(profile.get("resume_file")),
        "exp_company": most_recent.get("company") or "",
        "exp_title": most_recent.get("title") or "",
        "exp_industry": most_recent.get("industry") or "",
        "exp_mode": most_recent.get("mode") or "",
        "exp_start": most_recent.get("start") or "",
        "exp_end": most_recent.get("end") or "",
        "exp_highlight": most_recent.get("highlight") or "",
        "prior_companies": list_to_csv(experience.get("prior_companies") or []),
        "exp_summary": experience.get("summary") or "",
        "technical_stack": list_to_csv(profile.get("technical_stack") or []),
        "screening_total_years_experience": screening.get("total_years_experience")
        if screening.get("total_years_experience") is not None else "",
        # Legacy fallback: old profile.yaml files wrote default_unlisted_tech_years.
        "screening_default_unlisted_skill_years": screening.get("default_unlisted_skill_years")
        if screening.get("default_unlisted_skill_years") is not None
        else (screening.get("default_unlisted_tech_years")
              if screening.get("default_unlisted_tech_years") is not None else ""),
        "screening_notice_period": screening.get("notice_period") or "",
        "screening_earliest_start_date": screening.get("earliest_start_date") or "",
        "screening_willing_to_relocate": tri_from_yaml(screening.get("willing_to_relocate")),
        "screening_willing_remote": tri_from_yaml(screening.get("willing_remote")),
        "screening_willing_hybrid": tri_from_yaml(screening.get("willing_hybrid")),
        "screening_willing_onsite": tri_from_yaml(screening.get("willing_onsite")),
        "screening_has_bachelors_degree": tri_from_yaml(screening.get("has_bachelors_degree")),
        "screening_has_drivers_license": tri_from_yaml(screening.get("has_drivers_license")),
        "screening_requires_sponsorship": tri_from_yaml(screening.get("requires_sponsorship")),
        "screening_gender": screening.get("gender") or "prefer not to say",
        "mail_label": profile.get("mail_label") or "Job Applications",
    }

    languages = profile.get("languages") or []
    f["languages"] = [{"language": e.get("language", ""), "level": e.get("level", "")} for e in languages]

    education = profile.get("education") or []
    f["education"] = [
        {"degree": e.get("degree", ""), "school": e.get("school", ""), "years": e.get("years", "")}
        for e in education
    ]

    f["certifications"] = list(profile.get("certifications") or [])

    projects = profile.get("portfolio_projects") or []
    f["portfolio_projects"] = [{"name": p.get("name", ""), "desc": p.get("desc", "")} for p in projects]

    # Legacy fallback: old profile.yaml files wrote tech_years.
    skill_years = screening.get("skill_years") or screening.get("tech_years") or {}
    f["skill_years"] = [{"key": k, "val": v} for k, v in skill_years.items()]

    search_queries = job_search.get("search_queries") or []
    f["search_queries"] = [
        {
            "query": _clean(q.get("query", "")),
            "location": _clean(q.get("location", "")),
            "filters": q.get("filters", ""),
            "resume": _clean(q.get("resume", "")),
        }
        for q in search_queries[:MAX_SEARCH_QUERIES]
    ]

    return f


def form_from_cv_extract(data):
    """data: a CV_EXTRACT_ALLOWED_KEYS-filtered dict (see parse_cv_extract_yaml). Returns a
    flat dict shaped like form_from_profile()'s output, covering only the subset of form
    fields a CV extraction can plausibly produce — used purely to pre-fill EMPTY profile
    form fields (see merge_profile_draft), never to overwrite a saved value."""
    data = data or {}
    experience = data.get("experience") or {}
    most_recent = experience.get("most_recent") or {}

    draft = {
        "full_name": data.get("full_name") or "",
        "email": data.get("email") or "",
        "phone": data.get("phone") or "",
        "current_city": data.get("current_city") or "",
        "country": data.get("country") or "",
        "exp_company": most_recent.get("company") or "",
        "exp_title": most_recent.get("title") or "",
        "exp_industry": most_recent.get("industry") or "",
        "exp_mode": most_recent.get("mode") or "",
        "exp_start": most_recent.get("start") or "",
        "exp_end": most_recent.get("end") or "",
        "exp_highlight": most_recent.get("highlight") or "",
        "prior_companies": list_to_csv(experience.get("prior_companies") or []),
        "exp_summary": experience.get("summary") or "",
        "technical_stack": list_to_csv(data.get("technical_stack") or []),
        "languages": [
            {"language": e.get("language", ""), "level": e.get("level", "")}
            for e in (data.get("languages") or [])
        ],
        "education": [
            {"degree": e.get("degree", ""), "school": e.get("school", ""), "years": e.get("years", "")}
            for e in (data.get("education") or [])
        ],
        "certifications": list(data.get("certifications") or []),
        "portfolio_projects": [
            {"name": p.get("name", ""), "desc": p.get("desc", "")}
            for p in (data.get("portfolio_projects") or [])
        ],
    }
    return draft


def merge_profile_draft(form, draft):
    """form: form_from_profile()'s output (the saved profile, or a blank one). draft:
    form_from_cv_extract()'s output, or None. Returns a new dict where a draft value only
    shows up for a field that's currently empty/falsy in form — any non-empty saved value
    wins. Same "empty means unset" convention the rest of this module already uses."""
    if not draft:
        return form
    merged = dict(form)
    for key, draft_val in draft.items():
        if not merged.get(key):
            merged[key] = draft_val
    return merged


# ------------------------------------------------------------------------------
# Flat form (request.form — a dict-like object with .get()/.getlist(), same shape whether
# it's Flask's ImmutableMultiDict or Starlette's FormData) -> profile.yaml dict
# ------------------------------------------------------------------------------

def _zip_lists(form, *names):
    """Given parallel array field names (e.g. lang_language[], lang_level[]), zip them
    into a list of tuples, one per submitted row."""
    lists = [form.getlist(n) for n in names]
    return list(zip(*lists)) if lists and lists[0] else []


# What a real run cannot do without — required at save time and never written as a
# placeholder; see build_profile() below. Until 2026-11-25 this also demanded phone, city,
# country, nationality and currency. ONBOARD-001 (Luis) reduced it to the three a run
# cannot skip its way past: the name and email every form takes, and the salary number
# the engine types when a form forces one (an empty one would be written as 0 — the one
# answer that must never be guessed). The rest stays on the Profile page as optional: the
# routine already treats a blank as "no answer" and skips-and-logs the field (CLAUDE.md).
# One email only: the same address is used for LinkedIn and for external application forms
# (it should be the one on the LinkedIn account).
REQUIRED_FIELDS = [
    ("full_name", "Full name"),
    ("email", "Email"),
    ("comp_forced_single_number", "Salary — single number"),
]
OPTIONAL_NOTE = "(optional — some forms ask for it; a job that needs it is skipped until you add it)"


def validate(form):
    """Returns a list of human-readable error strings; empty list = OK to save."""
    errors = []
    for field, label in REQUIRED_FIELDS:
        if not (form.get(field) or "").strip():
            errors.append("{} is required.".format(label))
    queries = list(zip(form.getlist("sq_query"), form.getlist("sq_location")))
    filled = [(q, loc) for q, loc in queries if q.strip()]
    if not filled:
        errors.append("At least one job search is required — what to search for, and where.")
    elif any(not loc.strip() for _q, loc in filled):
        errors.append("Every job search needs a location (a country or a city).")
    if len(filled) > MAX_SEARCH_QUERIES:
        errors.append("Job searches: at most {} entries.".format(MAX_SEARCH_QUERIES))
    return errors


def default_resume_path(existing=None, cv_pool=None):
    """The resume path a profile falls back to: keep a saved one that still exists, else
    the newest PDF in the CV pool (uploaded or generated), else '' — never a placeholder."""
    current = ((existing or {}).get("resume_file") or "").strip()
    if current and "CHANGEME" not in current and os.path.isfile(paths.engine_file(current)):
        return current
    pool = list(cv_pool or [])
    if pool:
        newest = max(pool, key=lambda f: os.path.getmtime(os.path.join(paths.CV_DATA_DIR, f)))
        return "data/{}".format(newest)
    return ""


def _money(n):
    try:
        return "{:,}".format(int(n))
    except (TypeError, ValueError):
        return str(n)


def build_profile(form, existing=None, cv_pool=None):
    """form: request.form (Flask's ImmutableMultiDict or Starlette's FormData — both
    support .get()/.getlist(), so this function is framework-agnostic). existing: previously-loaded
    profile.yaml dict (or None) — used to preserve advanced job_search knobs the
    wizard doesn't expose a UI for. cv_pool: filenames in claude_applicator/data/ (see
    state.cv_pdfs) — the default resume is picked from it when the form doesn't name one.
    Returns the full profile dict, ready for save_profile_yaml()."""
    existing = existing or {}
    existing_job_search = existing.get("job_search") or {}

    g = lambda name: (form.get(name) or "").strip()

    resume_file = g("resume_file") or default_resume_path(existing, cv_pool)
    single_number = to_number(g("comp_forced_single_number"), 0)
    range_low = to_number(g("comp_range_low"), 0)
    range_high = to_number(g("comp_range_high"), 0)
    range_text = g("comp_range_or_text_field")
    if not range_text:
        if range_low and range_high:
            range_text = "{}-{}".format(_money(range_low), _money(range_high))
        elif single_number:
            range_text = _money(single_number)

    languages = [
        {"language": lang, "level": level}
        for lang, level in _zip_lists(form, "lang_language", "lang_level")
        if lang.strip() or level.strip()
    ]
    education = [
        {"degree": degree, "school": school, "years": years}
        for degree, school, years in _zip_lists(form, "edu_degree", "edu_school", "edu_years")
        if degree.strip() or school.strip() or years.strip()
    ]
    certifications = [c.strip() for c in form.getlist("cert_text") if c.strip()]
    portfolio_projects = [
        {"name": name, "desc": desc}
        for name, desc in _zip_lists(form, "proj_name", "proj_desc")
        if name.strip() or desc.strip()
    ]
    skill_years = {
        key.strip().lower(): to_number(val, 0)
        for key, val in _zip_lists(form, "skillyears_key", "skillyears_val")
        if key.strip()
    }
    search_queries = [
        {"query": query.strip(), "location": location.strip(), "filters": filters,
         "resume": (resume.strip() or resume_file)}
        for query, location, filters, resume in _zip_lists(
            form, "sq_query", "sq_location", "sq_filters", "sq_resume"
        )
        if query.strip()
    ][:MAX_SEARCH_QUERIES]

    profile = {
        "identity": {
            "full_name": g("full_name"),
            "email": g("email"),
            # One address for everything (LinkedIn + external forms) — the form asks for
            # just one; the engine still reads both keys.
            "email_external": g("email_external") or g("email"),
            "phone": g("phone"),
            "portfolio": g("portfolio"),
            "github": g("github"),
            "linkedin": g("linkedin"),
        },
        "account_credentials": {
            "username": g("acc_username"),
            "password": g("acc_password"),
        },
        "do_not_use": existing.get("do_not_use") or {"phones": [], "emails": []},
        "location": {
            "current_city": g("current_city"),
            "street_address": g("street_address"),
            "postal_code": g("postal_code"),
            "country": g("country"),
            "relocating_to": g("relocating_to") or None,
            "relocation_date": g("relocation_date") or None,
            "remote_preference": g("remote_preference"),
            "travel": g("travel"),
            "note": g("location_note"),
            "must_be_based_in": g("must_be_based_in") or None,
            "onsite_hybrid_cities": csv_to_list(g("onsite_hybrid_cities")),
            "willing_to_relocate": form.get("willing_to_relocate") == "on",
        },
        "work_authorization": {
            "authorized_in": csv_to_list(g("authorized_in")),
            "visa_sponsorship_required": tri_to_yaml(g("visa_sponsorship_required")),
            "nationality": g("nationality"),
            "eu_citizen": tri_to_yaml(g("eu_citizen")),
            "right_to_work_eu": tri_to_yaml(g("right_to_work_eu")),
            "work_permit_type": g("work_permit_type"),
            "work_permit_note": g("work_permit_note"),
            "citizenship_gate_pick_citizen": form.get("citizenship_gate_pick_citizen") == "on",
        },
        "compensation": {
            "currency": g("currency").upper(),
            "floor": to_number(g("comp_floor"), 0),
            "target": to_number(g("comp_target"), 0),
            "range_low": range_low,
            "range_high": range_high,
            "forced_single_number": single_number,
            "range_or_text_field": range_text,
            "strategy_note": g("comp_strategy_note"),
        },
        "languages": languages,
        "education": education,
        "certifications": certifications,
        "experience": {
            "most_recent": {
                "company": g("exp_company"),
                "title": g("exp_title"),
                "industry": g("exp_industry"),
                "mode": g("exp_mode"),
                "start": g("exp_start"),
                "end": g("exp_end"),
                "highlight": g("exp_highlight"),
            },
            "prior_companies": csv_to_list(g("prior_companies")),
            "summary": g("exp_summary"),
        },
        "portfolio_projects": portfolio_projects,
        "technical_stack": csv_to_list(g("technical_stack")),
        "resume_file": resume_file,
        "screening": {
            "total_years_experience": to_number(g("screening_total_years_experience"), 0),
            "default_unlisted_skill_years": to_number(g("screening_default_unlisted_skill_years"), 0),
            "skill_years": skill_years,
            "notice_period": g("screening_notice_period"),
            "earliest_start_date": g("screening_earliest_start_date"),
            "willing_to_relocate": tri_to_yaml(g("screening_willing_to_relocate")),
            "willing_remote": tri_to_yaml(g("screening_willing_remote")),
            "willing_hybrid": tri_to_yaml(g("screening_willing_hybrid")),
            "willing_onsite": tri_to_yaml(g("screening_willing_onsite")),
            "has_bachelors_degree": tri_to_yaml(g("screening_has_bachelors_degree")),
            "has_drivers_license": tri_to_yaml(g("screening_has_drivers_license")),
            "requires_sponsorship": tri_to_yaml(g("screening_requires_sponsorship")),
            "gender": g("screening_gender") or "prefer not to say",
        },
        "job_search": {
            "search_queries": search_queries,
            # Advanced knobs the MVP wizard doesn't expose a form for: keep whatever
            # was already in profile.yaml (empty defaults for a brand-new profile) so
            # re-saving the form never clobbers hand-edited values.
            "cv_by_title": existing_job_search.get("cv_by_title", []),
            "company_blocklist": existing_job_search.get("company_blocklist", []),
            "titles": existing_job_search.get("titles", []),
            "locations": existing_job_search.get("locations", []),
            "remote_first": existing_job_search.get("remote_first", True),
            "workplace_type": existing_job_search.get("workplace_type", "remote"),
            "posted_within_hours": existing_job_search.get("posted_within_hours", 24),
            "exclude_title_keywords": existing_job_search.get("exclude_title_keywords", []),
        },
        "mail_label": g("mail_label") or "Job Applications",
    }
    return profile
