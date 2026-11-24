-- F-4 Part A — outcome tracking schema migration (one-time; run once against applications.db).
-- Additive only: adds a derived-outcome rollup to `applications` and an events timeline table.
-- Take a fresh applications.db.bak-<ts> before running (ALTER ADD COLUMN is not re-runnable).
--
--   sqlite3 applications.db < migrate_outcomes.sql
--
-- Design of record: ARCHITECTURE.md §8 F-4. Events are the source of truth; `outcome` is DERIVED
-- from an application's events and recomputed after every insert, never hand-set. `status`
-- (pipeline fact: 'applied') is never touched by outcome tracking.

ALTER TABLE applications ADD COLUMN outcome    TEXT;   -- derived rollup: NULL until an outcome is known
ALTER TABLE applications ADD COLUMN outcome_at TEXT;   -- when `outcome` last changed (UTC 'YYYY-MM-DD HH:MM:SS')

-- Baseline: every already-submitted application starts life as 'no_response'.
UPDATE applications SET outcome='no_response', outcome_at=status_updated_at WHERE status='applied';

-- One row per real-world touch (email or human-reported). The timeline that `outcome` rolls up from.
CREATE TABLE IF NOT EXISTS application_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      TEXT NOT NULL REFERENCES applications(job_id),
    event_at    TEXT NOT NULL,   -- ORIGINAL email Date (UTC), NOT import time
    event_type  TEXT NOT NULL,   -- auto_ack|rejection|invite|assessment_request|interview_scheduled|interview_done|offer|withdrawn|other
    source      TEXT NOT NULL,   -- 'hotmail-manual' | 'gmail-agent' | 'user'
    msg_id      TEXT UNIQUE,     -- RFC822 Message-ID (email) or synthetic 'user:...' key -> idempotent re-imports
    sender      TEXT,            -- From header of the source mail
    subject     TEXT,            -- Subject of the source mail
    notes       TEXT,            -- the key detail: interview date, feedback, assessment link, reason
    created_at  TEXT NOT NULL    -- import time (audit only)
);
CREATE INDEX IF NOT EXISTS idx_events_job ON application_events(job_id);

-- Outcome vocabulary (event_type -> rollup), recomputed per application after each insert:
--   terminal event exists (rejection|offer|withdrawn) -> that outcome, latest wins;
--   else highest rung reached:
--     interviewing (interview_scheduled|interview_done) > invited > assessment
--     > acknowledged (auto_ack) > no_response.  'other' never moves the rollup.
