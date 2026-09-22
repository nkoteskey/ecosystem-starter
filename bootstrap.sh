#!/usr/bin/env bash
# bootstrap.sh — stamp a new repository from this template.
#
# Usage:
#   bootstrap.sh [<target-repo-root>] [--name <repo-name>] [--remote <url>]
#                [--apps <a,b,c>] [--force-config]
#
# With no <target>, stamps the current checkout in place (the normal path
# after "Use this template" on the hosting site: clone, then run this).
# With a <target> that is a different git repository, copies the template
# files there first, then stamps.
#
# Stamping:
#   --name    replaces the <REPO_NAME> placeholder in README.md, CLAUDE.md
#             and STATUS.md (default: the target directory's basename).
#   --remote  adds it as the `origin` remote when no origin exists.
#   --apps    comma-separated app names under apps/. Writes merge-gate.toml
#             with one [gates.frontend.<app>] table per app and lists them
#             in affected.frontend_apps / claims.app_crates. Without --apps
#             the shipped merge-gate.toml (one app, "example-app") is kept.
#
# Creates docs/NORTH_STAR.md from NORTH_STAR_TEMPLATE.md and CLAUDE.md from
# CLAUDE_TEMPLATE.md when they do not exist yet; never overwrites a file
# you have already edited (merge-gate.toml is the exception, with
# --force-config). Existing scripts of the same name are overwritten —
# that is how a newer template's scripts are applied.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
}

TARGET=""
NAME=""
REMOTE_URL=""
APPS=""
FORCE_CONFIG=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --remote) REMOTE_URL="$2"; shift 2 ;;
    --apps) APPS="$2"; shift 2 ;;
    --force-config) FORCE_CONFIG=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "bootstrap.sh: unknown flag '$1'" >&2; usage; exit 1 ;;
    *)
      if [ -n "$TARGET" ]; then
        echo "bootstrap.sh: only one target may be given" >&2
        exit 1
      fi
      TARGET="$1"; shift ;;
  esac
done

if [ -z "$TARGET" ]; then
  TARGET="$SRC"
fi
if [ ! -d "$TARGET" ]; then
  echo "bootstrap.sh: $TARGET is not a directory" >&2
  exit 1
fi
TARGET="$(cd "$TARGET" && pwd)"
if ! git -C "$TARGET" rev-parse --show-toplevel >/dev/null 2>&1; then
  echo "bootstrap.sh: $TARGET is not inside a git repository (run git init first)" >&2
  exit 1
fi
if [ -z "$NAME" ]; then
  NAME="$(basename "$TARGET")"
fi
case "$NAME" in
  ""|*[!A-Za-z0-9._-]*) echo "bootstrap.sh: --name must match [A-Za-z0-9._-]+" >&2; exit 1 ;;
esac

IN_PLACE=0
[ "$TARGET" = "$SRC" ] && IN_PLACE=1

# --------------------------------------------------------------------------
# 1. Copy template files into a different target (skipped in place).
# --------------------------------------------------------------------------

copy_if_absent() {
  local rel="$1"
  if [ -e "$TARGET/$rel" ]; then
    return 0
  fi
  mkdir -p "$(dirname "$TARGET/$rel")"
  cp "$SRC/$rel" "$TARGET/$rel"
  echo "  $rel"
}

