#!/usr/bin/env bash
# Human-readable reflection of applications.db — run anytime to follow what the agent saved.
# Prints to your terminal AND writes a mirror to the repo root (applications_report.txt) so you
# can read the latest state at a glance without opening sqlite.
#   ./db_report.sh
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/.." && pwd)"
# MULTI-001: the DB and the report mirror are per-user data — both follow the data root.
# Defaults: DB in the script dir, mirror at the repo root = today's behavior, byte-identical
# when JOBAPPLY_DATA is unset.
DATA_ROOT="${JOBAPPLY_DATA:-$DIR}"
DB="$DATA_ROOT/applications.db"
MIRROR="${JOBAPPLY_DATA:-$ROOT}/applications_report.txt"
[ -f "$DB" ] || { echo "no DB at $DB"; exit 1; }
# F-4: is the outcome-tracking schema present? (older DBs predate migrate_outcomes.sql)
HAS_OUTCOMES="$(sqlite3 "$DB" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='application_events';")"

{
  echo "==== applications.db   ($(date '+%Y-%m-%d %H:%M:%S %Z')) ===="
  echo "source: $DB"
  echo "(DB stores SUCCESSFUL applications only — applied / prepared."
  echo " Gated & skipped jobs are NOT here; they live in claude_applicator/output/run_*.log.)"
  echo
  echo "-- status breakdown (all time) --"
  sqlite3 -column -header "$DB" \
    "SELECT status, COUNT(*) AS n FROM applications GROUP BY status ORDER BY n DESC;"
  echo
  echo "-- activity in the last 24h --"
  sqlite3 -column -header "$DB" \
    "SELECT status, COUNT(*) AS n FROM applications
     WHERE status_updated_at >= datetime('now','-24 hours')
     GROUP BY status ORDER BY n DESC;"
  echo
  echo "-- CV & email routing (all time) --"
  sqlite3 -column -header "$DB" \
    "SELECT COALESCE(cv_file,'(unrecorded)') AS cv, COALESCE(apply_email,'(unrecorded)') AS email,
            COUNT(*) AS n
     FROM applications GROUP BY cv, email ORDER BY n DESC;"
  echo
  echo "-- by AI and application system (all time) --"
  sqlite3 -column -header "$DB" \
    "SELECT COALESCE(worker,'(none)') AS worker, COALESCE(model,'(none)') AS model, COUNT(*) AS n
     FROM applications GROUP BY worker, model ORDER BY n DESC;"
  echo
  sqlite3 -column -header "$DB" \
    "SELECT COALESCE(ats_host,'(none)') AS ats_host, COUNT(*) AS n
     FROM applications GROUP BY ats_host ORDER BY n DESC;"
  echo
  echo "================= OUTCOME / MARKET RESPONSE  (F-4) ================="
  if [ "$HAS_OUTCOMES" = "1" ]; then
    echo "(outcome is DERIVED from application_events, never hand-set; status stays the pipeline fact)"
    echo
    echo "-- response funnel (rollup across all applications) --"
    sqlite3 -column -header "$DB" \
      "WITH tot AS (SELECT COUNT(*) c FROM applications)
       SELECT COALESCE(outcome,'no_response') AS outcome, COUNT(*) AS n,
              printf('%.0f%%', 100.0*COUNT(*)/(SELECT c FROM tot)) AS pct
       FROM applications GROUP BY COALESCE(outcome,'no_response')
       ORDER BY CASE COALESCE(outcome,'no_response') WHEN 'no_response' THEN 0 WHEN 'acknowledged' THEN 1
                WHEN 'assessment' THEN 2 WHEN 'invited' THEN 3 WHEN 'interviewing' THEN 4
                WHEN 'rejected' THEN 5 WHEN 'offer' THEN 6 WHEN 'withdrawn' THEN 7 ELSE 9 END;"
    echo
    sqlite3 "$DB" \
      "SELECT 'responded (outcome <> no_response): '
              || (SELECT COUNT(*) FROM applications WHERE COALESCE(outcome,'no_response')<>'no_response')
              || '/' || (SELECT COUNT(*) FROM applications)
              || ' (' || printf('%.0f%%', 100.0*(SELECT COUNT(*) FROM applications WHERE COALESCE(outcome,'no_response')<>'no_response')
                                 /(SELECT COUNT(*) FROM applications)) || ')'
              || '   |   any email back: ' || (SELECT COUNT(DISTINCT job_id) FROM application_events)
              || '   |   reached interview/invite: '
              || (SELECT COUNT(DISTINCT job_id) FROM application_events
                  WHERE event_type IN ('invite','interview_scheduled','interview_done'));"
    echo
    echo "-- days from applying to first substantive reply (non-ack) / to rejection (agent rows; medians) --"
    sqlite3 -column -header "$DB" \
      "WITH fr AS (SELECT CASE WHEN d<0 THEN 0 ELSE d END AS d FROM
                     (SELECT MIN(julianday(e.event_at))-julianday(a.applied_at) AS d
                      FROM applications a JOIN application_events e ON e.job_id=a.job_id
                      WHERE a.job_id NOT LIKE 'manual-%' AND a.applied_at IS NOT NULL
                        AND e.event_type IN ('rejection','invite','interview_scheduled','interview_done','assessment_request','offer')
                      GROUP BY a.job_id) WHERE d>=-0.5),
            rj AS (SELECT d FROM
                     (SELECT MIN(julianday(e.event_at))-julianday(a.applied_at) AS d
                      FROM applications a JOIN application_events e ON e.job_id=a.job_id AND e.event_type='rejection'
                      WHERE a.job_id NOT LIKE 'manual-%' AND a.applied_at IS NOT NULL
                      GROUP BY a.job_id) WHERE d>=0)
       SELECT 'to first non-ack reply' AS metric, (SELECT COUNT(*) FROM fr) AS n,
              printf('%.1f',(SELECT AVG(d) FROM (SELECT d FROM fr ORDER BY d LIMIT 2-(SELECT COUNT(*) FROM fr)%2 OFFSET (SELECT (COUNT(*)-1)/2 FROM fr)))) AS median,
              printf('%.1f',(SELECT MIN(d) FROM fr)) AS min, printf('%.1f',(SELECT MAX(d) FROM fr)) AS max, printf('%.1f',(SELECT AVG(d) FROM fr)) AS mean
       UNION ALL
       SELECT 'to rejection', (SELECT COUNT(*) FROM rj),
              printf('%.1f',(SELECT AVG(d) FROM (SELECT d FROM rj ORDER BY d LIMIT 2-(SELECT COUNT(*) FROM rj)%2 OFFSET (SELECT (COUNT(*)-1)/2 FROM rj)))),
              printf('%.1f',(SELECT MIN(d) FROM rj)), printf('%.1f',(SELECT MAX(d) FROM rj)), printf('%.1f',(SELECT AVG(d) FROM rj));"
    echo
    echo "-- companies with repeats or that progressed past 'acknowledged' (singletons: see rows below) --"
    sqlite3 -column -header "$DB" \
      "SELECT substr(company,1,30) AS company, COUNT(*) AS apps,
              SUM(outcome IN ('invited','interviewing','offer')) AS itw,
              SUM(outcome='assessment') AS asmt,
              SUM(outcome='acknowledged') AS ackd,
              SUM(outcome='rejected') AS rej,
              SUM(COALESCE(outcome,'no_response')='no_response') AS pend
       FROM applications GROUP BY company
       HAVING COUNT(*)>1 OR SUM(outcome IN ('invited','interviewing','offer','assessment'))>0
       ORDER BY itw DESC, asmt DESC, rej DESC, apps DESC, company;"
    echo
    echo "-- interview / invite / offer touches (the deep-funnel timeline) --"
    sqlite3 -column -header "$DB" \
      "SELECT substr(e.event_at,1,10) AS date, e.event_type AS event,
              substr(a.company,1,20) AS company, a.outcome AS final, substr(e.notes,1,44) AS notes
       FROM application_events e JOIN applications a ON a.job_id=e.job_id
       WHERE e.event_type IN ('invite','interview_scheduled','interview_done','offer','assessment_request')
       ORDER BY e.event_at;"
  else
    echo "(application_events table not present -- run 'sqlite3 applications.db < migrate_outcomes.sql' to enable)"
  fi
  echo
  echo "-- all rows (newest first) --"
  sqlite3 -column -header "$DB" \
    "SELECT substr(job_id,1,10) AS job_id, status AS st,
            substr(title,1,40) AS title, substr(company,1,16) AS company,
            substr(location,1,18) AS loc, substr(status_updated_at,1,16) AS updated
     FROM applications ORDER BY status_updated_at DESC;"
  echo
  echo "-- detail: every saved application (applied / prepared) --"
  sqlite3 -line "$DB" \
    "SELECT job_id, status, title, company, location, url,
            cv_file, apply_email,
            extra_questions, substr(job_description,1,300) AS jd_preview,
            notes, status_updated_at
     FROM applications
     WHERE status IN ('applied','prepared')
     ORDER BY status_updated_at DESC;"
} | tee "$MIRROR"

echo
echo "(mirror written to $MIRROR)"
