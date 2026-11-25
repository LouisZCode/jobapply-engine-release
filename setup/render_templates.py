#!/usr/bin/env python3
"""setup/render_templates.py — PROD-001 render tooling.

Reads claude_applicator/profile.yaml (per-user data, gitignored — never committed) and
renders every tracked engine doc's `*.template.md` source to its live path, substituting
the doc placeholders {{user_first_name}}, {{user_full_name}}, {{mail_label}} and
{{run_config_path}} (MULTI-002b: the user's run_config.txt path). Also
writes the two small generated config files the CODE side (shell scripts / python tools)
reads at runtime: claude_applicator/user.env and
claude_applicator/inbox_reader/user_mail.json.

MULTI-002b: when JOBAPPLY_DATA is set, every rendered file is written under the data
root at the same relative path (the data root shadows the repo — see shadow() below);
claude_applicator/bind_user.sh points the repo paths at those copies with symlinks so
the engine still finds them relative to its own directory. user.env stays at
<DATA_ROOT>/user.env (where app/lib/paths.py and the run scripts read it). With
JOBAPPLY_DATA unset every target is exactly today's path.

None of the outputs are tracked in git (see .gitignore) — this script is what recreates
them locally, on first setup and again any time profile.yaml changes.

Idempotent: safe to re-run any time; every output is fully overwritten, never merged.
Refuses to run (clear error, nothing written) if profile.yaml is missing. Fails loudly
(nothing written) if a rendered doc still contains an unrecognized `{{...}}` placeholder
after substitution — an unknown placeholder is a template bug or a profile.yaml typo,
never something to ship silently.

Usage:
  python3 setup/render_templates.py [--profile PATH] [--dry-run]

  --profile PATH   path to profile.yaml (default: claude_applicator/profile.yaml)
  --dry-run        print what would be written; write nothing
"""
import argparse
import json
import os
import re
import sys

try:
    import yaml
except ImportError:
    sys.exit(
        "ERROR: PyYAML is required to read profile.yaml.\n"
        "Install it with: pip install pyyaml"
    )

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# MULTI-001, per-user data root; unset = legacy layout (byte-identical targets). The
# profile.yaml read path and user.env write path follow paths.py's flattened spots
# (<DATA_ROOT>/profile.yaml, <DATA_ROOT>/user.env); MULTI-002b: every other rendered
# file follows the shadow rule below.
DATA_ROOT = os.environ.get("JOBAPPLY_DATA") or None


def shadow(rel):
    """MULTI-002b: the user's copy of a repo-relative file — <DATA_ROOT>/<rel> when
    JOBAPPLY_DATA is set, the repo path otherwise (the old single-user layout)."""
    if DATA_ROOT:
        return os.path.join(DATA_ROOT, rel)
    return os.path.join(REPO_ROOT, rel)

DEFAULT_PROFILE = (
    os.path.join(DATA_ROOT, "profile.yaml") if DATA_ROOT
    else os.path.join(REPO_ROOT, "claude_applicator", "profile.yaml")
)

# (template path, rendered path) — both relative to REPO_ROOT. This is the doc-template
# contract shared with the four hand-authored *.template.md sources (the big prose docs):
# CLAUDE.md, writer/WRITER.md, inbox_reader/MAIL_AGENT.md, cv-builder/SKILL.md.
TEMPLATE_TARGETS = [
    ("claude_applicator/CLAUDE.template.md", "claude_applicator/CLAUDE.md"),
    ("claude_applicator/writer/WRITER.template.md", "claude_applicator/writer/WRITER.md"),
    ("claude_applicator/inbox_reader/MAIL_AGENT.template.md",
     "claude_applicator/inbox_reader/MAIL_AGENT.md"),
    (".claude/skills/cv-builder/SKILL.template.md", ".claude/skills/cv-builder/SKILL.md"),
]

KNOWN_PLACEHOLDERS = ("user_first_name", "user_full_name", "mail_label", "mail_label_slug",
                     "run_config_path")
