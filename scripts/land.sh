#!/usr/bin/env bash
# scripts/land.sh — the local train (docs/DESIGN.md §2 + §5.3).
#
# Assembles <train_prefix><timestamp> = <remote>/<main> + a --no-ff merge of
# each candidate branch, renews each candidate's claims, runs the SAME full
# gate CI runs (scripts/full-gate.sh — "passes locally, fails in CI" is a
# runner difference, never a gate difference), refreshes the baseline on a
# green gate, and fast-forwards <remote>/<main> to the train tip under
# MERGE_GATE_TRAIN=1 (the only thing that satisfies the pre-push hook's
# refusal of direct pushes to main). Never force-pushes; never deletes a
# remote branch other than its own temporary train ref.
#
# Usage:
#   scripts/land.sh [--dry-run] [--no-gate --i-understand-ungated]
#                   [--bootstrap] (<branch>|<PR#>)...
#
# Run from the PRIMARY checkout, on the main branch, with a clean working
# tree. The remote and branch names come from merge-gate.toml only — there
# is no environment override, so a stray variable cannot redirect a landing.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PY="$SCRIPT_DIR/mergegate_config.py"

cfg() { python3 "$CONFIG_PY" get "$@"; }

usage() {
  cat <<'EOF'
Usage: scripts/land.sh [--dry-run] [--no-gate --i-understand-ungated]
                       [--bootstrap] (<branch>|<PR#>)...

  --dry-run              print every mutating command instead of running
                          it. Read-only lookups (fetch existence checks, gh
                          pr view, git status) still execute for real so
                          the printed plan is accurate; nothing in the
                          repo, on disk, or on the remote is changed.
  --no-gate              skip scripts/full-gate.sh entirely. Requires
                          --i-understand-ungated. Skips the baseline
                          refresh and the Verified-Train: trailer too, so
                          a hosted push:main lane still runs against this
                          push. Operator override only.
  --i-understand-ungated required alongside --no-gate.
  --bootstrap            pass --bootstrap through to scripts/full-gate.sh
                          (only meaningful when the baseline has no
                          recorded suites yet — refused otherwise).

<branch>  a branch name, merged as <remote>/<branch>.
<PR#>     a bare integer, resolved to its head branch via
          `gh pr view <PR#> --json headRefName`.
EOF
}

RED=$'\033[1;31m'
RESET=$'\033[0m'

log() { echo "==> $*"; }
warn() { echo "WARNING: $*" >&2; }
warn_red() { printf '%s%s%s\n' "$RED" "$*" "$RESET" >&2; }

if ! python3 "$CONFIG_PY" validate >/dev/null; then
  exit 1
fi

REMOTE="$(cfg repo.remote)"
MAIN="$(cfg repo.main_branch)"
TRAIN_PREFIX="$(cfg repo.train_prefix)"
BASELINE_FILE="$(cfg repo.baseline_file)"
RESULTS_DIR="$(cfg repo.results_dir)"
STATUS_CONTEXT="$(cfg repo.status_context)"

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  echo "land.sh: remote '$REMOTE' (merge-gate.toml repo.remote) does not exist" >&2
  exit 1
fi

# --bootstrap is only meaningful when the baseline has no recorded suites
# yet — mirrors scripts/test-baseline.py's own bootstrap guard, which
# refuses to bootstrap over an already-established baseline. Prints
# nothing; exit 0 = bootstrap allowed, exit 1 = refuse.
bootstrap_allowed() {
  if [ ! -f "$BASELINE_FILE" ]; then
    return 0
  fi
  python3 - "$BASELINE_FILE" <<'PYEOF'
import json, sys
try:
    with open(sys.argv[1]) as f:
        data = json.load(f)
except Exception:
    sys.exit(0)  # missing/unreadable -> nothing recorded yet, bootstrap OK
suites = data.get("suites") or {}
sys.exit(0 if not suites else 1)
PYEOF
}

# Space-separated service ids from `claims.py list --json` whose `branch`
# field equals $1 — the post-land assertion below uses this to verify a
# `release --branch` actually cleared every claim it should have. Read-only;
# safe to call in --dry-run. Main is already pushed when this runs, so a
# failure here (remote gone, malformed record) must never abort the
# cleanup — callers append `|| true` and act on an empty result.
claims_held_by() {
  local branch="$1"
  python3 "$SCRIPT_DIR/claims.py" list --json | python3 -c '
import json, sys
branch = sys.argv[1]
try:
    records = json.load(sys.stdin)
except Exception:
    records = []
print(" ".join(r.get("service", "") for r in records if r.get("branch") == branch))
' "$branch"
}

valid_branch_name() {
  case "$1" in
    -*|"") return 1 ;;
  esac
  git check-ref-format --branch "$1" >/dev/null 2>&1
}

