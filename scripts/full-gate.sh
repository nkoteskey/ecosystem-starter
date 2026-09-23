#!/usr/bin/env bash
# shellcheck disable=SC2317  # gate_* functions are invoked indirectly via run_gate "<name>" <fn>
#
# full-gate.sh — the ONE full-gate definition (docs/DESIGN.md §5.2), used by
# both a hosted full lane and the local `scripts/land.sh` train. Every gate
# below runs regardless of earlier failures (no fail-fast) so a single
# invocation reports the complete picture; the script's own exit code is 1
# if any blocking gate failed.
#
# Usage:
#   scripts/full-gate.sh [--results <dir>] [--skip-frontend-install]
#                        [--dry-run] [--bootstrap] [--base <ref>]
#                        [--branch <name>]...
#
# Run from the repo root. Writes <results>/gate-status.json
# ({gate: {status: pass|fail|skip, seconds, blocking}}) plus the raw
# per-tool outputs scripts/test-baseline.py's `collect` knows how to parse
# (cargo-*.log, vitest-<name>.json, standards.status).
#
# --dry-run prints every command each gate would run, without running
# anything (every gate reports "skip") and without touching the filesystem.
#
# Blocking policy (docs/DESIGN.md §5.5): the test-*producing* gates
# (`cargo-test`, each frontend's test step unless `test_blocking = true`,
# any `[gates.extra.*]` with `blocking = false`) are recorded pass/fail in
# gate-status.json but never flip the script's own exit code —
# `baseline-compare` is the SOLE blocking decision for test outcomes (that
# is what lets a known-failing test at baseline not block, while a brand
# new failure still does). Every other gate stays blocking.
#
# The gate list, the app directories and the commands each step runs come
# from merge-gate.toml (see docs/CONFIG.md). Configured commands run
# through `bash -c` from the step's directory with MERGE_GATE_OUT (the
# result file the step should write) and MERGE_GATE_RESULTS (the results
# dir) in the environment.
#
# `set -e` is deliberately NOT enabled: a failing gate must be recorded and
# the next gate must still run.

# shellcheck disable=SC2329  # gate_* functions are invoked indirectly through run_gate
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
SCRIPT_DIR="$ROOT/scripts"
CONFIG_PY="$SCRIPT_DIR/mergegate_config.py"

cfg() { python3 "$CONFIG_PY" get "$@"; }
# cfg_list <key> — fills the global CFG_LIST array (bash 3.2 has no mapfile).
cfg_list() {
  CFG_LIST=()
  local line
  while IFS= read -r line; do
    if [ -n "$line" ]; then
      CFG_LIST+=("$line")
    fi
  done < <(cfg "$@")
}
cfg_keys() {
  CFG_LIST=()
  local line
  while IFS= read -r line; do
    if [ -n "$line" ]; then
      CFG_LIST+=("$line")
    fi
  done < <(python3 "$CONFIG_PY" keys "$1")
}

if ! python3 "$CONFIG_PY" validate >/dev/null; then
  exit 2
fi

RESULTS_DIR="$(cfg repo.results_dir)"
BASELINE_FILE="$(cfg repo.baseline_file)"
BASE_REF="$(cfg repo.remote)/$(cfg repo.main_branch)"
SKIP_FRONTEND_INSTALL=0
DRY_RUN=0
BOOTSTRAP=0
BRANCHES=()

