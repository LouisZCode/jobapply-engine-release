"""app/lib/paths.py — every filesystem path the app touches, in one place.

REPO_ROOT is derived from this file's own location (never from CWD), so the app works
no matter where it's launched from.
"""
import os

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../app
REPO_ROOT = os.path.dirname(APP_DIR)
ENGINE_DIR = os.path.join(REPO_ROOT, "claude_applicator")
SETUP_DIR = os.path.join(REPO_ROOT, "setup")

# MULTI-001, per-user data root; unset = legacy layout (every path below stays
# byte-identical to what it always was).
DATA_ROOT = os.environ.get("JOBAPPLY_DATA") or None


def shadow(rel):
    """MULTI-002b: this user's copy of a repo-relative file.

    The data root shadows the repo: a personal file the engine reads at <repo>/<rel>
    lives, for a user, at <DATA_ROOT>/<rel> (same relative path), where
    setup/render_templates.py writes it and claude_applicator/bind_user.sh points the
    repo path at it with a symlink. With JOBAPPLY_DATA unset this is just the repo path
    (the old single-user layout, byte-identical). Files paths.py keeps somewhere else
    under the data root (user.env, run_config.txt) never go through here.
    """
    if DATA_ROOT:
        return os.path.join(DATA_ROOT, rel)
    return os.path.join(REPO_ROOT, rel)

PROFILE_TEMPLATE = os.path.join(ENGINE_DIR, "profile.template.yaml")
PROFILE_YAML = os.path.join(ENGINE_DIR, "profile.yaml")
USER_ENV = os.path.join(ENGINE_DIR, "user.env")
APPLICATIONS_DB = os.path.join(ENGINE_DIR, "applications.db")
RUN_CONFIG = os.path.join(ENGINE_DIR, "run_config.txt")
LAUNCH_CHROME_SH = os.path.join(ENGINE_DIR, "launch_chrome.sh")
RUN_SCHEDULED_SH = os.path.join(ENGINE_DIR, "run_scheduled.sh")
COSTS_CSV = os.path.join(ENGINE_DIR, "output", "costs.csv")

# Documents page — answer bank (qa_bank.md) + CV pool, both git-ignored (personal data).
QA_BANK_TEMPLATE = os.path.join(ENGINE_DIR, "qa_bank.template.md")
QA_BANK_MD = os.path.join(ENGINE_DIR, "qa_bank.md")
CV_DATA_DIR = os.path.join(ENGINE_DIR, "data")

# The cv-builder skill's own gitignored answer bank (mirrors qa_bank.md so the skill can
# ground itself without reaching into claude_applicator/), and its rendered SKILL.md
# (setup/render_templates.py writes this from SKILL.template.md once profile.yaml exists).
SKILL_DIR = os.path.join(REPO_ROOT, ".claude", "skills", "cv-builder")
SKILL_ANSWER_BANK = os.path.join(SKILL_DIR, "answer_bank.md")
SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")

RENDER_TEMPLATES_PY = os.path.join(SETUP_DIR, "render_templates.py")
SETUP_SH = os.path.join(SETUP_DIR, "setup.sh")
GENERATED_DIR = os.path.join(SETUP_DIR, "generated")

APP_STATE_FILE = os.path.join(APP_DIR, "app_state.json")
JOBS_DIR = os.path.join(APP_DIR, "jobs")
ACTIVITY_FILE = os.path.join(APP_DIR, "activity.ndjson")

# "Fill my profile from my CV" job (see app/lib/jobs.py's cv_extract kind) — its one
# output file, overwritten on every run and deleted once harvested into app_state.json.
CV_EXTRACT_OUTPUT_YAML = os.path.join(JOBS_DIR, "cv_extract_output.yaml")

# MULTI-001: when JOBAPPLY_DATA is set (absolute path), per-user data lives under it.
# Templates (profile.template.yaml, qa_bank.template.md), scripts and SKILL_DIR stay at
# their repo locations — only the per-user DATA moves. Unset = legacy layout: everything
# above keeps its repo location exactly as before (zero behavior change).
if DATA_ROOT:
    PROFILE_YAML = os.path.join(DATA_ROOT, "profile.yaml")
    USER_ENV = os.path.join(DATA_ROOT, "user.env")
    APPLICATIONS_DB = os.path.join(DATA_ROOT, "applications.db")
    RUN_CONFIG = os.path.join(DATA_ROOT, "run_config.txt")
    COSTS_CSV = os.path.join(DATA_ROOT, "output", "costs.csv")
    QA_BANK_MD = os.path.join(DATA_ROOT, "qa_bank.md")
    CV_DATA_DIR = os.path.join(DATA_ROOT, "data")
    APP_STATE_FILE = os.path.join(DATA_ROOT, "app", "app_state.json")
    JOBS_DIR = os.path.join(DATA_ROOT, "app", "jobs")
    ACTIVITY_FILE = os.path.join(DATA_ROOT, "app", "activity.ndjson")
    # Re-derived from the relocated JOBS_DIR (was already JOBS_DIR-relative above).
    CV_EXTRACT_OUTPUT_YAML = os.path.join(JOBS_DIR, "cv_extract_output.yaml")
    # MULTI-002b: the cv-builder skill's two personal files follow the shadow rule like
    # every other engine doc (render_templates.py writes SKILL.md to shadow(); the
    # answer bank is per-user personal data). The repo skill dir keeps the tracked
    # tools (template.html, render.sh) and gets SKILL.md/answer_bank.md as bind_user.sh
    # symlinks for everything that works relative to the repo. Nothing pinned these to
    # the repo path under a data root before, so no behavior changes when unset.
    SKILL_DIR = shadow(".claude/skills/cv-builder")
    SKILL_ANSWER_BANK = os.path.join(SKILL_DIR, "answer_bank.md")
    SKILL_MD = os.path.join(SKILL_DIR, "SKILL.md")

LAUNCH_AGENTS_DIR = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")

# Chrome can be installed system-wide or per-user; both count as "installed".
CHROME_APP_CANDIDATES = (
    "/Applications/Google Chrome.app",
    os.path.join(os.path.expanduser("~"), "Applications", "Google Chrome.app"),
)
