#!/usr/bin/env python3
"""F-2 inbox reader — find an email-verification link OR one-time code for the apply agent.

The applicator phones this the moment an external ATS shows a "click the link we
emailed you" wall (--want link) or an "enter the code we emailed you" wall
(--want code). It searches ONLY mail sent from the named platform domain(s),
sent AFTER the trigger timestamp, extracts the verification link/code, and
returns it as JSON. It never sends / deletes / modifies mail, never opens the
link (the applicator does that in its own paced browser), never types the code
(ditto), and never prints email bodies.

Sender matching (v2): the strict `from:(<domain>)` query is tried first; if it
finds nothing, a fallback query on the BRAND token runs (pwc.de → pwc), and a
hit only counts if the brand is a dot-separated label of the actual sender host
(jobalerts-de.pwc.com ✓, pwc-fake.evil.com ✗). Fallback hits are flagged
"sender_match":"fallback" so run logs stay auditable.

Contract — stdout is STRICT JSON, exactly one object:
  found     : {"status":"found","link":"…","confidence":"high"|"low",   (--want link)
               "from":…,"subject":…,"date":…,"message_id":…,"candidates":[…],
               "sender_match":"exact"|"fallback"}
              {"status":"found","code":"NNNNNN","confidence":"high",…}  (--want code)
  not_found : {"status":"not_found","reason":…,"checked":N,"waited_seconds":S}
  error     : {"status":"error","reason":…}

Anything but a confident `found` → the applicator does its normal skip-and-log,
so this feature is strictly additive (worst case == today's behaviour).

Usage:
  find_verification.py --sender-domain join.com --since <epoch> \
      [--want link|code] [--wait-seconds 90] [--poll-interval 15] [--debug]
"""
import argparse
import base64
import json
import re
import sys
import time
from html.parser import HTMLParser

from gmail_auth import get_service

# A link is a verification link if its URL or its anchor text hits this (EN + DE).
STRONG_KW = re.compile(
    r"verif|confirm|activat|validat|magic"
    r"|best(ä|ae?)tig|aktivier|verifizier",  # bestätigen / aktivieren / verifizieren
    re.I,
)
# Never return these, even from a legit platform mail (unsubscribe would opt out!).
NOISE = re.compile(
    r"unsubscrib|abmeld|/privacy|datenschutz|/terms|/agb|/impressum|/help|/support"
    r"|mailto:|facebook\.com|twitter\.com|x\.com|linkedin\.com|instagram\.com|youtube\.com",
    re.I,
)
URL_RE = re.compile(r"https?://[^\s<>\"'\)\]]+", re.I)

# --- OTP codes (--want code) ---------------------------------------------------
# A 4-8 digit number is a code ONLY if a code keyword sits within ~60 chars of it
# (the anchor is what stops us returning a zip code, a date, or a phone number).
CODE_NUM = re.compile(r"\b\d{4,8}\b")
CODE_KW = re.compile(  # short/ambiguous words need \b; long stems are safe bare (EN + DE)
    r"\b(?:code|otp|pin|passcode|token)\b"
    r"|einmal|one[-\s]?time|verifizier|best(?:ä|ae?)tig|sicherheits|zugangs",
    re.I,
)
# Context that marks the nearby number as an address/phone artifact, NOT a code.
# Checked BEFORE the keyword anchor and wins ties (conservative → fail-safe skip).
CODE_NEG = re.compile(
    r"plz|postleitzahl|postal|post ?code|zip|area code|vorwahl|telefon|tel[.:]|phone|fax",
    re.I,
)

# Second-level labels that are TLD plumbing, not the brand (pwc.co.uk → pwc, not co).
_GENERIC_SLD = {"co", "com", "org", "net", "gov", "ac", "edu"}


class _Links(HTMLParser):
    """Collect (href, anchor_text) pairs from HTML anchors."""

    def __init__(self):
        super().__init__()
        self.links = []
        self._href = None
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href and href.lower().startswith(("http://", "https://")):
                self._href, self._buf = href, []

    def handle_data(self, data):
        if self._href is not None:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._buf).split())))
            self._href, self._buf = None, []