usage() {
  cat <<'EOF'
Usage: scripts/full-gate.sh [--results <dir>] [--skip-frontend-install]
                             [--dry-run] [--bootstrap] [--base <ref>]
                             [--branch <name>]...

  --results <dir>            Where to write gate-status.json + raw test
                              output (default: merge-gate.toml repo.results_dir).
  --skip-frontend-install     Skip the install step for a frontend whose
                              node_modules already exists.
  --dry-run                   Print every command each gate would run and
                              exit 0 without running anything.
  --bootstrap                 Pass --bootstrap through to
                              `test-baseline.py compare` (only valid when
                              the baseline's suites map is empty).
  --base <ref>                Ref adr-check/claims-check/baseline-compare
                              diff against (default: <remote>/<main>).
  --branch <name>             Repeatable. Branch name(s) that may hold a
                              claim on a touched shared service (a train
                              ref assembles several work branches, none of
                              which is the train's own branch name). With
                              no --branch, falls back to the current
                              `git rev-parse --abbrev-ref HEAD`.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --results)
      RESULTS_DIR="$2"; shift 2 ;;
    --results=*)
      RESULTS_DIR="${1#*=}"; shift ;;
    --skip-frontend-install)
      SKIP_FRONTEND_INSTALL=1; shift ;;
    --dry-run)
      DRY_RUN=1; shift ;;
    --bootstrap)
      BOOTSTRAP=1; shift ;;
    --base)
      BASE_REF="$2"; shift 2 ;;
    --base=*)
      BASE_REF="${1#*=}"; shift ;;
    --branch)
      BRANCHES+=("$2"); shift 2 ;;
    --branch=*)
      BRANCHES+=("${1#*=}"); shift ;;
    -h|--help)
      usage; exit 0 ;;
    *)
      echo "full-gate.sh: unknown argument: $1" >&2
      usage >&2
      exit 2 ;;
  esac
done

