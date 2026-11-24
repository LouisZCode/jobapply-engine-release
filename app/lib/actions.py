"""app/lib/actions.py — every subprocess/system call the app makes, in one place.

APP_TEST=1 guards the calls that reach OUTSIDE this repo's own files: opening a real
Chrome window, spending a real `claude -p` call against the user's account, opening a
Terminal window via AppleScript, and installing a launchd job into
~/Library/LaunchAgents. Under APP_TEST those are skipped and a clearly-labeled stub
result is returned instead, logged via print() so a test run's console shows exactly
what WOULD have run. Everything else here (rendering templates, initializing the DB,
editing run_config.txt, rendering — but not installing — the launchd plists) is local,
idempotent, and safe to actually run during tests.
"""
import base64
import json
import os
import re
import socket
import struct
import subprocess
import time
import urllib.error
import urllib.request

from . import paths, state  # noqa: E402  (state: M3 schedule-toggle persistence; actions must stay import-cycle-free — state imports paths/profile_io/activity only)

from . import paths, state

# The pipeline's dedicated Chrome-Agent profile always runs its DevTools port here —
# see claude_applicator/launch_chrome.sh, which this module shells out to.
# MULTI-001: per-user overrides — the launcher env-passes both values to
# launch_chrome.sh; unset env = byte-identical legacy behavior (9222, shared profile).
CDP_PORT = int(os.environ.get("JOBAPPLY_CDP_PORT", "9222"))
CDP_BASE = "http://127.0.0.1:{}".format(CDP_PORT)
CDP_JSON_URL = "{}/json".format(CDP_BASE)
# No url param: this Chrome build ignores it (the tab always opens on about:blank);
# navigation happens separately over the DevTools WebSocket (_ws_navigate).
CDP_NEW_TAB_URL = "{}/json/new".format(CDP_BASE)

# MULTI-001: desired Chrome user-data-dir for the honest-browser launcher (real Chrome
# via open -na — never Playwright-spawned). Default unchanged: the shared Chrome-Agent
# profile. Passed to launch_chrome.sh as env (see launch_linkedin_chrome below).
CHROME_PROFILE = os.environ.get("JOBAPPLY_CHROME_PROFILE") or os.path.join(
    os.path.expanduser("~"), "Library", "Application Support", "Google", "Chrome-Agent",
)


def test_mode():
    return os.environ.get("APP_TEST") == "1"


def _stub(label, would_run):
    print("[APP_TEST] skipped: {} — would run: {}".format(label, would_run))
    return {
        "ok": True,
        "stub": True,
        "returncode": None,
        "stdout": "",
        "stderr": "",
        "message": "APP_TEST=1 — skipped ({}). Would run: {}".format(label, would_run),
    }


def _run(cmd, cwd=None, timeout=30, input_text=None, env=None):
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd or paths.REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            env={**os.environ, **env} if env else None,
        )
        return {
            "ok": proc.returncode == 0,
            "stub": False,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "message": "",
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "stub": False,
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": (exc.stderr or "") + "\nTIMED OUT after {}s".format(timeout),
            "message": "timed out",
        }
    except FileNotFoundError as exc:
        return {
            "ok": False,
            "stub": False,
            "returncode": None,
            "stdout": "",
            "stderr": str(exc),
            "message": "command not found",
        }


# ------------------------------------------------------------------------------
# Profile / DB (never gated — local, idempotent, no external side effects)
# ------------------------------------------------------------------------------

def render_profile():
    return _run(["python3", paths.RENDER_TEMPLATES_PY], timeout=30)


def init_db():
    return _run(["bash", paths.SETUP_SH, "--init"], timeout=30)


# ------------------------------------------------------------------------------
# Connections (gated)
# ------------------------------------------------------------------------------

def _cdp_list_tabs(timeout=2):
    with urllib.request.urlopen(CDP_JSON_URL, timeout=timeout) as resp:
        return json.load(resp)


