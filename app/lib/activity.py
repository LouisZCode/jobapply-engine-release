"""app/lib/activity.py — append-only activity log (app/activity.ndjson, gitignored).

One JSON line per event so a test session can be reconstructed afterwards: page visits
and button presses (the server.py request middleware), plus explicit outcome events for
the transitions a bare status code can't tell (job started/finished, LinkedIn verified,
schedule install refused). Two hard rules keep it safe and readable:
  - NEVER log form values — profile fields are personal data; routes and outcomes only.
  - Logging must never break the app — every failure here is swallowed.
The 4-second poll endpoints are excluded at the middleware (see server.py), so the file
stays a short human-readable story, not a firehose.
"""
import json
import os
from datetime import datetime

from . import paths


def log_event(kind, **fields):
    event = {"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "kind": kind}
    event.update(fields)
    try:
        # MULTI-001: under JOBAPPLY_DATA the dir may not exist yet — page visits are the
        # first thing to touch a fresh data root, so create the parent on demand.
        os.makedirs(os.path.dirname(paths.ACTIVITY_FILE), exist_ok=True)
        with open(paths.ACTIVITY_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        pass
