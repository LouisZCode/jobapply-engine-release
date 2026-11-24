#!/usr/bin/env python3
"""Unit tests for the sweep's ported classifier + matcher (no Gmail, no DB).

Run: inbox_reader/.venv/bin/python inbox_reader/test_sweep.py
Covers the traps the 2026-07-09 backlog import surfaced: the ack-subject guard
(receipt subject beats "leider"/"unfortunately" body boilerplate), definitive-only
rejection, and sender-anchored high-confidence matching with the strict
"unsure -> don't touch" gate."""
import base64
import sqlite3
import sys

import sweep_responses
from sweep_responses import files_as_process, NOISE
from sweep_responses import (classify, resolve, ckey, _brand, company_candidates,
                             build_corpus, _body_text, is_process_mail, NOISE,
                             has_stage_event, should_star)

FAILS = []


def check(cond, msg):
    print(("[ok] " if cond else "[FAIL] ") + msg)
    if not cond:
        FAILS.append(msg)


def R(subject, body="", frm="Careers <no-reply@example.com>", dom="example.com", date="2026-07-05 09:00:00"):
    return {"from": frm, "from_domain": dom, "subject": subject, "body": body,
            "date_utc": date, "message_id": "<t@x>"}


# ---- classifier ------------------------------------------------------------
print("== classifier ==")
# the #1 trap: a clear receipt subject is an ack even when the body says "unfortunately/leider"
check(classify(R("Application received",
                 "Unfortunately, due to the high volume of applications we cannot reply to everyone."))
      == "auto_ack", "ack-subject guard beats body boilerplate (EN)")
check(classify(R("Eingangsbestätigung deiner Bewerbung",
                 "Leider können wir aufgrund der Vielzahl nicht jede Bewerbung einzeln beantworten."))
      == "auto_ack", "ack-subject guard beats body boilerplate (DE)")
check(classify(R("Update on your application",
                 "After careful consideration we have decided not to move forward with your application."))
      == "rejection", "definitive rejection classified")
check(classify(R("Nachricht zu deiner Bewerbung",
                 "Leider müssen wir dir absagen; wir haben uns für andere Kandidaten entschieden."))
      == "rejection", "german rejection (leider-gated) classified")
check(classify(R("Invitation to a first interview",
                 "We would like to invite you to a screening call next week.")) == "invite",
      "interview invite classified")
check(classify(R("We'd like to make you an offer",
                 "We are pleased to offer you the position.")) == "offer", "offer classified")
check(classify(R("Special offer just for you",
                 "special offer inside!")) != "offer",
      "bare-word 'offer' does NOT classify as offer (marketing subject)")
check(classify(R("Your job offer from ExampleCo",
                 "We are delighted to send you this job offer.")) == "offer",
      "explicit 'job offer' phrase still classifies as offer")
check(classify(R("Einstellungszusage",
                 "Wir freuen uns, dir hiermit eine Einstellungszusage zu senden.")) == "offer",
      "german 'einstellungszusage' classifies as offer")
check(classify(R("Complete a short coding assessment",
                 "Please complete the HackerRank test within 3 days.")) == "assessment_request",
      "assessment classified")
check(classify(R("Thanks for applying to ExampleCo",
                 "We received your application and will review it.")) == "auto_ack", "plain ack classified")
check(classify(R("Quick question about your availability",
                 "Are you around Thursday?")) == "other", "unclassifiable -> other (will be flagged)")

# 2026-07-13 regressions — real mails the first live weeks left flagged as 'other'
check(classify(R("Ihre Bewerbung bei U-Glow GmbH",
                 "Wir haben Ihre Bewerbung geprüft und Ihre Qualifikationen sorgfältig berücksichtigt. "
                 "Aufgrund Ihres Profils und der Anzahl der anderen qualifizierten Bewerbungen können wir "
                 "im Moment leider nicht mit Ihnen im Bewerbungsprozess fortfahren."))
      == "rejection", "disguised rejection: 'leider nicht ... fortfahren' behind an ack-looking subject (U-Glow)")
