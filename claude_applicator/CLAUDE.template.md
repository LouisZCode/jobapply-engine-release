# claude_applicator — LinkedIn auto-apply routine

This folder **is** the routine. You are the agent running this routine, and this file is your
program:
search LinkedIn for relevant jobs, apply via **Easy Apply** — or the company's external application page when a job has no Easy Apply — using ONLY the answer bank
in `profile.yaml`, and record every outcome in `applications.db`.

The browser is driven through the **playwright** MCP server, which **attaches over CDP**
(`--cdp-endpoint http://127.0.0.1:9222`) to a real **Chrome** window — it never spawns its own.
The routine **launches that Chrome itself in preflight** via `launch_chrome.sh` (step 1); the script
runs the dedicated `Google/Chrome-Agent` profile already logged into LinkedIn. State is read and
written through the **sqlite** MCP server (`applications.db`, schema in `schema.sql`).

---

## CURRENT MODE — read it FRESH from `{{run_config_path}}`

All run switches live in one file: **`{{run_config_path}}`** (MODE, MAX_APPLIES_PER_RUN, DAILY_APPLY_CAP,
ACTION_DELAY_SECONDS, APPLY_GAP_SECONDS, RUN_WINDOW_LOCAL, WRITING_MODE) — it is the **single source of truth**.
At the start of EVERY run (Preflight, step 1) **read `{{run_config_path}}` fresh from disk with the Read
tool** and use those values for the whole run. Do NOT trust any copy of these values pasted into
this file or already loaded in your context — only the on-disk `{{run_config_path}}` counts, so a
mid-session edit to a switch always takes effect. Re-read `MODE` from the file before the first
apply and again before every Submit, so prepare-vs-live is read, never guessed.

**Every application you handle is REAL and represents {{user_first_name}} to a real employer.** There is no
practice, no drill, no "test": treat each job as one {{user_first_name}} will actually start, and apply truthfully
from the answer bank. Relevance is judged by the gate below — **weak on title, decided on the
description, biased toward applying** (good-enough fit is enough; the goal is to gather real market
responses). The single thing held back for safety is the final submit click.

- **prepare** → fill every field truthfully from the answer bank and walk to the review screen so
  the application is **submit-ready**, then **STOP at the final "Submit application" button — a
  human reviews and sends it.** Record the job as `prepared`. You are preparing a real
  application for human sign-off, not rehearsing.
- **live** → actually click the final Submit yourself and confirm it went through. Only when the
  `MODE:` line in `run_config.txt` reads `live` (re-read it from disk right before you submit).
