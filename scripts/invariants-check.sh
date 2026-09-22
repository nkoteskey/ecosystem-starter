#!/usr/bin/env bash
#
# invariants-check.sh — grep-shaped architectural invariants
# (docs/DESIGN.md §5.2 G4).
#
# Each `[invariants.checks.<id>]` table in merge-gate.toml names a regular
# expression, the path globs it applies to, and the globs it excludes. A
# hit is a violation unless the `<id> <path>` pair is listed in the
# allowlist file (`invariants.allowlist`) with a reason.
#
# Ratchet: the allowlist snapshots pre-existing hits so a gate can be
# introduced on a tree that does not yet satisfy it. A listed hit is
# reported as an informational `allowlisted:` note and does not fail; any
# NEW file hitting an invariant fails. The list should only shrink — add to
# it only with a visible justification in the same change. A pattern that
# hits the clean tree is wrong, but weakening it is a human call, not
# something this script does silently.
#
# Matching is done by scripts/mergegate_scan.py (Python `re`, one dialect
# on every host). A scan failure (bad pattern, unreadable file) exits 3 so
# it can never read as a clean "0 hits".
#
# Exit 0 + "invariants: OK (N checked)" when clean; exit 1 on any hit, with
# each violation printed as `id: path:line: text`.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
SCRIPT_DIR="$ROOT/scripts"
CONFIG_PY="$SCRIPT_DIR/mergegate_config.py"

cfg() { python3 "$CONFIG_PY" get "$@"; }
cfg_list() {
  CFG_LIST=()
  local line
  while IFS= read -r line; do
    if [ -n "$line" ]; then
      CFG_LIST+=("$line")
    fi
  done < <("$@")
}

if ! python3 "$CONFIG_PY" validate >/dev/null; then
  exit 3
fi

ALLOWLIST="$ROOT/$(cfg invariants.allowlist)"
cfg_list cfg invariants.roots
ROOTS=("${CFG_LIST[@]:+${CFG_LIST[@]}}")

violation_count=0
allowlisted_count=0
checked_count=0

is_allowlisted() {
  # $1 = invariant id, $2 = repo-relative path
  [ -f "$ALLOWLIST" ] || return 1
  grep -Ev '^[[:space:]]*(#|$)' "$ALLOWLIST" | awk '{print $1" "$2}' | grep -Fxq "$1 $2"
}

# scan_invariant <id> — reads the check's pattern/paths/excludes from the
# config and reports each hit.
scan_invariant() {
  local id="$1" pattern
  pattern="$(cfg "invariants.checks.$id.pattern")"
  local -a args=(--pattern "$pattern")
  local r
  for r in "${ROOTS[@]}"; do
    args+=(--root "$r")
  done
  cfg_list cfg "invariants.checks.$id.paths"
  local p
  for p in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do
    args+=(--glob "$p")
  done
  cfg_list cfg "invariants.checks.$id.excludes"
  local e
  for e in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do
    args+=(--exclude "$e")
  done

  local err_file hits rc
  err_file="$(mktemp)"
  hits="$(python3 "$SCRIPT_DIR/mergegate_scan.py" "${args[@]}" 2>"$err_file")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "" >&2
    echo "invariants-check: FATAL — scan failed for '$id' (exit $rc):" >&2
    cat "$err_file" >&2
    rm -f "$err_file"
    exit 3
  fi
  rm -f "$err_file"
  checked_count=$((checked_count + 1))

  if [ -n "$hits" ]; then
    while IFS= read -r hit; do
      [ -n "$hit" ] || continue
      if is_allowlisted "$id" "${hit%%:*}"; then
        echo "allowlisted: $id: $hit"
        allowlisted_count=$((allowlisted_count + 1))
      else
        echo "$id: $hit"
        violation_count=$((violation_count + 1))
      fi
    done <<< "$hits"
  fi
}

echo "invariants-check: scanning ${ROOTS[*]} …"

cfg_list python3 "$CONFIG_PY" keys invariants.checks
for id in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do
  scan_invariant "$id"
done

echo ""
if [ "$violation_count" -gt 0 ]; then
  echo "invariants: FAIL ($violation_count violation(s))"
  exit 1
fi

echo "invariants: OK ($checked_count checked; $allowlisted_count allowlisted pre-existing hit(s) — see $(cfg invariants.allowlist))"
exit 0