def _cdp_open_blank_tab(timeout=3):
    # Modern Chrome's /json/new only accepts PUT (GET is rejected). The new tab lands
    # on about:blank — but crucially this materializes a WINDOW even when Chrome is
    # running windowless (--no-startup-window background respawn).
    req = urllib.request.Request(CDP_NEW_TAB_URL, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _cdp_activate_tab(target_id, timeout=2):
    urllib.request.urlopen("{}/json/activate/{}".format(CDP_BASE, target_id), timeout=timeout)


def _ws_navigate(ws_url, url, timeout=5):
    """Sends a single Page.navigate over the tab's DevTools WebSocket, stdlib-only.
    Needed because this Chrome build ignores the url param on /json/new (the created
    tab always lands on about:blank) — the WS command is the only reliable way to
    navigate an existing tab without a real CDP client library."""
    m = re.match(r"ws://([\d.]+):(\d+)(/.*)", ws_url or "")
    if not m:
        raise OSError("unexpected webSocketDebuggerUrl: {!r}".format(ws_url))
    host, port, path = m.group(1), int(m.group(2)), m.group(3)
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        "GET {} HTTP/1.1\r\nHost: {}:{}\r\nUpgrade: websocket\r\n"
        "Connection: Upgrade\r\nSec-WebSocket-Key: {}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).format(path, host, port, key)
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(handshake.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise OSError("ws handshake failed (connection closed)")
            buf += chunk
        if b" 101 " not in buf.split(b"\r\n", 1)[0]:
            raise OSError("ws upgrade refused")
        payload = json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}).encode()
        header = b"\x81"  # FIN + text frame
        n = len(payload)
        if n < 126:
            header += bytes([0x80 | n])  # client frames must be masked
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        s.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        try:
            s.recv(4096)  # wait for the command's ack so the navigation is in flight
        except socket.timeout:
            pass


def _activate_chrome_app():
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Google Chrome" to activate'],
            capture_output=True, timeout=5,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass  # best-effort — bringing Chrome forward isn't essential


def _linkedin_result(ok, message, script_result=None):
    script_result = script_result or {}
    return {
        "ok": ok,
        "stub": False,
        "returncode": script_result.get("returncode"),
        "stdout": script_result.get("stdout", ""),
        "stderr": script_result.get("stderr", ""),
        "message": message,
    }


def _wait_linkedin_state(wait_s=8):
    """Polls briefly after opening LinkedIn. Returns "logged_in" (a /feed tab was seen —
    state.linkedin_login_status has already PERSISTED the green check at that moment, so
    the user can close the window right away without losing it; this was a real bug:
    verification used to depend on the page poll finding the tab still open later),
    "open_not_signed_in" (a linkedin.com tab is showing, but it's the login page — the
    normal fresh-user case), or "no_tab"."""
    last = "no_tab"
    for _ in range(wait_s):
        if state.linkedin_login_status(timeout=1.0) == "logged_in":
            return "logged_in"
        try:
            if any(
                t.get("type") == "page" and "linkedin.com" in (t.get("url") or "")
                for t in _cdp_list_tabs()
            ):
                last = "open_not_signed_in"
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(1)
    return last


_LINKEDIN_STATE_RESULTS = {
    "logged_in": (True, "LinkedIn is open and you're already signed in — connected. "
                        "You can close that window."),
    "open_not_signed_in": (True, "LinkedIn is open in the app's Chrome window — sign in "
                                 "there; this page will turn green by itself."),
    "no_tab": (False, "Chrome is running but LinkedIn didn't load — in the Chrome "
                      "window, go to linkedin.com and sign in. This page still turns "
                      "green by itself."),
}


