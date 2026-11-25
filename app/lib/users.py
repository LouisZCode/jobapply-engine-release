"""app/lib/users.py — MULTI-002: several people, one Mac, one app.

One server process serves one user (or nobody — login mode). This module owns the
users directory (JOBAPPLY_USERS_DIR, default <JOBAPPLY_ENGINE_HOME or ~/JobApply>/users):
one folder per user with user.json (identity + the env the switch loop needs),
session.token (the one valid session-cookie value for that user) and that user's data
root. Switching is cooperative, never in-process: request_switch() writes the next user
(or "login") to <users dir>/.switch and the caller ends the process with exit code 62;
app/run_app.sh's loop reads the file, exports that user's env and restarts the server
on the same port. Nothing here talks to the network.

Passwords are a shared-Mac separator, NOT a vault: the minimum is 4 characters and the
PBKDF2 on disk exists so a sibling can't open your pages by clicking your name — not
so a determined local user can't read the file. Same honesty applies to session tokens.
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import time

from . import paths

USERS_DIR = os.environ.get("JOBAPPLY_USERS_DIR") or os.path.join(
    os.environ.get("JOBAPPLY_ENGINE_HOME") or os.path.expanduser("~/JobApply"), "users"
)

SWITCH_FILE = os.path.join(USERS_DIR, ".switch")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,23}$")

# The old single-user layout, as (path under a data root, legacy repo location) — the
# same relative layout lib/paths.py relocates under JOBAPPLY_DATA, evaluated against
# ENGINE_DIR/APP_DIR so it points at the LEGACY spots even when JOBAPPLY_DATA is set
# (the relocated constants would otherwise hide them).
_ADOPT_ITEMS = (
    ("profile.yaml", os.path.join(paths.ENGINE_DIR, "profile.yaml")),
    ("qa_bank.md", os.path.join(paths.ENGINE_DIR, "qa_bank.md")),
    ("data", os.path.join(paths.ENGINE_DIR, "data")),
    ("applications.db", os.path.join(paths.ENGINE_DIR, "applications.db")),
    ("run_config.txt", os.path.join(paths.ENGINE_DIR, "run_config.txt")),
    ("output/costs.csv", os.path.join(paths.ENGINE_DIR, "output", "costs.csv")),
    ("app/app_state.json", os.path.join(paths.APP_DIR, "app_state.json")),
    ("app/activity.ndjson", os.path.join(paths.APP_DIR, "activity.ndjson")),
    ("app/jobs", os.path.join(paths.APP_DIR, "jobs")),
    ("app/keys.json", os.path.join(paths.APP_DIR, "keys.json")),
)
# What makes a Mac count as "a setup from an earlier version is on this Mac": PERSONAL
# files only. run_config.txt ships with the product (a tracked default) and a stray
# costs.csv says nothing, so neither counts; data/ counts when it holds a CV; an empty
# applications.db is what a mere first launch creates, so it counts only with rows.
_PRESENCE_ITEMS = tuple(item for item in _ADOPT_ITEMS
                       if item[0] not in ("run_config.txt", "output/costs.csv"))


def login_mode():
    """True when the app knows no user: JOBAPPLY_DATA unset or empty."""
    return not (os.environ.get("JOBAPPLY_DATA") or "").strip()


def active_user():
    """The user this process serves (env JOBAPPLY_USER), or None."""
    return os.environ.get("JOBAPPLY_USER") or None


def clean_name(raw):
    return (raw or "").strip().lower()


def name_ok(name):
    return bool(NAME_RE.match(name or ""))


# ------------------------------------------------------------------------------
# Passwords — PBKDF2-HMAC-SHA256, 240k iterations, 16-byte salt. See the module
# docstring for the honesty note: separator, not vault.
# ------------------------------------------------------------------------------

def hash_password(password, salt=None):
    """PBKDF2-HMAC-SHA256 (240000 iterations, random 16-byte salt) → the dict stored in
    user.json. `salt` (bytes) exists for verify_password's re-derivation (and tests);
    every other caller passes None and gets a fresh salt. Raises ValueError under the
    4-character minimum."""
    if not isinstance(password, str) or len(password) < 4:
        raise ValueError("Password must be at least 4 characters.")
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 240000)
    return {
        "algorithm": "pbkdf2_sha256",
        "iterations": 240000,
        "salt": salt.hex(),
        "hash": digest.hex(),
    }


def verify_password(stored, password):
    """Constant-time check of `password` against a stored hash_password() dict. False —
    never an exception — when anything is missing or malformed."""
    if not isinstance(stored, dict) or not isinstance(password, str):
        return False
    if stored.get("algorithm") != "pbkdf2_sha256":
        return False
    salt_hex, hash_hex = stored.get("salt"), stored.get("hash")
    if not isinstance(salt_hex, str) or not isinstance(hash_hex, str):
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        iterations = int(stored.get("iterations"))
    except (TypeError, ValueError):
        return False
    if iterations < 1:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest.hex(), hash_hex)


# ------------------------------------------------------------------------------
# The users directory
# ------------------------------------------------------------------------------

def user_path(name):
    return os.path.join(USERS_DIR, name, "user.json")


def list_users():
    """Sorted names of the users that really exist (a directory with a user.json)."""
    if not os.path.isdir(USERS_DIR):
        return []
    return sorted(
        entry for entry in os.listdir(USERS_DIR) if os.path.isfile(user_path(entry))
    )


def load(name):
    """user.json as a dict, or None when the user doesn't exist (or the file is bad)."""
    if not name:
        return None
    try:
        with open(user_path(name), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_private(path, text):
    """Write `text` to `path` with mode 0600 (creates parent dirs as needed)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    # os.open's mode only applies at creation; enforce it for re-writes too.
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _next_cdp_port():
    """9222 + the smallest index >= 1 not used by another user's user.json."""
    used = set()
    for name in list_users():
        port = (load(name) or {}).get("cdp_port")
        if isinstance(port, int):
            used.add(port)
    port = 9223
    while port in used:
        port += 1
    return port


def create(name, password, chrome_profile=None):
    """Create USERS_DIR/<name>/ with user.json (mode 0600) and the data root directory.
    Raises ValueError (short message) when the name is invalid or already taken."""
    name = clean_name(name)
    if not name_ok(name):
        raise ValueError("Name must be 3-24 characters: lowercase letters, numbers, - or _.")
    if load(name) is not None or os.path.exists(user_path(name)):
        raise ValueError("That name is already taken.")
    record = {
        "name": name,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "password": hash_password(password),
        "data_root": os.path.join(USERS_DIR, name, "data"),
        "chrome_profile": chrome_profile or os.path.expanduser(
            "~/Library/Application Support/Google/Chrome-Agent-{}".format(name)
        ),
        "cdp_port": _next_cdp_port(),
    }
    os.makedirs(record["data_root"], exist_ok=True)
    # The run switches every user needs (MODE, caps, …): start from the product's own
    # default file. An adopted setup copies the person's edited copy over it afterwards.
    default_run_config = os.path.join(paths.ENGINE_DIR, "run_config.txt")
    user_run_config = os.path.join(record["data_root"], "run_config.txt")
    if os.path.isfile(default_run_config) and not os.path.exists(user_run_config):
        shutil.copy2(default_run_config, user_run_config)
    _write_private(user_path(name), json.dumps(record, indent=2) + "\n")
    return record


def env_for(name):
    """The env the switch loop exports for this user (values as strings, ready for
    os.environ). Raises ValueError when the user doesn't exist."""
    record = load(name)
    if record is None:
        raise ValueError("No such user.")
    return {
        "JOBAPPLY_DATA": record.get("data_root"),
        "JOBAPPLY_USER": record.get("name"),
        "JOBAPPLY_CHROME_PROFILE": record.get("chrome_profile"),
        "JOBAPPLY_CDP_PORT": str(record.get("cdp_port")),
    }


# ------------------------------------------------------------------------------
# Sessions — one token file per user; the cookie is only valid while it matches.
# ------------------------------------------------------------------------------

def issue_session(name):
    """Write a fresh session token for `name` (session.token, mode 0600) and return it.
    Issuing replaces any previous token — one live session per user."""
    token = secrets.token_urlsafe(32)
    _write_private(os.path.join(USERS_DIR, name, "session.token"), token)
    return token


def session_valid(name, token):
    """True when `token` is exactly the one in <name>/session.token (constant-time).
    False when the user, the file or the token is missing."""
    if not name or not token or not isinstance(token, str):
        return False
    try:
        with open(os.path.join(USERS_DIR, name, "session.token"), encoding="utf-8") as fh:
            stored = fh.read().strip()
    except OSError:
        return False
    if not stored:
        return False
    return hmac.compare_digest(stored, token)


def clear_session(name):
    """Delete <name>/session.token (missing file is fine, not an error)."""
    if not name:
        return
    try:
        os.remove(os.path.join(USERS_DIR, name, "session.token"))
    except OSError:
        pass


# ------------------------------------------------------------------------------
# Switching + legacy adoption
# ------------------------------------------------------------------------------

def request_switch(next):
    """Ask the run_app.sh loop to restart the server for `next` ("login" or a user
    name): written atomically (temp file in the same dir + os.replace) so the loop
    never reads a half-written value."""
    os.makedirs(USERS_DIR, exist_ok=True)
    tmp = os.path.join(USERS_DIR, ".switch.tmp.{}".format(os.getpid()))
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write((next or "login").strip() + "\n")
    os.replace(tmp, SWITCH_FILE)


def legacy_setup_present():
    """True when at least one of the old single-user files exists at its legacy
    (repo) location — computed from ENGINE_DIR/APP_DIR, never the JOBAPPLY_DATA-relocated
    constants, so this also sees the old files while running as a user."""
    for rel, legacy_path in _PRESENCE_ITEMS:
        if not os.path.exists(legacy_path):
            continue
        if rel == "data":
            if os.path.isdir(legacy_path) and os.listdir(legacy_path):
                return True
            continue
        if rel == "applications.db":
            try:
                import sqlite3
                con = sqlite3.connect("file:{}?mode=ro".format(legacy_path), uri=True)
                try:
                    if con.execute("SELECT 1 FROM applications LIMIT 1").fetchone():
                        return True
                finally:
                    con.close()
            except sqlite3.Error:
                continue
            continue
        return True
    return False


def adopt_legacy(data_root):
    """Copy the legacy single-user files into a new user's data root, preserving names
    and the relative layout lib/paths.py uses under JOBAPPLY_DATA. Copies only what
    exists, never deletes the originals, and returns the copied relative paths."""
    copied = []
    for rel, legacy_path in _ADOPT_ITEMS:
        if not os.path.exists(legacy_path):
            continue
        dest = os.path.join(data_root, rel)
        os.makedirs(os.path.dirname(dest) or data_root, exist_ok=True)
        if os.path.isdir(legacy_path):
            shutil.copytree(legacy_path, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(legacy_path, dest)
        copied.append(rel)
    return copied