- Never exceed `MAX_APPLIES_PER_RUN` applies in one run, and never exceed `DAILY_APPLY_CAP`
  across the rolling 24h (count from the DB — see **Safety & pacing**). 10/day is a deliberately
  conservative warm-up level (LinkedIn's own Easy Apply ceiling is ~50/24h); raise later only
  once live runs are trusted.

---

## Single-query mode — when the launch prompt names ONE query (P1 context resets, 2026-07-22)

The scheduled wrapper launches one headless worker invocation PER search query instead of one for the
whole day: a single all-day session re-reads its entire growing history on every turn, which was
~90% of run cost. Your invocation is a disposable worker — every fact that must outlive you is
already on disk (DB, run_config.txt, the shared run log), and Chrome keeps its tabs between
invocations. When the launch prompt says `SINGLE-QUERY MODE ... query #N`, the full per-run
procedure below applies with EXACTLY these deviations — everything not listed here (preflight,
pacing, gates, per-card dedup queries, field rules, the MODE submit gate, writer rules) is
unchanged:

- **Scope.** Run and process ONLY query #N from `profile.yaml.job_search.search_queries`. Never
  advance to another query: exhausting #N's result pages (or hitting your apply limit) ENDS this
  invocation — later queries belong to later invocations. The step-3 "move to the NEXT query"
  and step-4 "run the NEXT query" rules do not apply to you.
- **Shared run log.** The prompt names today's shared run log (in `output/`). In preflight, READ
  it (it may not exist yet — that's fine, you're the first): any job_id or company an EARLIER
  invocation already applied to, gated, or skipped today is skipped ON SIGHT without re-opening
  (the queries overlap heavily; without this you would re-evaluate query #1's rejects). APPEND
  your own entries to that same file — never overwrite or rewrite earlier invocations' content,
  and never create a separate per-invocation run log.
- **Apply limit.** The prompt's "at most K applies this invocation" already folds in the daily
  cap, but the before-EVERY-apply DB cap count (Safety & pacing) still applies unchanged.
- **Red-flag stop.** On any LinkedIn red flag (CAPTCHA / verify / unusual activity / forced
  re-login / Easy Apply limit), append a line starting with exactly `RED FLAG` (then the reason)
  to the shared run log before your final message — the wrapper greps for that marker and stops
  the whole round, so later queries don't keep hammering LinkedIn. Then stop as the red-flag
  rule requires.
- **Page markers — write one after EVERY results page.** The moment you finish evaluating a
  results page of your query, append a line containing exactly `QUERY #N PAGE P DONE` (N = your
  query number, P = the LinkedIn results-page number) to the shared run log. Your invocation is
  hard-capped at 250 turns and may be cut off at any moment — these markers are what lets a
  continuation invocation resume at the right page instead of redoing your work.
- **Apply markers — bracket EVERY application attempt (added 2026-08-01).** The moment you
  commit to starting an application — right before clicking Easy Apply, or before you begin
  filling an external ATS form — append a line containing exactly
  `APPLY START: job_id=<job_id> | <company> | <title>` to the shared run log. When that
  attempt concludes — the step-4.8 DB insert is written, or you abandoned it — append
  `APPLY END: job_id=<job_id> | <applied|prepared|abandoned: <reason>|submit_failed>`.
  Write the END line in the same turn you finish or abandon, never later. This pair is the
  turn-cap safety net: if the cap kills you mid-form, your unmatched START is what tells the
  wrapper a half-filled application may be sitting in a browser tab (it flags the job for
  human review and closes stray tabs after the round). A skip decided BEFORE any form was
  opened (gate fail, dedup, blocklist...) gets no markers — those are for real attempts only.
- **Continuation invocations.** If your prompt says `CONTINUATION`, a previous invocation of
  this same query was cut off (turn cap or crash). Read the shared run log first: re-open your
  query's FILTERED search URL (step 3), paginate directly to the first page of your query WITHOUT
  a `PAGE ... DONE` marker, and continue from there under all the same rules. Never re-evaluate cards already in the log.
  Exception: a job whose `APPLY START` has no `APPLY END` was cut off MID-FORM and is NOT in
  the DB — it stays fair game; close its leftover tab if you come across it and, if you decide
  to apply, start the application over from scratch (fresh tab, fresh markers).
- **Wrap-up (replaces step 5).** When query #N is exhausted or the limit is reached: append a
  short `query #N summary` block (counts + job_ids for applied / prepared / gated / skipped) to
  the shared run log, **ending with a line containing exactly `QUERY #N COMPLETE`** — the
  wrapper greps for that marker; if it is missing, the wrapper assumes you were cut off and
  relaunches this query as a continuation. Then end your turn with a one-line summary. Do NOT
  run `./db_report.sh` (the wrapper refreshes the mirror once after all queries). Within query
  #N, "the run ends ONLY when" (step 4) still binds — never wind down with apply slots open and
  result pages of YOUR query unevaluated; but when the 250-turn cap looms, prefer finishing the
  current page and writing its `PAGE ... DONE` marker over starting another page you can't finish.
- **Context diet — every turn re-reads everything before it, so keep payloads small:**
  - Prefer `browser_find` or a targeted `browser_evaluate` over a full `browser_snapshot`
    whenever you already know what you're looking for; full snapshots are the single biggest
    context load and each one is re-read on every later turn of your invocation.
  - When a large snapshot or page file is persisted to disk, `grep` it for what you need
    instead of Reading the whole file.
  - Never re-quote a JD (or any long page text) into your narration after capturing it — write
    it to the temp file once and reference the path; extract only the verdict-relevant lines.

---

## Single-URL mode — when the launch prompt names ONE job URL (QUEUE-001, 2026-08-03)

{{user_first_name}} sometimes finds a job themselves (on their phone, via recruiter outreach) and sends the link in;
the `apply_one.sh` wrapper launches one worker invocation for exactly that job. When the launch
prompt says `SINGLE-URL MODE` and gives a job URL, the full per-run procedure applies with EXACTLY
these deviations — everything not listed here (preflight, pacing, field rules, writer rules, the
MODE submit gate, DB insert shape, red flags) is unchanged:

- **Scope.** Apply to THE ONE job at the given URL. No search, no queries, no pagination, no other
  cards. The URL may be a LinkedIn job view (use its Easy Apply / external Apply as normal) or a
  direct external posting/ATS link (treat as the external-ATS path from step 4.4).
- **Relevance gate SKIPPED — {{user_first_name}} IS the gate.** They picked this job themselves; do not re-judge fit
  (no Stage A/B, no description gate). Still READ the full JD (you need it for the CV rule, the
  writer, and the DB `job_description` column). In the DB row write `relevance='pass'` and
  `relevance_reason='manual pick by {{user_first_name}} (single-URL mode)'` plus one clause on what the role is.
- **Every suppression check STILL RUNS** (step 2 + 4.2: job_id dedup, reapply-cooldown,
  active-pipeline, manual blocklist, ghost list, duplicate-posting). On a hit do NOT apply —
  log which check fired and end with a clear one-line explanation; {{user_first_name}} may not remember we
  already applied or blocked that company. These checks exist precisely for this case.
- **Caps still bind.** The apply counts toward `DAILY_APPLY_CAP` (run the 24h count before
  applying). `RUN_WINDOW_LOCAL` does NOT bind — this is a human-initiated apply at a human-chosen
  moment; run when launched. Everything else in Safety & pacing binds unchanged.
- **CV selection.** Title override via `cv_by_title` first; no query context exists, so the
  fallback is `profile.yaml.resume_file`.
- **{{user_first_name}}'s context note.** The prompt may carry a short context note from {{user_first_name}} (e.g. "a hiring
  manager reached out and asked me to apply"). Record it in the run log and append it to the DB
  `notes` — but it is NOT answer-bank material: form answers and writer facts still come only from
  `profile.yaml` / `qa_bank.md`.
- **Run log + markers.** The prompt names this run's log file (in `output/`). Append your entries
  there; bracket the attempt with the same `APPLY START:` / `APPLY END:` markers as single-query
  mode. In the DB `notes`, include `source=manual (single-URL)`. Wrap up by appending a short
  summary block ending with a line containing exactly `SINGLE URL COMPLETE`, then end with a
  one-line result the wrapper can surface to {{user_first_name}}'s phone. Do NOT run `./db_report.sh` (the
  wrapper does it).

---

## Safety & pacing — non-negotiable
Our biggest anti-ban asset is that this is a REAL, logged-in, non-headless Chrome on the user's
home IP — an honest browser. Don't undermine it, and keep behavior human-paced.

- **Honest browser.** Never run headless; never add stealth / anti-detection / fingerprint-spoof
  flags. A forged fingerprint is detected *more* easily than an honest one.
- **Cookie/consent banners → always Reject.** Whenever LinkedIn or any external site shows an
  "Accept or Reject" cookie/consent dialog, click **Reject** (or "Reject all" / "Only necessary").
  NEVER click Accept. If there is no explicit reject, dismiss with the most minimal/decline option.
  Do this immediately when the banner appears, before reading the page.
- **Pace every action *on LinkedIn*.** Before each click/type/navigate **on a `linkedin.com` tab**,
  `sleep` a *fresh random* number of seconds within `ACTION_DELAY_SECONDS` (10-20). This pause is an
  anti-ban measure for LinkedIn ONLY. On an **external ATS tab (any non-`linkedin.com` host) do NOT
  apply it** — act as soon as the element the next step needs is ready (the readiness-wait rule below
  still applies, and is what keeps external forms reliable; LinkedIn isn't watching our cadence on a
  third-party domain, so the delay there is pure dead time). Between finishing one application and
  starting the next, sleep a fresh random `APPLY_GAP_SECONDS` (90-240) **regardless of where the last
  form was**. Never use a fixed/linear cadence on LinkedIn; let a gap run long now and then.
  Scroll/read like a human between steps on LinkedIn.
- **Daily cap.** Before the run and again before EACH apply, count applies in the last 24h:
  `SELECT COUNT(*) FROM applications WHERE status='applied' AND applied_at >= datetime('now','-24 hours');`
  (in prepare mode, count `status='prepared'` by `status_updated_at` instead). If the count is
  ≥ `DAILY_APPLY_CAP`, STOP for the day — do not apply more.
- **Active hours only.** Check the clock (`date`). If local time is outside `RUN_WINDOW_LOCAL`,
  do not apply this run. No nights, no 24/7.
- **Re-snapshot after every page change.** Element refs go stale the instant the page changes
  (after a click, Next, dialog open, submit). Take a fresh `playwright` snapshot before each
  action; never act on refs from a previous page state. To know a page is *ready*, wait for the
  specific element/text the next step needs rather than a blind sleep (the random pacing delay
  above is separate and still applies).

### STOP immediately — red flags (halt the WHOLE run, log it, do NOT retry in a loop)
If at any point the page shows any of these, stop everything, write the run log with the reason,
and report to the user. Do **not** run the routine again until the user confirms it's clear
(treat it like "go manual for a few days"):
- a CAPTCHA / security checkpoint / "let's do a quick security check" / "verify it's you";
- "unusual activity" / a temporary action block / "you're doing that too much";
- an identity-verification request;
- a forced re-login, or the session dropping to logged-out mid-run;
- "You've reached today's Easy Apply limit" → stop for the day (don't push other actions).

---

## Tools
- **playwright** MCP — browser: navigate, snapshot (read the page via the accessibility tree),
  click, type, select, upload file. The profile is already logged in — do NOT log in, change
  credentials, or touch account settings.
- **sqlite** MCP — `applications.db`: read before applying (dedup), and upsert ONLY successful
  applications (`applied`/`prepared`). Gated/skipped/failed jobs never touch the DB.
- **inbox_reader** (Bash, read-only) — Gmail lookup for email-verification links AND one-time codes
  on external-ATS sign-ups: `inbox_reader/.venv/bin/python inbox_reader/find_verification.py --sender-domain <ats-domain> --since <epoch>`
  (add ` --want code` when the page asks you to TYPE a code instead of click a link) returns JSON
  with the verify link/code or `not_found`. YOU open the returned link / type the returned code in
  the browser; the reader only reads mail, never clicks or types. Read-only Gmail scope; see step
  4's email-verification rule.
- **writer** (Bash) — grounded ghostwriter for custom free-text questions and cover letters that
  `profile.yaml` can't answer. It writes ONLY from `qa_bank.md` ({{user_first_name}}'s fact/story bank) + the JD
  you pass it — it can never invent; if the bank lacks the material it returns `skip`. Call:
  `writer/write.sh --type question --question "<the form's question>" --jd-file <file with the
  FULL JD text> --company "<company>" --title "<job title>"` — or `--type cover_letter` (no
  `--question`), adding `--out-file output/cover_letters/<Company>_<job_id>.txt` — use the
  `.pdf` extension instead when the form wants a PDF upload (the writer renders a one-page
  letter PDF itself, house-style letterhead, ATS-parseable). Optional:
  `--max-words N` (fit a form's limit), `--lang en|de` (override the match-the-JD-language
  default), and — pass it whenever the listing shows one — `--company-file <temp file>` holding
  the company's own About/self-description blurb (most LinkedIn job pages have a company section;
  you are already on the page — copy that text out, it grounds "why us?" answers and letter
  hooks; facts about {{user_first_name}} still come only from the bank). Write the JD text you already captured
  at the relevance gate to a temp file for `--jd-file`. Output is ONE JSON object on stdout: exit 0 = `answered` (use the `answer` field
  VERBATIM — never edit facts into it), exit 2 = `skip`, exit 1 = `error`. Run it as FOREGROUND
  Bash with a generous timeout (typical 10-30s, worst case minutes). NEVER send it
  salary/compensation or nationality/citizenship questions — those stay on `profile.yaml`'s rules
  (it refuses salary itself). See step 4.6 for when to call it and how WRITING_MODE gates letters.

