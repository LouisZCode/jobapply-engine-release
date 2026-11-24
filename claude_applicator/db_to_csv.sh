#!/usr/bin/env bash
# Export applications.db -> two CSVs at the repo root (both gitignored — personal data):
#   applications.csv        every application, ALL columns incl. outcome/apply_email/cv_file
#   application_events.csv  the full response history (acks/invites/rejections/offers)
# The DB stays the working source of truth; the CSVs are the portable copy.
# Usage: db_to_csv.sh [--no-open]   (--no-open for automation; default opens the spreadsheet)
# Refreshed automatically after every scheduled round (run_scheduled.sh), and step 0b copies
# both files into backups/ alongside the DB snapshot.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# MULTI-001: the DB and both export CSVs are per-user data — all follow the data root
# (CSVs move OUT of the fixed repo-root location when JOBAPPLY_DATA is set; run_scheduled.sh's
# STEP 0b copies from the same expression, so the two always agree).
# Defaults: DB in the script dir, CSVs at the repo root = today's behavior, byte-identical unset.
DB="${JOBAPPLY_DATA:-$DIR}/applications.db"
OUT_APPS="${JOBAPPLY_DATA:-$DIR/..}/applications.csv"
OUT_EVENTS="${JOBAPPLY_DATA:-$DIR/..}/application_events.csv"

[[ -f "$DB" ]] || { echo "DB not found: $DB"; exit 1; }

# Big free-text columns (job_description) go last so the useful columns are readable up front.
sqlite3 -header -csv "$DB" \
  "SELECT job_id, status, outcome, outcome_at, title, company, location, applied_at,
          posted_at, apply_email, cv_file, relevance, relevance_reason, url,
          extra_questions, notes, first_seen, status_updated_at, job_description
   FROM applications
   ORDER BY status_updated_at DESC;" > "$OUT_APPS"

sqlite3 -header -csv "$DB" \
  "SELECT e.id, e.job_id, a.company, a.title, e.event_type, e.event_at, e.source,
          e.sender, e.subject, e.notes, e.created_at, e.msg_id
   FROM application_events e
   LEFT JOIN applications a ON a.job_id = e.job_id
   ORDER BY e.event_at;" > "$OUT_EVENTS"

echo "wrote $OUT_APPS ($(sqlite3 "$DB" 'SELECT COUNT(*) FROM applications;') rows)"
echo "wrote $OUT_EVENTS ($(sqlite3 "$DB" 'SELECT COUNT(*) FROM application_events;') rows)"

if [[ "${1:-}" != "--no-open" ]]; then
  open "$OUT_APPS"   # default CSV handler (Numbers / Excel)
fi
