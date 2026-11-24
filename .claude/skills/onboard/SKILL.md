---
name: onboard
description: Guided, conversational onboarding for the LinkedIn auto-apply pipeline — turns a naked checkout of this repo into a working prepare-mode application pipeline. Interviews the user for identity/authorization/compensation, background stories, and search strategy; writes profile.yaml and qa_bank.md; builds a first CV via cv-builder; bootstraps the DB; generates (but never installs) the launchd schedule; connects LinkedIn and optionally Gmail; ends with a live prepare-mode validation round. Use when the user asks to set up, onboard, get started, or configure this project, types /onboard, or on first-ever interaction when claude_applicator/profile.yaml doesn't exist yet.
---

# onboard — turn a naked checkout into a working pipeline

You are running the **staged interview** that takes this repo from "just cloned" to "a
working `MODE: prepare` application pipeline for one real person." You are talking to a
human, live, in this session — not writing a script for someone else to run unattended.
Go stage by stage, in order; each stage builds on data the previous one collected.

## Cross-cutting rules — read before you start

- **Never invent an answer the user didn't give.** This whole pipeline's credibility rests
  on the answer-bank contract: the apply agent and the writer tool can ONLY use what's in
  `profile.yaml` / `qa_bank.md`. If you write a plausible-sounding placeholder instead of
  asking, you've poisoned the well — a wrong nationality, a made-up employer, or an invented
  metric turns into a lie on a real job application later. When the user doesn't know, doesn't
  want to answer, or wants to skip something: **leave it empty** (`""`, `null`, `[]`, or omit
  the section). The engine is built to skip-and-log a field it can't fill rather than guess —
  an empty field costs a few unanswerable questions later; an invented one costs trust.
- **Plain language.** Many users setting this up are not developers. Don't assume familiarity
  with YAML, CDP, ATS, MCP, or launchd. Explain briefly in plain words the first time a term
  comes up, then you can use it. Ask one thing (or a small related cluster) at a time — don't
  dump the entire `profile.yaml` schema on someone in one message.
- **Pause/resume is fine.** The user may stop mid-interview and come back later — in this
  session or a new one. `profile.yaml` gets written incrementally as you go; that's fine as
  long as it's always valid YAML with no leftover `CHANGEME` in a field the user has actually
  answered. **At the start of every session, check what already exists** — `profile.yaml`,
  `qa_bank.md`, `claude_applicator/data/*.pdf`, `applications.db`, a rendered plist in
  `setup/generated/` — and pick up from the first incomplete stage rather than re-asking
  everything. Tell the user plainly what you found and where you're resuming.
- **You never touch credentials or click submit on the user's behalf during LinkedIn/Gmail
  connect** — those are hard rules, called out again in their stages below.
- **You never run `launchctl`** or copy anything into `~/Library/LaunchAgents` — `setup.sh
  --schedule` only renders files and prints the commands; the user runs them.
- Work from the repo root (the folder Claude Code was opened in). All paths below are relative
  to it unless stated otherwise.

---

## Stage 0 — prerequisites check

Run:

```bash
bash setup/setup.sh --check
```

Read the output with the user. Lines are `PASS` / `WARN` / `FAIL` / `INFO`:

- Any **FAIL** is a hard blocker (missing `claude` CLI, `python3`, PyYAML, `sqlite3`, Google
  Chrome, or `npx`) — help the user fix it (usually one install command) and re-run `--check`
  before moving on. Don't proceed to Stage 1 with a FAIL outstanding.
- A **WARN** on install location means the repo folder sits under `~/Desktop`, `~/Documents`,
  or `~/Downloads` — macOS silently blocks background (launchd) processes from reading those
  folders, which would make the daily schedule fail invisibly. Tell the user plainly: move the
  whole repo folder somewhere else under their home folder (e.g. `~/GitHub/`) before Stage 6,
  then re-open Claude Code there. It's fine to keep interviewing from the current location and
  fix this before scheduling, but don't let them forget.
- The **INFO** line about `profile.yaml` just tells you whether this is a fresh start or a
  resume — use it to decide where to pick up.

---

## Stage 1 — core identity interview

Collect the following, explaining each policy in plain words as you go. Write answers into
`claude_applicator/profile.yaml` as you collect them (copy the structure from
`claude_applicator/profile.template.yaml` if the file doesn't exist yet — every field there
has a comment explaining what it's for; lean on those comments rather than re-deriving the
schema from scratch).

