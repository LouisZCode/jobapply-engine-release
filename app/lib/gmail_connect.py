#!/usr/bin/env python3
"""app/lib/gmail_connect.py — one-shot interactive Gmail consent for the wizard.

Run as a SUBPROCESS by the onboarding flow (and usable by hand):

    .venv/bin/python3 app/lib/gmail_connect.py

It performs the same OAuth dance inbox_reader/gmail_auth.py performs with
allow_consent=True, for BOTH token scopes, but imports only the two lightweight
google packages (google-auth + google-auth-oauthlib — NOT googleapiclient, which
gmail_auth.py pulls in for API calls; the wizard never calls the API, it only
mints the tokens the engine later uses).

Deliberate design points:
- The browser consent page opens in the DEFAULT browser (InstalledAppFlow's own
  behavior). Inside the Electron tower, external windows are handed to the system
  browser anyway, so this behaves identically there.
- Tokens land where gmail_auth.py reads them (token.json, token_modify.json in
  claude_applicator/inbox_reader/) — no new config anywhere. MULTI-002b: with
  JOBAPPLY_DATA set they land in THAT user's copy under the data root (the same files
  the engine reads through bind_user.sh's symlinks).
- Exit codes: 0 ok, 2 missing python deps, 3 missing credentials.json, 1 failure.
  Everything human-readable goes to stdout, one line at a time, so the calling
  route can show the tail verbatim.
"""
import os
import sys

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(APP_DIR)

# MULTI-002b: per-user Gmail files — this app-side WRITER (it mints/replaces the cached
# tokens) targets the user's copy under the data root. Unset = the engine directory
# (today's paths, byte-identical).
DATA_ROOT = os.environ.get("JOBAPPLY_DATA") or None
INBOX_DIR = (
    os.path.join(DATA_ROOT, "claude_applicator", "inbox_reader") if DATA_ROOT
    else os.path.join(REPO_ROOT, "claude_applicator", "inbox_reader")
)

CREDS = os.path.join(INBOX_DIR, "credentials.json")
SCOPES = [
    ("token.json", "https://www.googleapis.com/auth/gmail.readonly"),
    ("token_modify.json", "https://www.googleapis.com/auth/gmail.modify"),
]


def main():
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        print("MISSING_DEPS: {}".format(exc))
        return 2

    if not os.path.isfile(CREDS):
        print("NO_CREDENTIALS: expected {}".format(CREDS))
        return 3

    for token_name, scope in SCOPES:
        token_path = os.path.join(INBOX_DIR, token_name)
        if os.path.exists(token_path):
            try:
                creds = Credentials.from_authorized_user_file(token_path, [scope])
                if creds.valid:
                    print("OK (already connected): {}".format(token_name))
                    continue
            except Exception:
                pass  # unreadable/stale token — re-consent below
        print("CONSENT: {} — your browser opens; pick the account to use.".format(token_name))
        try:
            # run_local_server is what gmail_auth.py uses too: localhost redirect,
            # no copy-paste code needed. Success message lands in the browser tab.
            flow = InstalledAppFlow.from_client_secrets_file(CREDS, scopes=[scope])
            creds = flow.run_local_server(port=0, timeout_seconds=600, prompt="consent")
        except Exception as exc:
            print("FAILED: {} ({})".format(token_name, exc))
            return 1
        with open(token_path, "w", encoding="utf-8") as fh:
            fh.write(creds.to_json())
        print("CONNECTED: {}".format(token_name))

    print("DONE: Gmail is connected (read-only reader + reply labeling).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
