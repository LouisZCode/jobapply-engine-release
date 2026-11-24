#!/usr/bin/env python3
"""Unit tests for the F-2 reader's link-matching (the security-sensitive core).

No network / no Gmail — feeds synthetic payloads straight into the pure functions.
Run:  ./.venv/bin/python test_matching.py   (exits non-zero on any failure)
"""
import base64

from find_verification import _brand, _collect_links, _extract_code, _pick, _sender_ok


def _html_payload(html):
    data = base64.urlsafe_b64encode(html.encode()).decode()
    return {"mimeType": "text/html", "body": {"data": data}, "parts": []}


def _text_payload(text):
    data = base64.urlsafe_b64encode(text.encode()).decode()
    return {"mimeType": "text/plain", "body": {"data": data}, "parts": []}


def check(name, cond):
    print(("[ok] " if cond else "[FAIL] ") + name)
    if not cond:
        raise SystemExit(1)


# 1. English confirm link wins over homepage; unsubscribe + social are excluded.
p = _html_payload("""
  <p>Welcome, please confirm your email.</p>
  <a href="https://join.com/verify?token=abc123">Confirm your email</a>
  <a href="https://join.com/">Go to homepage</a>
  <a href="https://join.com/unsubscribe?u=1">Unsubscribe</a>
  <a href="https://facebook.com/joincom">Facebook</a>
""")
links = _collect_links(p)
link, conf, cands = _pick(links, ["join.com"])
check("extracts all 4 anchors", len(links) == 4)
check("picks the verify link", link == "https://join.com/verify?token=abc123")
check("confidence is high", conf == "high")
check("unsubscribe excluded from candidates", all("unsubscribe" not in c for c in cands))
check("facebook excluded from candidates", all("facebook" not in c for c in cands))

# 2. German "E-Mail bestätigen" is recognised.
p = _html_payload("""
  <a href="https://jobs.pwc.de/bestaetigen?t=x">E-Mail-Adresse bestätigen</a>
  <a href="https://jobs.pwc.de/datenschutz">Datenschutz</a>
""")
link, conf, _ = _pick(_collect_links(p), ["pwc.de"])
check("German bestätigen link picked", link == "https://jobs.pwc.de/bestaetigen?t=x")
check("German link is high confidence", conf == "high")

# 3. ESP click-tracker: keyword only in the anchor text, host is a tracker domain.
p = _html_payload("""
  <a href="https://url9999.mailer.io/ls/click?upn=xyz">Verify your account</a>
  <a href="https://url9999.mailer.io/ls/click?upn=home">Dashboard</a>
""")
link, conf, _ = _pick(_collect_links(p), ["join.com"])
check("tracker link chosen by anchor keyword", link == "https://url9999.mailer.io/ls/click?upn=xyz")
check("tracker link high confidence", conf == "high")

# 4. Ambiguous: several non-keyword links, none clearly the verify link -> skip.
p = _html_payload("""
  <a href="https://join.com/a">Option A</a>
  <a href="https://join.com/b">Option B</a>
  <a href="https://join.com/c">Option C</a>
""")
link, conf, _ = _pick(_collect_links(p), ["join.com"])
check("ambiguous -> no link (safe skip)", link is None)

# 5. Single obvious non-keyword link -> low confidence (still returned).
p = _html_payload('<a href="https://join.com/continue?s=1">Continue</a>')
link, conf, _ = _pick(_collect_links(p), ["join.com"])
check("single link returned", link == "https://join.com/continue?s=1")
check("single non-keyword link is low confidence", conf == "low")

# 6. Plain-text email with a bare confirm URL.
p = _text_payload("Confirm here: https://join.com/confirm/9f8a7 \nThanks")
link, conf, _ = _pick(_collect_links(p), ["join.com"])
check("plain-text URL extracted + picked", link == "https://join.com/confirm/9f8a7")

