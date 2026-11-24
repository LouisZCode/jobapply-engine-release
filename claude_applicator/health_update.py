#!/usr/bin/env python3
"""RB-1 health heartbeat: tiny stdlib CLI that records start/end/rc for the
scheduled jobs (apply / sweep / health) into output/health_status.json, so
check_health.sh (and a human) can tell a run happened and succeeded without
opening any log.

Usage:
  python3 health_update.py <job> start
  python3 health_update.py <job> <rc> [note]

job in {apply, sweep, health}. State is a flat JSON dict keyed by job name:
  {"apply": {"last_start": "...", "last_end": "...", "last_rc": 0,
             "last_ok_end": "...", "note": "..."}, ...}
last_ok_end is updated ONLY when rc==0 (a health-check convenience: "when did
this job last finish cleanly", independent of how many failures happened since).

Atomic write (tmp file + os.replace) so a reader never sees a half-written file.

Testing hook: the state path defaults to output/health_status.json next to this
file, but can be redirected via the JOBHEALTH_STATUS_PATH env var (default path
unchanged for real runs) so a unit test can point at a scratch file instead.
"""
import datetime
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_STATUS_PATH = os.path.join(HERE, "output", "health_status.json")
STATUS_PATH = os.environ.get("JOBHEALTH_STATUS_PATH", DEFAULT_STATUS_PATH)

JOBS = {"apply", "sweep", "health"}


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}


def _atomic_write(path, data):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    # Per-PID tmp name: concurrent writers (23:00 sweep overlapping a manual apply
    # run) must never share a tmp file — a shared name let one os.replace() consume
    # the other's tmp and crash it. Last-writer-wins on the final file is fine for
    # a heartbeat; a lost merge self-heals on the next update.
    tmp = "{}.{}.tmp".format(path, os.getpid())
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main(argv):
    if len(argv) < 3:
        print("usage: health_update.py <job> start | health_update.py <job> <rc> [note]",
              file=sys.stderr)
        return 2
    job, action = argv[1], argv[2]
    if job not in JOBS:
        print("ERROR: unknown job '{}' (expected one of {})".format(job, sorted(JOBS)),
              file=sys.stderr)
        return 2

    state = _load(STATUS_PATH)
    entry = state.setdefault(job, {})

    if action == "start":
        entry["last_start"] = _now()
    else:
        try:
            rc = int(action)
        except ValueError:
            print("ERROR: expected 'start' or an integer rc, got '{}'".format(action),
                  file=sys.stderr)
            return 2
        note = argv[3] if len(argv) > 3 else ""
        now = _now()
        entry["last_end"] = now
        entry["last_rc"] = rc
        entry["note"] = note
        if rc == 0:
            entry["last_ok_end"] = now

    _atomic_write(STATUS_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