check(classify(R("Your application as Forward Deployed Engineer at Demodesk",
                 "Thanks for submitting your application! We will review your application shortly "
                 "and get back to you as quickly as possible."))
      == "auto_ack", "EN receipt in body only: 'thanks for submitting' (Demodesk)")
check(classify(R("Thanks for putting Bjak on your roadmap!",
                 "Thank you so much for your interest in joining BJAK! Our team is currently "
                 "reviewing your application."))
      == "auto_ack", "EN receipt in body only: 'for your interest' + 'reviewing your application' (BJAK)")
check(classify(R("Deine Bewerbung als AI Engineer Financial Services (m/w/d)",
                 "Hallo Luis, wir bedanken uns für deine Bewerbung und freuen uns darüber, "
                 "dass du Teil unseres Teams werden möchtest. Wir werden deine Unterlagen umgehend prüfen."))
      == "auto_ack", "DE receipt in body only: 'bedanken uns für deine Bewerbung' (Sopra Steria)")
# entity-laden text/plain (how Sopra actually arrives) must be unescaped before matching
_payload = {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(
    "wir bedanken uns f&uuml;r deine Bewerbung&nbsp;und freuen uns".encode()).decode()}}
check("bedanken uns für deine bewerbung und freuen uns" in _body_text(_payload).lower(),
      "text/plain HTML entities (f&uuml;r, &nbsp;) unescaped before classify")
check(classify(R("Bewerbung als Software Engineer AI Integration (m/w/d) bei DAMPSOFT GmbH",
                 "Wir haben Ihre Bewerbung mit Interesse geprüft, müssen Ihnen jedoch leider mitteilen, "
                 "dass wir diese nicht in der engeren Auswahl berücksichtigen können."))
      == "rejection", "DE rejection with dative declension: 'nicht in der engeren Auswahl' (DAMPSOFT)")
check(classify(R("Technical AI Consultant @ Aivisory",
                 "Ich finde Deinen CV sehr spannend und würde Dich gerne in einem ersten 30min Call besser "
                 "kennenlernen. Magst Du Dir einen Slot in meinem Kalender buchen?"))
      == "invite", "invite behind neutral subject: booking-action phrase in body (Aivisory)")
# ...but ack pleasantries ("freuen uns, dich kennenzulernen") must NOT become invites
check(classify(R("Danke für deine Bewerbung",
                 "Wir haben deine Bewerbung erhalten und freuen uns darauf, dich kennenzulernen."))
      == "auto_ack", "ack pleasantry 'kennenzulernen' in body does not upgrade to invite")

# ---- matcher ---------------------------------------------------------------
print("\n== matcher (unsure -> don't touch) ==")
corpus = build_corpus([
    ("j-cosuno", "Cosuno", "AI Automation Engineer", "2026-06-20"),
    ("j-nag-1", "Nagarro", "Applications Engineer", "2026-06-10"),
    ("j-nag-2", "Nagarro", "Solutions Engineer", "2026-06-25"),
    ("j-uglow", "Prelytics (U-Glow GmbH)", "Software Architekt", "2026-07-06"),
])

# single-row company, brand comes off the sender domain -> high
job, comp, conf = resolve(R("Your application", dom="cosuno.de", frm="Cosuno <jobs@cosuno.de>"), corpus)
check((job, conf) == ("j-cosuno", "high"), "single-row exact match via domain brand -> high")

# multi-row company disambiguated by title token + date proximity -> high, correct role
job, comp, conf = resolve(R("Update on your Solutions Engineer application", dom="nagarro.com",
                            frm="Nagarro <careers@nagarro.com>", date="2026-06-26 10:00:00"), corpus)
check((job, conf) == ("j-nag-2", "high"), "multi-row disambiguated to the right role -> high")

# unknown company -> low (flagged, never written)
job, comp, conf = resolve(R("Your application", dom="totallyunknown.io",
                            frm="Hiring <no-reply@totallyunknown.io>"), corpus)