class _TextExtract(HTMLParser):
    """Collect the visible text of an HTML body (script/style dropped)."""

    def __init__(self):
        super().__init__()
        self.chunks = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.chunks.append(data)


def _html_to_text(html):
    ex = _TextExtract()
    try:
        ex.feed(html)
    except Exception:
        pass
    return " ".join(" ".join(ex.chunks).split())


def _decode_parts(payload):
    """Yield (mimeType, text) for every text/html and text/plain part (recursive)."""
    stack = [payload]
    while stack:
        p = stack.pop()
        mt = p.get("mimeType", "")
        data = (p.get("body") or {}).get("data")
        if data and mt in ("text/html", "text/plain"):
            try:
                yield mt, base64.urlsafe_b64decode(data.encode()).decode("utf-8", "replace")
            except Exception:
                pass
        for sub in p.get("parts") or []:
            stack.append(sub)


def _collect_links(payload):
    """Return [(href, anchor_text)] from HTML anchors + bare URLs from text parts."""
    out, seen = [], set()
    for mt, text in _decode_parts(payload):
        if mt == "text/html":
            ex = _Links()
            try:
                ex.feed(text)
            except Exception:
                pass
            pairs = ex.links
        else:
            pairs = [(u.rstrip(".,);]"), "") for u in URL_RE.findall(text)]
        for href, atext in pairs:
            if href not in seen:
                seen.add(href)
                out.append((href, atext))
    return out


def _host(url):
    m = re.match(r"https?://([^/:?#]+)", url, re.I)
    return m.group(1).lower() if m else ""


def _score(href, atext, domains):
    if NOISE.search(href) or (atext and NOISE.search(atext)):
        return -1  # excluded outright
    s = 0
    if STRONG_KW.search(href):
        s += 3
    if atext and STRONG_KW.search(atext):
        s += 2
    host = _host(href)
    if any(host == d or host.endswith("." + d) for d in domains):
        s += 1
    return s


def _pick(links, domains):
    """Choose the verification link. Returns (link|None, confidence|None, candidates)."""
    scored = [(_score(h, t, domains), i, h) for i, (h, t) in enumerate(links)]
    usable = [x for x in scored if x[0] >= 0]  # drop noise
    ranked = sorted(usable, key=lambda x: (-x[0], x[1]))
    candidates = [x[2] for x in ranked][:6]
    strong = [x for x in ranked if x[0] >= 2]  # carries a verify keyword
    if strong:
        return strong[0][2], "high", candidates
    if len(usable) == 1:  # exactly one obvious, non-noise link
        return usable[0][2], "low", candidates
    return None, None, candidates  # zero, or ambiguous → let the applicator skip


def _extract_code(subject, payload):
    """Keyword-anchored OTP extraction; None unless a code keyword sits near the digits.

    Scans the subject alone, then subject+body per part (many platforms put the
    keyword in the subject and only the bare number in the body).
    """
    texts = [subject or ""]
    for mt, text in _decode_parts(payload):
        body = _html_to_text(text) if mt == "text/html" else text
        texts.append((subject or "") + " " + body)
    for raw in texts:
        text = " ".join(raw.split())  # collapse whitespace so the char window is meaningful
        for m in CODE_NUM.finditer(text):
            ctx = text[max(0, m.start() - 60):m.end() + 60]
            if CODE_NEG.search(ctx):
                continue  # looks like an address/phone artifact → never a code
            if CODE_KW.search(ctx):
                return m.group(0)
    return None


def _brand(domain):
    """Registrable brand token: pwc.de → pwc, pwc.co.uk → pwc; '' if unusable (<3 chars)."""
    labels = domain.split(".")
    if len(labels) < 2:
        return ""
    b = labels[-2]
    if b in _GENERIC_SLD and len(labels) >= 3:
        b = labels[-3]
    return b if len(b) >= 3 else ""


