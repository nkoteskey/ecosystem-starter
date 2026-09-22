#!/usr/bin/env bash
#
# standards-check.sh — mechanical enforcement of docs/standards/.
#
# Each `[standards.checks.<id>]` table in merge-gate.toml is the executable
# face of a rule in a standards document. Each check greps the tree for the
# pattern-shaped defect its rule forbids and fails when one reappears
# un-allowlisted.
#
# FAIL tier (`tier = "fail"`, exit nonzero): a re-introduced defect class.
# WARN tier (`tier = "warn"`, report only): weaker smells worth a glance.
#
# Scope: `scope = "ts"` scans `standards.ts_root` with `standards.ts_globs`;
# `scope = "rust"` scans `standards.rust_roots` with `standards.rust_globs`.
#
# Allowlist (`standards.allowlist`): path-substring suppressions with a
# mandatory justification. A FAIL match whose file path contains an
# allowlisted substring for that check id is downgraded to an informational
# note (printed under VERBOSE=1).
#
# No dependencies beyond bash + python3 (scripts/mergegate_scan.py).

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

ALLOWLIST="$ROOT/$(cfg standards.allowlist)"

fail_count=0
warn_count=0
note_count=0
checked_count=0

# scan <scope> <pattern> -> emits "path:line:content"; exits 3 on a scan failure.
scan() {
  local scope="$1" pattern="$2"
  local -a args=(--pattern "$pattern")
  local r g
  if [ "$scope" = "rust" ]; then
    cfg_list cfg standards.rust_roots
    for r in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do args+=(--root "$r"); done
    cfg_list cfg standards.rust_globs
    for g in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do args+=(--glob "$g"); done
  else
    args+=(--root "$(cfg standards.ts_root)")
    cfg_list cfg standards.ts_globs
    for g in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do args+=(--glob "$g"); done
  fi
  local err_file rc
  err_file="$(mktemp)"
  python3 "$SCRIPT_DIR/mergegate_scan.py" "${args[@]}" 2>"$err_file"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    echo "standards-check: FATAL — scan failed (exit $rc):" >&2
    cat "$err_file" >&2
    rm -f "$err_file"
    exit 3
  fi
  rm -f "$err_file"
}

# is_allowlisted <check_id> <filepath> -> 0 if suppressed
is_allowlisted() {
  local check_id="$1" path="$2" cid rest sub
  [ -f "$ALLOWLIST" ] || return 1
  while IFS= read -r line; do
    case "$line" in ''|'#'*) continue ;; esac
    cid="${line%% *}"
    [ "$cid" = "$check_id" ] || continue
    rest="${line#"$cid"}"
    rest="${rest#"${rest%%[![:space:]]*}"}"
    sub="${rest%%[[:space:]]*}"
    [ -n "$sub" ] || continue
    case "$path" in *"$sub"*) return 0 ;; esac
  done < "$ALLOWLIST"
  return 1
}

run_check() {
  local check_id="$1" tier scope pattern message
  tier="$(cfg "standards.checks.$check_id.tier")"
  scope="$(cfg "standards.checks.$check_id.scope")"
  pattern="$(cfg "standards.checks.$check_id.pattern")"
  message="$(cfg "standards.checks.$check_id.message")"
  checked_count=$((checked_count + 1))

  local hits path first=1
  hits="$(scan "$scope" "$pattern")" || exit 3
  [ -n "$hits" ] || return 0
  while IFS= read -r hit; do
    [ -n "$hit" ] || continue
    path="${hit%%:*}"
    if [ "$tier" = "warn" ]; then
      if [ "$first" = "1" ]; then
        echo ""
        echo "WARN [$check_id]: $message"
        first=0
      fi
      echo "  $hit"
      warn_count=$((warn_count + 1))
      continue
    fi
    if is_allowlisted "$check_id" "$path"; then
      note_count=$((note_count + 1))
      [ "${VERBOSE:-0}" = "1" ] && echo "  note [$check_id] allowlisted: $hit"
      continue
    fi
    if [ "$first" = "1" ]; then
      echo ""
      echo "FAIL [$check_id]: $message"
      first=0
    fi
    echo "  $hit"
    fail_count=$((fail_count + 1))
  done <<< "$hits"
}

echo "standards-check: scanning …"

cfg_list python3 "$CONFIG_PY" keys standards.checks
for id in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do
  run_check "$id"
done

echo ""
echo "standards-check summary: $checked_count check(s), $fail_count failure(s), $warn_count warning(s), $note_count allowlisted note(s)."
if [ "$fail_count" -gt 0 ]; then
  echo "FAILED — see docs/standards/ for the rule each check enforces, or add a justified"
  echo "         entry to $(cfg standards.allowlist) if the site is a legitimate exception."
  exit 1
fi
echo "OK — no standards violations."
exit 0