if [ "$IN_PLACE" -eq 0 ]; then
  echo "copying template into $TARGET"
  mkdir -p "$TARGET/scripts/git-hooks" "$TARGET/scripts/tests" "$TARGET/ci" \
    "$TARGET/adr" "$TARGET/claims" "$TARGET/docs/standards" "$TARGET/.github/workflows"
  for f in "$SRC"/scripts/*.sh "$SRC"/scripts/*.py; do
    cp "$f" "$TARGET/scripts/$(basename "$f")"
    chmod +x "$TARGET/scripts/$(basename "$f")"
    echo "  scripts/$(basename "$f")"
  done
  cp "$SRC/scripts/git-hooks/pre-push" "$TARGET/scripts/git-hooks/pre-push"
  chmod +x "$TARGET/scripts/git-hooks/pre-push"
  echo "  scripts/git-hooks/pre-push"
  for f in "$SRC"/scripts/tests/test_*.py; do
    cp "$f" "$TARGET/scripts/tests/$(basename "$f")"
    echo "  scripts/tests/$(basename "$f")"
  done
  cp "$SRC/scripts/VENDORED" "$TARGET/scripts/VENDORED"
  for rel in scripts/invariants-allowlist.txt scripts/standards-allowlist.txt ci/baseline.json \
    adr/README.md adr/0000-template.md adr/OPEN-QUESTIONS.md claims/REGISTRY.toml \
    docs/PRODUCTION_FLIP_CHECKLIST.md docs/standards/README.md docs/MERGE_GATE.md \
    NORTH_STAR_TEMPLATE.md CLAUDE_TEMPLATE.md STATUS.md LICENSE SOURCE \
    .github/workflows/ci.yml; do
    copy_if_absent "$rel"
  done
  cp "$SRC/bootstrap.sh" "$TARGET/bootstrap.sh"
  chmod +x "$TARGET/bootstrap.sh"
  if [ -e "$TARGET/merge-gate.toml" ] && [ "$FORCE_CONFIG" -ne 1 ] && [ -z "$APPS" ]; then
    echo "  keeping existing merge-gate.toml (pass --force-config to overwrite)"
  elif [ -z "$APPS" ]; then
    cp "$SRC/merge-gate.toml" "$TARGET/merge-gate.toml"
    echo "  merge-gate.toml"
  fi
  if ! grep -qs '^ci-results/$' "$TARGET/.gitignore" 2>/dev/null; then
    printf 'ci-results/\n' >> "$TARGET/.gitignore"
    echo "  .gitignore (+ ci-results/)"
  fi
fi

# --------------------------------------------------------------------------
# 2. merge-gate.toml from --apps.
# --------------------------------------------------------------------------

write_config_for_apps() {
  local out="$TARGET/merge-gate.toml"
  local -a apps=()
  local IFS_SAVE="$IFS" a
  IFS=','
  for a in $APPS; do
    IFS="$IFS_SAVE"
    a="${a// /}"
    [ -n "$a" ] || continue
    case "$a" in
      *[!A-Za-z0-9._-]*) echo "bootstrap.sh: app name '$a' must match [A-Za-z0-9._-]+" >&2; exit 1 ;;
    esac
    apps+=("$a")
  done
  IFS="$IFS_SAVE"
  if [ "${#apps[@]}" -eq 0 ]; then
    echo "bootstrap.sh: --apps given but no app names parsed" >&2
    exit 1
  fi
  local quoted=""
  for a in "${apps[@]}"; do
    quoted="${quoted:+$quoted, }\"$a\""
  done
  local npm_dirs=""
  for a in "${apps[@]}"; do
    npm_dirs="${npm_dirs:+$npm_dirs, }\"apps/$a\""
  done
  {
    cat <<EOF
# merge-gate.toml — written by bootstrap.sh for: ${apps[*]}.
# Schema: docs/MERGE_GATE.md (points at the vendored gate's CONFIG.md).
# Use 'literal' strings for regular expressions.

schema = 1

[repo]
remote = "origin"
main_branch = "main"

[worktree]
bootstrap_copy = []
bootstrap_npm_dirs = [$npm_dirs]

[claims]
app_crates = [$quoted]
min_consumers = 2

[affected]
frontend_apps = [$quoted]

[gates]
rust = true
scripts_selftest = true
standards = true
adr = true
invariants = true
claims = true
EOF
    for a in "${apps[@]}"; do
      cat <<EOF

[gates.frontend.$a]
dir = "apps/$a"
install = "npm ci"
typecheck = "npx --no-install tsc --noEmit"
build = "npm run build"
test = 'npx --no-install vitest run --reporter=json --outputFile="\$MERGE_GATE_OUT"'
test_blocking = false
EOF
    done
    cat <<'EOF'

[invariants.checks.no_todo_panics]
description = "No todo!() panics outside tests"
pattern = 'todo!\('
paths = ["crates/**/*.rs", "apps/**/src-tauri/**/*.rs"]
excludes = ["**/tests/**", "**/*_test.rs", "**/tests.rs"]

[invariants.checks.no_server_bind]
description = "No HTTP/RPC server frameworks bound in application code"
pattern = '(TcpListener::bind|axum::Server|warp::serve|actix_web::HttpServer|tonic::transport::Server)'
paths = ["crates/**/*.rs", "apps/**/src-tauri/**/*.rs"]
excludes = ["**/tests/**", "**/*_test.rs", "**/tests.rs"]

[standards.checks.swallowed-catch]
tier = "warn"
scope = "ts"
pattern = 'catch\s*(\([^)]*\))?\s*\{\s*console\.error\([^;{}]*\);?\s*\}'
message = "catch surfaces only console.error — no user-visible error; confirm this is not an action flow"
EOF
  } > "$out"
  echo "  merge-gate.toml (apps: ${apps[*]})"
}

if [ -n "$APPS" ]; then
  if [ -e "$TARGET/merge-gate.toml" ] && [ "$FORCE_CONFIG" -ne 1 ] && [ "$IN_PLACE" -eq 1 ]; then
    echo "bootstrap.sh: merge-gate.toml exists; pass --force-config to rewrite it for --apps" >&2
    exit 1
  fi
  write_config_for_apps