PLACEHOLDER_RE = re.compile(r"\{\{[^{}]*\}\}")
# {{ spaced token }} → {{token}} — templates may spell a placeholder either way; the
# replace loop below matches the spaceless canonical form (all existing templates use
# it; MULTI-002b's {{run_config_path}} is written to match it too).
SPACED_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# Tokens allowed to SURVIVE a render: prose in a doc template may legitimately mention a
# placeholder that belongs to a DIFFERENT templating layer (cv-builder/template.html's
# build-time zones, filled by the skill per CV — not by this script).
DOC_LITERAL_ALLOWLIST = {"{{contact_line}}"}


def render_text(text, values):
    """Substitute the known placeholders (values: dict of the KNOWN_PLACEHOLDERS keys).

    Returns (rendered_text, unknown_leftovers) — unknown_leftovers is a sorted list of
    any `{{...}}`-shaped token still present after substitution (should be empty for a
    clean template; a non-empty list means the caller should refuse to write the file).
    """
    rendered = SPACED_TOKEN_RE.sub(r"{{\1}}", text)
    for key in KNOWN_PLACEHOLDERS:
        rendered = rendered.replace("{{" + key + "}}", values[key])
    unknown = sorted(set(PLACEHOLDER_RE.findall(rendered)) - DOC_LITERAL_ALLOWLIST)
    return rendered, unknown


def load_profile(path):
    if not os.path.isfile(path):
        sys.exit(
            "ERROR: profile not found at {}\n"
            "Create claude_applicator/profile.yaml first (the setup interview / "
            "profile.template.yaml), then re-run this script.".format(path)
        )
    with open(path, encoding="utf-8") as fh:
        try:
            data = yaml.safe_load(fh)
        except yaml.YAMLError as exc:
            sys.exit("ERROR: {} is not valid YAML: {}".format(path, exc))
    if not isinstance(data, dict):
        sys.exit("ERROR: {} did not parse to a mapping — check its YAML syntax".format(path))
    return data


def derive_values(profile):
    """Pull the rendered-doc placeholders + the two generated-config payloads out of a
    parsed profile.yaml dict. Exits with a clear error on missing required fields."""
    identity = profile.get("identity") or {}
    full_name = (identity.get("full_name") or "").strip()
    if not full_name:
        sys.exit("ERROR: profile.yaml identity.full_name is missing/empty")
    first_name = full_name.split()[0]

    mail_label = profile.get("mail_label") or (profile.get("job_search") or {}).get("mail_label")
    mail_label = (mail_label or "Job Applications").strip() or "Job Applications"
    # Gmail's search operator wants the slugified label form (lowercase, non-alphanumeric
    # runs collapsed to hyphens): "Germany/Job Applications 26" -> "germany-job-applications-26".
    mail_label_slug = re.sub(r"[^a-z0-9]+", "-", mail_label.lower()).strip("-")

    emails = []
    for key in ("email", "email_external"):
        val = (identity.get(key) or "").strip()
        if val and val not in emails:
            emails.append(val)

    location = profile.get("location") or {}
    city = (location.get("current_city") or "").strip()
    country = (location.get("country") or "").strip()
    contact_line = ", ".join(p for p in (city, country) if p)

    return {
        "user_first_name": first_name,
        "user_full_name": full_name,
        "mail_label": mail_label,
        "mail_label_slug": mail_label_slug,
        "self_emails": emails,
        "contact_line": contact_line,
        # MULTI-002b: where the run switches live for THIS render — the user's
        # run_config.txt under a data root (absolute, so the CLAUDE.md routine reads the
        # user's switches, not the tracked repo default), the repo-relative name exactly
        # as before when JOBAPPLY_DATA is unset.
        "run_config_path": os.path.join(DATA_ROOT, "run_config.txt") if DATA_ROOT else "run_config.txt",
    }


