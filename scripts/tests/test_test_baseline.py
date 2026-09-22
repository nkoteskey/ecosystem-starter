"""Unit tests for scripts/test-baseline.py (zero-regression baseline, docs/DESIGN.md §5.5).

Stdlib `unittest` only, no network. `record`/`compare` shell out to `git`
for the current commit and `Baseline-Drop:` trailers, so range-dependent
tests build a small real temporary git repo; everything else exercises the
parsers/compare logic directly against in-memory or tempdir fixtures.

Run:
    python3 -m unittest scripts/tests/test_test_baseline.py -v
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "test-baseline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("test_baseline_mod", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tb = _load_module()


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


class TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name).resolve()

    def tearDown(self) -> None:
        self._tmp.cleanup()


# ── cargo log parsing ─────────────────────────────────────────────────────


class TestParseCargoLog(unittest.TestCase):
    def test_unittests_header_and_test_lines(self):
        log = (
            "   Compiling example-core v0.1.0\n"
            "    Finished test [unoptimized + debuginfo] target(s) in 1.23s\n"
            "     Running unittests src/lib.rs (target/debug/deps/example_core-abc123ef)\n"
            "\n"
            "running 3 tests\n"
            "test tests::round_trip_json ... ok\n"
            "test tests::round_trip_binary ... ok\n"
            "test tests::flaky_thing ... FAILED\n"
            "\n"
            "test result: FAILED. 2 passed; 1 failed; 0 ignored\n"
        )
        suites = tb.parse_cargo_log(log)
        self.assertIn("cargo:example_core", suites)
        s = suites["cargo:example_core"]
        self.assertEqual(s["tests::round_trip_json"], "passed")
        self.assertEqual(s["tests::round_trip_binary"], "passed")
        self.assertEqual(s["tests::flaky_thing"], "failed")

    def test_ignored_with_reason(self):
        log = (
            "     Running unittests src/lib.rs (target/debug/deps/foo-deadbeef01)\n"
            "test slow::big_one ... ignored, slow test\n"
        )
        suites = tb.parse_cargo_log(log)
        self.assertEqual(suites["cargo:foo"]["slow::big_one"], "ignored")

    def test_integration_test_binary_header(self):
        log = (
            "     Running tests/vocabulary_consistency.rs "
            "(target/debug/deps/vocabulary_consistency-0123abcd)\n"
            "test all_terms_covered ... ok\n"
        )
        suites = tb.parse_cargo_log(log)
        self.assertIn("cargo:vocabulary_consistency", suites)
        self.assertEqual(suites["cargo:vocabulary_consistency"]["all_terms_covered"], "passed")

    def test_doc_tests_header(self):
        log = (
            "   Doc-tests example-core\n\nrunning 1 test\ntest src/lib.rs - foo (line 12) ... ok\n"
        )
        suites = tb.parse_cargo_log(log)
        self.assertIn("cargo:example-core:doc", suites)
        self.assertEqual(suites["cargo:example-core:doc"]["src/lib.rs - foo (line 12)"], "passed")

    def test_multiple_binaries_become_separate_suites(self):
        log = (
            "     Running unittests src/lib.rs (target/debug/deps/foo-aaaa1111)\n"
            "test a ... ok\n"
            "     Running unittests src/lib.rs (target/debug/deps/bar-bbbb2222)\n"
            "test b ... FAILED\n"
        )
        suites = tb.parse_cargo_log(log)
        self.assertEqual(suites["cargo:foo"]["a"], "passed")
        self.assertEqual(suites["cargo:bar"]["b"], "failed")

    def test_ansi_colored_output_still_parses(self):
        esc = "\x1b"
        log = (
            f"{esc}[0m{esc}[1m{esc}[32m     Running{esc}[0m unittests src/lib.rs "
            f"(target/debug/deps/example_core-deadbeef)\n"
            f"{esc}[0mtest{esc}[0m tests::round_trip {esc}[0m... {esc}[32mok{esc}[0m\n"
            f"test tests::broken_one {esc}[0m... {esc}[31mFAILED{esc}[0m\n"
        )
        suites = tb.parse_cargo_log(log)
        self.assertIn("cargo:example_core", suites)
        self.assertEqual(suites["cargo:example_core"]["tests::round_trip"], "passed")
        self.assertEqual(suites["cargo:example_core"]["tests::broken_one"], "failed")


class TestParseVitestJson(TempDirCase):
    def test_status_mapping_and_key_shape(self):
        base_dir = Path("/repo")
        data = {
            "testResults": [
                {
                    "name": "/repo/src/foo.test.ts",
                    "assertionResults": [
                        {"fullName": "foo > works", "status": "passed"},
                        {"fullName": "foo > broken", "status": "failed"},
                        {"fullName": "foo > later", "status": "skipped"},
                        {"fullName": "foo > todo", "status": "todo"},
                    ],
                }
            ]
        }
        out = tb.parse_vitest_json(data, base_dir)
        self.assertEqual(out["src/foo.test.ts::foo > works"], "passed")
        self.assertEqual(out["src/foo.test.ts::foo > broken"], "failed")
        self.assertEqual(out["src/foo.test.ts::foo > later"], "ignored")
        self.assertEqual(out["src/foo.test.ts::foo > todo"], "ignored")

    def test_relative_name_used_as_is(self):
        data = {
            "testResults": [
                {
                    "name": "src/bar.test.ts",
                    "assertionResults": [{"fullName": "x", "status": "passed"}],
                }
            ]
        }
        out = tb.parse_vitest_json(data, Path("/repo"))
        self.assertIn("src/bar.test.ts::x", out)

    def test_path_outside_base_dir_falls_back_to_repo_root_relative(self):
        # A test file path not under the expected base_dir must NOT
        # collapse to its bare basename (that would silently merge
        # same-named files from different packages into one key) — it
        # falls back to a path relative to the actual repo root.
        repo = self.tmp / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        abs_path = repo / "apps" / "mobile" / "example-app" / "src" / "foo.test.ts"
        data = {
            "testResults": [
                {"name": str(abs_path), "assertionResults": [{"fullName": "x", "status": "passed"}]}
            ]
        }
        old_cwd = os.getcwd()
        os.chdir(repo)
        try:
            out = tb.parse_vitest_json(data, Path("/completely/unrelated/base"))
        finally:
            os.chdir(old_cwd)
        self.assertIn("apps/mobile/example-app/src/foo.test.ts::x", out)
        self.assertNotIn("foo.test.ts::x", out)

    def test_undeterminable_repo_root_fails_loudly(self):
        original = tb._repo_root
        try:

            def _boom():
                raise RuntimeError("not a git repository")

            tb._repo_root = _boom
            data = {
                "testResults": [
                    {
                        "name": "/some/other/root/x.test.ts",
                        "assertionResults": [{"fullName": "y", "status": "passed"}],
                    }
                ]
            }
            with self.assertRaises(RuntimeError):
                tb.parse_vitest_json(data, Path("/unrelated/base"))
        finally:
            tb._repo_root = original


class TestCollectDir(TempDirCase):
    def test_collect_merges_all_recognized_inputs(self):
        (self.tmp / "cargo-test.log").write_text(
            "     Running unittests src/lib.rs (target/debug/deps/example_core-deadbeef)\n"
            "test a ... ok\n"
        )
        (self.tmp / "cargo-audit.log").write_text(
            "     Running unittests src/lib.rs (target/debug/deps/audit_crate-deadbeef)\n"
            "test b ... FAILED\n"
        )
        (self.tmp / "vitest-example-app.json").write_text(
            json.dumps(
                {
                    "testResults": [
                        {
                            "name": "src/x.test.ts",
                            "assertionResults": [{"fullName": "y", "status": "passed"}],
                        }
                    ]
                }
            )
        )
        (self.tmp / "standards.status").write_text("0\n")

        summary = tb.collect_dir(self.tmp, base_dir=Path("/repo"))
        self.assertIn("cargo:example_core", summary["suites"])
        self.assertEqual(summary["suites"]["cargo:audit_crate"]["failed"], ["b"])
        self.assertIn("vitest:example-app", summary["suites"])
        self.assertIn("standards", summary["suites"])
        self.assertEqual(summary["suites"]["standards"]["passed"], ["standards-check"])

    def test_missing_files_are_skipped_not_errors(self):
        summary = tb.collect_dir(self.tmp)
        self.assertEqual(summary["suites"], {})

    def test_every_vitest_json_becomes_its_own_suite(self):
        (self.tmp / "vitest-embed.json").write_text(
            json.dumps(
                {
                    "testResults": [
                        {
                            "name": "src/embed.test.ts",
                            "assertionResults": [{"fullName": "renders", "status": "passed"}],
                        }
                    ]
                }
            )
        )
        summary = tb.collect_dir(self.tmp, base_dir=Path("/repo"))
        self.assertIn("vitest:embed", summary["suites"])
        self.assertEqual(
            summary["suites"]["vitest:embed"]["passed"], ["src/embed.test.ts::renders"]
        )


class TestCmdRecord(TempDirCase):
    def test_refuses_empty_summary_without_allow_empty(self):
        out_path = self.tmp / "baseline.json"
        ns = _ns(results=str(self.tmp), out=str(out_path), train=None, allow_empty=False)
        rc = tb.cmd_record(ns)
        self.assertEqual(rc, 1)
        self.assertFalse(out_path.exists())

    def test_allow_empty_permits_it(self):
        out_path = self.tmp / "baseline.json"
        ns = _ns(results=str(self.tmp), out=str(out_path), train=None, allow_empty=True)
        rc = tb.cmd_record(ns)
        self.assertEqual(rc, 0)
        self.assertTrue(out_path.exists())
        data = json.loads(out_path.read_text())
        self.assertEqual(data["suites"], {})


# ── compare (R1-R4) ──────────────────────────────────────────────────────


class TestBaselineDropTrailerParsing(unittest.TestCase):
    def test_parses_suite_glob_reason(self):
        body = "Some commit\n\nBaseline-Drop: cargo:foo tests::old_thing removed in the rewrite\n"
        trailers = tb.parse_baseline_drop_trailers(body)
        self.assertEqual(trailers, [("cargo:foo", "tests::old_thing", "removed in the rewrite")])

    def test_parses_glob_pattern(self):
        body = "Baseline-Drop: vitest:app-a foo.test.ts::* whole file deleted\n"
        trailers = tb.parse_baseline_drop_trailers(body)
        self.assertEqual(len(trailers), 1)
        suite, glob, reason = trailers[0]
        self.assertEqual(suite, "vitest:app-a")
        self.assertEqual(glob, "foo.test.ts::*")
        self.assertEqual(reason, "whole file deleted")


class TestCompareLogic(TempDirCase):
    def _write_results(self, suites: dict) -> None:
        lines = []
        for suite_id, tests in suites.items():
            assert suite_id.startswith("cargo:")
            crate = suite_id[len("cargo:") :]
            lines.append(f"     Running unittests src/lib.rs (target/debug/deps/{crate}-cafefeed)")
            for name, status in tests.items():
                if status == "passed":
                    lines.append(f"test {name} ... ok")
                elif status == "failed":
                    lines.append(f"test {name} ... FAILED")
                elif status == "ignored":
                    lines.append(f"test {name} ... ignored")
        (self.tmp / "cargo-test.log").write_text("\n".join(lines) + "\n")

    def _baseline(self, suites: dict, flaky_retry=None) -> Path:
        # flaky_retry: (suite, test) tuples, or raw entries (dicts).
        entries = [e if isinstance(e, dict) else _flaky(e[0], e[1]) for e in flaky_retry or []]
        return self._write_baseline(suites, entries)

    def _write_baseline(self, suites: dict, flaky_retry: list) -> Path:
        path = self.tmp / "baseline.json"
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "recorded_at": "2026-01-01T00:00:00Z",
                    "commit": "abc123",
                    "train": None,
                    "flaky_retry": flaky_retry,
                    "suites": suites,
                }
            )
        )
        return path

    def test_empty_baseline_without_bootstrap_blocks_on_new_fail(self):
        self._write_results({"cargo:foo": {"tests::x": "failed"}})
        baseline_path = self._baseline({})
        ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None, bootstrap=False)
        self.assertEqual(tb.cmd_compare(ns), 1)

    def test_empty_baseline_with_bootstrap_passes_and_reports_informational(self):
        self._write_results({"cargo:foo": {"tests::x": "failed"}})
        baseline_path = self._baseline({})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None, bootstrap=True)
            rc = tb.cmd_compare(ns)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("BOOTSTRAP MODE", out)
        self.assertIn("cargo:foo", out)
        self.assertIn("tests::x", out)

    def test_bootstrap_refused_when_baseline_already_has_suites(self):
        self._write_results({"cargo:foo": {"tests::x": "passed"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x"], "failed": [], "ignored": []}}
        )
        ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None, bootstrap=True)
        self.assertEqual(tb.cmd_compare(ns), 1)

    def test_r1_passed_test_now_failed_is_a_regression(self):
        self._write_results({"cargo:foo": {"tests::x": "failed"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x"], "failed": [], "ignored": []}}
        )
        ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
        self.assertEqual(tb.cmd_compare(ns), 1)

    def test_r2_passed_test_absent_without_trailer_is_a_regression(self):
        self._write_results({"cargo:foo": {"tests::other": "passed"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x", "tests::other"], "failed": [], "ignored": []}}
        )
        ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
        self.assertEqual(tb.cmd_compare(ns), 1)

    def test_r2_exempted_by_baseline_drop_trailer(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.email", "test-fixture")
        _git(repo, "config", "user.name", "T")
        (repo / "f.txt").write_text("1\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "base")
        base = _git(repo, "rev-parse", "HEAD").strip()
        (repo / "f.txt").write_text("2\n")
        _git(repo, "add", "-A")
        _git(
            repo,
            "commit",
            "-q",
            "-m",
            "drop it\n\nBaseline-Drop: cargo:foo tests::x removed, superseded by tests::other",
        )

        self._write_results({"cargo:foo": {"tests::other": "passed"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x", "tests::other"], "failed": [], "ignored": []}}
        )

        old_cwd = os.getcwd()
        os.chdir(repo)
        try:
            ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=f"{base}..HEAD")
            rc = tb.cmd_compare(ns)
        finally:
            os.chdir(old_cwd)
        self.assertEqual(rc, 0)

    def test_r3_passed_now_ignored_is_a_regression(self):
        self._write_results({"cargo:foo": {"tests::x": "ignored"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x"], "failed": [], "ignored": []}}
        )
        ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
        self.assertEqual(tb.cmd_compare(ns), 1)

    def test_r4_whole_suite_missing_is_a_regression(self):
        self._write_results({})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x"], "failed": [], "ignored": []}}
        )
        ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
        self.assertEqual(tb.cmd_compare(ns), 1)

    def test_new_failing_test_still_blocks_as_new_fail(self):
        self._write_results({"cargo:foo": {"tests::brand_new": "failed"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x"], "failed": [], "ignored": []}}
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
            rc = tb.cmd_compare(ns)
        self.assertEqual(rc, 1)
        self.assertIn("NEW-FAIL", buf.getvalue())
        self.assertIn("tests::brand_new", buf.getvalue())

    def test_suite_with_no_baseline_counterpart_is_also_new_fail(self):
        self._write_results({"cargo:brand-new-crate": {"tests::a": "failed"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x"], "failed": [], "ignored": []}}
        )
        ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
        self.assertEqual(tb.cmd_compare(ns), 1)

    def test_known_failing_at_baseline_does_not_block_but_is_reported_informational(self):
        self._write_results({"cargo:foo": {"tests::known_bad": "failed"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": [], "failed": ["tests::known_bad"], "ignored": []}}
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
            rc = tb.cmd_compare(ns)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("tests::known_bad", out)
        self.assertIn("not blocking", out)
        self.assertNotIn("NEW-FAIL", out)

    def test_known_ignored_at_baseline_still_failing_does_not_block(self):
        self._write_results({"cargo:foo": {"tests::was_ignored": "failed"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": [], "failed": [], "ignored": ["tests::was_ignored"]}}
        )
        ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
        self.assertEqual(tb.cmd_compare(ns), 0)

    def test_flaky_listed_test_that_still_fails_is_r1(self):
        # A listing buys one re-run by the retry gate, nothing more: if the
        # results still show the test failing, it is a regression.
        self._write_results({"cargo:foo": {"tests::x": "failed"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x"], "failed": [], "ignored": []}},
            flaky_retry=[("cargo:foo", "tests::x")],
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
            rc = tb.cmd_compare(ns)
        self.assertEqual(rc, 1)
        self.assertIn("R1", buf.getvalue())
        self.assertIn("no passing retry observed", buf.getvalue())
        self.assertNotIn("0 regressions", buf.getvalue())

    def test_flaky_listed_test_that_passed_its_rerun_is_fine(self):
        # The retry gate appends the re-run to the same log; the last
        # observation wins, so a passing re-run clears the failure.
        (self.tmp / "cargo-test.log").write_text(
            "     Running unittests src/lib.rs (target/debug/deps/foo-cafefeed)\n"
            "test tests::x ... FAILED\n"
            "     Running unittests src/lib.rs (target/debug/deps/foo-cafefeed)\n"
            "test tests::x ... ok\n"
        )
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x"], "failed": [], "ignored": []}},
            flaky_retry=[("cargo:foo", "tests::x")],
        )
        ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
        self.assertEqual(tb.cmd_compare(ns), 0)

    def test_flaky_entry_requires_reason_and_expiry(self):
        self._write_results({"cargo:foo": {"tests::x": "passed"}})
        for bad in (
            "cargo:foo\ttests::x",
            {"suite": "cargo:foo", "test": "tests::x"},
            {"suite": "cargo:foo", "test": "tests::x", "reason": "", "expires": "2099-01-01"},
            {"suite": "cargo:foo", "test": "tests::x", "reason": "r", "expires": "soon"},
            {"suite": "cargo:foo", "test": "tests::x", "reason": "r", "expires": "2099-13-45"},
        ):
            baseline_path = self._write_baseline(
                {"cargo:foo": {"passed": ["tests::x"], "failed": [], "ignored": []}}, [bad]
            )
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = tb.main(
                    ["compare", "--results", str(self.tmp), "--baseline", str(baseline_path)]
                )
            self.assertEqual(rc, 1, bad)
            self.assertIn("flaky_retry", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())

    def test_expired_flaky_entry_is_ignored_with_a_warning(self):
        self._write_results({"cargo:foo": {"tests::x": "failed"}})
        baseline_path = self._baseline(
            {"cargo:foo": {"passed": ["tests::x"], "failed": [], "ignored": []}},
            flaky_retry=[_flaky("cargo:foo", "tests::x", expires="2000-01-01")],
        )
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            ns = _ns(results=str(self.tmp), baseline=str(baseline_path), range=None)
            rc = tb.cmd_compare(ns)
        self.assertEqual(rc, 1)
        self.assertIn("expired", err.getvalue())

    def test_malformed_baseline_json_fails_with_file_name_no_traceback(self):
        self._write_results({"cargo:foo": {"tests::x": "passed"}})
        baseline_path = self.tmp / "baseline.json"
        baseline_path.write_text("{ not json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = tb.main(["compare", "--results", str(self.tmp), "--baseline", str(baseline_path)])
        self.assertEqual(rc, 1)
        self.assertIn(str(baseline_path), err.getvalue())
        self.assertIn("not valid JSON", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_malformed_results_json_fails_with_file_name_no_traceback(self):
        (self.tmp / "vitest-app-a.json").write_text("[1, 2,")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = tb.main(["collect", "--results", str(self.tmp)])
        self.assertEqual(rc, 1)
        self.assertIn("vitest-app-a.json", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


class TestRetryList(TempDirCase):
    def test_only_prefixed_suites_and_flaky_listed_failures_are_emitted(self):
        (self.tmp / "cargo-test.log").write_text(
            "     Running unittests src/lib.rs (target/debug/deps/example_contracts-aaaa1111)\n"
            "test tests::flaky_one ... FAILED\n"
            "test tests::not_flaky ... FAILED\n"
        )
        (self.tmp / "vitest-app-a.json").write_text(
            json.dumps(
                {
                    "testResults": [
                        {
                            "name": "src/x.test.ts",
                            "assertionResults": [{"fullName": "z", "status": "failed"}],
                        }
                    ]
                }
            )
        )
        baseline_path = self.tmp / "baseline.json"
        baseline_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "flaky_retry": [_flaky("cargo:example_contracts", "tests::flaky_one")],
                    "suites": {},
                }
            )
        )

        buf = io.StringIO()
        ns = _ns(results=str(self.tmp), baseline=str(baseline_path), suite_prefix="cargo:")
        with contextlib.redirect_stdout(buf):
            rc = tb.cmd_retry_list(ns)
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("cargo:example_contracts\ttests::flaky_one", out)
        self.assertNotIn("not_flaky", out)
        self.assertNotIn("::z", out)  # vitest suites are never retried


def _flaky(suite: str, test: str, expires: str = "2099-12-31") -> dict:
    return {"suite": suite, "test": test, "reason": "fixture", "expires": expires}


def _ns(**kwargs):
    class NS:
        pass

    ns = NS()
    for k, v in kwargs.items():
        setattr(ns, k, v)
    return ns


if __name__ == "__main__":
    unittest.main()
