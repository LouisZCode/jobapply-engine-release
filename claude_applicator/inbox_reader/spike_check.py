#!/usr/bin/env python3
"""F-2 step-0 spike — prove a local, read-only Gmail read works from a plain subprocess.

Runs the one-time OAuth consent (opens a browser, caches token.json next to this file),
then reads the 5 newest message headers (From / Subject / Date) to confirm the
`gmail.readonly` scope works end to end. It NEVER sends, deletes, or modifies anything.

This is a throwaway verification, not the shipped reader. If it prints your address and a
handful of recent subjects, the auth foundation for the F-2 inbox reader is proven.
"""
import os
import sys

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]  # read-only, enforced at the token
HERE = os.path.dirname(os.path.abspath(__file__))
CREDS = os.path.join(HERE, "credentials.json")
TOKEN = os.path.join(HERE, "token.json")


def get_service():
    creds = None
    if os.path.exists(TOKEN):
        creds = Credentials.from_authorized_user_file(TOKEN, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # silent refresh — no browser
        else:
            if not os.path.exists(CREDS):
                sys.exit(f"[fail] missing OAuth client at {CREDS}")
            flow = InstalledAppFlow.from_client_secrets_file(CREDS, SCOPES)
            creds = flow.run_local_server(port=0)  # first run only: opens the browser
        with open(TOKEN, "w") as f:
            f.write(creds.to_json())
        os.chmod(TOKEN, 0o600)
    return build("gmail", "v1", credentials=creds)


def main():
    svc = get_service()
    profile = svc.users().getProfile(userId="me").execute()
    print(f"[ok] authenticated as: {profile.get('emailAddress')}")
    print(f"[ok] scope granted    : {SCOPES[0]}")

    resp = svc.users().messages().list(userId="me", maxResults=5).execute()
    msgs = resp.get("messages", [])
    print(f"[ok] read works — {len(msgs)} recent message(s):")
    for m in msgs:
        full = (
            svc.users()
            .messages()
            .get(
                userId="me",
                id=m["id"],
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )
        h = {x["name"]: x["value"] for x in full["payload"]["headers"]}
        print(
            f"   - {h.get('Date','?')[:25]:25} | "
            f"{h.get('From','?')[:38]:38} | {h.get('Subject','?')[:48]}"
        )
    print("\n[done] auth + read-only foundation for F-2 is PROVEN.")


if __name__ == "__main__":
    main()