def _sender_ok(from_header, brands):
    """True iff the actual sender HOST carries a brand as a dot-separated label.

    'PwC <noreply@jobalerts-de.pwc.com>' + ['pwc'] → True; a brand mention in the
    display name or a lookalike host (pwc-fake.evil.com) does NOT count.
    """
    m = re.search(r"@([A-Za-z0-9.\-]+)", from_header or "")
    if not m:
        return False
    labels = m.group(1).lower().split(".")
    return any(b in labels for b in brands)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sender-domain", required=True,
                    help="comma-separated platform domains, e.g. join.com,mail.join.com")
    ap.add_argument("--since", type=int, default=int(time.time()) - 600,
                    help="epoch seconds; only mail newer than this is considered")
    ap.add_argument("--want", choices=["link", "code"], default="link")
    ap.add_argument("--wait-seconds", type=int, default=90,
                    help="max poll window; keep under the caller's Bash timeout (default 120s)")
    ap.add_argument("--poll-interval", type=int, default=15)
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()

    def out(obj):
        print(json.dumps(obj))
        sys.exit(0)

    def dbg(msg):
        if a.debug:
            print(msg, file=sys.stderr)

    domains = [d.strip().lower() for d in a.sender_domain.split(",") if d.strip()]
    if not domains:
        out({"status": "error", "reason": "no --sender-domain given"})
    strict_q = "from:({}) after:{}".format(" OR ".join(domains), a.since)
    brands = sorted({b for b in (_brand(d) for d in domains) if b})
    fallback_q = "from:({}) after:{}".format(" OR ".join(brands), a.since) if brands else None

    try:
        svc = get_service()  # allow_consent=False → never hangs waiting on a browser
    except Exception as e:
        out({"status": "error", "reason": "gmail auth unavailable: {}".format(e)})

    deadline = time.time() + a.wait_seconds
    seen = set()
    checked = 0

    def _list_new(q, tag):
        try:
            resp = svc.users().messages().list(userId="me", q=q, maxResults=10).execute()
        except Exception as e:
            out({"status": "error", "reason": "gmail query failed: {}".format(e)})
        ids = [m["id"] for m in (resp.get("messages") or []) if m["id"] not in seen]
        seen.update(ids)
        dbg("[poll:{}] q='{}' -> {} new msg(s)".format(tag, q, len(ids)))
        return ids

    def _examine(mid, sender_match):
        nonlocal checked
        full = svc.users().messages().get(userId="me", id=mid, format="full").execute()
        checked += 1
        payload = full.get("payload", {})
        hdr = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        if sender_match == "fallback" and not _sender_ok(hdr.get("from", ""), brands):
            dbg("  [fallback] sender {!r} lacks a brand label -> ignored".format(hdr.get("from")))
            return
        base = {
            "status": "found", "sender_match": sender_match,
            "from": hdr.get("from"), "subject": hdr.get("subject"),
            "date": hdr.get("date"), "message_id": mid,
        }
        if a.want == "code":
            code = _extract_code(hdr.get("subject", ""), payload)
            dbg("  from={!r} subj={!r} -> code={}".format(
                hdr.get("from"), hdr.get("subject"), code))
            if code:
                base.update({"code": code, "confidence": "high"})
                out(base)
        else:
            link, conf, cands = _pick(_collect_links(payload), domains)
            dbg("  from={!r} subj={!r} -> link={} conf={}".format(
                hdr.get("from"), hdr.get("subject"), link, conf))
            if link:
                base.update({"link": link, "confidence": conf, "candidates": cands})
                out(base)

    while True:
        ids = _list_new(strict_q, "strict")
        for mid in ids:  # newest first
            _examine(mid, "exact")
        if fallback_q and not ids:  # strict silent this round → try the brand query
            for mid in _list_new(fallback_q, "fallback"):
                _examine(mid, "fallback")
        if time.time() >= deadline:
            out({
                "status": "not_found",
                "reason": "no verification {} from {} after {} within {}s".format(
                    a.want, domains, a.since, a.wait_seconds),
                "checked": checked, "waited_seconds": a.wait_seconds,
            })
        time.sleep(max(1, min(a.poll_interval, int(deadline - time.time()))))


if __name__ == "__main__":
    main()