def render_templates(values, dry_run):
    doc_values = {k: values[k] for k in KNOWN_PLACEHOLDERS}
    results = []
    for tmpl_rel, out_rel in TEMPLATE_TARGETS:
        tmpl_path = os.path.join(REPO_ROOT, tmpl_rel)
        # MULTI-002b: rendered docs are per-user — shadow(<rel>) under a data root
        # (bind_user.sh then points the repo path at the user's copy with a symlink),
        # the repo path exactly as before when JOBAPPLY_DATA is unset.
        out_path = shadow(out_rel)
        if not os.path.isfile(tmpl_path):
            results.append((tmpl_rel, "MISSING template — skipped"))
            continue
        with open(tmpl_path, encoding="utf-8") as fh:
            text = fh.read()
        rendered, unknown = render_text(text, doc_values)
        if unknown:
            sys.exit(
                "ERROR: {} still has unrecognized placeholder(s) after render: {}\n"
                "Known placeholders are: {}".format(
                    tmpl_rel, ", ".join(unknown),
                    ", ".join("{{" + k + "}}" for k in KNOWN_PLACEHOLDERS),
                )
            )
        # MULTI-002b: show the real target (the absolute data-root path; legacy keeps
        # the repo-relative label exactly as before).
        display = out_path if DATA_ROOT else out_rel
        if dry_run:
            results.append((tmpl_rel, "would write -> {} ({} bytes)".format(display, len(rendered))))
        else:
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(rendered)
            results.append((tmpl_rel, "wrote -> {}".format(display)))
    return results


def write_user_env(values, dry_run):
    rel = os.path.join("claude_applicator", "user.env")
    # MULTI-001: under DATA_ROOT the generated user.env lands at $DATA_ROOT/user.env.
    path = os.path.join(DATA_ROOT, "user.env") if DATA_ROOT else os.path.join(REPO_ROOT, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = [
        "# Generated by setup/render_templates.py from profile.yaml.",
        "# Do not edit by hand — change profile.yaml and re-run the script instead.",
        "USER_FIRST_NAME={}".format(values["user_first_name"]),
        "USER_FULL_NAME={}".format(values["user_full_name"]),
    ]
    # CONTACT_LINE is optional (writer/write.sh's cover-letter PDF header sub-line —
    # see writer/letter_template.html); only written when profile.yaml has a location.
    if values.get("contact_line"):
        lines.append("CONTACT_LINE={}".format(values["contact_line"]))
    content = "\n".join(lines) + "\n"
    # MULTI-001: show the real target in the status line (DATA_ROOT mode prints the
    # absolute path; legacy keeps the repo-relative label exactly as before).
    display = path if DATA_ROOT else rel
    if dry_run:
        return "would write -> {}".format(display)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return "wrote -> {}".format(display)


def write_user_mail_json(values, dry_run):
    rel = os.path.join("claude_applicator", "inbox_reader", "user_mail.json")
    # MULTI-002b: per-user like the rendered docs — shadow(<rel>) under a data root
    # (bind_user.sh symlinks the repo path at it), the repo path exactly as before unset.
    path = shadow(rel)
    payload = {"self_emails": values["self_emails"], "label": values["mail_label"]}
    display = path if DATA_ROOT else rel
    if dry_run:
        return "would write -> {}".format(display)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    return "wrote -> {}".format(display)


def main():
    ap = argparse.ArgumentParser(
        description="Render engine *.template.md docs + generate user.env / "
                     "user_mail.json from claude_applicator/profile.yaml."
    )
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                     help="path to profile.yaml (default: claude_applicator/profile.yaml)")
    ap.add_argument("--dry-run", action="store_true",
                     help="print what would be written; write nothing")
    args = ap.parse_args()

    profile = load_profile(args.profile)
    values = derive_values(profile)

    print("profile: {}".format(args.profile))
    print("user_first_name = {}".format(values["user_first_name"]))
    print("user_full_name  = {}".format(values["user_full_name"]))
    print("mail_label      = {}".format(values["mail_label"]))
    print("self_emails     = {}".format(values["self_emails"]))
    print("contact_line    = {}".format(values["contact_line"] or "(empty)"))
    print()

    for tmpl_rel, status in render_templates(values, args.dry_run):
        print("[doc]  {:60s} {}".format(tmpl_rel, status))
    print("[env]  {}".format(write_user_env(values, args.dry_run)))
    print("[mail] {}".format(write_user_mail_json(values, args.dry_run)))

    if args.dry_run:
        print("\n(dry run — nothing written)")


if __name__ == "__main__":
    main()