fi

# --------------------------------------------------------------------------
# 3. Stamp documents.
# --------------------------------------------------------------------------

stamp() {
  local rel="$1"
  [ -f "$TARGET/$rel" ] || return 0
  if grep -q '<REPO_NAME>' "$TARGET/$rel"; then
    python3 - "$TARGET/$rel" "$NAME" <<'PYEOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1])
p.write_text(p.read_text().replace("<REPO_NAME>", sys.argv[2]))
PYEOF
    echo "  stamped <REPO_NAME> -> $NAME in $rel"
  fi
}

if [ ! -e "$TARGET/docs/NORTH_STAR.md" ]; then
  mkdir -p "$TARGET/docs"
  {
    printf '# %s — North Star\n\n' "$NAME"
    printf '> Written from NORTH_STAR_TEMPLATE.md. Delete the method text as you fill each section;\n'
    printf '> keep the section order. This document wins over every other document on conflict.\n\n'
    sed -n '/^## 1\. The thesis/,$p' "$SRC/NORTH_STAR_TEMPLATE.md"
  } > "$TARGET/docs/NORTH_STAR.md"
  echo "  docs/NORTH_STAR.md (from template — fill it in)"
fi
if [ ! -e "$TARGET/CLAUDE.md" ]; then
  python3 - "$SRC/CLAUDE_TEMPLATE.md" "$TARGET/CLAUDE.md" "$NAME" <<'PYEOF'
import pathlib, sys
src, dst, name = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3]
lines = src.read_text().splitlines()
# Drop the template's own title and its "copy me" paragraph: everything
# before the blockquote line that starts the real content.
start = next(i for i, ln in enumerate(lines) if ln.startswith("> **Start here"))
dst.write_text(f"# {name}\n\n" + "\n".join(lines[start:]) + "\n")
PYEOF
  echo "  CLAUDE.md (from template)"
fi
if [ ! -e "$TARGET/STATUS.md" ]; then
  cp "$SRC/STATUS.md" "$TARGET/STATUS.md"
fi
# A stamped repository gets its own short README, not the template's
# description of itself: written when absent, or when the README is still
# the template's own (in-place use after "Use this template").
if [ ! -e "$TARGET/README.md" ] || head -1 "$TARGET/README.md" | grep -q '^# ecosystem-starter$'; then
  {
    printf '# %s\n\n' "$NAME"
    printf '(One paragraph: what this is and who it is for — the thesis from\n'
    printf 'docs/NORTH_STAR.md §1, in plain words.)\n\n'
    printf -- '- Start with [docs/NORTH_STAR.md](docs/NORTH_STAR.md); it wins over every other document.\n'
    printf -- '- Working conventions, hard rules and the change loop: [CLAUDE.md](CLAUDE.md).\n'
    printf -- '- Decisions: [adr/](adr/). Open questions: [adr/OPEN-QUESTIONS.md](adr/OPEN-QUESTIONS.md).\n'
    printf -- '- Landing changes: [docs/MERGE_GATE.md](docs/MERGE_GATE.md).\n'
    printf -- '- Current state: [STATUS.md](STATUS.md).\n\n'
    printf 'Stamped from the ecosystem-starter template; see SOURCE.\n'
  } > "$TARGET/README.md"
  echo "  README.md (stub for $NAME)"
fi
stamp README.md
stamp CLAUDE.md
stamp STATUS.md

# --------------------------------------------------------------------------
# 4. Remote.
# --------------------------------------------------------------------------

if [ -n "$REMOTE_URL" ]; then
  if git -C "$TARGET" remote get-url origin >/dev/null 2>&1; then
    echo "  origin already exists ($(git -C "$TARGET" remote get-url origin)); not changed"
  else
    git -C "$TARGET" remote add origin "$REMOTE_URL"
    echo "  git remote add origin $REMOTE_URL"
  fi
fi

# --------------------------------------------------------------------------
# 5. Validate.
# --------------------------------------------------------------------------

if ! ( cd "$TARGET" && python3 scripts/mergegate_config.py validate ); then
  exit 1
fi

echo ""
echo "bootstrap complete for '$NAME'. Next:"
echo "  1. fill in docs/NORTH_STAR.md (the method text tells you what each section needs)"
echo "  2. edit CLAUDE.md: hard rules with their incidents, conventions, pointers"
echo "  3. review merge-gate.toml; bash scripts/full-gate.sh --dry-run"
echo "  4. bash scripts/wt.sh new <name>    # installs the pre-push hook"
echo "  5. first landing: bash scripts/land.sh --bootstrap <branch>"