def launch_linkedin_chrome():
    """Gets a VISIBLE LinkedIn tab in front of the user, whatever state Chrome-Agent is
    in — including the windowless leftover Chrome's own background mode respawns with
    --no-startup-window (it holds the CDP port with zero tabs, so launch_chrome.sh
    reports "reusing" and nothing appears; killing it is pointless, it comes back).
    Cases, checked over CDP first:
      - a linkedin.com tab already exists -> activate it
      - port up (with or without other tabs) -> open a tab via /json/new (materializes
        a window even on the windowless instance) and Page.navigate it over the
        DevTools WebSocket (see _ws_navigate); other tabs are never touched, so a
        mid-flight apply run can't be disturbed
      - port unreachable -> cold launch via launch_chrome.sh (opens the feed URL)"""
    if test_mode():
        return _stub(
            "LinkedIn Chrome launcher",
            "bash {} ; ensure a visible LinkedIn tab over CDP :{}".format(paths.LAUNCH_CHROME_SH, CDP_PORT),
        )

    try:
        tabs = _cdp_list_tabs()
    except (urllib.error.URLError, OSError, ValueError):
        tabs = None

    if tabs is None:
        script_result = _run(
            ["bash", paths.LAUNCH_CHROME_SH], timeout=30,
            # MULTI-001: env overlay — launcher script reads JOBAPPLY_CHROME_PROFILE /
            # JOBAPPLY_CDP_PORT; the launch mechanism itself (open -na real Chrome) is
            # untouched.
            env={"JOBAPPLY_CHROME_PROFILE": CHROME_PROFILE, "JOBAPPLY_CDP_PORT": str(CDP_PORT)},
        )
        outcome = _wait_linkedin_state()
        _activate_chrome_app()
        ok, message = _LINKEDIN_STATE_RESULTS[outcome]
        if outcome == "no_tab":
            message = ("Chrome launched but LinkedIn didn't open — in the Chrome "
                       "window, go to linkedin.com and sign in. This page still turns "
                       "green by itself.")
        return _linkedin_result(ok, message, script_result)

    li = next(
        (t for t in tabs if t.get("type") == "page" and "linkedin.com" in (t.get("url") or "")),
        None,
    )
    if li is not None:
        try:
            _cdp_activate_tab(li["id"])
        except (urllib.error.URLError, OSError, KeyError):
            pass
        outcome = _wait_linkedin_state(wait_s=3)
        _activate_chrome_app()
        ok, message = _LINKEDIN_STATE_RESULTS[outcome]
        if outcome == "open_not_signed_in":
            message = ("Brought the existing LinkedIn tab to the front — sign in "
                       "there; this page will turn green by itself.")
        return _linkedin_result(ok, message)

    try:
        tab = _cdp_open_blank_tab()
        _ws_navigate(tab.get("webSocketDebuggerUrl"), "https://www.linkedin.com/feed/")
        if tab.get("id"):
            _cdp_activate_tab(tab["id"])
    except (urllib.error.URLError, OSError, ValueError):
        _activate_chrome_app()
        return _linkedin_result(
            False, "Chrome is running but LinkedIn couldn't be opened automatically — "
                   "in the Chrome window, go to linkedin.com and sign in. This page "
                   "still turns green by itself.",
        )

    outcome = _wait_linkedin_state()
    _activate_chrome_app()
    ok, message = _LINKEDIN_STATE_RESULTS[outcome]
    return _linkedin_result(ok, message)


def probe_claude():
    if test_mode():
        return _stub("claude CLI probe", 'claude -p "reply with exactly: ok"')
    return _run(["claude", "-p", "reply with exactly: ok"], timeout=60)


def open_terminal_for_claude_login():
    if test_mode():
        return _stub(
            "open Terminal for claude login",
            'osascript -e \'tell application "Terminal" to do script "claude"\'',
        )
    script = 'tell application "Terminal" to do script "claude"'
    return _run(["osascript", "-e", script], timeout=15)


# ------------------------------------------------------------------------------
# Schedule (rendering is safe/local; install is gated)
# ------------------------------------------------------------------------------

def render_schedule(hh_mm):
    return _run(["bash", paths.SETUP_SH, "--schedule", hh_mm], timeout=30)


def label_prefix():
    import getpass

    return "com.{}".format(getpass.getuser())