DRY_RUN=0
run_mut() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+'
    printf ' %q' "$@"
    printf '\n'
    return 0
  fi
  "$@"
}

# --------------------------------------------------------------------------
# Argument parsing
# --------------------------------------------------------------------------

NO_GATE=0
I_UNDERSTAND_UNGATED=0
BOOTSTRAP=0
RAW_CANDIDATES=()

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --no-gate)
      NO_GATE=1
      shift
      ;;
    --i-understand-ungated)
      I_UNDERSTAND_UNGATED=1
      shift
      ;;
    --bootstrap)
      BOOTSTRAP=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    --)
      shift
      while [ $# -gt 0 ]; do
        RAW_CANDIDATES+=("$1")
        shift
      done
      ;;
    -*)
      echo "land.sh: unknown flag '$1'" >&2
      usage >&2
      exit 1
      ;;
    *)
      RAW_CANDIDATES+=("$1")
      shift
      ;;
  esac
done

if [ ${#RAW_CANDIDATES[@]} -eq 0 ]; then
  echo "land.sh: no branches or PR numbers given" >&2
  usage >&2
  exit 1
fi

if [ "$NO_GATE" -eq 1 ] && [ "$I_UNDERSTAND_UNGATED" -ne 1 ]; then
  echo "land.sh: --no-gate requires --i-understand-ungated (operator override only)" >&2
  exit 1
fi

# --------------------------------------------------------------------------
# Pre-flight: primary checkout, on main, clean.
# --------------------------------------------------------------------------

CURRENT_ROOT="$(git rev-parse --show-toplevel)"
COMMON_DIR="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
if [ -z "$COMMON_DIR" ]; then
  COMMON_DIR="$(git rev-parse --git-common-dir)"
  case "$COMMON_DIR" in
    /*) : ;;
    *) COMMON_DIR="$(cd "$(dirname "$COMMON_DIR")" && pwd)/$(basename "$COMMON_DIR")" ;;
  esac
fi
PRIMARY_ROOT="$(dirname "$COMMON_DIR")"

if [ "$CURRENT_ROOT" != "$PRIMARY_ROOT" ]; then
  echo "land.sh: must be run from the primary checkout ($PRIMARY_ROOT), not $CURRENT_ROOT" >&2
  exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "$MAIN" ]; then
  echo "land.sh: primary checkout must be on '$MAIN' (currently on '$CURRENT_BRANCH')" >&2
  exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
  echo "land.sh: primary checkout is dirty — refusing" >&2
  exit 1
fi

# --------------------------------------------------------------------------
# Resolve candidates (branch name or PR number) to validated branch names.
# --------------------------------------------------------------------------

CANDIDATES=()
for raw in "${RAW_CANDIDATES[@]}"; do
  if [[ "$raw" =~ ^[0-9]+$ ]]; then
    log "resolving PR #$raw -> head branch (gh pr view)"
    head_ref="$(gh pr view "$raw" --json headRefName --jq .headRefName 2>/dev/null || true)"
    if [ -z "$head_ref" ]; then
      warn "could not resolve PR #$raw via gh — skipping"
      continue
    fi
    if ! valid_branch_name "$head_ref"; then
      warn "PR #$raw resolved to an invalid branch name — skipping"
      continue
    fi
    log "PR #$raw -> $head_ref"
    CANDIDATES+=("$head_ref")
  else
    if ! valid_branch_name "$raw"; then
      echo "land.sh: '$raw' is not a valid branch name" >&2
      exit 1
    fi
    CANDIDATES+=("$raw")
  fi
done

if [ ${#CANDIDATES[@]} -eq 0 ]; then
  echo "land.sh: no candidates resolved — nothing to land" >&2
  exit 1
fi

log "candidates: ${CANDIDATES[*]}"

# --------------------------------------------------------------------------
# Fetch + assemble the train
# --------------------------------------------------------------------------

log "git fetch $REMOTE"
run_mut git fetch "$REMOTE"

TS="$(date -u +%Y%m%d%H%M%S)"
TRAIN_BRANCH="${TRAIN_PREFIX}${TS}"
MAIN_REF="$REMOTE/$MAIN"

log "creating $TRAIN_BRANCH from $MAIN_REF"
run_mut git branch "$TRAIN_BRANCH" "$MAIN_REF"
run_mut git checkout "$TRAIN_BRANCH"

LANDED=()
DROPPED=()

for branch in "${CANDIDATES[@]}"; do
  ref="$REMOTE/$branch"
  if ! git rev-parse --verify -q "$ref" >/dev/null 2>&1; then
    if [ "$DRY_RUN" -eq 1 ]; then
      warn "$ref not found on $REMOTE yet — dry-run assumes it will be pushed before a real run; showing the planned merge anyway"
      log "merging $ref --no-ff into $TRAIN_BRANCH (planned)"
      run_mut git merge --no-ff --no-edit "$ref"
      echo "   (dry-run: ref not found — this line is illustrative only; a real run would drop this candidate unless it is pushed first)"
      LANDED+=("$branch")
      continue
    fi
    warn "$ref not found on $REMOTE (branch not pushed yet?) — dropping '$branch' from this train"
    DROPPED+=("$branch")
    continue
  fi

  log "merging $ref --no-ff into $TRAIN_BRANCH"
  if [ "$DRY_RUN" -eq 1 ]; then
    run_mut git merge --no-ff --no-edit "$ref"
    echo "   (dry-run: assumed to merge cleanly — only an actual run's merge detects real conflicts)"
    LANDED+=("$branch")
    continue
  fi

  if git merge --no-ff --no-edit "$ref"; then
    LANDED+=("$branch")
  else
    warn "'$branch' conflicts with the train — dropping it and aborting its merge"
    git merge --abort || true
    DROPPED+=("$branch")
  fi
done

log "landed: ${LANDED[*]:-(none)}"
if [ ${#DROPPED[@]} -gt 0 ]; then
  log "dropped (conflict or not pushed): ${DROPPED[*]}"
fi

if [ ${#LANDED[@]} -eq 0 ]; then
  echo "land.sh: nothing landed cleanly — cleaning up $TRAIN_BRANCH" >&2
  if [ "$DRY_RUN" -eq 0 ]; then
    git checkout "$MAIN"
    git branch -D "$TRAIN_BRANCH"
  fi
  exit 1
fi

# --------------------------------------------------------------------------
# Renew claims for every landed candidate branch.
# --------------------------------------------------------------------------

for branch in "${LANDED[@]}"; do
  log "renewing claims held by '$branch'"
  run_mut python3 "$SCRIPT_DIR/claims.py" renew --branch "$branch"
done

# --------------------------------------------------------------------------
# --bootstrap eligibility
# --------------------------------------------------------------------------

if [ "$BOOTSTRAP" -eq 1 ] && ! bootstrap_allowed; then
  echo "land.sh: --bootstrap refused — $BASELINE_FILE already has recorded suites. --bootstrap is only for creating the FIRST baseline; run a normal landing instead." >&2
  run_mut git checkout "$MAIN"
  run_mut git branch -D "$TRAIN_BRANCH"
  exit 1
fi

# --------------------------------------------------------------------------
# Full gate
# --------------------------------------------------------------------------

GATE_ARGS=(--results "$RESULTS_DIR" --base "$MAIN_REF")
for branch in "${LANDED[@]}"; do
  GATE_ARGS+=(--branch "$branch")
done
if [ "$BOOTSTRAP" -eq 1 ]; then
  GATE_ARGS+=(--bootstrap)
fi

if [ "$NO_GATE" -eq 1 ]; then
  warn_red ""
  warn_red "*******************************************************************"
  warn_red "UNGATED LANDING: --no-gate — skipping scripts/full-gate.sh entirely."
  warn_red "This is an OPERATOR-OVERRIDE-ONLY escape hatch. The train is about to"
  warn_red "be pushed to $MAIN WITHOUT running the build/test/ADR/invariants/"
  warn_red "claims/zero-regression gate. Because there is no green gate, this"
  warn_red "landing does NOT refresh the baseline and does NOT carry a"
  warn_red "Verified-Train: trailer — a hosted push:$MAIN lane WILL run"
  warn_red "against this push (it only skips when that trailer is present)."
  warn_red "Do not use this in normal operation."
  warn_red "*******************************************************************"
  GATE_OK=1
else
  log "running scripts/full-gate.sh ${GATE_ARGS[*]}"
  if [ "$DRY_RUN" -eq 1 ]; then
    run_mut bash "$SCRIPT_DIR/full-gate.sh" "${GATE_ARGS[@]}"
    GATE_OK=1
  elif bash "$SCRIPT_DIR/full-gate.sh" "${GATE_ARGS[@]}"; then
    GATE_OK=1
  else
    GATE_OK=0
  fi
fi

if [ "$GATE_OK" -ne 1 ]; then
  echo "land.sh: full gate failed — train NOT pushed. '$TRAIN_BRANCH' left in place for inspection." >&2
  git checkout "$MAIN"
  exit 1
fi

# --------------------------------------------------------------------------
# Baseline refresh + commit — SKIPPED entirely under --no-gate: with no
# green gate there is nothing honest to record, and omitting the
# Verified-Train: trailer is what lets a hosted push:main lane run against
# this landing instead of skipping it.
# --------------------------------------------------------------------------

REMOTE_TRAIN_PUSHED=0

if [ "$NO_GATE" -eq 1 ]; then
  log "skipping baseline refresh and Verified-Train trailer (--no-gate)"
else
  if [ "$DRY_RUN" -eq 1 ]; then
    TRAIN_TIP_BEFORE_BASELINE="<train-tip-sha>"
  else
    TRAIN_TIP_BEFORE_BASELINE="$(git rev-parse HEAD)"
  fi

  log "recording baseline: scripts/test-baseline.py record --results $RESULTS_DIR --out $BASELINE_FILE --train $TRAIN_BRANCH"
  run_mut python3 "$SCRIPT_DIR/test-baseline.py" record --results "$RESULTS_DIR" --out "$BASELINE_FILE" --train "$TRAIN_BRANCH"

  run_mut git add "$BASELINE_FILE"

  COMMIT_MSG="ci: refresh zero-regression baseline

Verified-Train: $TRAIN_BRANCH $TRAIN_TIP_BEFORE_BASELINE"

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+ git commit -m %q\n' "$COMMIT_MSG"
  else
    git commit -q -m "$COMMIT_MSG"
  fi

  # ------------------------------------------------------------------------
  # Commit status for branch protection. A ruleset on the main branch can
  # require the status context below to be green on the commit being
  # pushed. A hosted lane provides it as a check-run; a LOCAL train has no
  # hosted run, so we post an equivalent commit status ourselves, on the
  # exact sha that will become main (the baseline commit, i.e. HEAD now). A
  # user token with `repo` scope may create commit statuses. Under
  # --no-gate nothing is posted, so protection rejects ungated landings —
  # that is the intended server-side backstop.
  #
  # A commit status can only be attached to a commit the host already has,
  # so the TRAIN ref is pushed first (a plain branch push; the pre-push
  # hook allows the train prefix under MERGE_GATE_TRAIN=1). The remote
  # train ref is deleted after the main push, success or failure.
  # ------------------------------------------------------------------------
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '+ MERGE_GATE_TRAIN=1 git push %q %q:refs/heads/%q   # train ref first so the host knows the sha\n' "$REMOTE" "$TRAIN_BRANCH" "$TRAIN_BRANCH"
    printf '+ gh api -X POST repos/{owner}/{repo}/statuses/<head-sha> -f state=success -f context=%q -f description=%q\n' \
      "$STATUS_CONTEXT" "local train $TRAIN_BRANCH: full gate green"
  else
    HEAD_SHA="$(git rev-parse HEAD)"
    if MERGE_GATE_TRAIN=1 git push -q "$REMOTE" "$TRAIN_BRANCH:refs/heads/$TRAIN_BRANCH"; then
      REMOTE_TRAIN_PUSHED=1
    else
      warn_red "could not push the train ref '$TRAIN_BRANCH' to $REMOTE — without it the required status cannot be attached and a protected main will reject the landing."
    fi
    if command -v gh >/dev/null 2>&1; then
      STATUS_ERR="$(mktemp)"
      posted=0
      for attempt in 1 2; do
        if gh api -X POST "repos/{owner}/{repo}/statuses/$HEAD_SHA" \
            -f state=success -f context="$STATUS_CONTEXT" \
            -f description="local train $TRAIN_BRANCH: full gate green" >/dev/null 2>"$STATUS_ERR"; then
          posted=1
          break
        fi
        [ "$attempt" -eq 1 ] && sleep 3
      done
      if [ "$posted" -eq 1 ]; then
        log "posted commit status '$STATUS_CONTEXT' on $HEAD_SHA"
      else
        warn_red "could not post the '$STATUS_CONTEXT' commit status on $HEAD_SHA — gh said: $(tr '\n' ' ' <"$STATUS_ERR" | cut -c1-400). A ruleset that requires it will reject the push; fix the cause (gh auth / network / the train-ref push above) and re-run."
      fi
      rm -f "$STATUS_ERR"
    else
      warn "gh not installed — no commit status posted (only matters if the main branch requires '$STATUS_CONTEXT')"
    fi
  fi
fi

# --------------------------------------------------------------------------
# Push train -> main (never --force; MERGE_GATE_TRAIN=1 is what pre-push
# accepts)
# --------------------------------------------------------------------------

log "pushing $TRAIN_BRANCH -> $MAIN (MERGE_GATE_TRAIN=1)"
if [ "$DRY_RUN" -eq 1 ]; then
  printf '+ MERGE_GATE_TRAIN=1 git push %q %q:%q\n' "$REMOTE" "$TRAIN_BRANCH" "$MAIN"
else
  PUSH_OUT="$(mktemp)"
  if MERGE_GATE_TRAIN=1 git push "$REMOTE" "$TRAIN_BRANCH:$MAIN" 2>&1 | tee "$PUSH_OUT" && [ "${PIPESTATUS[0]}" -eq 0 ]; then
    log "pushed to $MAIN"
    rm -f "$PUSH_OUT"
  else
    if grep -q "GH013\|rule violations\|Required status check" "$PUSH_OUT"; then
      cat >&2 <<EOF
land.sh: push to $MAIN was REJECTED BY A RULESET (required status check
missing, or another rule) — see the remote lines above. This is not
"$MAIN moved": the required '$STATUS_CONTEXT' status was not attached to
$(git rev-parse --short HEAD) (see the status-post warning above, if any).
Fix the cause and re-run scripts/land.sh with the same candidates.
'$TRAIN_BRANCH' left in place locally for inspection.
EOF
    else
      cat >&2 <<EOF
land.sh: push to $MAIN was rejected ($MAIN_REF moved since fetch — not a
fast-forward). Re-run scripts/land.sh with the same candidates; it will
re-fetch and rebuild the train against the new $MAIN. Never force-push.
'$TRAIN_BRANCH' left in place for inspection.
EOF
    fi
    rm -f "$PUSH_OUT"
    if [ "$REMOTE_TRAIN_PUSHED" -eq 1 ]; then
      MERGE_GATE_TRAIN=1 git push -q "$REMOTE" --delete "$TRAIN_BRANCH" 2>/dev/null || true
    fi
    git checkout "$MAIN"
    exit 1
  fi
fi

# The temporary remote train ref has done its job (the status is attached to
# the sha that is now main's tip); remove it so the remote does not
# accumulate train branches.
if [ "$DRY_RUN" -ne 1 ] && [ "$REMOTE_TRAIN_PUSHED" -eq 1 ]; then
  MERGE_GATE_TRAIN=1 git push -q "$REMOTE" --delete "$TRAIN_BRANCH" 2>/dev/null \
    || warn_red "could not delete the remote train ref '$TRAIN_BRANCH' — delete it by hand (git push $REMOTE --delete $TRAIN_BRANCH)."
fi

# --------------------------------------------------------------------------
# Release claims for landed branches; return to main; clean up.
# --------------------------------------------------------------------------

for branch in "${LANDED[@]}"; do
  log "releasing claims held by '$branch'"
  # A release failure here must never abort the train — main is already
  # pushed. claims.py's own retries absorb transient ref-push failures; a
  # nonzero exit just means something is worth checking below.
  run_mut python3 "$SCRIPT_DIR/claims.py" release --branch "$branch" \
    || warn "claims.py release --branch '$branch' reported a failure — checking for leftover claims below"

  # Post-land assertion: confirm nothing held by this branch survived the
  # release. If it did, retry once more, then report loudly — never fail
  # the train over it, since main already moved.
  if [ "$DRY_RUN" -ne 1 ]; then
    leftover="$(claims_held_by "$branch")" || true
    if [ -n "$leftover" ]; then
      warn "post-land assertion: '$branch' still holds claim(s) after release — retrying once: $leftover"
      python3 "$SCRIPT_DIR/claims.py" release --branch "$branch" || true
      leftover="$(claims_held_by "$branch")" || true
      if [ -n "$leftover" ]; then
        warn_red "LEFTOVER CLAIMS after retry — '$branch' still holds: $leftover (train already landed; release by hand: python3 scripts/claims.py release --branch $branch)"
      else
        log "post-land assertion: retry cleared the leftover claim(s) held by '$branch'"
      fi
    fi
  fi
done

run_mut git checkout "$MAIN"
run_mut git pull --ff-only
run_mut git branch -D "$TRAIN_BRANCH"

log "done. Landed: ${LANDED[*]}"