check(job is None and conf == "low", "no confident company -> low (flag, don't touch)")

# a mail whose date predates every application for that company -> low
job, comp, conf = resolve(R("Regarding your application", dom="nagarro.com",
                            frm="Nagarro <careers@nagarro.com>", date="2026-05-01 10:00:00"), corpus)
check(conf == "low", "mail predating all applications -> low (flag)")

# parenthetical brand: DB row is "Prelytics (U-Glow GmbH)", the mail only names U-Glow
job, comp, conf = resolve(R("Ihre Bewerbung bei U-Glow GmbH", dom="msg.join.com",
                            frm="U-Glow <no-reply@msg.join.com>", date="2026-07-07 09:00:00"), corpus)
check((job, conf) == ("j-uglow", "high"), "parenthetical brand matched (U-Glow -> Prelytics row)")

# 2026-07-13 match regressions (real flagged mail)
corpus2 = build_corpus([
    ("j-zirkel", "ZIRKEL8 (TIECORP UG haftungsbeschränkt)", "Automation Specialist (m/w/d)", "2026-07-06"),
    ("j-damp", "DAMPSOFT GmbH", "Software Engineer AI Integration (m/w/d)", "2026-07-11"),
    ("j-aiv", "Aivisory", "Technical AI Consultant (all genders)", "2026-07-13"),
])
# compound name with spaced dash: mail says "ZIRKEL8 – TIECORP UG", DB keys to "zirkel8"
job, comp, conf = resolve(R("Vielen Dank für deine Bewerbung bei ZIRKEL8 – TIECORP UG (haftungsbeschränkt)",
                            dom="msg.join.com", frm="ZIRKEL8 - TIECORP UG (haftungsbeschränkt) Recruiting Team <no-reply@msg.join.com>",
                            date="2026-07-06 16:36:00"), corpus2)
check((job, conf) == ("j-zirkel", "high"), "spaced-dash compound name matched (ZIRKEL8 – TIECORP UG)")
# job-board subject puts the company LAST: "Bewerbung als <Role> bei <Company>"
job, comp, conf = resolve(R("Bewerbung als Software Engineer AI Integration (m/w/d)  bei DAMPSOFT GmbH",
                            dom="heyjobs.de", frm="Bewerben mit HeyJobs <no-reply@heyjobs.de>",
                            date="2026-07-12 06:00:00"), corpus2)
check((job, conf) == ("j-damp", "high"), "'Bewerbung als ... bei <Company>' subject matched (HeyJobs/DAMPSOFT)")
# "<Role> @ <Company>" subject from a personal-name sender on a join.com subdomain
job, comp, conf = resolve(R("Technical AI Consultant @ Aivisory", dom="aivisoryde.msg.join.com",
                            frm="Irene Ternes <irene.ternes@aivisoryde.msg.join.com>",
                            date="2026-07-13 14:00:00"), corpus2)
check((job, conf) == ("j-aiv", "high"), "'Role @ Company' subject matched (Aivisory)")

# ---- process-mail gate (2026-07-13: ATS codes -> label + archive, no event) --
print("\n== process-mail gate ==")
check(is_process_mail(R("Security code for your application to ATOSS Software SE",
                        frm="Greenhouse <no-reply@eu.greenhouse-mail.io>", dom="eu.greenhouse-mail.io")),
      "ATS security-code mail is process mail (Greenhouse/ATOSS)")
check(is_process_mail(R("Security code for your application to X",
                        frm="Hiring <no-reply@some-company.io>", dom="some-company.io")),
      "application-subject anchors a code mail even off known-ATS domains")
check(not is_process_mail(R("Your verification code",
                            frm="MyBank <security@mybank.com>", dom="mybank.com")),
      "bank/personal code mail is NOT process mail (no ATS/application anchor)")