def install_schedule():
    """Copies the two generated plists into ~/Library/LaunchAgents and loads them
    with `launchctl bootstrap gui/$UID <plist>`, boot-out-ing first if already loaded
    (idempotent re-install). Gated behind APP_TEST."""
    prefix = label_prefix()
    plists = [
        ("{}.jobhealth.plist".format(prefix), os.path.join(paths.GENERATED_DIR, "{}.jobhealth.plist".format(prefix))),
        ("{}.jobapply.plist".format(prefix), os.path.join(paths.GENERATED_DIR, "{}.jobapply.plist".format(prefix))),
    ]
    if test_mode():
        would = "; ".join(
            "cp {} ~/Library/LaunchAgents/{} && launchctl bootout gui/$UID/... ; "
            "launchctl bootstrap gui/$UID ~/Library/LaunchAgents/{}".format(src, name, name)
            for name, src in plists
        )
        return _stub("install launch agents", would)

    uid = str(os.getuid())
    steps = []
    ok = True
    for name, src in plists:
        if not os.path.isfile(src):
            steps.append("SKIP {} — not found (run render first)".format(src))
            ok = False
            continue
        dest = os.path.join(paths.LAUNCH_AGENTS_DIR, name)
        # Same-label collision guard: launchd labels derive from the macOS username, so
        # a SECOND copy of this pipeline on the same account (e.g. a dev checkout and a
        # /Users/Shared install) would silently OVERWRITE the other copy's schedule.
        # If the installed plist points at a different directory than this install,
        # refuse — replacing it must be a deliberate manual act, not a button click.
        if os.path.isfile(dest):
            with open(dest, encoding="utf-8", errors="replace") as prev:
                installed = prev.read()
            if paths.REPO_ROOT not in installed:
                steps.append(
                    "REFUSED {} — a schedule with this name already exists on this Mac "
                    "account and belongs to a different copy of the pipeline (not {}). "
                    "Not touching it. If you really mean to replace it, remove it "
                    "yourself first: launchctl bootout gui/{}/{} && rm {}".format(
                        name, paths.REPO_ROOT, uid, name.replace(".plist", ""), dest
                    )
                )
                ok = False
                continue
        os.makedirs(paths.LAUNCH_AGENTS_DIR, exist_ok=True)
        cp = _run(["cp", src, dest])
        steps.append("cp {} -> {} : {}".format(src, dest, "ok" if cp["ok"] else cp["stderr"]))
        ok = ok and cp["ok"]

        # bootout first (ignore failure — fine if it wasn't loaded), then bootstrap.
        bootout = _run(["launchctl", "bootout", "gui/{}/{}".format(uid, name.replace(".plist", ""))])
        steps.append("launchctl bootout ... : rc={}".format(bootout["returncode"]))

        bootstrap = _run(["launchctl", "bootstrap", "gui/{}".format(uid), dest])
        steps.append(
            "launchctl bootstrap gui/{} {} : {}".format(
                uid, dest, "ok" if bootstrap["ok"] else bootstrap["stderr"]
            )
        )
        ok = ok and bootstrap["ok"]

    return {
        "ok": ok,
        "stub": False,
        "returncode": 0 if ok else 1,
        "stdout": "\n".join(steps),
        "stderr": "" if ok else "one or more install steps failed — see output above",
        "message": "",
    }


def prewake_time(hh_mm):
    """Mirrors setup.sh cmd_schedule's pre-wake math: one minute before the given HH:MM
    run time, expressed as the "HH:MM:30" pmset wants. Returns None on invalid input."""
    m = re.match(r"^([01]\d|2[0-3]):([0-5]\d)$", hh_mm or "")
    if not m:
        return None
    run_h, run_m = int(m.group(1)), int(m.group(2))
    pw_h, pw_m = run_h, run_m - 1
    if pw_m < 0:
        pw_m = 59
        pw_h = (run_h - 1 + 24) % 24
    return "{:02d}:{:02d}:30".format(pw_h, pw_m)


def pmset_command(hh_mm):
    """Purely informational — the plain sudo command line, kept around for anything
    that still wants the raw string (the Home page's keep-awake toggle below runs the
    equivalent through osascript instead, so this app never needs the user's sudo
    password directly)."""
    pw = prewake_time(hh_mm)
    if pw is None:
        return None
    return "sudo pmset repeat wakeorpoweron MTWRFSU {}".format(pw)


def keep_awake_status():
    """Read-only check of the current pmset wake schedule (`pmset -g sched` needs no
    sudo). A repeat line for the "wakeorpoweron" event type means the daily pre-wake
    this app's Home-page toggle installs is active; its time is pulled out so the
    toggle always reflects reality (e.g. someone could have changed it by hand in
    Terminal), not just whatever this app last did.

    macOS actually PRINTS that event type as "wakepoweron" (no "or") in `-g sched`
    output, and shows the time 12-hour with no seconds ("5:00AM") even though it was
    set with seconds in 24-hour form — matched here against real output on a live
    Mac, not just the set command's own syntax."""
    if test_mode():
        if os.environ.get("APP_TEST_PMSET") == "on":
            return {"on": True, "time": "05:00:30", "error": ""}
        return {"on": False, "time": "", "error": ""}

    result = _run(["pmset", "-g", "sched"], timeout=10)
    if not result["ok"]:
        return {"on": False, "time": "", "error": (result["stderr"] or result["message"] or "").strip()}

    for line in result["stdout"].splitlines():
        if re.search(r"wake\w*poweron", line):
            m = re.search(r"\d{1,2}:\d{2}(?::\d{2})?\s*(?:[AP]M)?", line, re.IGNORECASE)
            return {"on": True, "time": m.group(0) if m else "", "error": ""}
    return {"on": False, "time": "", "error": ""}


