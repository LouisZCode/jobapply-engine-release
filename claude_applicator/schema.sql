-- claude_applicator application-tracking state.
-- One row per **successful application** — status 'applied' or 'prepared' ONLY. job_id (from the
-- job URL) is the dedup key: before applying, Claude reads this table and skips any job already
-- here. Gated / skipped / failed jobs are NOT stored — they live only in the per-run log
-- (output/run_*.log). Claude upserts a row only when an application is submitted (live) or
-- prepared (review-ready). Timestamps are UTC text in EXACTLY this format: 'YYYY-MM-DD HH:MM:SS'
-- (space separator — never 'T', never a 'Z' suffix). It must compare correctly against SQLite's
-- datetime('now') in the daily-cap query: a 'T' separator sorts AFTER a space and silently
-- inflates the rolling-24h count (bit us once — job 4425094792).

CREATE TABLE IF NOT EXISTS applications (
    job_id            TEXT PRIMARY KEY,             -- LinkedIn job id parsed from the URL (dedup key)
    url               TEXT,
    title             TEXT,
    company           TEXT,
    location          TEXT,
    posted_at         TEXT,                         -- what LinkedIn shows ("2 hours ago" etc.), as text
    first_seen        TEXT NOT NULL,                -- when our system first saw this job
    relevance         TEXT,                         -- gate verdict: 'pass' | 'fail'
    relevance_reason  TEXT,                         -- why (matched title / excluded keyword / off-target)
    status            TEXT NOT NULL DEFAULT 'seen', -- in practice only 'applied' or 'prepared' is stored (DB = successes only)
    applied_at        TEXT,                         -- timestamp of a successful submit (NULL until then)
    status_updated_at TEXT NOT NULL,                -- last time status changed
    notes             TEXT,                         -- missing-answer field, error text, free notes
    job_description   TEXT,                          -- full JD text, copied on a successful application (so we can review the role later)
    extra_questions   TEXT,                          -- employer screening Q&A captured during apply ("Q? -> A | ..."); 'none asked' on a clean submission
    apply_email       TEXT,                          -- inbox used for THIS application: identity.email (Easy Apply / LinkedIn-native) | identity.email_external (external ATS). See routing in profile.yaml identity.
    cv_file           TEXT,                          -- CV filename uploaded for this application (e.g. CandidateName_RoleFamily_2026.pdf); chosen by cv_by_title / query default (CLAUDE.md step 3)
    outcome           TEXT,                          -- F-4 DERIVED market-response rollup (recomputed from application_events, never hand-set): no_response|acknowledged|assessment|invited|interviewing|rejected|offer|withdrawn. NULL on non-applied rows.
    outcome_at        TEXT,                          -- when `outcome` last changed (UTC 'YYYY-MM-DD HH:MM:SS')
    worker            TEXT,                          -- harness that ran this application (claude | pi | codex), stamped from the environment, never from what the model says it is
    model             TEXT,                          -- model that ran it (e.g. claude-sonnet-5), same source
    ats_host          TEXT                           -- registrable domain of the host the form was submitted on (e.g. greenhouse.io); NULL for LinkedIn Easy Apply
);

-- F-4 outcome tracking: one row per real-world touch on an application (email or human-reported).
-- Events are the SOURCE OF TRUTH; applications.outcome is derived from them. See ARCHITECTURE.md §8 F-4.
CREATE TABLE IF NOT EXISTS application_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES applications(job_id),
    event_at    TEXT NOT NULL,   -- ORIGINAL email Date (UTC), NOT import time
    event_type  TEXT NOT NULL,   -- auto_ack|rejection|invite|assessment_request|interview_scheduled|interview_done|offer|withdrawn|other
    source      TEXT NOT NULL,   -- 'hotmail-manual' | 'gmail-agent' | 'user'
    msg_id      TEXT UNIQUE,     -- RFC822 Message-ID (email) or synthetic 'user:...' key -> idempotent re-imports
    sender      TEXT,            -- From header of the source mail
    subject     TEXT,            -- Subject of the source mail
    notes       TEXT,            -- key detail: interview date, feedback, assessment link, reason
    created_at  TEXT NOT NULL    -- import time (audit only)
);
CREATE INDEX IF NOT EXISTS idx_events_job ON application_events(job_id);

-- Outcome rollup rule (recompute per application after each event insert):
--   terminal event exists (rejection|offer|withdrawn) -> that outcome, latest wins;
--   else highest rung reached: interviewing (interview_scheduled|interview_done) > invited
--     > assessment (assessment_request) > acknowledged (auto_ack) > no_response. 'other' never moves it.

CREATE INDEX IF NOT EXISTS idx_applications_status  ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_company ON applications(company);

-- star-once ledger: a msg_id listed here had STARRED successfully applied once; never
-- re-star -- a manual unstar must stick (F-4 Part E follow-up, 2026-07-17).
CREATE TABLE IF NOT EXISTS starred_msgs (
    msg_id      TEXT PRIMARY KEY,   -- RFC822 Message-ID of the starred mail
    starred_at  TEXT NOT NULL       -- UTC 'YYYY-MM-DD HH:MM:SS' when first starred
);

-- Status vocabulary. The DB stores ONLY the first two (successful applications); the rest are
-- run-log-only outcomes (output/run_*.log) and are NEVER written to this table:
--   applied           [STORED]  submitted successfully — Easy Apply or external (applied_at set)
--   prepared          [STORED]  walked to the review screen, submit-ready, NOT yet submitted (MODE=prepare; a human sends it)
--   gated_out         [run log] failed the relevance/exclude gate -> never apply
--   skipped_no_answer [run log] a required field had no answer in profile.yaml -> skipped, not applied
--   skipped_external  [run log] external apply needed an account/login or hit a captcha -> not completed (retry-able next run)
--   skipped_reapply_cooldown [run log] company interviewed us then rejected us within REAPPLY_INTERVIEW_REJECT_COOLDOWN_DAYS -> don't re-apply yet
--   submit_failed     [run log] attempted but the submit did not complete -> retry-able next run
