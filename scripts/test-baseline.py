#!/usr/bin/env python3
"""test-baseline.py — the zero-regression baseline machinery (docs/DESIGN.md §5.5).

Subcommands:
    collect     --results <dir>
    record      --results <dir> --out <baseline.json> [--train <id>]
    compare     --results <dir> --baseline <baseline.json> [--range <base>..<head>]
    retry-list  --results <dir> --baseline <baseline.json>

`collect` normalizes raw test-runner output living in <dir> into
<dir>/summary.json = {"suites": {id: {"passed":[...], "failed":[...],
"ignored":[...]}}} — see each parser's docstring for the exact input
shapes. `record` and `compare` both re-derive that summary themselves (so
a stale summary.json never silently drives a decision) then either freeze
it as the new baseline or diff it against the existing one per rules
R1-R4.

Recognized inputs in <dir>:
    cargo-test.log        `cargo test` output           -> suites `cargo:<binary>`
    cargo-<name>.log      any other cargo test output   -> suites `cargo:<binary>`
    vitest-<name>.json    vitest `--reporter=json`      -> suite  `vitest:<name>`
    standards.status      exit code of standards-check  -> suite  `standards`

Stdlib only. No network calls. `record`/`compare`/`retry-list` shell out to
`git` for the current commit / commit-message trailers; `collect` alone
never touches git.

`flaky_retry` entries are `"<suite-id>\\t<test-name>"` — keyed by the raw
suite id, not a crate name guessed back out of it, since a suite id like
`cargo:some_integration_test` is a test-binary stem, not necessarily a
crate name. `retry-list` emits the same shape; `scripts/full-gate.sh`
re-runs each with a workspace-wide `cargo test ... -- --exact <test>`.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mergegate_config

# ── raw-output parsers ───────────────────────────────────────────────────────

_CARGO_UNIT_HEADER_RE = re.compile(
    r"^\s*Running\s+(?:unittests\s+\S+|tests/\S+)\s+\(.*?[\\/]deps[\\/]([A-Za-z0-9_]+)-[0-9a-f]+\)\s*$"
)
_CARGO_DOC_HEADER_RE = re.compile(r"^\s*Doc-tests\s+(.+?)\s*$")
_CARGO_TEST_LINE_RE = re.compile(r"^test\s+(.+?)\s+\.\.\.\s+(.+?)\s*$")

# A CI runner with CARGO_TERM_COLOR=always wraps `Running`/`test ... ok`
# lines in ANSI SGR escapes; unstripped, none of the regexes above match
# and the parser silently yields zero suites. Strip before parsing.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def parse_cargo_log(text: str) -> Dict[str, Dict[str, str]]:
    """Parse `cargo test` stdout into {suite_id: {test_name: status}}.

    suite_id is `cargo:<binary>` (the binary stem under a
    `target/.../deps/<binary>-<hash>` path in a `Running unittests ...` /
    `Running tests/...` header — kept exactly as printed) or
    `cargo:<name>:doc` (from a `Doc-tests <name>` header).
    """
    text = _strip_ansi(text)
    suites: Dict[str, Dict[str, str]] = {}
    current: Optional[str] = None
    for line in text.splitlines():
        m = _CARGO_UNIT_HEADER_RE.match(line)
        if m:
            current = f"cargo:{m.group(1)}"
            suites.setdefault(current, {})
            continue
        m = _CARGO_DOC_HEADER_RE.match(line)
        if m:
            current = f"cargo:{m.group(1)}:doc"
            suites.setdefault(current, {})
            continue
        m = _CARGO_TEST_LINE_RE.match(line)
        if m and current is not None:
            name, status = m.group(1), m.group(2)
            if status == "ok":
                suites[current][name] = "passed"
            elif status == "FAILED":
                suites[current][name] = "failed"
            elif status.startswith("ignored"):
                suites[current][name] = "ignored"
            # any other status token (e.g. a bench line) is skipped
    return suites


def _rel_path(name: str, base_dir: Path) -> str:
    p = Path(name)
    if not p.is_absolute():
        return name
    try:
        return str(p.relative_to(base_dir))
    except ValueError:
        pass
    # Not under the expected base dir (vitest ran with a different cwd).
    # Collapsing to the bare basename would silently merge distinct
    # same-named test files from different packages into one key — fall
    # back to a path relative to the repo root instead, and fail loudly if
    # the repo root itself cannot be determined.
    root = _repo_root()
    try:
        return str(p.relative_to(root))
    except ValueError:
        return str(p)


def parse_vitest_json(data: dict, base_dir: Path) -> Dict[str, str]:
    """Parse a vitest `--reporter=json` document into {test_name: status}.

    test_name is `<file relative to base_dir>::<assertion fullName>`.
    vitest statuses passed/failed map 1:1; skipped/pending/todo all map to
    "ignored" (this schema only has three buckets).
    """
    out: Dict[str, str] = {}
    for tr in data.get("testResults", []) or []:
        rel = _rel_path(tr.get("name", ""), base_dir)
        for ar in tr.get("assertionResults", []) or []:
            full_name = ar.get("fullName", "")
            status = ar.get("status", "")
            key = f"{rel}::{full_name}"
            if status == "passed":
                out[key] = "passed"
            elif status == "failed":
                out[key] = "failed"
            else:
                out[key] = "ignored"
    return out


def parse_standards_status(text: str) -> Dict[str, str]:
    val = _strip_ansi(text).strip()
    return {"standards-check": "passed" if val == "0" else "failed"}


def _merge_suites(dst: Dict[str, Dict[str, str]], src: Dict[str, Dict[str, str]]) -> None:
    for suite_id, tests in src.items():
        dst.setdefault(suite_id, {}).update(tests)


def collect_dir(results_dir: Path, base_dir: Optional[Path] = None) -> dict:
    """Read every recognized raw-output file in results_dir and return the
    summary dict (does not write it — callers decide where/whether to)."""
    base_dir = base_dir or Path.cwd()
    suites: Dict[str, Dict[str, str]] = {}

    for cargo_log in sorted(results_dir.glob("cargo-*.log")):
        _merge_suites(suites, parse_cargo_log(cargo_log.read_text()))

    for f in sorted(results_dir.glob("vitest-*.json")):
        name = f.name[len("vitest-") : -len(".json")]
        if not name:
            continue
        data = json.loads(f.read_text())
        suites.setdefault(f"vitest:{name}", {}).update(parse_vitest_json(data, base_dir))

    standards_f = results_dir / "standards.status"
    if standards_f.exists():
        suites.setdefault("standards", {}).update(parse_standards_status(standards_f.read_text()))

    out_suites: Dict[str, Dict[str, List[str]]] = {}
    for suite_id, tests in suites.items():
        out_suites[suite_id] = {
            "passed": sorted(n for n, s in tests.items() if s == "passed"),
            "failed": sorted(n for n, s in tests.items() if s == "failed"),
            "ignored": sorted(n for n, s in tests.items() if s == "ignored"),
        }
    return {"suites": out_suites}


# ── git plumbing ─────────────────────────────────────────────────────────────


def _run(cmd: List[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({' '.join(cmd)}): {proc.stderr}")
    return proc.stdout


def _git_head_commit() -> Optional[str]:
    try:
        return _run(["git", "rev-parse", "HEAD"]).strip()
    except RuntimeError:
        return None


def _repo_root() -> Path:
    """Used by `_rel_path`'s fallback. Raises RuntimeError (via _run) if
    this is not inside a git working tree — callers let that propagate
    rather than silently falling back to a basename."""
    return Path(_run(["git", "rev-parse", "--show-toplevel"]).strip())


_BASELINE_DROP_RE = re.compile(r"^Baseline-Drop:\s*(\S+)\s+(\S+)\s+(.+?)\s*$", re.M)


def parse_baseline_drop_trailers(commit_body_text: str) -> List[Tuple[str, str, str]]:
    return [
        (m.group(1), m.group(2), m.group(3)) for m in _BASELINE_DROP_RE.finditer(commit_body_text)
    ]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _retry_suite_prefix() -> str:
    try:
        return str(mergegate_config.load_config()["baseline"]["retry_suite_prefix"])
    except mergegate_config.ConfigError:
        return str(mergegate_config.DEFAULTS["baseline"]["retry_suite_prefix"])


# ── subcommands ──────────────────────────────────────────────────────────────


def cmd_collect(args: argparse.Namespace) -> int:
    results_dir = Path(args.results)
    summary = collect_dir(results_dir)
    out_path = results_dir / "summary.json"
    out_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    n_suites = len(summary["suites"])
    n_tests = sum(
        len(s["passed"]) + len(s["failed"]) + len(s["ignored"]) for s in summary["suites"].values()
    )
    print(
        f"test-baseline collect: {n_suites} suite(s), {n_tests} test(s) -> {out_path}",
        file=sys.stderr,
    )
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    results_dir = Path(args.results)
    summary = collect_dir(results_dir)

    if not summary["suites"] and not getattr(args, "allow_empty", False):
        print(
            "test-baseline record: error: collected 0 suites — refusing to record an "
            "empty baseline (this would erase every existing protection). Pass "
            "--allow-empty if this is deliberate (e.g. a brand new, test-free "
            "workspace).",
            file=sys.stderr,
        )
        return 1

    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    out_path = Path(args.out)
    existing_flaky: List[str] = []
    if out_path.exists():
        try:
            old = json.loads(out_path.read_text())
            existing_flaky = old.get("flaky_retry", [])
        except (json.JSONDecodeError, OSError):
            existing_flaky = []

    baseline = {
        "schema": 1,
        "recorded_at": _utc_now_iso(),
        "commit": _git_head_commit(),
        "train": args.train,
        "flaky_retry": existing_flaky,
        "suites": summary["suites"],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n")
    print(
        f"test-baseline record: {len(baseline['suites'])} suite(s) -> {out_path} "
        f"(commit={baseline['commit']}, train={baseline['train']})"
    )
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    results_dir = Path(args.results)
    summary = collect_dir(results_dir)
    current_suites = summary["suites"]

    baseline_path = Path(args.baseline)
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text())
    else:
        baseline = {"suites": {}, "flaky_retry": []}

    baseline_suites: dict = baseline.get("suites", {}) or {}
    flaky_retry = set(baseline.get("flaky_retry", []) or [])
    bootstrap = bool(getattr(args, "bootstrap", False))

    if bootstrap and baseline_suites:
        print(
            "test-baseline compare: error: --bootstrap given but the baseline already "
            "has recorded suites — refusing (bootstrap is only for an empty baseline; "
            "run compare without --bootstrap so R1-R4 actually protect it)",
            file=sys.stderr,
        )
        return 1

    if bootstrap:
        print("=" * 70)
        print("BOOTSTRAP MODE — baseline is empty. Nothing is enforced this run.")
        print("Every currently-failing test below will become known-failing the")
        print("next time `record` runs (protected from then on).")
        print("=" * 70)
        informational = [
            (suite_id, name)
            for suite_id, cur in current_suites.items()
            for name in cur.get("failed", [])
        ]
        if informational:
            print(f"{len(informational)} currently-failing test(s):")
            for suite_id, name in informational:
                print(f"  {suite_id}  {name}")
        else:
            print("(no currently-failing tests)")
        print("\ncompare: OK (bootstrap mode — 0 blocking regressions)")
        return 0

    if not baseline_suites:
        print(
            "test-baseline compare: baseline has no recorded suites and --bootstrap "
            "was not given — every currently-failing test blocks as NEW-FAIL (a red "
            "tree cannot silently become the first baseline; pass --bootstrap to "
            "record it as the known-failing starting point instead)",
            file=sys.stderr,
        )

    trailers: List[Tuple[str, str, str]] = []
    if args.range:
        trailers = parse_baseline_drop_trailers(_run(["git", "log", "--format=%B", args.range]))

    def dropped(suite: str, name: str) -> bool:
        return any(s == suite and fnmatch.fnmatch(name, glob) for (s, glob, _r) in trailers)

    regressions: List[dict] = []
    retries: List[dict] = []
    informational: List[dict] = []

    for suite_id, bsuite in baseline_suites.items():
        if suite_id not in current_suites:
            if not dropped(suite_id, "*"):
                regressions.append(
                    {
                        "rule": "R4",
                        "suite": suite_id,
                        "test": "*",
                        "note": "suite missing from this run",
                    }
                )
            continue

        cur = current_suites[suite_id]
        cur_passed = set(cur.get("passed", []))
        cur_failed = set(cur.get("failed", []))
        cur_ignored = set(cur.get("ignored", []))

        for name in bsuite.get("passed", []):
            if name in cur_passed:
                continue
            if name in cur_failed:
                if f"{suite_id}\t{name}" in flaky_retry:
                    retries.append(
                        {
                            "suite": suite_id,
                            "test": name,
                            "note": "failed; flaky_retry-listed, needs retry",
                        }
                    )
                else:
                    regressions.append(
                        {
                            "rule": "R1",
                            "suite": suite_id,
                            "test": name,
                            "note": "passed at baseline, now FAILED",
                        }
                    )
                continue
            if name in cur_ignored:
                if not dropped(suite_id, name):
                    regressions.append(
                        {
                            "rule": "R3",
                            "suite": suite_id,
                            "test": name,
                            "note": "passed at baseline, now ignored/skipped",
                        }
                    )
                continue
            if not dropped(suite_id, name):
                regressions.append(
                    {
                        "rule": "R2",
                        "suite": suite_id,
                        "test": name,
                        "note": "passed at baseline, now absent (deleted/renamed)",
                    }
                )

    # Failures not already handled above as R1. Three cases:
    #   - known-failing/ignored at baseline, still failing now: informational
    #     only, never blocks ("known-failing tests at baseline do not block
    #     until they pass once").
    #   - no baseline entry at all (brand new test, or a suite with no
    #     baseline counterpart): a plain failure, tagged NEW-FAIL, blocks —
    #     unless it is flaky_retry-listed, in which case it is a retry.
    for suite_id, cur in current_suites.items():
        bsuite = baseline_suites.get(suite_id, {})
        baseline_passed = set(bsuite.get("passed", []))
        baseline_known_nonpassing = set(bsuite.get("failed", [])) | set(bsuite.get("ignored", []))
        for name in cur.get("failed", []):
            if name in baseline_passed:
                continue
            if name in baseline_known_nonpassing:
                informational.append(
                    {
                        "suite": suite_id,
                        "test": name,
                        "note": "known-failing at baseline, still failing (not a regression)",
                    }
                )
                continue
            if f"{suite_id}\t{name}" in flaky_retry:
                retries.append(
                    {
                        "suite": suite_id,
                        "test": name,
                        "note": "new failing test, flaky_retry-listed, needs retry",
                    }
                )
            else:
                regressions.append(
                    {
                        "rule": "NEW-FAIL",
                        "suite": suite_id,
                        "test": name,
                        "note": "failing test with no baseline-passed entry",
                    }
                )

    if informational:
        n_info = len(informational)
        print(f"({n_info} known-failing-at-baseline test(s), still failing, not blocking:)")
        for i in informational:
            print(f"  {i['suite']}  {i['test']}  -- {i['note']}")

    if regressions:
        print("test-baseline compare: REGRESSIONS FOUND")
        print(f"{'rule':<12} {'suite':<28} test")
        for r in regressions:
            print(f"{r['rule']:<12} {r['suite']:<28} {r['test']}  -- {r['note']}")
        if retries:
            n_retry = len(retries)
            print(
                f"\n(also {n_retry} test(s) reported as needing retry — not counted as regressions)"
            )
        return 1

    print(
        f"compare: OK (0 regressions, {len(retries)} needing retry, "
        f"{len(informational)} known-failing informational, "
        f"{len(baseline_suites)} baseline suite(s) checked)"
    )
    return 0


def cmd_retry_list(args: argparse.Namespace) -> int:
    results_dir = Path(args.results)
    summary = collect_dir(results_dir)
    current_suites = summary["suites"]

    baseline_path = Path(args.baseline)
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else {}
    flaky_retry = set(baseline.get("flaky_retry", []) or [])
    prefix = getattr(args, "suite_prefix", None) or _retry_suite_prefix()

    # Emit the raw "<suite>\t<test>" key. The caller (scripts/full-gate.sh)
    # re-runs each by exact test name workspace-wide.
    count = 0
    for suite_id, cur in sorted(current_suites.items()):
        if not suite_id.startswith(prefix):
            continue  # only suites under the configured prefix get the retry-once treatment
        for name in sorted(cur.get("failed", [])):
            key = f"{suite_id}\t{name}"
            if key in flaky_retry:
                print(key)
                count += 1
    print(f"retry-list: {count} test(s) to retry", file=sys.stderr)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--results", required=True)
    p_collect.set_defaults(func=cmd_collect)

    p_record = sub.add_parser("record")
    p_record.add_argument("--results", required=True)
    p_record.add_argument("--out", required=True)
    p_record.add_argument("--train", default=None)
    p_record.add_argument(
        "--allow-empty",
        action="store_true",
        help="allow recording a baseline with 0 collected suites (without this, "
        "`record` refuses rather than silently erasing every existing protection)",
    )
    p_record.set_defaults(func=cmd_record)

    p_compare = sub.add_parser("compare")
    p_compare.add_argument("--results", required=True)
    p_compare.add_argument("--baseline", required=True)
    p_compare.add_argument("--range", default=None)
    p_compare.add_argument(
        "--bootstrap",
        action="store_true",
        help="allowed ONLY when the baseline has no recorded suites: report every "
        "currently-failing test as informational (it becomes known-failing on the "
        "next `record`) and always exit 0",
    )
    p_compare.set_defaults(func=cmd_compare)

    p_retry = sub.add_parser("retry-list")
    p_retry.add_argument("--results", required=True)
    p_retry.add_argument("--baseline", required=True)
    p_retry.add_argument(
        "--suite-prefix", default=None, help="override baseline.retry_suite_prefix"
    )
    p_retry.set_defaults(func=cmd_retry_list)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"test-baseline: error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