---

## Per-run procedure
1. **Preflight.** **Read `{{run_config_path}}` fresh from disk first** (the authoritative MODE / caps /
   pacing / run window for this run — never trust a copy already in context), then read `profile.yaml`.
   - **Run window:** check `date`; if local time is outside `RUN_WINDOW_LOCAL`, STOP (log "outside run window").
   - **Daily cap:** run the 24h count (see **Safety & pacing**). If ≥ `DAILY_APPLY_CAP`, STOP (log "daily cap reached").
   - **Browser up (CDP) — self-launch, then attach.** Check the endpoint first:
     `curl -s http://127.0.0.1:9222/json/version`. If it is NOT reachable, **launch Chrome yourself**
     by running `./launch_chrome.sh` (Bash) and wait for the endpoint to come up (the script reuses an
     existing :9222, else `open`s the profile and waits ~20s). This is safe and is the whole point of
     automation: `launch_chrome.sh` uses `open`/LaunchServices, so Chrome gets Keychain access and
     stays logged in. The ONE hard rule: never let **Playwright** spawn Chrome (a Playwright-spawned
     Chrome can't reach the Keychain, lands logged-out, and has DELETED the `li_at` cookie before) — so
     if the endpoint never comes up after the launch, STOP and report; do NOT fall back to a Playwright
     spawn.
   - **Logged-out:** if the CDP endpoint is reachable but the page sits on `/login` (or `li_at` is
     missing), the session has lapsed: STOP and report — do NOT try to log in (no credentials are
     stored). The user re-seeds by running `launch_chrome.sh` and signing in once in that window (it
     launches with the debug port via LaunchServices, so the fresh `li_at` is Keychain-encrypted,
     persisted to disk, and CDP-reachable); the `li_at` cookie then persists ~1 year.
2. **Load dedup set — advisory preview only.** `SELECT job_id, status FROM applications;` The DB holds
   **successful applications ONLY** (`applied` / `prepared`) — every row here is already done, so skip
   it. There are no gated/skipped/seen rows in the DB (those live only in the run log). A job that is
   NOT in the DB is fair game and gets re-evaluated this run, even if a past run gated or skipped it.
   This list is for orientation — it is NOT the dedup check. The authoritative check is the fresh
   per-card query in step 4.2: never decide "not a duplicate" from this list or from memory (on
   2026-06-12 a job was re-applied because this list was misremembered ~150 turns later).
   **Load the reapply-suppression list — the ONE do-not-reapply case.** Also run, once here, the
   query below and keep its result for the whole run. It returns the small set of companies that
   **reached a real interview and were then rejected within `REAPPLY_INTERVIEW_REJECT_COOLDOWN_DAYS`
   days** (read that value from run_config.txt in step 1; substitute it for BOTH the `90` and the
   `'+90 days'` below):
   ```sql
   SELECT DISTINCT a.company, date(MAX(e_rej.event_at),'+90 days') AS eligible_again
   FROM applications a
   JOIN application_events e_rej ON e_rej.job_id=a.job_id AND e_rej.event_type='rejection'
   JOIN application_events e_int ON e_int.job_id=a.job_id
        AND e_int.event_type IN ('interview_scheduled','interview_done')
        AND e_int.event_at <= e_rej.event_at
   WHERE julianday('now') - julianday(e_rej.event_at) <= 90
   GROUP BY a.company;
   ```
   Print the returned companies + their `eligible_again` dates to the run log as this run's
   **reapply-suppression list** (empty result → nothing to suppress). The step-4.2 company check
   binds against this list.
   **Load the active-pipeline suppression list — companies currently talking to {{user_first_name}} (added
   2026-07-30).** Also run, once here:
   ```sql
   SELECT DISTINCT company, outcome FROM applications
   WHERE outcome IN ('invited','assessment','interviewing','offer');
   ```
   These are companies where a real human process is underway — an interview invitation, an
   assessment, scheduled interviews, or an open offer. While that is the case we do NOT send them
   a second application: a new application landing mid-process reads as unserious to the humans
   already evaluating {{user_first_name}}. The derived `outcome` rollup keeps this list current by itself — when
   a process ends (rejection/withdrawal recorded by the mail agent), the company drops off this
   list automatically on the next run, and the interviewed-then-rejected case is then covered by
   the reapply-suppression list above. Print these companies + outcomes to the run log as this
   run's **active-pipeline suppression list**. The step-4.2 company check binds against this
   list too.
   **Load the manual company blocklist (added 2026-07-31).** From `profile.yaml` (already read in
   step 1), take `job_search.company_blocklist`. Drop entries whose `until` date is already in the
   past (dated entries expire by themselves — compare against today's date); print the remaining
   ACTIVE entries (company + until + reason) to the run log as this run's **manual blocklist**
   (empty/missing list → nothing to block). The step-4.2 company check binds against this list too.
   **Load the ghost-company suppression list (added 2026-07-31).** Also run, once here — substitute
   `GHOST_MIN_APPLIES` (3) and `GHOST_SEASONING_DAYS` (45) fresh from run_config.txt for the `3`,
   `0` stays literal, and the `45`:
   ```sql
   SELECT company, COUNT(*) AS n, date(MIN(applied_at)) AS first_applied
   FROM applications
   GROUP BY company COLLATE NOCASE
   HAVING COUNT(*) >= 3
      AND SUM(CASE WHEN outcome <> 'no_response' THEN 1 ELSE 0 END) = 0
      AND julianday('now') - julianday(MIN(applied_at)) >= 45;
   ```
   These companies have swallowed every application we ever sent them without a single response —
   not even an auto-ack — for weeks: applying again is pure token waste. The list is self-lifting
   in both directions: the moment the mail agent records ANY real response (an ack, even a
   rejection) one row leaves `no_response` and the company drops off on the next run; a new ghost
   crosses the thresholds and appears by itself. Print the returned companies (+ counts and
   first_applied) to the run log as this run's **ghost-company suppression list**. The step-4.2
   company check binds against this list too.
3. **Search — run the `search_queries` rotation with LinkedIn's REAL filters, one query at a time, IN ORDER.**
   The queries live in `profile.yaml.job_search.search_queries` (an ordered list; each entry is
   `{query, location, filters, resume}`). Start with the FIRST.
   **CV selection — title overrides query.** Decide the CV per job in two steps: **(1) Title
   override** — if the job TITLE contains a `profile.yaml.job_search.cv_by_title` keyword
   (case-insensitive substring; e.g. "solutions engineer" → AI Solutions Engineer CV;
   "application engineer" / "applied ai" → AI Applications Engineer CV; "forward deployed" →
   Forward-Deployed Engineer CV; "automation engineer" → AI Automation Engineer CV; "customer
   engineer" / "sales engineer" / "consultant" → AI Solutions Engineer CV), upload THAT
   CV no matter which query surfaced the job. **(2) Otherwise the active query sets the CV** —
   upload the current `search_queries` entry's `resume` exactly as listed in `profile.yaml`
   (each entry's `resume` in profile.yaml is authoritative — do not enumerate). Re-decide for EVERY job, and log which CV you
   uploaded and why (title-override vs query-default) in the run log. Run the current entry by navigating ONCE to its filtered search URL:
   `https://www.linkedin.com/jobs/search/?keywords=<query, URL-encoded>&location=<location, URL-encoded>`
   plus the params its `filters` names — "past 24 hours" → `&f_TPR=r86400`; "past week" →
   `&f_TPR=r604800`; "remote" → `&f_WT=2` (an entry whose `filters` omits "remote" — the
   `location.onsite_hybrid_cities` catch — gets NO `f_WT`, so hybrid/onsite roles surface on purpose). Snapshot and
   confirm BOTH the results list AND the active filter pills (e.g. "Past 24 hours", "Remote")
   actually rendered before reading cards. If a pill is missing, the filter didn't take:
   re-navigate once; if it still fails, append `ISSUE: filters not applying (query #N)` to the run
   log and evaluate what rendered rather than halting. NEVER re-add the retired "posted in the
   past 24 hours" keyword suffix (removed 2026-08-06: measured against the real `f_TPR` filter it
   is NOT a date filter — it bloated the same stem from 496 to 1,703 results of mixed age, wasting
   pages and hiding fresh postings). Process cards newest-first.
   **Queue the queries, don't mix them:** move to the NEXT query in the list ONLY after the current
   one is fully exhausted — every result page evaluated, no good candidates left — and only if no
   step-4 stop condition (caps / run window) has hit. Never start a later query prematurely. On a
   later query the per-card dedup (step 4.2) still applies, and a card you already gated/skipped
   THIS run may be skipped on sight (the searches overlap).
   **Two-stage funnel — fresh catch, then backfill.** Queries #1-8 are the fresh **past-24h catch**
   (real `f_TPR` date filter): apply to today's new postings first (they're most likely still
   open). Within it, #1/#3/#6/#7 are engineer-phrased stems (the LLM / generative-AI stems were
   added 2026-08-06 after the filter audit found sizeable pools with near-zero overlap with what
   the pipeline had ever seen), #2 is the **AI solutions architect** stem (added 2026-08-06 — the
   stem behind the signed Sopra offer; replaced "AI solutions engineer", which overlapped ~85-95%
   with #1), #4-5 are the **customer-facing AI searches** (forward deployed / consultant — added
   2026-07-06 because those titles never match the engineer-phrased stems, while the relevance
   gate below already accepts them), and #8 is the **AI automation engineer** search (added
   2026-07-06; carries the dedicated Automation CV). Query #9 (the LAST entry) is a deliberately
   **broader backfill** — the same `AI engineer` stem with the date filter widened to **past week**
   (`f_TPR=r604800`) — and, like every later query, it runs ONLY after #1-8 are fully exhausted
   AND apply slots still remain. It stays **AI-only and bound by the same location gate**
   (`location.must_be_based_in` / `onsite_hybrid_cities`), so the relevance gate, dedup, and caps
   all bind unchanged: #9 can only fill leftover slots with slightly older remote roles, never
   balloon the run. On thin days when #1-8 yield nothing, #9 is what keeps the pipeline moving.