1. **Name.** Full legal name as it should appear on applications and CVs.
2. **Email(s) — explain the dual-routing policy plainly:** the agent uses one address for
   LinkedIn/Easy Apply forms (`identity.email`) and a second for everything else — external
   company application pages, and any new account it signs up for (`identity.email_external`).
   The reason: LinkedIn locks Easy Apply to the address on your LinkedIn account, and having a
   separate address for external sites means verification emails/replies land somewhere you
   actually read. **Offer the one-address option** — if the user only has/wants one email,
   the same address goes in both fields; that's fully supported, just say so.
3. **Phone.** Used on most forms; ask for it in a normal-looking international format.
4. **Links (all optional).** Portfolio site, GitHub, their own LinkedIn profile URL. Skip
   any they don't have — leave blank, don't guess a URL pattern.
5. **Location: city/country, and base area vs. relocation.** Ask their current city and
   country, then separately: are they relocating? If yes, where to and roughly when
   (`location.relocating_to` / `location.relocation_date`) — and explain that this also means
   their search locations later (Stage 3) should target the destination, not where they live
   today. Then ask the HARD gate questions, explaining what they control:
   - `location.must_be_based_in` — the area a role's remote work must be workable from. Leave
     null/empty for "anywhere" (no geographic restriction on remote roles).
   - `location.onsite_hybrid_cities` — cities where they'd actually accept a hybrid/onsite
     role (commute or already-planned move). Empty list = never accept onsite/hybrid.
   - `location.willing_to_relocate` — true only if they'd accept an offer that requires moving
     somewhere not already on the above list.
6. **Work authorization, nationality, and the citizenship-gate policy — explain this one
   carefully, it trips people up:**
   - `work_authorization.authorized_in` — the country/area(s) they can legally work in today.
   - `work_authorization.nationality` — their real, truthful nationality. This is a **DATA
     field**: whenever a form has an actual Nationality dropdown or asks for a passport
     country, this value is what gets typed, always truthfully, no matter anything else below.
   - **The gate-vs-data distinction (plain-language explanation to give the user):** some
     application forms ask a *different kind* of question that only LOOKS like a nationality
     question — e.g. "Are you a citizen of `<country>` or other?" or "EU citizen: yes/no" —
     where the real thing being tested is "does the employer need to do anything special to
     hire you (sponsorship, visa paperwork)?", not your actual passport. If the user holds
     full, unrestricted work rights in that country WITHOUT being a citizen (e.g. via a
     permanent residence permit), they may choose to treat that style of gate question as a
     "no employer action needed" proxy and let the agent pick the citizen-coded answer on
     THOSE specific gate-style questions — while any true data field (an actual passport/
     nationality field) always stays 100% truthful regardless. This is `work_authorization.
     citizenship_gate_pick_citizen` (default `false` — only set `true` if the user understands
     and actively wants this). If they're unsure, default to `false` and move on; it's a
     nuance they can revisit later.
   - `work_authorization.visa_sponsorship_required` and `work_permit_type` /
     `work_permit_note` — ask plainly: do they need an employer to sponsor a visa/work permit?
     What's their permit called, and is there anything they'd want stated in their own words
     if a form asks for a free-text explanation?
7. **Compensation — explain the secrecy rule up front, before asking for the number:**
   say plainly that the `floor` (their real walk-away minimum) is **never typed into any
   application, ever** — it exists only so a later automation (or the user themselves) knows
   the line, and it never appears in the config's meaning to the agent as a value to disclose.
   What actually gets typed are two SEPARATE fields the user approves themselves:
   - `compensation.forced_single_number` — what to type when a form forces one exact number.
   - `compensation.range_or_text_field` — what to type when a form takes a range or free text
     (e.g. `"65,000-72,000"`).
   Collect all of `currency`, `floor` (reassure: never shown on a form), `target`, `range_low`/
   `range_high`, then ask them explicitly to state the `forced_single_number` and
   `range_or_text_field` values they're comfortable with the agent actually typing — these
   don't have to equal the floor or target, they're the user's own choice of what to show.
8. **Earliest start date + notice period.** Ask both — `screening.earliest_start_date` (a
   real date the user would actually start) and `screening.notice_period` (in their own words,
   e.g. "4 weeks notice" or "available immediately").

