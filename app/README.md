# app/ — local setup web app

A local FastAPI app (served by uvicorn) that replaces the chat-based onboarding: fill in
forms in a real browser, deterministic Python writes `claude_applicator/profile.yaml` and
the other config files. Claude is only ever used as an explicit background job you start
yourself on the CV & background page (generating the answer bank or a CV) — see
`app/lib/jobs.py`. Every other page is pure deterministic Python; nothing runs Claude
implicitly.

**Home screen:** three plain-language option cards — "Your profile & CV", "Connect to
Claude", "Connect to LinkedIn" — each showing done/not-done and a single button to the
page that finishes it, plus small "Advanced" links to Schedule and Dashboard underneath.
No technical checklist or step counter is shown; the database and other local setup
files are created automatically and silently the first time the app starts (see
"Auto-setup" below), so there's nothing to initialize by hand.

**Start it:** double-click `JobApply.command` at the repo root, or run
`bash app/run_app.sh` from anywhere. First run creates `app/.venv` and installs
`fastapi` + `uvicorn` + `jinja2` + `python-multipart` + `pyyaml` into it.

**Where it lives:** `http://127.0.0.1:8765`, bound to localhost only.

**Auto-setup:** on startup, if `claude_applicator/applications.db` doesn't exist yet,
the app runs `setup/setup.sh --init` for you (idempotent — only creates what's missing,
never touches an existing database) and logs the result to stdout. This only surfaces
on the home page if it fails.

**State:** `app/app_state.json` (gitignored) — which setup steps are done, plus the
last schedule/probe settings. Never holds passwords; the app re-derives most status
from the real filesystem/PATH, not from this file.