def set_keep_awake(turn_on, hh_mm):
    """Flips the daily pre-wake on or off. Runs pmset directly as root via osascript's
    "with administrator privileges" clause — that makes macOS itself pop the password
    dialog, so this app's own process never sees or handles the password. Gated behind
    test_mode() like every other real system-changing call in this module; the 180s
    timeout is generous because osascript blocks on the user actually typing (or
    canceling) that dialog."""
    pw = prewake_time(hh_mm)
    if turn_on:
        cmd = "/usr/bin/pmset repeat wakeorpoweron MTWRFSU {}".format(pw or "??:??:30")
    else:
        cmd = "/usr/bin/pmset repeat cancel"

    if test_mode():
        return _stub("keep-awake toggle ({})".format("on" if turn_on else "off"), cmd)

    if turn_on and pw is None:
        return {
            "ok": False,
            "stub": False,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "message": "invalid run time",
        }

    script = 'do shell script "{}" with administrator privileges'.format(cmd)
    result = _run(["osascript", "-e", script], timeout=180)
    stderr_lower = (result["stderr"] or "").lower()
    if not result["ok"] and ("canceled" in stderr_lower or "cancelled" in stderr_lower):
        result = dict(result, message="You canceled the password dialog — nothing was changed.")
    return result


def update_run_config(mode, max_applies):
    """Rewrites ONLY the MODE and MAX_APPLIES_PER_RUN keys in run_config.txt, preserving
    every other line byte-for-byte (comments, other knobs, ordering)."""
    with open(paths.RUN_CONFIG, encoding="utf-8") as fh:
        lines = fh.readlines()

    def replace_key(lines, key, value):
        # Captures (key + colon + leading whitespace), (old value token), (everything
        # else on the line verbatim — inline-comment spacing, comment text, and the
        # trailing newline itself). DOTALL + no `$` anchor so group(3) keeps the
        # newline (a bare `(.*)$` would exclude it, silently eating the line break).
        # Only the value token is swapped, so alignment/comments are preserved exactly.
        pattern = re.compile(r"^({}:\s*)(\S+)(.*)".format(re.escape(key)), re.DOTALL)
        found = False
        out = []
        for line in lines:
            m = pattern.match(line)
            if m:
                out.append("{}{}{}".format(m.group(1), value, m.group(3)))
                found = True
            else:
                out.append(line)
        return out, found

    lines, found_mode = replace_key(lines, "MODE", mode)
    lines, found_max = replace_key(lines, "MAX_APPLIES_PER_RUN", max_applies)

    with open(paths.RUN_CONFIG, "w", encoding="utf-8") as fh:
        fh.writelines(lines)

    return found_mode and found_max


# ------------------------------------------------------------------------------
# M3: schedule state + toggle + health heartbeat (read-only). Same launchd
# verbs install_schedule() already uses; the health snapshot parses the SAME
# health_status.json check_health.sh reads — but read-only and NEVER notifying
# (a UI heartbeat must not double-fire the 09:00 watchdog's notifications).
# ------------------------------------------------------------------------------

def _launchd_label(agent):
    return "{}.{}".format(label_prefix(), agent)


def _agent_installed(agent):
    """True only when the installed plist belongs to THIS checkout. The label
    derives from the macOS username, so two copies of the pipeline on one account
    (e.g. the production repo + this product install) collide on the same name —
    same guard as install_schedule: a foreign plist counts as NOT installed here,
    so no button in this app can ever load/unload the other copy's schedule."""
    dest = os.path.join(paths.LAUNCH_AGENTS_DIR, _launchd_label(agent) + ".plist")
    if not os.path.isfile(dest):
        return False
    try:
        with open(dest, encoding="utf-8", errors="replace") as fh:
            return paths.REPO_ROOT in fh.read()
    except OSError:
        return False


def _agent_loaded(agent):
    """launchctl print succeeds (rc 0) exactly when the label is loaded in the
    user's gui domain. Cheap, no side effects, safe on every view."""
    if test_mode():
        # Stub the same way install does: pretend the state the UI last wrote.
        st = state.load_state()
        return bool(st.get("schedule_loaded_" + agent, _agent_installed(agent)))
    uid = str(os.getuid())
    res = _run(["launchctl", "print", "gui/{}/{}".format(uid, _launchd_label(agent))], timeout=10)
    return bool(res["ok"])