check(not is_process_mail(R("Einladung zum Interview",
                            frm="Careers <no-reply@eu.greenhouse-mail.io>", dom="eu.greenhouse-mail.io")),
      "a real ATS response without a code subject is NOT process mail")

# 2026-07-17 verifier BLOCKER regressions: outcome mail behind a plumbing-shaped
# subject must NEVER file as process (classify() is the veto — files_as_process).
_r = R("Continue your application: next steps at BigCo",
       "We would like to invite you to complete a short online assessment as the next step.",
       frm="Greenhouse <no-reply@greenhouse.io>", dom="greenhouse.io")
check(classify(_r) == "assessment_request" and not files_as_process(_r),
      "assessment behind 'Continue your application' subject records, never files as plumbing")
_r = R("Update to your Workday applicant profile",
       "After careful consideration we have decided not to move forward with your application.",
       frm="Workday <no-reply@myworkday.com>", dom="myworkday.com")
check(classify(_r) == "rejection" and not files_as_process(_r),
      "rejection behind 'applicant profile' subject records, never files as plumbing")
check(files_as_process(R("Security code for your application to ATOSS Software SE",
                         frm="Greenhouse <no-reply@eu.greenhouse-mail.io>", dom="eu.greenhouse-mail.io")),
      "content-free security-code mail still files as process (classify -> other)")
check(files_as_process(R("Access your Dynatrace applicant profile",
                         frm="Dynatrace <no-reply@dynatrace.com>", dom="dynatrace.com")),
      "content-free applicant-profile mail still files as process (classify -> other)")

# 2026-07-17: Google Calendar invitations classify as 'invite' and (being unmatched)
# were labeled+starred by the keep-type flag path — they are personal noise, never job mail
check(bool(NOISE.search("Invitation: Portugal x Espanha @ Mon Jul 6, 2026 9pm - 11pm (GMT+2) (someone@example.com)")),
      "google-calendar invitation subject is noise (never labeled/starred)")
check(not NOISE.search("Invitation to a first interview"),
      "a real interview invitation subject is NOT swallowed by the calendar-noise rule")

# ---- helpers ---------------------------------------------------------------
print("\n== helpers ==")
check(_brand("cosuno.de") == "cosuno" and _brand("pwc.co.uk") == "pwc", "_brand strips TLD plumbing")
check(ckey("Cosuno GmbH") == ckey("cosuno") == "cosuno", "ckey normalizes legal form")
check("cosuno" in [ckey(c) for c in company_candidates(
    R("x", dom="cosuno.de", frm="Cosuno Hiring Team <jobs@cosuno.de>"))],
    "display-name + domain both yield the company candidate")

# ---- noise: billing/receipt mail (2026-07-17 hardening) --------------------
print("\n== noise: billing/receipts ==")
check(bool(NOISE.search("Your Google Play Order Receipt from Jul 16")),
      "Google Play order receipt caught by NOISE (order receipt)")
check(bool(NOISE.search("Your receipt from Anthropic, PBC #2390")),
      "Anthropic receipt caught by NOISE (your receipt from)")
check(bool(NOISE.search("Payment receipt for your subscription")),
      "generic payment receipt caught by NOISE")
check(bool(NOISE.search("Invoice #4471 from ExampleCo")), "invoice subject caught by NOISE")
check(bool(NOISE.search("Ihr Zahlungsbeleg")), "german Zahlungsbeleg caught by NOISE")

# ---- offer corroboration: has_stage_event (2026-07-17 hardening) -----------
print("\n== has_stage_event ==")
_con = sqlite3.connect(":memory:")
_cur = _con.cursor()
_cur.execute("CREATE TABLE application_events (job_id TEXT, event_type TEXT, event_at TEXT)")
_cur.executemany("INSERT INTO application_events VALUES (?,?,?)", [
    ("j-1", "invite", "2026-07-01 00:00:00"),
    ("j-2", "auto_ack", "2026-07-01 00:00:00"),
])
_con.commit()
check(has_stage_event(_cur, "j-1") is True,
      "has_stage_event True when an invite event is on file")
