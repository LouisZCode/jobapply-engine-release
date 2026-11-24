#!/usr/bin/env python3
"""LOOP-003 — deterministic hands for the Sonnet mail agent.

The LLM agent decides; THIS tool executes. Every Gmail/DB write the agent can
perform goes through one of the subcommands below, so the guarantees live in
code the model cannot bypass:

  * READ + LABEL only. There is NO delete, NO trash, NO spam, NO send, NO reply
    code path in this file. Label ops touch exactly two labels: our sweep label
    (LABEL_NAME, configured in inbox_reader/user_mail.json) and INBOX (archive =
    remove INBOX).
  * DB writes are append-only `application_events` rows (INSERT OR IGNORE on
    msg_id) + the derived outcome rollup recompute — same contract as the
    LOOP-002 sweep. Pipeline fields (status/applied_at/...) are never touched.
  * event types are whitelisted; the job_id must exist in `applications`.

Subcommands (all print ONE JSON object/array on stdout):
  corpus                          applications + their events + recorded msg_ids
  list  --query '<gmail q>' [--max N]     message metadata + snippet
  read  --gid <id>                full parsed mail (headers + body text)
  file  --gid <id> --keep|--archive       add sweep label; --archive drops INBOX
  unfile --gid <id>               remove ONLY the sweep label (fix a mis-file)
  event --job <job_id> --type <t> --at 'YYYY-MM-DD HH:MM:SS' --msg-id '<rfc822>'
        [--sender S] [--subject SU] [--notes N]
"""
import argparse
import json
import os
import sqlite3
import sys
from html import unescape

from gmail_auth import SCOPES_MODIFY, TOKEN_MODIFY, get_service
from sweep_responses import (DB, LABEL_NAME, _hdr, _retry, build_r,
                             get_or_create_label, recompute)

EVENT_TYPES = {"auto_ack", "rejection", "withdrawn", "invite", "assessment_request",
               "interview_scheduled", "interview_done", "offer"}


def svc():
    return get_service(scopes=SCOPES_MODIFY, token_file=TOKEN_MODIFY)


def out(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=1))


def cmd_corpus(_a):
    con = sqlite3.connect(DB)
    cur = con.cursor()
    apps = []
    for job, co, ti, ap, st, oc in cur.execute(
            "SELECT job_id, company, title, substr(applied_at,1,10), status, outcome "
            "FROM applications ORDER BY applied_at"):
        apps.append({"job_id": job, "company": co, "title": ti, "applied": ap,
                     "status": st, "outcome": oc, "events": []})
    by_job = {a["job_id"]: a for a in apps}
    for job, at, et in cur.execute(
            "SELECT job_id, substr(event_at,1,10), event_type FROM application_events ORDER BY event_at"):
        if job in by_job:
            by_job[job]["events"].append("{} {}".format(at, et))
    msg_ids = [x[0] for x in cur.execute(
        "SELECT msg_id FROM application_events WHERE msg_id IS NOT NULL")]
    con.close()
    out({"applications": apps, "recorded_msg_ids": msg_ids})


def cmd_list(a):
    s = svc()
    ids, tok = [], None
    while len(ids) < a.max:
        resp = _retry(lambda: s.users().messages().list(
            userId="me", q=a.query, maxResults=min(100, a.max - len(ids)),
            pageToken=tok).execute(), "messages.list")
        ids += [m["id"] for m in (resp.get("messages") or [])]
        tok = resp.get("nextPageToken")
        if not tok:
            break
    rows = []
    for gid in ids:
        m = _retry(lambda: s.users().messages().get(
            userId="me", id=gid, format="metadata",
            metadataHeaders=["From", "Subject", "Date"]).execute(), "messages.get")
        hdr = {h["name"].lower(): h["value"]
               for h in m.get("payload", {}).get("headers", [])}
        rows.append({"gid": gid, "from": _hdr(hdr.get("from", "")),
                     "subject": _hdr(hdr.get("subject", "")),
                     "date": hdr.get("date", ""),
                     "labels": m.get("labelIds", []),
                     "snippet": unescape(m.get("snippet", ""))[:200]})
    out(rows)


def cmd_read(a):
    s = svc()
    full = _retry(lambda: s.users().messages().get(
        userId="me", id=a.gid, format="full").execute(), "messages.get")
    r = build_r(full)
    r["labels"] = full.get("labelIds", [])
    out(r)


def cmd_file(a):
    s = svc()
    label_id = get_or_create_label(s, LABEL_NAME)
    body = {"addLabelIds": [label_id]}
    if a.archive:
        body["removeLabelIds"] = ["INBOX"]
    _retry(lambda: s.users().messages().modify(
        userId="me", id=a.gid, body=body).execute(), "messages.modify")
    out({"ok": True, "gid": a.gid, "labeled": LABEL_NAME,
         "archived": bool(a.archive)})


def cmd_unfile(a):
    s = svc()
    label_id = get_or_create_label(s, LABEL_NAME)
    _retry(lambda: s.users().messages().modify(
        userId="me", id=a.gid, body={"removeLabelIds": [label_id]}).execute(),
        "messages.modify")
    out({"ok": True, "gid": a.gid, "unlabeled": LABEL_NAME})


def cmd_event(a):
    if a.type not in EVENT_TYPES:
        out({"ok": False, "error": "event type '{}' not in {}".format(
            a.type, sorted(EVENT_TYPES))})
        sys.exit(1)
    if not (len(a.at) == 19 and a.at[4] == "-" and a.at[10] == " "):
        out({"ok": False, "error": "--at must be UTC 'YYYY-MM-DD HH:MM:SS'"})
        sys.exit(1)
    con = sqlite3.connect(DB)
    cur = con.cursor()
    if not cur.execute("SELECT 1 FROM applications WHERE job_id=?", (a.job,)).fetchone():
        con.close()
        out({"ok": False, "error": "job_id '{}' not in applications".format(a.job)})
        sys.exit(1)
    cur.execute(
        "INSERT OR IGNORE INTO application_events "
        "(job_id,event_at,event_type,source,msg_id,sender,subject,notes,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
        (a.job, a.at, a.type, "mail-agent", a.msg_id or None,
         (a.sender or "")[:120], (a.subject or "")[:200], a.notes or "mail agent"))
    inserted = cur.rowcount
    con.commit()
    oc = recompute(cur, a.job)
    con.commit()
    con.close()
    out({"ok": True, "job_id": a.job, "event": a.type,
         "inserted": bool(inserted), "outcome_now": oc})


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("corpus")
    p = sub.add_parser("list")
    p.add_argument("--query", required=True)
    p.add_argument("--max", type=int, default=60)
    p = sub.add_parser("read")
    p.add_argument("--gid", required=True)
    p = sub.add_parser("file")
    p.add_argument("--gid", required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--keep", action="store_true")
    g.add_argument("--archive", action="store_true")
    p = sub.add_parser("unfile")
    p.add_argument("--gid", required=True)
    p = sub.add_parser("event")
    p.add_argument("--job", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--at", required=True)
    p.add_argument("--msg-id", default="")
    p.add_argument("--sender", default="")
    p.add_argument("--subject", default="")
    p.add_argument("--notes", default="")
    a = ap.parse_args()
    {"corpus": cmd_corpus, "list": cmd_list, "read": cmd_read,
     "file": cmd_file, "unfile": cmd_unfile, "event": cmd_event}[a.cmd](a)


if __name__ == "__main__":
    main()
