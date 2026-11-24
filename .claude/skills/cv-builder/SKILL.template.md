---
name: cv-builder
description: Build a role-tailored CV/résumé PDF for {{user_full_name}} in the established house style — the blue (#1F4E79) template — grounded ONLY in the answer bank, never inventing facts. Use whenever asked to create, tailor, refresh, translate, or add a CV variant (e.g. Forward-Deployed Engineer, AI Automation, AI Consultant, German), render a CV to PDF, or wire a new CV into the job-applicator's data/ + profile.yaml routing. Produces ATS-parseable PDFs via headless-Chrome HTML→PDF.
---

# cv-builder — {{user_full_name}}'s CV factory

Produce a **role-tailored CV** as a polished, ATS-parseable **PDF** in {{user_first_name}}'s house style. Every CV
is the **same facts, re-emphasized per role** — never new facts. The gold-standard reference is
`data/drafts/<Name>_FDE_2026.{html,pdf}` (the Forward-Deployed Engineer CV, approved v1).

## Hard rules (do not break)

1. **Grounding — zero fabrication.** Every fact (products, employers, dates, metrics, stack, certs,
   contact) comes from `answer_bank.md` (this folder) — which mirrors the real CVs + `profile.yaml`.
   You may **rephrase and re-emphasize**, never invent a skill, employer, date, or achievement. If a
   role wants something {{user_first_name}} can't truthfully claim, leave it out — don't manufacture it.
2. **Voice, and NO AI tells.** First-person, concrete, metric-driven, confident. The recruiting-to-AI
   pivot is framed as an **asset** (discovery, stakeholder work, 4 languages), never hidden. Short
   sentences. Name real models/providers/architectures. Match the existing CVs' tone exactly.
   **ZERO em dashes anywhere** — the #1 AI giveaway. Use commas, periods, colons, or parentheses
   instead. No arrow chains (`scope > build > deploy`), no buzzwords (leverage, seamless, robust,
   cutting-edge, delve, tapestry, showcase), no "not just X but Y", no forced rule-of-three. If a line
   copied from `answer_bank.md` contains an em dash, **strip it** — that file uses em dashes only as
   source shorthand. (The human CVs use em dashes structurally; we avoid them entirely to be safe.)
   **Before rendering, run the global `de-ai-ify` skill as the final pass** — it is the canonical,
   fuller tell-list (tricolons, "not X but Y" antithesis, hedging stacks, buzzword blacklist, the
   summary-paragraph reflex). For CVs, {{user_first_name}}'s calibration = zero em dashes.
3. **No prose bolding.** Inside sentences/bullets, **nothing is bold** — not "and", not emphasis
   words. Bold is ONLY for titles: the name, section headers, project names, company names, degree/
   cert titles, and the STACK line-labels. (Bug we fixed on FDE v1.)
4. **Email = `identity.email_external`.** CVs show the address from `identity.email_external`
   (the keeper address). Never `identity.email` (the LinkedIn-locked address) on a CV.
5. **Location = `location.current_city`, `location.country`.** Always read it from profile.yaml —
   never hardcode a city (an old version of this template shipped with a stale, wrong one).
6. **Page target** ~2 pages (1 if deliberately compressed). Single column only — ATS-safe.

## House style (all in `template.html`)

- **Format:** A4, `Helvetica Neue`/Helvetica/Arial, 10pt body, single column, selectable text.
- **Colors:** accent `#1F4E79` (blue) on name · role-title · section headers · company & project
  names · STACK labels. Secondary text `#595959`. Heading rules `#BFBFBF`. Body `#1c1c1c`.
- **Structure (7 sections, in order):**
  1. **Header** — the name in uppercase (`{{user_full_name}}`) · role-title+subtitle · contact line
     (`{{contact_line}}`, built from profile.yaml identity/location fields — city, email, phone,
     portfolio, github, LinkedIn)
  2. **PROFILE** — 3–5 lines, re-angled per role
  3. **SELECTED PROJECTS** — the products from `answer_bank.md` (profile.yaml `portfolio_projects`, 3 blocks), re-emphasized
  4. **<ROLE> & CUSTOMER EXPERIENCE** — the employers from `answer_bank.md` (profile.yaml `experience`, 4 blocks), re-framed; section heading renamed per role
  5. **STACK** — 3 labelled lines (stable)
  6. **EDUCATION & CERTIFICATIONS** — stable (entries + highlight sentences verbatim from `answer_bank.md`)
  7. **LANGUAGES** — stable

## Build a new CV — steps

1. **Read `answer_bank.md`** (facts + per-role framings) and the FDE reference HTML.
2. **Pick the role angle.** Each variant changes only: the **role-title**, the **Profile**, the
   **experience-section heading**, and **which facet of each block is emphasized**. Projects/stack/
   education/languages stay verbatim. See `answer_bank.md` → "Role variants" for the angle of each
   (AIAE · Solutions · Consulting · FDE · Automation · German).
3. **Copy `template.html`** → `data/drafts/<Name>_<ROLE>_2026.html`, fill the placeholder zones
   with answer-bank content in the chosen framing. Keep the CSS untouched (that IS the house style).
4. **Render:** `bash .claude/skills/cv-builder/render.sh data/drafts/<Name>_<ROLE>_2026.html`
   → writes+opens the PDF. (Renders via headless Chrome, isolated temp profile, bounded so it can't
   hang — see Gotchas.)
5. **Verify** (the script prints page count; for deeper checks use pypdf): correct email
   (`identity.email_external`), accent color `#1F4E79` present, no red, no stray prose bold,
   dates/facts match the answer bank.
6. **Show the PDF for review.** Iterate on wording before wiring.

## German variant

Translate the **AIAE** content into German (best fit for KI / m-w-d / "Entwickler" postings). Keep
the same structure, colors, and the same facts — only the language changes. Section headers in
German (PROFIL · AUSGEWÄHLTE PROJEKTE · … · SPRACHEN). Ask before defaulting to a Solutions angle.

## Wire a finished CV into the applicator

Once approved:
1. Copy the PDF into the CV pool: `claude_applicator/data/<Name>_<ROLE>_2026.pdf`.
2. Route it in `claude_applicator/profile.yaml`:
   - `job_search.cv_by_title[]` — add title-keyword(s) → the new file (title override wins; first
     match wins, so order matters — see the CV-001 rule in the file).
   - and/or a `search_queries[].resume:` default for the query that surfaces the role.
3. The applicator logs the uploaded filename into the DB `cv_file` column automatically (CLAUDE.md
   step 4.8) — no code change needed.
4. Note which `cv_by_title` mapping changed (e.g. `forward deployed` moved Solutions→FDE CV).

## Gotchas (learned the hard way)

- **Headless Chrome doesn't self-exit** after `--print-to-pdf` — `render.sh` backgrounds it with a
  watchdog kill so it can't hang the shell. Don't call Chrome directly without that bound.
- **This is unrelated to the applicator's Chrome.** `render.sh` uses a throwaway temp `user-data-dir`,
  headless, no CDP — it never touches the logged-in `Chrome-Agent` LinkedIn session. Safe.
- **TCC blocks writes to ~/Desktop, ~/Documents, ~/Downloads.** Keep all CV files inside the repo
  (`data/drafts/`); reads from Desktop are flaky too. Never write output there.
- **No PDF tooling installed** (no poppler/pandoc/wkhtmltopdf). Chrome is the renderer; **pypdf**
  (`/usr/bin/python3 -c "import pypdf"`) is available for text/color verification.
- **Extract a color from an existing PDF** (to match a template) by scanning content-stream `rg`
  operators with pypdf — that's how `#1F4E79` was found.

## Checklist before declaring a CV done
- [ ] `identity.email_external` email · `location.current_city` · correct role-title
- [ ] Profile re-angled for the role; every fact traceable to answer_bank.md
- [ ] no prose bolding (only titles/names/labels bold)
- [ ] ZERO em dashes / arrows / buzzwords — `grep -c '&mdash;\|—\|&rarr;' the .html` returns 0
- [ ] `#1F4E79` accent, no leftover accent from another template
- [ ] Education entries: dates + highlight sentence copied verbatim from answer_bank.md
- [ ] renders clean, ~2 pages, text selectable
- [ ] (if wiring) copied to data/ + profile.yaml routing updated
