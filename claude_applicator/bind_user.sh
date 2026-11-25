#!/usr/bin/env bash
# bind_user.sh — MULTI-002b: make the engine directory point at the ACTIVE user's files.
#
# The data root shadows the repo: a personal file the engine reads at <repo>/<rel> lives,
# for a user, at <DATA_ROOT>/<rel> — this script puts a symlink at <repo>/<rel> so
# everything that works relative to the engine directory (the claude worker, the sqlite
# and playwright MCP servers, inbox_reader tools, the writer) sees the user's copy and
# never another user's. Called (no arguments) by the engine run scripts right after they
# resolve their DATA_ROOT, BEFORE anything reads a personal file.
#
# Safe to run repeatedly (idempotent): an already-correct symlink is left alone, an old
# single-user copy at a repo location is moved into the data root ONCE (or renamed to
# <link>.legacy-<ts> when the user already has a copy, so nothing is lost), and existing
# -wal/-shm siblings of applications.db are never touched (they are transient).
#
# Mapping targets: every file goes to <DATA_ROOT>/<rel> (the shadow rule) EXCEPT the
# files app/lib/paths.py and the run scripts already keep at a flattened data-root spot —
# user.env (<DATA_ROOT>/user.env) plus the MULTI-001 set (profile.yaml, qa_bank.md,
# data/, applications.db) — those are bound to their real locations, so the app, the run
# scripts and this script's symlinks all agree on one file per user. run_config.txt and
# everything else tracked in the repo are never mapped.
#
# With JOBAPPLY_DATA unset/empty (the old single-user way): prints nothing, changes
# nothing, exits 0. Exits 1 when the required rendered CLAUDE.md is missing from the
# data root, so a run never continues with another user's (or the repo default's) files.
#
# The repo root is resolved from this script's own location; JOBAPPLY_REPO_ROOT overrides
# it (used by the test suite to run this script against a scratch tree).
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${JOBAPPLY_REPO_ROOT:-$(cd "$DIR/.." && pwd)}"
DATA_ROOT="${JOBAPPLY_DATA:-}"

if [[ -z "$DATA_ROOT" ]]; then
  # Legacy single-user layout: the engine directory already IS the user's directory.
  exit 0
fi

# bind REL TARGET — one repo-relative mapping. Handles the three link states:
#   (a) link is a symlink  -> re-point it when it doesn't already point at TARGET;
#   (b) link is a regular file/dir (an old single-user copy) -> move it into the data
#       root when TARGET doesn't exist, else rename it to <link>.legacy-<ts> so nothing
#       is lost, then create the symlink;
#   (c) link doesn't exist -> create the symlink (dangling is fine for optional files).
# Prints one line per change: "[bind] <rel> -> <target>" / "[bind] moved old <rel> ...".
bind() {
  local rel="$1" target="$2"
  local link="$REPO_ROOT/$rel"
  if [[ -L "$link" ]]; then
    if [[ "$(readlink "$link")" == "$target" ]]; then
      return 0
    fi
    ln -sfn "$target" "$link"
    echo "[bind] $rel -> $target"
    return 0
  fi
  if [[ -e "$link" ]]; then
    if [[ -e "$target" ]]; then
      mv "$link" "$link.legacy-$(date +%Y%m%d-%H%M%S)"
    else
      mkdir -p "$(dirname "$target")"
      mv "$link" "$target"
      echo "[bind] moved old $rel into the data root"
    fi
  fi
  mkdir -p "$(dirname "$link")"   # tracked dirs always exist in a real checkout;
                                   # a scratch/partial tree still binds cleanly
  ln -sfn "$target" "$link"
  echo "[bind] $rel -> $target"
}

# REQUIRED — the routine itself. A missing rendered copy means this user never pressed
# "Prepare the files" in the app; refuse loudly instead of running on stale files.
CLAUDE_MD="$DATA_ROOT/claude_applicator/CLAUDE.md"
if [[ ! -e "$CLAUDE_MD" ]]; then
  echo "bind_user: $CLAUDE_MD is missing — open Job Apply and press \"Prepare the files\" for this user first"
  exit 1
fi
bind "claude_applicator/CLAUDE.md" "$CLAUDE_MD"

# Optional — every other personal file the engine reads relative to the repo. Dangling
# links are fine here: the run scripts write their own outputs under $DATA_ROOT, and
# these targets fill in as the app renders/uploads them.
bind "claude_applicator/writer/WRITER.md"                   "$DATA_ROOT/claude_applicator/writer/WRITER.md"
bind "claude_applicator/inbox_reader/MAIL_AGENT.md"         "$DATA_ROOT/claude_applicator/inbox_reader/MAIL_AGENT.md"
bind "claude_applicator/inbox_reader/user_mail.json"        "$DATA_ROOT/claude_applicator/inbox_reader/user_mail.json"
bind "claude_applicator/inbox_reader/credentials.json"      "$DATA_ROOT/claude_applicator/inbox_reader/credentials.json"

# Gmail client secrets + cached tokens: only the ones that exist on either side.
for pat in 'client_secret*.json' 'token*.json'; do
  for f in "$REPO_ROOT/claude_applicator/inbox_reader/"$pat "$DATA_ROOT/claude_applicator/inbox_reader/"$pat; do
    [[ -e "$f" || -L "$f" ]] || continue
    bind "claude_applicator/inbox_reader/$(basename "$f")" "$DATA_ROOT/claude_applicator/inbox_reader/$(basename "$f")"
  done
done

# Files app/lib/paths.py and the run scripts keep at a flattened data-root spot — bind
# the repo location to THAT copy (the one place the app and the engine both use).
bind "claude_applicator/profile.yaml"     "$DATA_ROOT/profile.yaml"
bind "claude_applicator/qa_bank.md"       "$DATA_ROOT/qa_bank.md"
bind "claude_applicator/data"             "$DATA_ROOT/data"
bind "claude_applicator/applications.db"  "$DATA_ROOT/applications.db"

# Exception: user.env lives directly in the data root (read as $DATA_ROOT/user.env by
# the run scripts; the symlink makes it visible at its repo-relative location too).
bind "claude_applicator/user.env"         "$DATA_ROOT/user.env"

# The cv-builder skill's two personal files (tracked template.html/render.sh stay in
# place next to the symlinks; SKILL.md is rendered into the data root, answer_bank.md is
# written there by the app).
bind ".claude/skills/cv-builder/SKILL.md"        "$DATA_ROOT/.claude/skills/cv-builder/SKILL.md"
bind ".claude/skills/cv-builder/answer_bank.md"  "$DATA_ROOT/.claude/skills/cv-builder/answer_bank.md"

exit 0
