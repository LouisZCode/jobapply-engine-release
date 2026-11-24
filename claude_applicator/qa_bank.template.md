# qa_bank.md — YOUR long-form answer bank for the writer tool
# ==============================================================================
# Copy this file to claude_applicator/qa_bank.md (that exact path — it is
# git-ignored, so your personal stories never get committed) and fill it in,
# in your own words. There's no script to "render" this file: you (or the
# onboard skill, interviewing you) just write it directly as plain Markdown.
#
# WHO READS THIS FILE: writer/write.sh — the ghostwriter tool the apply agent
# calls whenever an application form asks a custom free-text question or wants
# a cover letter and `profile.yaml` has no matching field. Its system prompt
# (writer/WRITER.md) is blunt about the contract:
#
#   "The ONLY facts you are allowed to use are: 1. the QA bank text ...
#    You MUST NOT guess, extrapolate, round up, or invent an employer, a
#    metric, a tool, a certification, or a story to fill a gap."
#
# That means THIS FILE is the entire universe of facts the writer can draw on.
# It cannot see your CV, your LinkedIn, or anything else — only what you put
# here. A thin file means most custom questions get skipped (safe, but a
# missed application). A rich file — real employers, real numbers, real
# stories — means the writer can answer almost anything honestly. Richer
# really does mean more questions answerable, so it's worth being thorough.
#
# THE ONE HARD RULE: every fact in this file must be TRUE. Never round a
# number up, never invent a metric to sound more impressive, never claim a
# tool/employer/certification you don't actually have. The writer treats this
# file as ground truth and will repeat what's here verbatim into real
# applications — an invented number here becomes a lie on a real job form.
#
# VOICE: write like you actually talk, not like a corporate bio. Short,
# concrete sentences. Real numbers over vague adjectives ("cut report time
# from two days to two hours" beats "significantly improved efficiency").
# The writer's own style rules (WRITER.md) strip AI-sounding language on
# output, but it can only sound like you if the source material does too.
# ==============================================================================

# ------------------------------------------------------------------------------
# EDITORIAL ANNOTATIONS — a convention you can use anywhere in this file
# ------------------------------------------------------------------------------
# Bracket a note to yourself/the writer with your first name, like this:
#   [Alex: keep this project description generic — don't name the client]
#   [Alex: frame the layoff as "the team was restructured", nothing more]
# These are INSTRUCTIONS, not facts — the writer must never quote or repeat
# the bracketed text itself in an answer, but it MUST obey whatever the note
# says (a framing constraint, a "don't mention X", a "always call it Y").
# Use them wherever a fact needs careful handling — an NDA'd client, a
# sensitive gap, a detail you'd rather stay vague about.
# ------------------------------------------------------------------------------

## 1. Identity summary

One short paragraph, plain language: who you are, where you're based, what
kind of role you're looking for, and the headline version of your background.
This is the "if you only read one paragraph" summary the writer leans on for
short intros and "tell us about yourself" questions.

CHANGEME — e.g. "I'm [Name], based in [city]. I've spent the last [N] years
doing [X], and I'm looking for [kind of role]. [One sentence on what makes
your background a little unusual or particularly strong.]"

## 2. Employers

One subsection per employer, most recent first. For each: the real dates, the
real title, and — this is the part people under-write — what you ACTUALLY did
day to day, not just the job description. Concrete tasks, concrete outputs,
concrete numbers wherever you have them (team size, deal size, users served,
% improvement, time saved). If you don't have a number for something, say so
honestly rather than inventing one ("no hard metric for this, but...").

### CHANGEME — Employer Name (start–end, e.g. 2022–present)
- Title: CHANGEME
- Mode: CHANGEME (remote / hybrid / onsite)
- What I actually did: CHANGEME — 3-6 concrete bullets. Real tools, real
  scope, real outcomes. Avoid resume-speak ("responsible for") — say what you
  built, ran, sold, fixed, or shipped, and for whom.
- Numbers: CHANGEME — anything quantifiable (team size, revenue influenced,
  users, latency/cost improvements, deals closed, tickets resolved/day...).

### CHANGEME — Prior Employer Name (start–end)
- (same shape as above)

(Add as many employer sections as you've had — don't skip early-career jobs
that taught you something relevant, even briefly. Repeat the pattern above.)

## 3. Projects

Anything you built — a side project, a work project you can talk about
outside NDA, an open-source contribution, a portfolio piece. Name, what it
does, the stack, and REAL traction if you have it (paying users, GitHub
stars, a specific problem it solved, a demo that's actually live). If a
project has no metric worth stating, say that plainly instead of manufacturing
one — "no users yet, built to learn X" is a fine, honest answer.

### CHANGEME — Project Name
- What it does: CHANGEME (1-3 sentences)
- Stack: CHANGEME
- Real traction / status: CHANGEME (paying users? live in production? a
  demo link? or honestly: "personal project, not deployed")

(Repeat for each project worth mentioning.)

## 4. Stories (Challenge → Action → Result)

3-6 short stories in Challenge/Action/Result shape — these are what the
writer pulls from for "tell us about a time you..." and behavioral questions.
Plain language, not polished corporate-speak. Real numbers in the Result
wherever you have them.

### Story: CHANGEME short title
- Challenge: CHANGEME — what was the actual situation/problem?
- Action: CHANGEME — what did YOU specifically do (not "the team")?
- Result: CHANGEME — what changed, ideally with a number
  (e.g. "reduced churn by 12%", "shipped 2 weeks early", "the client renewed
  a 6-figure contract").

(Repeat for each story. Good categories to cover if you have them: a hard
technical problem you solved, a time you dealt with conflict/ambiguity, a
time you had to learn something fast, a time you shipped under pressure, a
time you influenced a decision without formal authority.)

## 5. Technical stack — depth, not just a list

Don't just list technologies — say how deep each one goes and where you used
it. This is what lets the writer answer "how comfortable are you with X?"
honestly instead of guessing from a bare list.

- CHANGEME (e.g. Python): CHANGEME depth — years, what you built with it,
  how comfortable (e.g. "4 years, daily driver, built the FastAPI backend
  for Project A, comfortable with async and testing").
- CHANGEME (repeat per technology worth explaining — the ones your target
  roles will actually ask about; profile.yaml's `technical_stack` is the
  flat list, this section is the depth behind the ones that matter).

## 6. Languages & fluency

Explicit statements, one per language — the writer's language rule (match
the job posting's language, when you're fluent enough) reads THIS section to
decide, so be precise about real comfort level, not just a CEFR label.

- CHANGEME language: CHANGEME level (e.g. "German: C1, comfortable writing
  formal business German including cover letters" or "German: B1, can read
  job postings but would not write a cover letter in German yet").
- (repeat per language)

## 7. Availability & anything else worth stating once

Optional catch-all — notice period in your own words, anything about your
situation the writer should know once rather than re-derive every time
(career change context, why you're looking, anything you want framed a
particular way). Use a bracketed annotation (see the convention above) for
anything sensitive.

CHANGEME — free text.
