# automated_job_application

A Claude-Code-driven LinkedIn auto-apply pipeline: it searches for roles matching your
criteria, fills out applications from your own answer bank (never inventing facts), and
records every outcome — so you get real market-response data instead of a manual slog.

> **Status:** a private project shared with a small circle of friends, non-commercial, not a
> product. It automates actions on your own LinkedIn account, which LinkedIn's User Agreement
> (§8.2) does not permit — use is at your own risk, and daily volumes are capped low on purpose.

## Setup

**Prerequisites (one-liner):** macOS, [Claude Code](https://claude.com/claude-code) installed
and logged in, Python 3, the `sqlite3` CLI, Node.js (for `npx`), and Google Chrome. Check them
all at once with:

```bash
bash setup/setup.sh --check
```

**Onboarding:** open Claude Code in this folder and ask it to set up / onboard (or run
`/onboard`). The `onboard` skill drives the whole process conversationally — it interviews
you, then does the mechanical work itself:

- your identity, contact routing, work authorization, and compensation numbers (the
  compensation *floor* is never typed anywhere — only a target/range you approve is)
- a long-form background interview that becomes `claude_applicator/qa_bank.md`, the grounded
  fact/story bank the writer tool draws from for custom questions and cover letters (it can
  only use what's in that file — nothing is ever invented)
- your search strategy: which roles, which locations, which CV for which title
- writes `claude_applicator/profile.yaml` and renders the live engine docs from it
- builds your first role-tailored CV via the `cv-builder` skill
- bootstraps the database and output folder (`setup/setup.sh --init`)
- generates your daily-run launchd schedule (`setup/setup.sh --schedule HH:MM`) — you review
  and install it yourself; onboarding never runs `launchctl` for you
- walks you through connecting LinkedIn (you sign in by hand, once, in a dedicated Chrome
  window — the pipeline never sees or stores your credentials) and, optionally, Gmail
- ends with a live validation round in **prepare mode**, so you can review a real prepared
  application before anything is ever actually submitted

**Prepare → live graduation.** Every fresh install starts in `MODE: prepare`
(`claude_applicator/run_config.txt`): the agent fills out and walks every application to the
final Submit screen, then stops for you to review and send it by hand. Nothing is ever
auto-submitted until you flip `MODE:` to `live` yourself, once you trust what the pipeline is
producing on your own profile and search queries.

**Manual / advanced use.** `setup/setup.sh` also works stand-alone, subcommand style, if you'd
rather drive the mechanics yourself instead of going through the `onboard` skill:

| Command | What it does |
|---|---|
| `bash setup/setup.sh --check` | Verify prerequisites (safe, read-only). |
| `bash setup/setup.sh --init` | Create `claude_applicator/output/`, bootstrap `applications.db` from `schema.sql` (only if it doesn't already exist), render engine docs/config from `profile.yaml`. |
| `bash setup/setup.sh --schedule HH:MM` | Render the launchd plists for a daily run at the given time into `setup/generated/` and print the install commands (never installs or loads them itself). |
| `python3 setup/render_templates.py` | Re-render the engine docs (`CLAUDE.md`, `WRITER.md`, `MAIL_AGENT.md`, cv-builder's `SKILL.md`) and generated config from `claude_applicator/profile.yaml` — run this again any time you edit `profile.yaml` by hand. |

See `claude_applicator/profile.template.yaml` for the full field-by-field schema (or
`setup/test_profile.yaml` for a fully-filled fictional example), and `claude_applicator/RUN.md`
for day-to-day operating commands once you're up and running.