Save progress to `profile.yaml` as you go (it's fine if later sections are still `CHANGEME` —
just don't leave anything the user HAS already answered unfilled).

---

## Stage 2 — background long-form interview → `qa_bank.md`

Explain the point of this stage before diving in: **this file is the entire universe of
facts the "writer" tool can use** when a job application asks a custom question or wants a
cover letter — it is not allowed to invent anything not in this file, so a richer bank means
more questions get answered instead of skipped. Everything in it must be literally true — no
rounding a number up, no manufacturing a metric to sound better.

Interview the user in whatever order feels natural, covering:

- **Employers** — each one: real dates, real title, and (push for this) what they *actually*
  did day to day, not resume-speak — concrete tasks, concrete outputs, real numbers wherever
  they have them (team size, revenue, users, % improvement, time saved).
- **Projects** — anything they built (work, side, open source), what it does, the stack, and
  real traction if any — or an honest "no metric, built to learn X" if there isn't one.
- **Stories** — 3-6 Challenge → Action → Result stories in plain language, the kind that
  answer "tell us about a time you...". Push for a real number in the Result when one exists.
- **Technical stack, in depth** — not just a list: how deep, on what, how comfortable.
- **Languages / fluency** — explicit per-language statements (this is what the writer's
  language-matching rule reads to decide whether it's safe to write a cover letter in that
  language).

Write the answers into `claude_applicator/qa_bank.md`, following the structure and headers in
`claude_applicator/qa_bank.template.md` (copy it as your starting skeleton if the file doesn't
exist yet). Two things to actively enforce as you write:
- **No invented numbers.** If the user gives you a vague claim, ask for the real number or
  write it down as genuinely unquantified — never round up or estimate on their behalf.
- **Write like the user talks.** Capture their own phrasing and voice where you can, rather
  than smoothing everything into generic corporate language — that's what makes the writer's
  later output sound human. If something needs careful framing (an NDA'd client, a sensitive
  gap), use the bracketed-annotation convention explained at the top of the template
  (`[FirstName: ...]` — an instruction to the writer, never quoted in its output).

This stage can be long. It's fine to do it across multiple sittings — tell the user that
explicitly, and pick it back up next session from whatever sections are still thin or missing.

---

## Stage 3 — search strategy

Ask what roles they're targeting (title(s) / role family), then turn that into
`job_search.search_queries` in `profile.yaml`: **2-6 entries**, each
`{query, location, filters, resume}`. Explain:

- `query` — the LinkedIn keyword search term.
- `location` — a city, country, or "Remote"; free choice, and **can be where they're moving
  to, not where they live today** (see Stage 1's relocation answer — point queries at the
  destination if they're mid-move).
- `filters` — a short phrase the engine maps to LinkedIn's real filters: `"past 24 hours"` /
  `"past week"` for date, and `"+ remote"` to add the remote-workplace filter (omit "remote"
  on purpose for a local/hybrid-catching query).
- `resume` — which CV file this query uploads by default (Stage 5 fills in the real path;
  it's fine to leave a placeholder path now and correct it once the CV exists).

Then collect:
- `exclude_title_keywords` — the ONE hard title-level filter (seniority words like senior,
  lead, principal, manager, intern, junior, graduate, ...). Ask what seniority band they're
  targeting and build the list from that — empty means no seniority filter at all.
- `cv_by_title` — title-keyword overrides that pick a specific CV regardless of which query
  surfaced the job (e.g. "data scientist" -> a Data Science CV). Order matters — more specific
  keywords before broad catch-alls. Fine to leave this section thin or empty until Stage 5
  produces more than one CV.

---

## Stage 4 — write `profile.yaml` and render

By this point `profile.yaml` should be substantially filled from Stages 1 and 3. Do a pass:

1. Confirm the file is present at `claude_applicator/profile.yaml` and is valid YAML — every
   REQUIRED field (see the comments in `profile.template.yaml`: identity name/emails,
   location city/country, the whole `work_authorization` section, compensation's forced/range
   fields, `job_search.search_queries`) has a real value, not the template's placeholder text.
   A quick check: `grep -n CHANGEME claude_applicator/profile.yaml` — any match on an
   **active line** (not a `#`-commented example) means something the user already discussed
   didn't make it into the file; fix it. Matches inside commented-out example lines are just
   template documentation and are fine to leave or delete.
2. Run:
   ```bash
   python3 setup/render_templates.py
   ```
   Confirm it exits cleanly (prints `[doc] ... wrote ->` for all four engine docs and
   `[env]` / `[mail]` lines, no `ERROR:`). If it errors on a missing/empty required field,
   that's exactly the field to go back and ask about — don't work around it by inventing a
   value.

---

## Stage 5 — first CV

The apply agent needs at least one CV on file before it can do anything. Two grounding files
are involved here, and they're NOT the same file — say this plainly if the user is confused:
`qa_bank.md` (Stage 2, used by the *writer* tool for custom questions/letters) and
`.claude/skills/cv-builder/answer_bank.md` (used by the *cv-builder* skill specifically). You
don't need a second interview for the CV — derive `answer_bank.md` from the same facts already
collected in Stages 1-2 (profile.yaml + qa_bank.md), reformatted the way the cv-builder skill's
instructions expect, if that file doesn't already exist.

Then invoke the **cv-builder** skill to build a role-tailored CV grounded in that material, for
the primary role family from Stage 3. Follow its own instructions for house style, grounding,
and the no-fabrication rule (same contract as everywhere else here: only real facts). Once
approved by the user:

1. Copy the rendered PDF into `claude_applicator/data/` (this folder is gitignored).
2. Set `resume_file` in `profile.yaml` to that path.
3. Update the `resume:` field of the relevant `job_search.search_queries` entries (and any
   `cv_by_title` mapping this CV corresponds to) to point at the real file.
4. Re-run `python3 setup/render_templates.py` if `profile.yaml` changed.

Building more than one CV variant (for different role families from Stage 3) is welcome here
or later — one is the minimum to proceed.

---

## Stage 6 — mechanics: DB, output dir, and the daily schedule

Bootstrap the local state:

```bash
bash setup/setup.sh --init
```

This creates `claude_applicator/output/`, creates `claude_applicator/applications.db` from
`schema.sql` **only if it doesn't already exist** (an existing DB is never touched — if you
see "already exists — leaving it untouched", that's expected on a resume, not an error), and
re-renders the engine docs/config now that `profile.yaml` is complete.

Then ask the user what local time they want the daily run to fire (a sensible default to
suggest: early morning, before their day starts — the engine's own default active window is
05:00-20:00, see `run_config.txt`). Run:

```bash
bash setup/setup.sh --schedule HH:MM
```

Explain plainly what this does and doesn't do: it **generates** two launchd plist files under
`setup/generated/` (one for the daily apply round, one for a 09:00 health check) and prints
the exact `cp` + `launchctl load` commands to install them — **it does not install or load
anything itself.** Walk the user through reading the printed commands, then have THEM run the
`cp` and `launchctl load` commands in their own terminal. If the script printed a `pmset`
pre-wake suggestion (relevant if their Mac sleeps overnight), mention it's optional but
recommended, and that it needs `sudo` once.

---

## Stage 7 — LinkedIn connect

**Hard rule, state it plainly to the user: you (the onboarding skill) never see, ask for, or
touch LinkedIn credentials.** The connection is entirely manual, in a real browser window.

```bash
bash claude_applicator/launch_chrome.sh
```

This opens a dedicated Chrome profile (`Chrome-Agent`) with the automation port enabled. Tell
the user: **sign in to LinkedIn by hand in that window**, exactly as they normally would
(their password, their 2FA if any) — nothing automated touches this step. Once they've signed
in, verify the connection by re-running the same command:

```bash
bash claude_applicator/launch_chrome.sh
```

Read its output with the user — it prints each open tab and ends with either `OK: logged in`
(the feed loaded — connection verified) or a note that it's not on `/feed` yet (still on the
login page — have them finish signing in and re-run once more).

---

## Stage 8 — Gmail (OPTIONAL — fully skippable)

Ask the user if they want this before explaining it in depth, since it's optional. If they'd
rather skip it, skip cleanly and move to Stage 9 — explicitly mention the pipeline runs fine
without it via `JOBAPPLY_SKIP_SWEEPS=1` (used automatically in Stage 9's validation round when
Gmail was skipped).

**What it unlocks, in plain terms:** without Gmail, some external application sites that
require email verification (a code or a confirmation link) can't be completed automatically —
those get skipped and logged instead. With Gmail connected, the pipeline can (a) read those
verification emails to finish sign-ups, and (b) run a nightly "mail agent" that files
application-related mail under a label and records interview/rejection/offer outcomes into the
database automatically, so the user's inbox stays organized and outcome tracking works without
manual effort.

**What it requires:** an OAuth client file (`credentials.json`) placed in
`claude_applicator/inbox_reader/` — this comes from the project maintainer (it's a shared,
already-published Google OAuth app; the user's own token and inbox data never leave their
machine). If the user doesn't have this file:

- Tell them plainly to ask the maintainer for it, then skip this stage for now — it can be
  completed later by re-running this stage.

If `claude_applicator/inbox_reader/credentials.json` IS present:

1. Check for a Python virtual environment at `claude_applicator/inbox_reader/.venv`. If it
   doesn't exist yet, create one and install the Gmail dependencies:
   ```bash
   cd claude_applicator/inbox_reader && python3 -m venv .venv && \
     .venv/bin/pip install --upgrade pip && \
     .venv/bin/pip install google-auth google-auth-oauthlib google-api-python-client
   ```
2. Run the one-time interactive consent flow (opens a browser; the user signs into their OWN
   Gmail and accepts). Two scopes are used for two separate purposes, with separate cached
   tokens (per RUN.md conventions) — walk the user through both:
   ```bash
   cd claude_applicator/inbox_reader
   .venv/bin/python gmail_auth.py --readonly   # verification-link/code reader (F-2)
   .venv/bin/python gmail_auth.py --modify     # nightly mail organizer/sweep (LOOP-003)
   ```
   Google will show an "unverified app" interstitial the first time (expected for this kind of
   shared small-scale OAuth client) — the user clicks Advanced -> Go to (app name) to proceed.
   Each command writes its own token file (`token.json` / `token_modify.json`) and prints `OK`.
3. Confirm both token files exist in `claude_applicator/inbox_reader/`. That's the whole setup
   — no further config; the mail label name comes from `profile.yaml`'s `mail_label` (already
   rendered into `inbox_reader/user_mail.json` in Stage 4/6).

---

## Stage 9 — validation round

Before running anything, confirm the safety default is in place:

```bash
grep '^MODE:' claude_applicator/run_config.txt
```

This should read `MODE: prepare` (the product default — nothing gets submitted in this mode).
If a previous session flipped it, that's the user's call to make later, not now — for the
FIRST validation round it should be `prepare`.

Run a small, real, one-query test round with the user watching:

```bash
JOBAPPLY_QUERY_COUNT=1 JOBAPPLY_SKIP_SWEEPS=1 bash claude_applicator/run_scheduled.sh
```

(Add `JOBAPPLY_SKIP_SWEEPS=1` only if Gmail (Stage 8) was skipped — it disables the pre/post
mail sweep steps, which need a working Gmail connection. Drop it if Gmail is connected, so the
full round including the mail agent gets exercised.) `JOBAPPLY_QUERY_COUNT=1` limits this test
to the first search query only, so it's quick to review.

Once it finishes, review the result together with the user:
- Read the run's log under `claude_applicator/output/` (the wrapper prints the exact path).
- If an application reached `prepared` status, look at it together — either the actual
  submit-ready page in the Chrome-Agent window (if the tab is still open) or the DB row
  (`./db_report.sh` from `claude_applicator/` prints a human-readable summary).
- Talk through anything that looked wrong: a field filled incorrectly, a job that shouldn't
  have passed the relevance gate, a company that should be blocklisted
  (`job_search.company_blocklist` in `profile.yaml`). Fix `profile.yaml` and re-render
  (`python3 setup/render_templates.py`) as needed, then it's fine to run another small test
  round to confirm the fix.

**Explain graduation plainly, and leave the decision to the user:** the pipeline stays in
`MODE: prepare` — reviewing and hand-submitting every application — for as long as they want.
When they've reviewed enough prepared applications to trust the agent's judgment on their own
profile and search queries, they flip `MODE:` to `live` themselves, directly in
`claude_applicator/run_config.txt` (a one-line edit — no script needed). Nothing else changes
automatically; that edit is the entire graduation step, and it's entirely the user's call.