check(has_stage_event(_cur, "j-2") is False,
      "has_stage_event False when only auto_ack is on file (not a stage event)")
check(has_stage_event(_cur, "j-3") is False,
      "has_stage_event False when the job has no events at all")
_con.close()

# ---- star ledger: should_star (2026-07-17 star-once-ledger fix) ------------
print("\n== should_star (star ledger) ==")
check(should_star("invite", "<a@x>", set()) is True,
      "should_star True for a KEEP_TYPE mail not yet in the ledger")
check(should_star("invite", "<a@x>", {"<a@x>"}) is False,
      "should_star False once the msg_id is already in the ledger (manual unstar sticks)")
check(should_star("offer", "<c@x>", set()) is True,
      "should_star True for another KEEP_TYPE (offer) not yet in the ledger")
check(should_star("auto_ack", "<b@x>", set()) is False,
      "should_star False for a non-KEEP_TYPE classification (auto_ack), ledger or not")

# ---- company alias map: Expedition Growth Capital -> Nesto (2026-07-17) ----
print("\n== company alias map (Expedition Growth Capital -> Nesto) ==")
corpus3 = build_corpus([
    ("j-nesto", "Nesto Software GmbH", "Solutions Engineer", "2026-07-14"),
])
_orig_aliases = sweep_responses.ALIASES
sweep_responses.ALIASES = {ckey("Expedition Growth Capital"): "Nesto Software GmbH"}
try:
    job, comp, conf = resolve(R("Application Received", dom="pinpoint.email",
                                frm="Expedition Growth Capital <no-reply@pinpoint.email>",
                                date="2026-07-15 09:00:00"), corpus3)
    check((job, conf) == ("j-nesto", "high"),
          "alias map resolves 'Expedition Growth Capital' -> Nesto (j-nesto, high)")
finally:
    sweep_responses.ALIASES = _orig_aliases

# ---- process-mail: account-plumbing extension (2026-07-17) -----------------
print("\n== process-mail: account-plumbing extension ==")
check(is_process_mail(R("Passwort erstellen", frm="HeyJobs <no-reply@heyjobs.de>", dom="heyjobs.de")),
      "HeyJobs 'Passwort erstellen' is process mail (account-plumbing, ATS-domain anchor)")
check(is_process_mail(R("Passwort erstellen", frm="softgarden <noreply@member.softgarden.de>",
                        dom="member.softgarden.de")),
      "softgarden/OneQrew 'Passwort erstellen' is process mail (account-plumbing, ATS-domain anchor)")
check(is_process_mail(R("Capgemini - Karriere Account erstellt",
                        frm="Capgemini Careers <no-reply@capgemini.com>", dom="capgemini.com")),
      "Capgemini 'Karriere Account erstellt' is process mail (account-plumbing, career-subject anchor)")
check(is_process_mail(R("Access your Dynatrace applicant profile",
                        frm="Dynatrace <no-reply@dynatrace.com>", dom="dynatrace.com")),
      "Dynatrace 'applicant profile' is process mail (account-plumbing, applicant-subject anchor)")

check(not is_process_mail(R("Create your password",
                            frm="MyBank <security@mybank.com>", dom="mybank.com")),
      "bank 'Create your password' is NOT process mail (no ATS/career anchor)")
check(not is_process_mail(R("Sign in and continue your application",
                            frm="LinkedIn <messages-noreply@linkedin.com>", dom="linkedin.com")),
      "linkedin.com sender never matches process-mail, even with a plumbing+anchor-shaped subject")
check(not is_process_mail(R("Thank you for your application",
                            frm="Careers <no-reply@eu.greenhouse-mail.io>", dom="eu.greenhouse-mail.io")),
      "ATS ack without a plumbing subject is NOT process mail")

print("\n" + ("ALL SWEEP TESTS PASSED" if not FAILS else "FAILURES: %d" % len(FAILS)))
sys.exit(1 if FAILS else 0)