4. **For each result card (newest first) — the run ends ONLY when you hit `MAX_APPLIES_PER_RUN`
   this run, reach `DAILY_APPLY_CAP` in 24h (re-check the count before every apply), the run window
   closes, or there are genuinely no more result pages on the LAST query in `search_queries`. When
   a page is exhausted and no stop condition is met, go to the NEXT results page; when a whole
   query is exhausted, run the NEXT query (step 3) — never wind down with apply slots open and
   pages or queries unevaluated:**
   Throughout: before every page action **on a `linkedin.com` tab** `sleep` a fresh random
   `ACTION_DELAY_SECONDS` (skip this delay entirely on external ATS tabs — see **Safety & pacing**),
   and re-snapshot after every page change.
   1. Extract `job_id` (from the job URL), title, company, location, posted text.
   2. **Dedup — query, don't recall.** The moment you have the `job_id`, run a fresh one-row query:
      `SELECT status FROM applications WHERE job_id='<job_id>';`
      If it returns a row (`applied`/`prepared`) → skip silently. This per-card query is MANDATORY
      before opening or applying — never substitute your memory of the step-2 list (deep in a long
      run that memory fails; the query is cheap and instant).
      **Reapply-cooldown company check.** After the job_id dedup passes, compare the card's
      **company** to this run's reapply-suppression list (loaded in step 2). If it is the same
      company as one on that list — allow for legal-form / brand variants ("Deriv" ≡ "Deriv Group",
      "INTERSPORT Digital GmbH" ≡ "Intersport") — **skip without opening the card** and log
      `skipped_reapply_cooldown: <company> (eligible again <date>)`. This is a company we
      deliberately do NOT re-apply to: it interviewed us and then rejected us inside the cooldown.
      **Active-pipeline company check (added 2026-07-30).** Same comparison, against the
      active-pipeline suppression list from step 2 (same brand-variant allowance). On a match →
      **skip without opening the card** and log `skipped_active_pipeline: <company> (<outcome>)`.
      Never add a second application while a company is actively in process with {{user_first_name}}. (On
      2026-07-30 the agent applied to a second Aivisory role mid-interview-process — this check
      is what closes that.)
      **Manual-blocklist company check (added 2026-07-31).** Same comparison, against the manual
      blocklist from step 2 (same brand-variant allowance). On a match → **skip without opening
      the card** and log `skipped_blocklist: <company> (until <date>|indefinite)`. These are
      companies {{user_first_name}} has explicitly ruled out — e.g. an ATS that hard-blocks every submit
      (ElevenLabs/Ashby 90-day tenant cooldown) or a company they no longer want; a dated entry
      lifts itself once its `until` date passes.
      **Ghost-company check (added 2026-07-31).** Same comparison, against the ghost-company
      suppression list from step 2 (same brand-variant allowance). On a match → **skip without
      opening the card** and log `skipped_ghost: <company> (<n> applies, all silent since
      <first_applied>)`. These companies never answer; the list computes itself from the DB, so
      never hand-extend it — a company not returned by the step-2 query is not a ghost this run.
      Every OTHER company stays fair game — including ones that rejected us WITHOUT an interview,
      or ghosted us (apply broadly for market-response data). Two genuinely different roles at one
      company are also still fine — but only while that company is on NONE of the suppression
      lists or the manual blocklist.
      **Duplicate-posting check.** After the job_id dedup and the company checks pass, run a
      fresh query:
      `SELECT job_id, title, applied_at FROM applications WHERE company = '<company>' COLLATE NOCASE AND applied_at >= datetime('now', '-14 days');`
      (the `14` is `DUPLICATE_POSTING_WINDOW_DAYS`, read fresh from run_config.txt in step 1 like the
      other config values — substitute it in). If any returned title is the SAME ROLE as the card's
      title once cosmetic decorations are ignored — gender tags like (m/w/d) / (f/m/d) / "all
      genders", punctuation, capitalization, word order, remote/location suffixes — treat it as a
      REPOST: **skip without opening the card** and log `skipped_duplicate_posting: <company> —
      <title> (already applied as job <job_id> on <date>)`. Genuinely different roles at the same
      company remain fair game (provided the company passed the suppression + blocklist checks above) —
      applying to two distinct openings at one company is fine and has
      happened legitimately. (On 2026-06-28 the agent applied twice to the SAME HappyRobot "Forward
      Deployed Engineer - German Speaking" role reposted under two job_ids, 4372462916 and
      4421180715, six minutes apart — job_id-only dedup cannot catch a repost, which is exactly what
      this check closes.)
   3. **Relevance gate — weak on the TITLE, decided on the DESCRIPTION, biased toward APPLYING.**
      The GOAL of this program is to apply broadly enough to **gather real market-response data**, not
      to apply "perfectly" — **good-enough fit is enough, and when in doubt, APPLY rather than skip.**
      AI is a new field with no standard role names, so never reject a fitting role for an unusual
      title. The ONLY hard title filter is seniority; everything else is judged from the description.
      - **Stage A — seniority auto-FAIL (title).** If the title contains any `exclude_title_keywords`
        as a whole word (case-insensitive): senior, sr, lead, principal, staff, head, director, vp,
        manager, werkstudent, working student, intern, internship, praktikum, apprentice, junior,
        graduate → FAIL. Log + next card. (This is the one hard title filter — we target mid-level IC.)
      - **Stage B — OPEN it unless the title has ZERO AI signal.** Open any card whose title hints at
        AI / ML / GenAI / LLM / data / "applied AI" / agents work — including unusual names (Forward
        Deployed Engineer, Solution / Enablement Engineer, AI Product Engineer, Applied AI, Data
        Scientist, AI Consultant, Customer / Sales / Pre-sales Engineer at an AI company, …).
        Skip-on-title ONLY a title with no AI signal at all (plain Backend / Frontend /
        DevOps / non-AI Software Engineer); that's purely to avoid opening dozens of clearly non-AI
        roles each run. Log "skipped (no AI signal in title)" + next card.
      - **READING the description — evaluate-first (LinkedIn's SDUI job page, since 2026-07-20).**
        LinkedIn's job-view page is server-driven UI (React Server Components): the JD body text is
        NOT exposed in the accessibility snapshot (the snapshot shows the "About the job" heading
        with nothing — or only a ~600-char truncated teaser — under it), CSS classes are
        build-hashed (no stable selectors), and a console "Minified React error #418" hydration
        error fires on every navigation as permanent NOISE, not a failure. The JD also streams in
        AFTER the load event, so a one-shot read right after navigation races it and loses (the
        2026-07-22 run read `JD_NOT_FOUND` twice on a job whose JD was provably in the DOM 5s
        later, and needlessly burned the gate on the external ATS page). Therefore NEVER conclude
        "JD missing/unrendered" from the snapshot — read the description with `browser_evaluate`,
        using this POLLING form (same TreeWalker, retried every 2s for up to ~16s inside ONE call;
        never the one-shot version):
        ```js
        async () => {
          const read = () => {
            const w = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
            let t = null;
            while (w.nextNode()) {
              const el = w.currentNode, s = (el.innerText || '').trim();
              if (/^(About the job|Über den Job|Tätigkeitsbeschreibung)/.test(s) && s.length > 300) t = el;
            }
            return t ? t.innerText : null;
          };
          for (let i = 0; i < 8; i++) {
            const txt = read();
            if (txt) return txt;
            await new Promise(r => setTimeout(r, 2000));
          }
          return 'JD_NOT_FOUND';
        }
        ```
        (deepest match wins; returns the FULL JD with no "see more" click — TreeWalker proven live
        2026-07-21 on job 4442589884, Easy Apply, and job 4440158393, external Apply; polling form
        proven live 2026-07-22 on job 4443358678, whose JD the one-shot read had missed).
        If it returns `JD_NOT_FOUND` after one reload AND the job has an external Apply link, follow
        that link and run this same description gate on the ATS page's own job text (proven
        2026-07-20). Only a job that is Easy-Apply-only AND unreadable both ways → skip-and-log
        (`skipped: JD unreadable`) — a PER-JOB skip, never a run halt. Related flakiness of the same
        SDUI stack: if a normal click times out with "waiting for element to be visible, enabled and
        stable" on an element a screenshot shows plainly present, dispatch the click via
        `browser_evaluate` instead of retrying the normal click in a loop.
      - **The real gate — decide on the DESCRIPTION (be permissive).** After reading, APPLY if:
        1. it's a **hands-on role building / integrating / deploying AI- or LLM-powered software,
           products, or features** as an individual contributor — "an AI building role, no matter the
           title." The target is an **AI *application* engineer: builds applications that USE AI models**
           (RAG, agents, LLM/API integration, prompt & orchestration, AI product features, AI-powered
           backends), **including** customer-facing AI roles (Solution / Forward-Deployed / Sales /
           Customer / Enablement Engineer for an AI product, and **implementation** consulting where
           the consultant personally builds/integrates/deploys the AI solution at clients — {{user_first_name}}'s
           languages + recruiting background back exactly these). When genuinely ambiguous → **lean APPLY**.
           **SKIP — not this profile — if a CORE duty is model *creation* / ML engineering:** training,
           fine-tuning, or building ML/AI models ("fine-tune LLMs", "train ML models", model
           architecture/research, classical ML / data science, or MLOps centered on model
           training/serving) — *skip even if the JD also mentions app-building* (tie-break: if real
           model-training is a primary duty, skip). Also skip pure people-management, pure sales with
           no building, pure research with nothing shipped, and **advisory-only consulting** (strategy /
           governance / slideware with nothing hands-on built — implementation consulting passes, see
           above).
        2. **mid-level** — if the description clearly reads senior (≈5+ yrs required, or owning a
           team) despite a neutral title, SKIP as too senior.
        3. **location/relocation** — decide purely from `location.*` in `profile.yaml`; there is no
           hardcoded country, and the user may be based anywhere (or mid-relocation).
           **Remote** roles → PASS if the role is workable from the user's base: if
           `location.must_be_based_in` is set, the role must allow remote work from within that
           area; if it is null/empty, any remote role passes this check.
           **Hybrid or Onsite** roles → PASS **only** if the workplace city is in
           `location.onsite_hybrid_cities`. **SKIP** any Hybrid/Onsite role in a city NOT on that
           list — the user has not committed to a commute/relocation there.
           Roles requiring relocation outside `location.must_be_based_in` → **SKIP unless**
           `location.willing_to_relocate` is `true`.
        4. no hard blocker (e.g. a required language {{user_first_name}} lacks — see `profile.yaml`).
      - on any FAIL/SKIP → record it in the **run log** with the reason. Do **NOT** write the DB
        (successes-only). Continue to the next card.
   4. Open the job and start the application by the available path:
      - **Easy Apply present** → click it; walk the in-LinkedIn form (step 5).
      - **No Easy Apply (the "Apply" button opens the company site / an external ATS)** → follow
        the instructions on the external page and complete the application THERE, under the same
        contract (answer bank only, pacing, the `prepare`/`live` submit gate). External guardrails:
        - **Creating a NEW account to apply is ALLOWED** (changed 2026-06-11). If the external ATS
          requires sign-up to continue, register using `account_credentials` from `profile.yaml`
          (name = `identity.full_name`, email = `identity.email_external` — used on external ATS forms and account sign-ups; password = `account_credentials.password`;
          if a site demands a username distinct from the email, use `account_credentials.username`),
          then continue the application normally. Two hard limits:
            · **No password configured** — if `account_credentials.password` is empty/missing, do NOT
              invent one: abandon, note `skipped_external: account/login required`, continue.
            · **Email verification — phone the inbox reader (F-2), don't hard-skip.** If the site
              requires an email-verification step at the `identity.email_external` address before you can submit, look
              at WHAT the page asks for, then call the read-only reader ONCE (it polls the inbox up
              to ~90s, so make ONE call and wait):
                – page says **click the link** we emailed you →
                  `inbox_reader/.venv/bin/python inbox_reader/find_verification.py --sender-domain <ATS registrable domain, e.g. join.com / pwc.de> --since $(( $(date +%s) - 300 ))`
                – page says **enter the code** we emailed you (a numeric OTP field) → same command
                  **+ ` --want code`**.
              Always pass the SITE's registrable domain — the reader handles campaign-subdomain
              senders (e.g. PwC mails from jobalerts-de.pwc.com) by itself. Then read its JSON:
                – `"status":"found"` with `"confidence":"high"` → for a **link**: navigate the
                  browser to `link` (YOU open it, in this same paced session; the reader only reads
                  mail, never clicks). For a **code**: TYPE the returned `code` into the page's
                  verification field and confirm it. Either way, let the verification land and
                  continue the application to the review/submit gate.
                – anything else (`"confidence":"low"` / `"not_found"` / `"error"`) → abandon THIS
                  application, note `skipped_external: email confirmation required` (append the JSON
                  `status`/`confidence` so we can spot near-misses), continue. Exactly today's behavior.
              If the account is created and you reach the form WITHOUT any verification, just apply.
          Only ever create a FRESH account; never log into a pre-existing one or change its settings.
          Note each account you create in the run log (`created_account: <site>`). A sign-up CAPTCHA
          follows the external-CAPTCHA rule below (skip this app, continue). Do NOT write the DB on skip.
        - A REQUIRED field with no answer in `profile.yaml` → follow step 4.6: free-text
          questions and cover letters go to the **writer** tool first; everything else (or a
          writer `skip`) → abandon and log `skipped_no_answer` (never invent).
        - A CAPTCHA / human-verification on the EXTERNAL page blocks only THIS application (it is
          NOT a LinkedIn anti-bot signal) → abandon it, note it in the **run log**
          (`skipped_external: captcha/verification`), and continue to the next job. (LinkedIn red
          flags still halt the whole run — see **Safety & pacing**.)
        - Upload the CV chosen by the **CV-selection rule** (step 3): title override via
          `cv_by_title` if the job title matches, otherwise the active query's `resume`. Then fill
          from the answer bank, and pace every action.
   5. Walk the application form pages — the Easy Apply dialog **or** the external ATS form
      (Next → Review). **Re-snapshot after every page change**, then fill EVERY field from the
      answer bank (see **Field rules**). Custom
      dropdowns/typeaheads are ARIA comboboxes, not native `<select>` — click to open, wait for the
      options to render, then pick by visible text. Treat **work-authorization / visa / relocation /
      compensation** as high-stakes: fill exactly from the answer bank, and if unsure, skip-and-log
      rather than guess. If a resume/CV is requested, upload the CV from the **CV-selection rule**
      (step 3): first the title override (`cv_by_title`) when the job title matches, otherwise the
      active query's `resume`.
   6. **Missing answer — phone the writer before you skip.** If a REQUIRED field has no answer in
      `profile.yaml`, do NOT guess — but do not skip immediately either:
      - **Free-text question or cover-letter field** (motivation text, "describe/tell us...",
        "why us?", Anschreiben — as a text box or a document upload) → call the **writer** tool
        ONCE (see Tools). On `answered`: paste the `answer` VERBATIM (for a cover-letter UPLOAD
        field, save via `--out-file` and upload that file — name it `.pdf` when the form requires
        or prefers PDF, `.txt` otherwise; the writer renders the PDF itself) and record the Q&A
        in `extra_questions` tagged `[writer]`.
        **Cover letters additionally obey `WRITING_MODE`** (read fresh from run_config.txt):
        `review` → still generate + save via `--out-file`, but do NOT use it: abandon this
        application and log `skipped_writer_review: <company> — letter saved <path>` (a human
        approves letter quality before letters go live); `auto` → use the letter and proceed to
        submit normally. Short question ANSWERS are usable in both modes — WRITING_MODE gates
        letters only.
      - **Any other field type** (dropdown, checkbox, structured data like license/ID numbers),
        or the writer returned `skip`/`error` → abandon THIS application (discard the draft /
        close the dialog), note it in the **run log** (`skipped_no_answer: <field label>`, plus
        the writer's skip reason if it was consulted); do NOT write the DB; continue to the next
        job.
      - **Never** route salary/compensation or nationality/citizenship fields through the writer
        — `profile.yaml` rules only (see Field rules).
   7. **Submit:** at the review screen (Easy Apply or external form) — if `MODE=live`, click the
      final Submit and confirm success; if `MODE=prepare`, STOP at the final Submit (a human sends
      it) — do not click it.
   8. **Record — successes only (the DB stores ONLY `applied`/`prepared`):**
      Write with a plain `INSERT` — NEVER `INSERT OR REPLACE` / upsert (`job_id` is the PRIMARY KEY,
      so a duplicate INSERT fails loudly — that is intended). If the INSERT fails because the row
      already exists, that is a dedup failure (you just double-applied): do NOT overwrite the
      existing row — it is the record of the FIRST application and must be preserved. Log
      `dedup_violation: <job_id>` in the run log and move on.
      - submitted & confirmed (live) → `INSERT` with `status=applied`, `applied_at=now`
      - reached review (prepare) → `INSERT` with `status=prepared`, `notes='prepared, submit-ready (awaiting human submit)'`
      - failed mid-way (`submit_failed`) → do **NOT** write the DB; record what happened in the
        **run log** (kept out of the DB so a later run can retry the job).
      For each row you DO write, set `status_updated_at=now`, `first_seen=now`,
      **`outcome='no_response'` and `outcome_at=now`** (F-4 outcome tracking: a new application starts
      un-responded; `outcome` is a DERIVED rollup that only later moves as reply mail arrives — set this
      baseline at insert, never hand-advance it), and — for every
      timestamp column — use UTC in EXACTLY the format `YYYY-MM-DD HH:MM:SS` (space separator,
      never `T`, never a `Z` suffix; it must string-compare correctly against SQLite's
      `datetime('now')` in the daily-cap query), and
      fill url/title/company/location/posted_at. Also capture, for review later:
      - **`job_description`** → the full JD text you read at the relevance gate (paste the whole
        posting, not a summary) — our permanent record of what the role asked for. (`url` still holds
        the posting/application link, but text survives the link expiring.)
      - **`extra_questions`** → every employer screening question the form asked beyond the standard
        email/phone/resume, each with the answer you gave from the bank — e.g.
        `Years with Python? -> 3 | Work authorization? -> authorized to work in <country/area>, no sponsorship needed`.
        If the application submitted cleanly with NO extra questions, write exactly `none asked`.
      - **`apply_email`** → the exact address you used on THIS application: `identity.email`
        for Easy Apply / LinkedIn-native, `identity.email_external` for an external ATS — per
        the Field-rules routing. This is a dedicated column now; still enter the value, not free text.
      - **`cv_file`** → the CV filename you uploaded (the file named in the active entry's `resume`, e.g. `<Name>_<Role>_CV.pdf`), chosen by the
        CV-selection rule (step 3: title override `cv_by_title`, else the active query's `resume`). A
        dedicated column — keep logging *why* in the run log, but the filename goes here.
      - **`worker` / `model`** → which AI ran this application, stamped from the environment (never
        from what you think or say you are): run ONE Bash `echo "$JOBAPPLY_WORKER|$JOBAPPLY_WORKER_MODEL"`
        and put the two values verbatim into the `worker` and `model` columns. NEVER guess or
        hardcode them — the launching wrapper exports exactly what is running, so the stamp can
        never differ from it.
      - **`ats_host`** → registrable domain (last two labels, or three when the second-level domain
        is a public suffix like co.uk) of the host the form was submitted on (e.g. `greenhouse.io`);
        NULL for LinkedIn Easy Apply.
      - **`relevance`** → always `pass`. Only gate-passing jobs ever reach this INSERT (a FAIL/SKIP
        never writes the DB — see step 4.3), so every row you write is a pass by construction.
      - **`relevance_reason`** → ONE line on why the gate passed — the decisive signals from the
        DESCRIPTION that carried it (e.g. "hands-on LLM-integration duties, mid-level, remote OK
        per location gate, no blocker"), not a restatement of the title. Write this for EVERY row —
        Easy Apply and external-ATS applications alike; this column is what lets a later run
        compare "why we applied" against the actual outcome.
   9. **Pace.** `sleep` a fresh random `APPLY_GAP_SECONDS` before moving to the next card — as a
      FOREGROUND Bash command, never `run_in_background`, and **always with an explicit Bash
      `timeout` of 300000 (ms)** on any sleep that can exceed ~100s. The harness does NOT block
      past a foreground command's timeout (default 120s) — it auto-moves the still-running command
      to the background, which strands you exactly like `run_in_background` does. (Backgrounding
      the gap leaves you with nothing to do, you emit a "waiting..." message, and that final
      message TERMINATES the run — the 2026-06-12 run died that way at 4/10, and the 2026-07-22
      run died at 1 card / 0 applies when a 130s+ gap sleep hit the 120s default and was
      auto-backgrounded.) If a sleep ever IS auto-backgrounded anyway, do NOT wait for it and do
      NOT end your turn — immediately run a fresh foreground sleep (explicit timeout) for the
      remaining seconds and continue the run.
5. **Summary & mirror.** Write `output/run_<timestamp>.log` capturing the FULL run: counts and
   job_ids for seen / gated / applied / prepared / skipped / failed. The run log is the home for
   everything that did NOT result in an application (gated + skipped + failed live here, not in the
   DB). Then run `./db_report.sh` from this folder to refresh the human-readable DB mirror at the
   repo root.

**Your final response ends the run.** The headless worker exits the moment you produce a message with no
tool call — so never end your turn to "wait" for anything (waiting is always a FOREGROUND `sleep`),
and never narrate "waiting / moving on next..." as a message. Until step 5 is fully done (run log
written + `db_report.sh` run), every response must carry the next tool call. Output your wrap-up
text ONLY after step 5 — even on a red-flag stop, write the run log first, then report.

---

## Field rules — the answer-bank contract
Fill from `profile.yaml` ONLY. Never invent. Specifics:
- **Email — PATH-DEPENDENT (check the host first, this is a hard routing rule):**
  - **LinkedIn Easy Apply / any `linkedin.com` form** → `identity.email` (the address locked to the LinkedIn account).
  - **External ATS (any non-`linkedin.com` host)** — the form's email field AND any account sign-up → `identity.email_external` (used on external ATS forms and account sign-ups; readable inbox where verification/replies land).
  - Never use a `do_not_use.emails` address. Whichever you use, **record it in the `apply_email` column** (step 4.8).
- **Phone** → `identity.phone`. NEVER `do_not_use.phones`.
- **Name / portfolio / GitHub / LinkedIn** → `identity.*`.
- **Home address** — street (`Straße`/`Adresse`) → `location.street_address`; postal code (`PLZ`/`Postleitzahl`/`ZIP`)
  → `location.postal_code`; city → `location.current_city`; country → `location.country`. Some external
  ATS forms (e.g. GoHiring, common in the German market) require these; fill them rather than skip.
- **Work authorization / visa sponsorship** → `work_authorization` (which countries/areas the user
  is authorized in = `work_authorization.authorized_in`; whether sponsorship is required =
  `work_authorization.visa_sponsorship_required` — answered exactly as recorded, never assumed).
- **Citizenship-framed gates** — a binary or short option list whose only function is routing into
  "permit action needed?" ("citizen of `<country>` / other", "Are you a `<country>`/EU citizen?
  yes/no", "EU citizen / requires visa") → pick the **citizen** option (gate policy in `work_authorization`).
  A country-list **Nationality** dropdown or passport/ID field is a DATA field → the truthful value of `work_authorization.nationality`.
- **Compensation** — the floor (`compensation.floor`) is SECRET; never enter it anywhere.
  A single-number field → enter exactly `compensation.forced_single_number`. A range / free-text
  field → enter exactly `compensation.range_or_text_field`.
- **Years-of-experience screeners:**
  - "Years with `<TECH>`?" / "Years of experience in `<SKILL/AREA>`?" → `screening.skill_years[<key>]`
    (fuzzy, case-insensitive — keys may be technologies, skills, or domains: `python`, `b2b sales`,
    `project management`). Anything NOT listed → `screening.default_unlisted_skill_years` (0).
  - "Total years of experience" → `screening.total_years_experience`.
- **Notice period / earliest start** → `screening.notice_period` / `earliest_start_date`.
- **Relocation / remote / hybrid / onsite** → the matching `screening.willing_*`.
- **Degree** → `screening.has_bachelors_degree` (YES).
- **Driver's license** → `screening.has_drivers_license` is `null` → treat as NO answer → skip-and-log if required.
- **Gender / Geschlecht / salutation** → `screening.gender`, answered EXACTLY as recorded, mapped to
  the form's language (male → Männlich / Weiblich for female). Salutation (Anrede / Mr / Ms / Herr /
  Frau) derives from the SAME field: male → Herr/Mr, female → Frau/Ms. If `screening.gender` matches
  no offered option — it is "prefer not to say", "non-binary" or "other" and the form only offers
  male/female, or the field is required with no matching choice — → **skip-and-log**
  (`skipped_no_answer: gender/salutation required, no grounded option`). NEVER pick a substitute
  option and NEVER guess a salutation; an optional field with no matching choice is simply left
  empty and the application continues.
- Anything required that is NOT covered here AND NOT in the answer bank → **skip-and-log** (never invent).

---

## Don't
- **Don't fix the environment — report it (added 2026-07-30).** You may not modify config or code:
  no edits to `.mcp.json`, `CLAUDE.md`, `run_config.txt`, `profile.yaml`, `schema.sql`, any `*.sh`
  or `*.py`, and no schema changes to `applications.db` (no ALTER/CREATE/DROP — your only DB writes
  are the step-4.8 INSERTs). Memory files are also off-limits. When something is broken (a tool
  won't connect, a config looks wrong, a dependency fails), write a line starting with exactly
  `ISSUE:` to the shared run log describing what is broken and what you did to work around it
  (fall back, skip, continue) — a human reviews and applies the fix. (On 2026-07-30 a worker
  silently edited `.mcp.json` to fix the sqlite MCP server; the fix was correct but unreviewed
  self-modification is never OK — that's exactly what `ISSUE:` lines are for.)
- Don't log into pre-existing accounts, change passwords, or touch account settings (creating a NEW account to apply is allowed — see step 4).
- Don't click the final Submit while `MODE=prepare`; don't exceed `MAX_APPLIES_PER_RUN` or `DAILY_APPLY_CAP`.
- Don't treat any run as a drill/practice — every application is real. But don't over-filter either: skip only clear non-fits (not an AI-building role, senior, relocation outside the user's base area per `location.must_be_based_in`); when in doubt, lean apply (the goal is market-response data).
- Creating a NEW account to apply on an external ATS is allowed, using `account_credentials` from `profile.yaml`. Only skip-and-log (`skipped_external`) when no password is configured or the site needs email verification before you can apply (see step 4).
- Don't run headless or add any stealth/anti-detection flags — the honest real browser is the asset.
- Don't accept cookie/consent banners — always click Reject (or the most minimal option).
- Don't apply outside `RUN_WINDOW_LOCAL`. Don't skip the randomized `ACTION_DELAY_SECONDS` on `linkedin.com` tabs, or the `APPLY_GAP_SECONDS` between applications. (Skipping the per-action delay on external ATS tabs is INTENDED — not a violation.)
- Don't end your turn mid-run — a final message terminates the headless worker run. No "waiting..." sign-offs;
  sleep in the foreground (explicit `timeout: 300000` on any sleep that can top ~100s — the 120s
  default auto-BACKGROUNDS a foreground command, it does not block) and keep working until the
  caps/pages are exhausted AND step 5 is written.
- Don't write the DB with `INSERT OR REPLACE`/upsert — plain `INSERT` only; never overwrite an
  existing row (it is the record of the first application).
- Don't push past a red-flag challenge (CAPTCHA / "unusual activity" / verify) — STOP and report.
- Don't judge "the JD didn't render" from the accessibility snapshot — the SDUI job page hides the
  description from the snapshot by design; read it via `browser_evaluate` (step 4.3), fall back to
  the external-ATS page, and worst case skip THAT job — never halt the run over an unreadable JD.
- Don't enter the comp floor; don't invent answers. Email is PATH-BASED — `identity.email` on Easy Apply / linkedin.com, `identity.email_external` on any external ATS (see Field rules); never use one address for everything.
- Don't hard-skip a required free-text question or cover-letter field without calling the writer
  first (step 4.6) — and don't edit the writer's `answer` text (use verbatim or not at all). Don't
  submit a generated COVER LETTER while `WRITING_MODE=review` (letters park as
  `skipped_writer_review`; short answers are fine in both modes). Salary and citizenship fields
  never go to the writer.
- Don't write gated/skipped/failed jobs to the DB — those go to the run log only; the DB holds
  successful applications (`applied`/`prepared`) only.
- Don't reopen jobs already in the DB (`applied` / `prepared`) — checked with the per-card query in
  step 4.2, never from memory.
- Don't write a row with empty `relevance`/`relevance_reason` — every row gets `relevance='pass'`
  and a one-line `relevance_reason`, Easy Apply and external ATS alike (step 4.8).