# --dry-run must not touch the filesystem at all — no results dir, no
# gate-status.json. RESULTS_DIR_ABS is still computed (gate functions print
# it in their would-run commands) without requiring the directory to exist.
if [ "$DRY_RUN" = "1" ]; then
  case "$RESULTS_DIR" in
    /*) RESULTS_DIR_ABS="$RESULTS_DIR" ;;
    *) RESULTS_DIR_ABS="$ROOT/$RESULTS_DIR" ;;
  esac
else
  mkdir -p "$RESULTS_DIR"
  RESULTS_DIR_ABS="$(cd "$RESULTS_DIR" && pwd)"
fi

# A color-enabled cargo wraps its output in ANSI escapes; the baseline
# parser strips them, but avoid generating them for every cargo invocation
# this script makes.
export CARGO_TERM_COLOR=never
export MERGE_GATE_RESULTS="$RESULTS_DIR_ABS"

OVERALL_FAIL=0
GATE_NAMES=()
GATE_STATUS=()
GATE_SECONDS=()
GATE_BLOCKING=()

record_gate() {
  GATE_NAMES+=("$1")
  GATE_STATUS+=("$2")
  GATE_SECONDS+=("$3")
  GATE_BLOCKING+=("$4")
}

# run_gate <name> <blocking 1|0> <gate-function> [args...]
# rc 0 -> pass, rc 200 -> skip (used by gates that no-op when a dependency
# they need is not present), anything else -> fail. In --dry-run every gate
# function is still CALLED (so it prints what it would do) but is always
# recorded "skip" and never flips OVERALL_FAIL. A non-blocking gate's
# failure is recorded but never sets OVERALL_FAIL — baseline-compare is the
# blocking decision for test outcomes.
run_gate() {
  local name="$1" blocking="$2"; shift 2
  local start end secs rc note=""
  [ "$blocking" = "0" ] && note=" (non-blocking; see baseline-compare)"
  echo ""
  echo "=== gate: $name$note ==="
  start=$(date +%s)
  "$@"
  rc=$?
  end=$(date +%s)
  secs=$((end - start))
  if [ "$DRY_RUN" = "1" ]; then
    record_gate "$name" "skip" "$secs" "$blocking"
    return 0
  fi
  if [ "$rc" -eq 0 ]; then
    record_gate "$name" "pass" "$secs" "$blocking"
  elif [ "$rc" -eq 200 ]; then
    record_gate "$name" "skip" "$secs" "$blocking"
  else
    record_gate "$name" "fail" "$secs" "$blocking"
    if [ "$blocking" = "1" ]; then
      OVERALL_FAIL=1
    fi
  fi
}

# maybe_run <description> -- <cmd...> — in --dry-run, print <description>
# and return 0; otherwise run <cmd...> and return its exit code.
maybe_run() {
  local desc="$1"; shift
  if [ "${1:-}" = "--" ]; then shift; fi
  if [ "$DRY_RUN" = "1" ]; then
    echo "  $desc"
    return 0
  fi
  "$@"
}

# run_configured <dir> <command> <out-file> — run one configured command
# string through bash from <dir>, with MERGE_GATE_OUT set to <out-file>.
run_configured() {
  local dir="$1" cmd="$2" out="$3"
  if [ "$DRY_RUN" = "1" ]; then
    echo "  (cd $dir && MERGE_GATE_OUT=$out bash -c $(printf '%q' "$cmd"))"
    return 0
  fi
  ( cd "$ROOT/$dir" && MERGE_GATE_OUT="$out" bash -c "$cmd" )
}

# ── pre steps ([gates.pre.<name>], blocking) ─────────────────────────────────

gate_pre() {
  local name="$1" cmd
  cmd="$(cfg "gates.pre.$name.command")"
  run_configured "." "$cmd" ""
}

# ── cargo fmt / clippy / test ────────────────────────────────────────────────

gate_cargo_fmt() {
  maybe_run "cargo fmt --all -- --check" -- cargo fmt --all -- --check
}

gate_cargo_clippy() {
  cfg_list gates.cargo_clippy_args
  maybe_run "cargo clippy ${CFG_LIST[*]}" -- cargo clippy "${CFG_LIST[@]}"
}

gate_cargo_test() {
  cfg_list gates.cargo_test_args
  if [ "$DRY_RUN" = "1" ]; then
    echo "  cargo test ${CFG_LIST[*]} 2>&1 | tee $RESULTS_DIR_ABS/cargo-test.log"
    return 0
  fi
  cargo test "${CFG_LIST[@]}" 2>&1 | tee "$RESULTS_DIR_ABS/cargo-test.log"
  return "${PIPESTATUS[0]}"
}

# ── frontends ([gates.frontend.<name>]) ──────────────────────────────────────

# gate_frontend_build: install + typecheck + build — blocking (a build/type
# failure is not a "test outcome" the baseline governs).
gate_frontend_build() {
  local name="$1" ok=0 dir install typecheck build
  dir="$(cfg "gates.frontend.$name.dir")"
  install="$(cfg "gates.frontend.$name.install")"
  typecheck="$(cfg "gates.frontend.$name.typecheck")"
  build="$(cfg "gates.frontend.$name.build")"

  if [ -n "$install" ]; then
    if [ "$SKIP_FRONTEND_INSTALL" = "1" ] && [ -d "$dir/node_modules" ]; then
      if [ "$DRY_RUN" = "1" ]; then
        echo "  (skip-frontend-install: $dir/node_modules present, skipping install)"
      fi
    else
      run_configured "$dir" "$install" "" || ok=1
    fi
  fi
  if [ -n "$typecheck" ]; then
    run_configured "$dir" "$typecheck" "" || ok=1
  fi
  if [ -n "$build" ]; then
    run_configured "$dir" "$build" "" || ok=1
  fi
  return "$ok"
}

# gate_frontend_test: the test step only — non-blocking unless
# `test_blocking = true` (baseline-compare governs test outcomes).
gate_frontend_test() {
  local name="$1" dir test
  dir="$(cfg "gates.frontend.$name.dir")"
  test="$(cfg "gates.frontend.$name.test")"
  if [ -z "$test" ]; then
    echo "skip: no test command configured for frontend '$name'" >&2
    return 200
  fi
  run_configured "$dir" "$test" "$RESULTS_DIR_ABS/vitest-$name.json"
}

# ── extra checks ([gates.extra.<name>]) ──────────────────────────────────────

gate_extra() {
  local name="$1" cmd cargo_log
  cmd="$(cfg "gates.extra.$name.command")"
  cargo_log="$(cfg "gates.extra.$name.cargo_log")"
  if [ "$cargo_log" = "true" ]; then
    local out="$RESULTS_DIR_ABS/cargo-$name.log"
    if [ "$DRY_RUN" = "1" ]; then
      echo "  bash -c $(printf '%q' "$cmd") 2>&1 | tee $out"
      return 0
    fi
    ( MERGE_GATE_OUT="$out" bash -c "$cmd" ) 2>&1 | tee "$out"
    return "${PIPESTATUS[0]}"
  fi
  run_configured "." "$cmd" ""
}

# ── gate scripts self-test ───────────────────────────────────────────────────

# The gate's own machinery has a stdlib unittest suite under scripts/tests/.
# A regression there silently weakens every other gate, so it is a BLOCKING
# gate here.
gate_scripts_selftest() {
  if [ ! -d "$SCRIPT_DIR/tests" ]; then
    echo "skip: scripts/tests not present" >&2
    return 200
  fi
  if [ "$DRY_RUN" = "1" ]; then
    echo "  python3 -m unittest discover -s scripts/tests -p 'test_*.py'"
    return 0
  fi
  python3 -m unittest discover -s "$SCRIPT_DIR/tests" -p 'test_*.py' 2>&1 | tail -n 20
  return "${PIPESTATUS[0]}"
}

# ── standards-check ──────────────────────────────────────────────────────────

gate_standards() {
  if [ "$DRY_RUN" = "1" ]; then
    echo "  bash scripts/standards-check.sh   (exit code recorded to $RESULTS_DIR_ABS/standards.status)"
    return 0
  fi
  bash "$SCRIPT_DIR/standards-check.sh"
  local rc=$?
  echo "$rc" > "$RESULTS_DIR_ABS/standards.status"
  return "$rc"
}

# ── ADR immutability ─────────────────────────────────────────────────────────

gate_adr_check() {
  maybe_run "python3 scripts/adr-check.py --base $BASE_REF --head HEAD" \
    -- python3 "$SCRIPT_DIR/adr-check.py" --base "$BASE_REF" --head HEAD
}

# ── grep invariants ──────────────────────────────────────────────────────────

gate_invariants() {
  maybe_run "bash scripts/invariants-check.sh" -- bash "$SCRIPT_DIR/invariants-check.sh"
}

# ── claims ───────────────────────────────────────────────────────────────────

gate_claims_sync_registry() {
  maybe_run "python3 scripts/claims.py sync-registry --check" \
    -- python3 "$SCRIPT_DIR/claims.py" sync-registry --check
}

gate_claims_check() {
  # The full gate runs on a TRAIN ref, but claims are held by the WORK
  # branches it assembled — repeat --branch once per work branch so
  # claims.py treats "holder is any of these" as satisfying the check.
  local -a branch_args=()
  if [ "${#BRANCHES[@]}" -gt 0 ]; then
    local b
    for b in "${BRANCHES[@]}"; do
      branch_args+=(--branch "$b")
    done
  else
    branch_args+=(--branch "$(git rev-parse --abbrev-ref HEAD)")
  fi
  maybe_run "python3 scripts/claims.py check --base $BASE_REF --head HEAD ${branch_args[*]}" \
    -- python3 "$SCRIPT_DIR/claims.py" check --base "$BASE_REF" --head HEAD "${branch_args[@]}"
}

# ── zero-regression baseline ─────────────────────────────────────────────────

gate_baseline_collect() {
  maybe_run "python3 scripts/test-baseline.py collect --results $RESULTS_DIR_ABS" \
    -- python3 "$SCRIPT_DIR/test-baseline.py" collect --results "$RESULTS_DIR_ABS"
}

gate_baseline_retry() {
  cfg_list gates.cargo_test_args
  local -a test_args=("${CFG_LIST[@]}")
  if [ "$DRY_RUN" = "1" ]; then
    echo "  python3 scripts/test-baseline.py retry-list --results $RESULTS_DIR_ABS --baseline $BASELINE_FILE"
    echo "  # for each <suite>TAB<test> listed: cargo test ${test_args[*]} -- --exact <test>, then re-collect"
    return 0
  fi
  local retry_file="$RESULTS_DIR_ABS/.retry-list.tsv"
  if ! python3 "$SCRIPT_DIR/test-baseline.py" retry-list --results "$RESULTS_DIR_ABS" \
    --baseline "$BASELINE_FILE" > "$retry_file"; then
    echo "baseline-retry: retry-list failed (malformed baseline or results) — see above" >&2
    return 1
  fi
  if [ -s "$retry_file" ]; then
    echo "retrying flaky_retry-listed failing tests once (workspace-wide, exact name match):"
    cat "$retry_file"
    # retry-list emits "<suite>\t<test>" (the raw suite id, not a crate
    # name guessed back out of it — a suite id can be an integration-test
    # binary stem). Filter workspace-wide by exact test name instead;
    # names are fully qualified, so this is unambiguous.
    cut -f2 "$retry_file" | sort -u | while IFS= read -r testname; do
      [ -n "$testname" ] || continue
      echo "  retry: cargo test ${test_args[*]} -- --exact '$testname'"
      cargo test "${test_args[@]}" -- --exact "$testname" 2>&1 | tee -a "$RESULTS_DIR_ABS/cargo-test.log" || true
    done
    # Re-collect so compare sees the re-run's outcome; a collect failure
    # here is a gate failure, not a silent "no retries happened".
    python3 "$SCRIPT_DIR/test-baseline.py" collect --results "$RESULTS_DIR_ABS" || return 1
  fi
  return 0
}

gate_baseline_compare() {
  local -a extra=()
  [ "$BOOTSTRAP" = "1" ] && extra+=(--bootstrap)
  maybe_run "python3 scripts/test-baseline.py compare --results $RESULTS_DIR_ABS --baseline $BASELINE_FILE --range $BASE_REF..HEAD ${extra[*]:-}" \
    -- python3 "$SCRIPT_DIR/test-baseline.py" compare --results "$RESULTS_DIR_ABS" \
       --baseline "$BASELINE_FILE" --range "$BASE_REF..HEAD" "${extra[@]:+${extra[@]}}"
}

# ── fuzz-corpus: seed-corpus replay, non-blocking, ONLY when the cargo-fuzz
#    binary and the pinned nightly are already installed — never installs
#    anything (`-runs=0`, no mutation).

gate_fuzz_corpus() {
  local dir nightly
  dir="$(cfg gates.fuzz.dir)"
  nightly="$(cfg gates.fuzz.nightly)"
  cfg_list gates.fuzz.targets
  local -a targets=("${CFG_LIST[@]:+${CFG_LIST[@]}}")
  if [ -z "$dir" ] || [ "${#targets[@]}" -eq 0 ]; then
    echo "skip: no [gates.fuzz] configured" >&2
    return 200
  fi
  if [ "$DRY_RUN" = "1" ]; then
    echo "  check: cargo +$nightly --version && cargo fuzz --version"
    local t
    for t in "${targets[@]}"; do
      echo "  (cd $dir && cargo +$nightly fuzz run $t corpus/$t -- -runs=0)"
    done
    return 0
  fi
  if ! cargo "+${nightly}" --version >/dev/null 2>&1; then
    echo "skip: pinned nightly ${nightly} not installed — this gate never installs a toolchain" >&2
    return 200
  fi
  if ! cargo fuzz --version >/dev/null 2>&1; then
    echo "skip: cargo-fuzz not installed — this gate never installs it" >&2
    return 200
  fi
  local ok=0 t
  for t in "${targets[@]}"; do
    ( cd "$ROOT/$dir" && cargo "+${nightly}" fuzz run "$t" "corpus/$t" -- -runs=0 ) || ok=1
  done
  return "$ok"
}

# ── run everything, in docs/DESIGN.md §5.2 order ─────────────────────────────

cfg_keys gates.pre
for name in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do
  run_gate "pre-$name" 1 gate_pre "$name"
done

if [ "$(cfg gates.rust)" = "true" ]; then
  if [ "$(cfg gates.cargo_fmt)" = "true" ]; then
    run_gate "cargo-fmt" 1 gate_cargo_fmt
  fi
  run_gate "cargo-clippy" 1 gate_cargo_clippy
  run_gate "cargo-test" 0 gate_cargo_test
fi

cfg_keys gates.frontend
for name in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do
  run_gate "frontend-$name" 1 gate_frontend_build "$name"
  blocking=0
  [ "$(cfg "gates.frontend.$name.test_blocking")" = "true" ] && blocking=1
  run_gate "frontend-$name-test" "$blocking" gate_frontend_test "$name"
done

cfg_keys gates.extra
for name in "${CFG_LIST[@]:+${CFG_LIST[@]}}"; do
  blocking=1
  [ "$(cfg "gates.extra.$name.blocking")" = "false" ] && blocking=0
  run_gate "extra-$name" "$blocking" gate_extra "$name"
done

[ "$(cfg gates.scripts_selftest)" = "true" ] && run_gate "gate-scripts-selftest" 1 gate_scripts_selftest
[ "$(cfg gates.standards)" = "true" ] && run_gate "standards-check" 1 gate_standards
[ "$(cfg gates.adr)" = "true" ] && run_gate "adr-check" 1 gate_adr_check
[ "$(cfg gates.invariants)" = "true" ] && run_gate "invariants-check" 1 gate_invariants
if [ "$(cfg gates.claims)" = "true" ]; then
  run_gate "claims-sync-registry" 1 gate_claims_sync_registry
  run_gate "claims-check" 1 gate_claims_check
fi
run_gate "baseline-collect" 1 gate_baseline_collect
run_gate "baseline-retry" 1 gate_baseline_retry
run_gate "fuzz-corpus" 0 gate_fuzz_corpus
run_gate "baseline-compare" 1 gate_baseline_compare

# ── gate-status.json + final table ────────────────────────────────────────────

write_gate_status_json() {
  local out="$RESULTS_DIR_ABS/gate-status.json"
  {
    echo "{"
    local i n=${#GATE_NAMES[@]}
    for ((i = 0; i < n; i++)); do
      local blocking=true
      [ "${GATE_BLOCKING[$i]}" = "0" ] && blocking=false
      printf '  "%s": {"status": "%s", "seconds": %s, "blocking": %s}' \
        "${GATE_NAMES[$i]}" "${GATE_STATUS[$i]}" "${GATE_SECONDS[$i]}" "$blocking"
      if [ $((i + 1)) -lt "$n" ]; then echo ","; else echo ""; fi
    done
    echo "}"
  } > "$out"
  echo "gate status written to $out"
}

if [ "$DRY_RUN" != "1" ]; then
  write_gate_status_json
fi

echo ""
echo "=== full-gate summary ==="
printf '%-32s %-6s %-8s %s\n' "gate" "status" "seconds" "note"
n=${#GATE_NAMES[@]}
for ((i = 0; i < n; i++)); do
  note=""
  [ "${GATE_BLOCKING[$i]}" = "0" ] && note="(non-blocking; see compare)"
  printf '%-32s %-6s %-8s %s\n' "${GATE_NAMES[$i]}" "${GATE_STATUS[$i]}" "${GATE_SECONDS[$i]}" "$note"
done

if [ "$DRY_RUN" = "1" ]; then
  echo ""
  echo "(--dry-run: nothing was actually executed)"
  exit 0
fi

if [ "$OVERALL_FAIL" = "1" ]; then
  echo ""
  echo "full-gate: FAIL"
  exit 1
fi

echo ""
echo "full-gate: OK"
exit 0