def schedule_agent_state():
    """The Schedule/Home UI's one-stop status: install + load state of the daily
    apply agent and the health watchdog."""
    return {
        "apply": {"installed": _agent_installed("jobapply"), "loaded": _agent_loaded("jobapply")},
        "health": {"installed": _agent_installed("jobhealth"), "loaded": _agent_loaded("jobhealth")},
    }


def schedule_toggle(enabled):
    """Pause/resume the DAILY RUN (the jobapply agent) without uninstalling it:
    bootstrap = resume, bootout = pause. The health watchdog stays as it is —
    it should keep watching (a paused pipeline that silently rots is exactly
    what RB-1 exists to catch). Idempotent both ways."""
    agent = "jobapply"
    label = _launchd_label(agent)
    dest = os.path.join(paths.LAUNCH_AGENTS_DIR, label + ".plist")
    if test_mode():
        state.update_state(schedule_loaded_jobapply=bool(enabled))
        return _stub(
            "launchctl {}".format("bootstrap" if enabled else "bootout"),
            "launchctl {} gui/$UID/{}".format("bootstrap" if enabled else "bootout", label),
        )
    if not _agent_installed(agent):
        other = os.path.isfile(os.path.join(paths.LAUNCH_AGENTS_DIR, _launchd_label(agent) + ".plist"))
        return {
            "ok": False, "stdout": "", "stderr": "",
            "message": (
                "A schedule named {} exists on this Mac but belongs to a DIFFERENT copy of "
                "the pipeline (not {}). This app won't touch it — remove it manually first if "
                "you really mean to.".format(_launchd_label(agent), paths.REPO_ROOT)
            ) if other else
            "The daily schedule isn't installed yet — save & install it below first.",
        }
    uid = str(os.getuid())
    if enabled:
        # bootout first (ignore rc — fine if it wasn't loaded), then bootstrap.
        _run(["launchctl", "bootout", "gui/{}/{}".format(uid, label)], timeout=15)
        res = _run(["launchctl", "bootstrap", "gui/{}".format(uid), dest], timeout=15)
    else:
        res = _run(["launchctl", "bootout", "gui/{}/{}".format(uid, label)], timeout=15)
        res = {"ok": True, "stdout": res.get("stdout") or "", "stderr": res.get("stderr") or "",
               "message": "Daily run paused — the schedule stays installed, just switched off."}
    state.update_state(schedule_loaded_jobapply=bool(enabled) and bool(res.get("ok")))
    return {"ok": bool(res.get("ok")), "stdout": (res.get("stdout") or "") + (res.get("message") or ""),
            "stderr": res.get("stderr") or "", "message": res.get("message") or ""}


def health_snapshot():
    """Read-only view of output/health_status.json in check_health.sh's own
    terms (apply OK today? sweep stale >26h? last rc?), minus the notifications.
    Returns a plain dict for the Schedule + Home templates; never raises."""
    import datetime
    import json as _json

    path = os.environ.get(
        "APP_TEST_HEALTH_STATUS",
        os.path.join(paths.ENGINE_DIR, "output", "health_status.json"),
    )
    try:
        with open(path, encoding="utf-8") as fh:
            data = _json.load(fh)
    except (OSError, ValueError):
        data = {}

    def entry(job):
        return data.get(job) or {}

    apply_e, sweep_e = entry("apply"), entry("sweep")
    now = datetime.datetime.now()
    today = datetime.date.today().isoformat()

    def age_hh(iso):
        try:
            return round((now - datetime.datetime.fromisoformat(iso)).total_seconds() / 3600.0, 1)
        except (TypeError, ValueError):
            return None

    apply_ok_day = (apply_e.get("last_ok_end") or "")[:10]
    sweep_age = age_hh(sweep_e.get("last_ok_end"))
    return {
        "exists": bool(data),
        "apply": {
            "ok_today": apply_ok_day == today,
            "last_ok_end": apply_e.get("last_ok_end") or "",
            "last_rc": apply_e.get("last_rc"),
            "note": apply_e.get("note") or "",
            "last_start": apply_e.get("last_start") or "",
        },
        "sweep": {
            "last_ok_end": sweep_e.get("last_ok_end") or "",
            "stale": sweep_age is None or sweep_age > 26,
            "age_hours": sweep_age,
        },
    }