# 7. Nested multipart (multipart/alternative inside multipart/mixed) is walked.
inner = _html_payload('<a href="https://join.com/verify/deep">Confirm email</a>')
nested = {"mimeType": "multipart/mixed", "body": {}, "parts": [
    {"mimeType": "multipart/alternative", "body": {}, "parts": [inner]},
]}
link, conf, _ = _pick(_collect_links(nested), ["join.com"])
check("nested multipart link found", link == "https://join.com/verify/deep")

# 8. Only-noise email -> nothing returned.
p = _html_payload('<a href="https://join.com/unsubscribe">Unsubscribe</a>')
link, conf, _ = _pick(_collect_links(p), ["join.com"])
check("only-noise -> no link", link is None)

# ---- v2: OTP code extraction (--want code) -----------------------------------

# 9. The real PwC specimen (2026-07-06 live run), German plain text.
p = _text_payload(
    "Hallo Luis,\n\nDein Verifizierungscode lautet: 183773\n\n"
    "Bitte gib ihn auf der Webseite ein, um deine E-Mail-Adresse zu bestätigen.\n"
)
check("PwC German OTP extracted", _extract_code("", p) == "183773")

# 10. HTML body, English phrasing, code inside markup.
p = _html_payload("<p>Your verification code is <b>482913</b>. It expires in 10 minutes.</p>")
check("HTML English OTP extracted", _extract_code("", p) == "482913")

# 11. Code lives in the SUBJECT, body is boilerplate without it.
p = _text_payload("Open the app and enter the number shown above to continue.")
check("OTP taken from subject", _extract_code("729184 is your verification code", p) == "729184")

# 12. Keyword in the subject anchors a bare number in the body.
p = _text_payload("471209")
check("subject keyword anchors body-only digits",
      _extract_code("Dein Verifizierungscode", p) == "471209")

# 13. English one-time-passcode phrasing.
p = _text_payload("Use this one-time passcode to sign in: 55021\nIt is valid for 15 minutes.")
check("one-time passcode extracted", _extract_code("", p) == "55021")

# 14. Footer with PLZ + phone number but NO code -> None (nothing keyword-anchored).
p = _text_payload(
    "Danke für deine Registrierung!\n\n"
    "PwC GmbH, Kaiserstraße 12, 04109 Leipzig\nTelefon: +49 341 985612\n"
)
check("address/phone footer yields no code", _extract_code("", p) is None)

# 15. Phone number near the word 'code' is still rejected (negative context wins).
p = _text_payload("Enter the code we sent you. Questions? Phone 0341 4928123.")
check("phone digits near 'code' rejected", _extract_code("", p) is None)

# 16. Dates/years without a nearby keyword are not codes.
p = _text_payload(
    "Wir haben deine Bewerbung am 12.06.2026 erhalten und melden uns in Kürze "
    "mit den nächsten Schritten bei dir. Viele Grüße, dein Recruiting-Team."
)
check("date digits are not a code", _extract_code("", p) is None)

# ---- v2: brand token + sender fallback verification ---------------------------

# 17. Brand derivation from the registrable domain.
check("brand pwc.de -> pwc", _brand("pwc.de") == "pwc")
check("brand join.com -> join", _brand("join.com") == "join")
check("brand pwc.co.uk -> pwc (generic SLD skipped)", _brand("pwc.co.uk") == "pwc")
check("brand jobs.pwc.de -> pwc (subdomain tolerated)", _brand("jobs.pwc.de") == "pwc")
check("brand hp.com -> unusable (too short)", _brand("hp.com") == "")

# 18. Sender host must carry the brand as a dot-separated label.
check("campaign subdomain accepted",
      _sender_ok("PwC Careers <noreply@jobalerts-de.pwc.com>", ["pwc"]))
check("display-name mention alone rejected",
      not _sender_ok("PwC Alumni <news@mailer-esp.com>", ["pwc"]))
check("hyphen lookalike host rejected",
      not _sender_ok("Totally PwC <x@pwc-fake.evil.com>", ["pwc"]))
check("empty/garbled From rejected", not _sender_ok("", ["pwc"]))

print("\nAll matching tests passed.")
