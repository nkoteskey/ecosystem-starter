"""Unit tests for scripts/claims.py (shared-service claim protocol, docs/DESIGN.md §4).

Stdlib `unittest` only, no network. Two test styles:

- Pure-function tests import claims.py directly (glob matching, TOML
  parsing/serialization, registry derivation from a fake metadata dict).
  These never touch git, so importing the module (which resolves its ROOT
  via `git rev-parse --show-toplevel` against THIS process's cwd) is safe.

- Protocol tests invoke `python3 scripts/claims.py ...` as a subprocess with
  cwd set to a throwaway git clone of a throwaway bare "origin" created in a
  tempdir. This is deliberate: claims.py resolves its own ROOT once, at
  import/process start, from the process's cwd — so exercising claim/renew/
  release/list/check against a fake repo requires a fresh subprocess per
  scenario.

Run:
    python3 -m unittest discover -s scripts/tests -p 'test_claims.py' -v
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CLAIMS_PY = Path(__file__).resolve().parent.parent / "claims.py"
HOOKS_DIR = Path(__file__).resolve().parent.parent / "git-hooks"

APPS = ["app-a", "app-b", "app-c"]
CONFIG_TEXT = 'schema = 1\n[claims]\napp_crates = ["app-a", "app-b", "app-c"]\n'


def _load_module():
    spec = importlib.util.spec_from_file_location("claims_module", CLAIMS_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


claims = _load_module()


def run_claims(cwd, *args, env=None):
    full_env = dict(os.environ)
    full_env["MERGE_GATE_SESSION"] = "test-session"
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, str(CLAIMS_PY), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=full_env,
    )


def git(cwd, *args, check=True):
    res = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr}")
    return res


def commit_file(cwd, relpath, content, message):
    p = Path(cwd) / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    git(cwd, "add", relpath)
    git(cwd, "commit", "-q", "-m", message)


def rev_parse(cwd, ref):
    return git(cwd, "rev-parse", ref).stdout.strip()


# --------------------------------------------------------------------------
# Pure-function tests
# --------------------------------------------------------------------------


class GlobMatchingTests(unittest.TestCase):
    def test_trailing_double_star_matches_nested_paths(self):
        self.assertTrue(claims.glob_matches("crates/identity/**", "crates/identity/src/lib.rs"))
        self.assertTrue(claims.glob_matches("crates/identity/**", "crates/identity/Cargo.toml"))
        self.assertFalse(claims.glob_matches("crates/identity/**", "crates/other-crate/src/lib.rs"))

    def test_leading_double_star_slash_matches_any_depth_including_zero(self):
        self.assertTrue(claims.glob_matches("**/package-lock.json", "package-lock.json"))
        self.assertTrue(claims.glob_matches("**/package-lock.json", "apps/app-a/package-lock.json"))
        self.assertFalse(
            claims.glob_matches("**/package-lock.json", "apps/app-a/package-lock.json.bak")
        )

    def test_github_star_star(self):
        self.assertTrue(claims.glob_matches(".github/**", ".github/workflows/ci.yml"))
        self.assertFalse(claims.glob_matches(".github/**", "scripts/ci.yml"))

    def test_exact_literal_path(self):
        self.assertTrue(claims.glob_matches("Cargo.lock", "Cargo.lock"))
        self.assertFalse(claims.glob_matches("Cargo.lock", "Cargo.toml"))

    def test_single_star_does_not_cross_separators(self):
        self.assertTrue(claims.glob_matches("apps/*/package.json", "apps/app-a/package.json"))
        self.assertFalse(claims.glob_matches("apps/*/package.json", "apps/app-a/sub/package.json"))


class FailureClassificationTests(unittest.TestCase):
    """`claim` must never exit 0 for a claim that was not published, and must
    tell an authentication refusal apart from being offline. In-process:
    the push and probe helpers are monkeypatched, nothing touches a remote."""

    def setUp(self):
        for name in ("mirror_write", "build_orphan_commit", "load_registry", "current_branch"):
            patch = mock.patch.object(claims, name)
            patch.start()
            self.addCleanup(patch.stop)
        claims.build_orphan_commit.return_value = "deadbeef"
        claims.load_registry.return_value = {"svc": {}}
        claims.current_branch.return_value = "wt/mine"
        valid_branch = mock.patch.object(claims, "valid_branch_name", return_value=True)
        valid_branch.start()
        self.addCleanup(valid_branch.stop)

    @staticmethod
    def _args(**overrides):
        base = dict(
            service="svc",
            intent="test",
            ttl_hours=8.0,
            branch=None,
            session="s",
            takeover=False,
            allow_offline=False,
        )
        base.update(overrides)
        return argparse.Namespace(**base)

    def _claim_with_push_error(self, err, reachable=None, **overrides):
        with (
            mock.patch.object(claims, "push_create", return_value=(False, err)),
            mock.patch.object(claims, "remote_reachable", return_value=reachable),
            mock.patch.object(claims, "ls_remote_ref", return_value=None),
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                rc = claims.cmd_claim(self._args(**overrides))
        return rc, stderr.getvalue()

    def test_auth_failures_are_fatal_and_record_nothing(self):
        for err in (
            "remote: Permission denied (publickey).",
            "fatal: unable to access 'https://host/': The requested URL returned error: 403",
            "fatal: Authentication failed for 'https://host/'",
            "ERROR: Repository not found.\nfatal: Could not read from remote repository.",
            "remote: ! [remote rejected] deadbeef -> refs/claims/svc (pre-receive hook declined)",
            "fatal: could not read Username for 'https://host': terminal prompts disabled",
        ):
            rc, err_out = self._claim_with_push_error(err, allow_offline=True)
            self.assertEqual(rc, 1, err)
            self.assertIn("FATAL", err_out)
            claims.mirror_write.assert_not_called()

    def test_git_boilerplate_about_access_rights_is_not_treated_as_auth(self):
        # git appends this sentence to every could-not-read-from-remote
        # failure; an unreachable remote must still take the offline path.
        rc, err_out = self._claim_with_push_error(
            "fatal: 'x' does not appear to be a git repository\n"
            "fatal: Could not read from remote repository.\n\n"
            "Please make sure you have the correct access rights\nand the repository exists.",
            reachable=False,
            allow_offline=True,
        )
        self.assertEqual(rc, 3, err_out)
        self.assertNotIn("FATAL", err_out)

    def test_network_error_with_reachable_remote_is_an_error_not_offline(self):
        rc, err_out = self._claim_with_push_error(
            "fatal: unable to access 'https://host/': Connection timed out",
            reachable=True,
            allow_offline=True,
        )
        self.assertEqual(rc, 1)
        self.assertIn("reachable", err_out)
        claims.mirror_write.assert_not_called()

    def test_unreachable_without_flag_fails_and_records_nothing(self):
        rc, err_out = self._claim_with_push_error(
            "ssh: connect to host example port 22: Network is unreachable", reachable=False
        )
        self.assertEqual(rc, 1)
        self.assertIn("--allow-offline", err_out)
        claims.mirror_write.assert_not_called()

    def test_unreachable_with_flag_records_local_only_and_exits_3(self):
        rc, err_out = self._claim_with_push_error(
            "fatal: Could not read from remote repository.", reachable=False, allow_offline=True
        )
        self.assertEqual(rc, 3)
        self.assertIn("NOT a published claim", err_out)
        record = claims.mirror_write.call_args[0][1]
        self.assertTrue(record["local_only"])

    def test_control_characters_in_intent_and_session_are_rejected(self):
        for field in ("intent", "session"):
            with mock.patch.object(claims, "push_create") as push:
                with contextlib.redirect_stdout(io.StringIO()):
                    rc = claims.cmd_claim(self._args(**{field: "line one\nline two"}))
                self.assertEqual(rc, 1, field)
                push.assert_not_called()

    def test_default_session_strips_control_characters(self):
        with mock.patch.dict(os.environ, {"MERGE_GATE_SESSION": "a\nb\tc"}):
            self.assertEqual(claims.default_session(), "a_b_c")


class ServiceIdTests(unittest.TestCase):
    def test_valid_ids(self):
        for ok in ("core", "shared-ui", "a.b_c", "x0"):
            self.assertTrue(claims.valid_service_id(ok), ok)

    def test_invalid_ids(self):
        for bad in ("", "crates/core", "-lead", ".hidden", "a..b", "a b", "ref;rm"):
            self.assertFalse(claims.valid_service_id(bad), bad)


class ClaimRecordTests(unittest.TestCase):
    def test_registry_round_trip(self):
        text = (
            "# a comment\n"
            "[services.ci-workflows]\n"
            'kind = "declared"\n'
            'paths = [".github/**", "scripts/claims.py"]\n'
            'consumers = ["app-a", "app-b"]\n'
            'source = "issue #12 — the # is inside a string"\n'
        )
        parsed = claims.parse_toml_lite(text)
        svc = parsed["services"]["ci-workflows"]
        self.assertEqual(svc["kind"], "declared")
        self.assertEqual(svc["paths"], [".github/**", "scripts/claims.py"])
        self.assertEqual(svc["consumers"], ["app-a", "app-b"])
        self.assertEqual(svc["source"], "issue #12 — the # is inside a string")

    def test_toml_string_escapes_control_characters(self):
        text = claims.toml_string("a\nb\rc\td")
        self.assertEqual(text, '"a\\nb\\rc\\td"')
        parsed = claims.parse_toml_lite(f"x = {text}\ny = 1\n")
        self.assertEqual(parsed["x"], "a\nb\rc\td")
        self.assertEqual(parsed["y"], 1)

    def test_claim_record_round_trip_with_previous(self):
        record = {
            "service": "ci-workflows",
            "branch": "wt/mine",
            "session": "user@host:tty",
            "claimed_at": "2026-09-02T00:00:00Z",
            "expires_at": "2026-09-02T08:00:00Z",
            "intent": 'smoke "test" with \\ backslash',
            "previous": {
                "branch": "wt/other",
                "session": "user@host2:tty",
                "claimed_at": "2026-09-01T00:00:00Z",
            },
        }
        text = claims.serialize_claim(record)
        parsed = claims.parse_toml_lite(text)
        self.assertEqual(parsed["service"], "ci-workflows")
        self.assertEqual(parsed["branch"], "wt/mine")
        self.assertEqual(parsed["intent"], 'smoke "test" with \\ backslash')
        self.assertEqual(parsed["previous"]["branch"], "wt/other")

    def test_is_expired(self):
        past = {"expires_at": claims.iso(claims.utcnow() - claims.timedelta(hours=1))}
        future = {"expires_at": claims.iso(claims.utcnow() + claims.timedelta(hours=1))}
        self.assertTrue(claims.is_expired(past))
        self.assertFalse(claims.is_expired(future))


class ExemptionHelperTests(unittest.TestCase):
    def test_docs_only(self):
        self.assertTrue(claims.is_docs_only(["docs/foo.md", "README.md"]))
        self.assertFalse(claims.is_docs_only(["docs/foo.md", "src/lib.rs"]))
        self.assertFalse(claims.is_docs_only([]))

    def test_lockfile_only(self):
        self.assertTrue(claims.is_lockfile_only(["Cargo.lock"]))
        self.assertTrue(claims.is_lockfile_only(["apps/app-a/package-lock.json", "Cargo.lock"]))
        self.assertFalse(claims.is_lockfile_only(["Cargo.lock", "src/lib.rs"]))
        self.assertFalse(claims.is_lockfile_only(["notpackage-lock.json"]))


class RegistryDerivationTests(unittest.TestCase):
    """derive_crate_services is pure (dict in, dict out) — no ROOT/git."""

    def test_shared_crate_reaches_two_apps(self):
        metadata = {
            "packages": [
                {
                    "name": "app-a",
                    "manifest_path": "/repo/apps/app-a/Cargo.toml",
                    "dependencies": [
                        {"name": "shared-crate", "path": "/repo/crates/shared-crate", "kind": None}
                    ],
                },
                {
                    "name": "app-b",
                    "manifest_path": "/repo/apps/app-b/Cargo.toml",
                    "dependencies": [
                        {"name": "shared-crate", "path": "/repo/crates/shared-crate", "kind": None}
                    ],
                },
                {
                    "name": "app-c",
                    "manifest_path": "/repo/apps/app-c/Cargo.toml",
                    "dependencies": [],
                },
                {
                    "name": "shared-crate",
                    "manifest_path": "/repo/crates/shared-crate/Cargo.toml",
                    "dependencies": [],
                },
                {
                    "name": "solo-crate",
                    "manifest_path": "/repo/crates/solo-crate/Cargo.toml",
                    "dependencies": [],
                },
            ]
        }
        result = claims.derive_crate_services(
            metadata, app_crates=APPS, min_consumers=2, root=Path("/repo")
        )
        self.assertIn("shared-crate", result)
        self.assertNotIn("solo-crate", result)
        self.assertNotIn("app-a", result)
        self.assertEqual(result["shared-crate"]["consumers"], ["app-a", "app-b"])
        self.assertEqual(result["shared-crate"]["paths"], ["crates/shared-crate/**"])

    def test_min_consumers_is_honoured(self):
        metadata = {
            "packages": [
                {
                    "name": "app-a",
                    "manifest_path": "/repo/apps/app-a/Cargo.toml",
                    "dependencies": [{"name": "only", "path": "/repo/crates/only", "kind": None}],
                },
                {
                    "name": "only",
                    "manifest_path": "/repo/crates/only/Cargo.toml",
                    "dependencies": [],
                },
            ]
        }
        self.assertNotIn(
            "only", claims.derive_crate_services(metadata, app_crates=["app-a"], min_consumers=2)
        )
        self.assertIn(
            "only", claims.derive_crate_services(metadata, app_crates=["app-a"], min_consumers=1)
        )


class ReleaseBranchRetryTests(unittest.TestCase):
    """`release --branch <b>` must release EVERY claim held by `<b>`,
    retrying a single transient (network-shaped) failure on any one ref
    update rather than silently excluding that service.

    Pure in-process tests: they monkeypatch the git-plumbing functions
    directly on the imported module and call `cmd_release` in-process — no
    real git remote, and no real retry sleeps."""

    def setUp(self):
        sleep_patch = mock.patch.object(claims.time, "sleep")
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)

        mirror_patch = mock.patch.object(claims, "mirror_delete")
        mirror_patch.start()
        self.addCleanup(mirror_patch.stop)

    @staticmethod
    def _record(service, branch):
        return {
            "service": service,
            "branch": branch,
            "session": "test-session",
            "claimed_at": "2026-09-12T00:00:00Z",
            "expires_at": "2026-09-12T08:00:00Z",
            "intent": "test",
        }

    @staticmethod
    def _release_branch_args(branch):
        return argparse.Namespace(service=None, branch=branch, all_mine=False, force=False)

    def test_release_branch_releases_all_three_when_one_push_fails_once(self):
        branch = "wt/target"
        services = ["svc-a", "svc-b", "svc-c"]
        refs = {svc: f"sha-{svc}" for svc in services}
        records = {svc: self._record(svc, branch) for svc in services}
        push_attempts = {"svc-a": 0}

        def fake_push_delete(service):
            if service == "svc-a":
                push_attempts[service] += 1
                if push_attempts[service] == 1:
                    return False, "fatal: unable to access 'https://origin/': Connection timed out"
            return True, ""

        with (
            mock.patch.object(claims, "ls_remote_refs", return_value=dict(refs)),
            mock.patch.object(claims, "ls_remote_ref", side_effect=lambda svc: refs[svc]),
            mock.patch.object(
                claims, "fetch_and_parse", side_effect=lambda svc: dict(records[svc])
            ),
            mock.patch.object(claims, "push_delete", side_effect=fake_push_delete),
        ):
            rc = claims.cmd_release(self._release_branch_args(branch))

        self.assertEqual(rc, 0)
        self.assertEqual(push_attempts["svc-a"], 2)

    def test_release_branch_releases_all_three_when_one_scan_read_fails_once(self):
        branch = "wt/target"
        services = ["svc-a", "svc-b", "svc-c"]
        refs = {svc: f"sha-{svc}" for svc in services}
        records = {svc: self._record(svc, branch) for svc in services}
        fetch_attempts = {"svc-a": 0}

        def fake_fetch_and_parse(service):
            if service == "svc-a":
                fetch_attempts[service] += 1
                if fetch_attempts[service] == 1:
                    raise RuntimeError(
                        "fatal: unable to access 'https://origin/': Connection timed out"
                    )
            return dict(records[service])

        with (
            mock.patch.object(claims, "ls_remote_refs", return_value=dict(refs)),
            mock.patch.object(claims, "ls_remote_ref", side_effect=lambda svc: refs[svc]),
            mock.patch.object(claims, "fetch_and_parse", side_effect=fake_fetch_and_parse),
            mock.patch.object(claims, "push_delete", return_value=(True, "")),
        ):
            rc = claims.cmd_release(self._release_branch_args(branch))

        self.assertEqual(rc, 0)
        self.assertGreaterEqual(fetch_attempts["svc-a"], 2)

    def test_release_branch_reports_persistent_failure_instead_of_dropping_it(self):
        branch = "wt/target"
        services = ["svc-a", "svc-b", "svc-c"]
        refs = {svc: f"sha-{svc}" for svc in services}
        records = {svc: self._record(svc, branch) for svc in services}

        def fake_push_delete(service):
            if service == "svc-a":
                return False, "fatal: unable to access 'https://origin/': Connection timed out"
            return True, ""

        with (
            mock.patch.object(claims, "ls_remote_refs", return_value=dict(refs)),
            mock.patch.object(claims, "ls_remote_ref", side_effect=lambda svc: refs[svc]),
            mock.patch.object(
                claims, "fetch_and_parse", side_effect=lambda svc: dict(records[svc])
            ),
            mock.patch.object(claims, "push_delete", side_effect=fake_push_delete),
        ):
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = claims.cmd_release(self._release_branch_args(branch))

        self.assertEqual(rc, 1)
        out = stdout.getvalue()
        self.assertIn("svc-b", out)
        self.assertIn("svc-c", out)
        self.assertIn("failed for", out)
        self.assertIn("svc-a", out)


# --------------------------------------------------------------------------
# Protocol tests — subprocess against a throwaway bare origin + clone.
# --------------------------------------------------------------------------


class ClaimProtocolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Resolve symlinks so paths built here match `git rev-parse
        # --show-toplevel` inside the subprocess.
        root = Path(self._tmp.name).resolve()
        self.origin = root / "origin.git"
        self.clone = root / "clone"
        subprocess.run(["git", "init", "--bare", "-q", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)], check=True)
        git(self.clone, "config", "user.email", "test-fixture")
        git(self.clone, "config", "user.name", "Test")
        git(self.clone, "checkout", "-q", "-b", "main")
        commit_file(self.clone, "README.md", "test repo\n", "init")
        commit_file(self.clone, "merge-gate.toml", CONFIG_TEXT, "config")
        git(self.clone, "push", "-q", "-u", "origin", "main")

    def tearDown(self):
        self._tmp.cleanup()

    def test_claim_create_second_refused_renew_release(self):
        clone = self.clone
        git(clone, "checkout", "-q", "-b", "wt/mine")

        res = run_claims(
            clone, "claim", "ci-workflows", "--intent", "first claim", "--ttl-hours", "8"
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("claimed 'ci-workflows'", res.stdout)

        ls = git(clone, "ls-remote", "origin", "refs/claims/ci-workflows")
        self.assertIn("refs/claims/ci-workflows", ls.stdout)

        git(clone, "checkout", "-q", "-b", "wt/other")
        res2 = run_claims(clone, "claim", "ci-workflows", "--intent", "second claim")
        self.assertEqual(res2.returncode, 2, res2.stdout + res2.stderr)
        self.assertIn("REFUSED", res2.stdout)
        self.assertIn("wt/mine", res2.stdout)

        git(clone, "checkout", "-q", "wt/mine")
        res3 = run_claims(clone, "renew", "ci-workflows")
        self.assertEqual(res3.returncode, 0, res3.stdout + res3.stderr)
        self.assertIn("renewed 'ci-workflows'", res3.stdout)

        git(clone, "checkout", "-q", "wt/other")
        res4 = run_claims(clone, "renew", "ci-workflows")
        self.assertNotEqual(res4.returncode, 0)

        git(clone, "checkout", "-q", "wt/mine")
        res5 = run_claims(clone, "release", "ci-workflows")
        self.assertEqual(res5.returncode, 0, res5.stdout + res5.stderr)
        ls2 = git(clone, "ls-remote", "origin", "refs/claims/ci-workflows")
        self.assertEqual(ls2.stdout.strip(), "")

    def test_malformed_service_id_is_refused_before_any_push(self):
        clone = self.clone
        git(clone, "checkout", "-q", "-b", "wt/mine")
        res = run_claims(clone, "claim", "crates/core", "--intent", "bad id")
        self.assertEqual(res.returncode, 1)
        self.assertIn("refusing", res.stdout)
        ls = git(clone, "ls-remote", "origin", "refs/claims/*")
        self.assertEqual(ls.stdout.strip(), "")

    def test_expiry_takeover_cas(self):
        clone = self.clone
        git(clone, "checkout", "-q", "-b", "wt/mine")
        res = run_claims(
            clone, "claim", "ci-workflows", "--intent", "will expire", "--ttl-hours", "-0.001"
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        git(clone, "checkout", "-q", "-b", "wt/other")
        res2 = run_claims(
            clone, "claim", "ci-workflows", "--intent", "takeover", "--ttl-hours", "8"
        )
        self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)
        self.assertIn("took over expired claim", res2.stdout)
        self.assertIn("wt/mine", res2.stdout)

        res3 = run_claims(clone, "list", "--json")
        self.assertEqual(res3.returncode, 0, res3.stdout + res3.stderr)
        records = json.loads(res3.stdout)
        rec = next(r for r in records if r["service"] == "ci-workflows")
        self.assertEqual(rec["branch"], "wt/other")
        self.assertEqual(rec["previous"]["branch"], "wt/mine")

        run_claims(clone, "release", "ci-workflows")

    def test_check_exemptions(self):
        clone = self.clone

        base_sha = rev_parse(clone, "HEAD")
        git(clone, "checkout", "-q", "-b", "wt/docs")
        commit_file(clone, "docs/foo.md", "hello\n", "docs change")
        head_sha = rev_parse(clone, "HEAD")
        res = run_claims(
            clone, "check", "--base", base_sha, "--head", head_sha, "--branch", "wt/docs"
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("docs-only", res.stdout)

        git(clone, "checkout", "-q", "main")
        git(clone, "checkout", "-q", "-b", "wt/lockfile")
        commit_file(clone, "package-lock.json", "{}\n", "lockfile bump")
        head_sha2 = rev_parse(clone, "HEAD")
        res2 = run_claims(
            clone, "check", "--base", base_sha, "--head", head_sha2, "--branch", "wt/lockfile"
        )
        self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)
        self.assertIn("lockfile-only", res2.stdout)

        git(clone, "checkout", "-q", "main")
        git(clone, "checkout", "-q", "-b", "wt/deps")
        commit_file(clone, "src/app.rs", "fn main() {}\n", "code change")
        head_sha3 = rev_parse(clone, "HEAD")
        res3 = run_claims(
            clone,
            "check",
            "--base",
            base_sha,
            "--head",
            head_sha3,
            "--branch",
            "wt/deps",
            "--actor",
            "dependabot[bot]",
        )
        self.assertEqual(res3.returncode, 0, res3.stdout + res3.stderr)
        self.assertIn("dependabot[bot]", res3.stdout)

        # Same non-docs diff WITHOUT the exempt actor and with no claim
        # held — must not be exempt; since no service in this bare repo
        # owns src/app.rs it reports "no shared services touched".
        res4 = run_claims(
            clone, "check", "--base", base_sha, "--head", head_sha3, "--branch", "wt/deps"
        )
        self.assertEqual(res4.returncode, 0, res4.stdout + res4.stderr)
        self.assertIn("no shared services touched", res4.stdout)

    def test_check_branch_is_repeatable(self):
        """A train's full-gate run checks every landed candidate's branch at
        once — `check` must accept --branch repeated, and pass when the live
        holder is ONE OF them. Single-branch behavior stays unchanged."""
        clone = self.clone
        claims_dir = clone / "claims"
        claims_dir.mkdir(exist_ok=True)
        (claims_dir / "test-svc.md").write_text(
            "# test-svc\n\n"
            "- **kind:** declared\n"
            "- **owned paths:**\n"
            "  - `src/app.rs`\n"
            "- **consumers:** none\n"
            "- **source:** test fixture\n"
        )

        base_sha = rev_parse(clone, "HEAD")
        git(clone, "checkout", "-q", "-b", "wt/holder")
        commit_file(clone, "src/app.rs", "fn main() {}\n", "touch test-svc")
        head_sha = rev_parse(clone, "HEAD")

        res_claim = run_claims(clone, "claim", "test-svc", "--intent", "multi-branch test")
        self.assertEqual(res_claim.returncode, 0, res_claim.stdout + res_claim.stderr)

        res_multi = run_claims(
            clone,
            "check",
            "--base",
            base_sha,
            "--head",
            head_sha,
            "--branch",
            "wt/some-other-candidate",
            "--branch",
            "wt/holder",
            "--branch",
            "wt/yet-another",
        )
        self.assertEqual(res_multi.returncode, 0, res_multi.stdout + res_multi.stderr)
        self.assertIn("OK", res_multi.stdout)

        res_single_wrong = run_claims(
            clone,
            "check",
            "--base",
            base_sha,
            "--head",
            head_sha,
            "--branch",
            "wt/some-other-candidate",
        )
        self.assertEqual(res_single_wrong.returncode, 1)
        self.assertIn("BLOCKED", res_single_wrong.stdout)

        run_claims(clone, "release", "test-svc")

    def test_registry_generation_against_fake_metadata(self):
        clone = self.clone
        git(clone, "checkout", "-q", "-b", "wt/registry")

        metadata = {
            "packages": [
                {
                    "name": "app-a",
                    "manifest_path": str(clone / "apps/app-a/Cargo.toml"),
                    "dependencies": [
                        {
                            "name": "shared-crate",
                            "path": str(clone / "crates/shared-crate"),
                            "kind": None,
                        }
                    ],
                },
                {
                    "name": "app-b",
                    "manifest_path": str(clone / "apps/app-b/Cargo.toml"),
                    "dependencies": [
                        {
                            "name": "shared-crate",
                            "path": str(clone / "crates/shared-crate"),
                            "kind": None,
                        }
                    ],
                },
                {
                    "name": "app-c",
                    "manifest_path": str(clone / "apps/app-c/Cargo.toml"),
                    "dependencies": [],
                },
                {
                    "name": "shared-crate",
                    "manifest_path": str(clone / "crates/shared-crate/Cargo.toml"),
                    "dependencies": [],
                },
                {
                    "name": "solo-crate",
                    "manifest_path": str(clone / "crates/solo-crate/Cargo.toml"),
                    "dependencies": [],
                },
            ]
        }
        metadata_path = clone / "fake-metadata.json"
        metadata_path.write_text(json.dumps(metadata))

        registry_dir = clone / "claims"
        registry_dir.mkdir(exist_ok=True)
        (registry_dir / "REGISTRY.toml").write_text(
            "[services.workspace-manifests]\n"
            'kind = "declared"\n'
            'paths = ["Cargo.toml", "Cargo.lock"]\n'
            'consumers = ["app-a", "app-b", "app-c"]\n'
            'source = "test fixture"\n'
        )

        res = run_claims(clone, "sync-registry", "--metadata-json", str(metadata_path))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        self.assertTrue((registry_dir / "shared-crate.md").exists())
        self.assertFalse((registry_dir / "solo-crate.md").exists())
        self.assertTrue((registry_dir / "workspace-manifests.md").exists())
        self.assertTrue((registry_dir / "README.md").exists())

        shared_content = (registry_dir / "shared-crate.md").read_text()
        self.assertIn("kind:** crate", shared_content)
        self.assertIn("app-a, app-b", shared_content)
        self.assertIn("`crates/shared-crate/**`", shared_content)

        res_check = run_claims(
            clone, "sync-registry", "--check", "--metadata-json", str(metadata_path)
        )
        self.assertEqual(res_check.returncode, 0, res_check.stdout + res_check.stderr)

        (registry_dir / "shared-crate.md").write_text("tampered\n")
        res_drift = run_claims(
            clone, "sync-registry", "--check", "--metadata-json", str(metadata_path)
        )
        self.assertEqual(res_drift.returncode, 1)
        self.assertIn("drift", res_drift.stdout)

        (registry_dir / "shared-crate.md").write_text(shared_content)
        (registry_dir / "stale-service.md").write_text("# stale-service\n")
        res_extra = run_claims(
            clone, "sync-registry", "--check", "--metadata-json", str(metadata_path)
        )
        self.assertEqual(res_extra.returncode, 1)
        self.assertIn("stale-service", res_extra.stdout)

    def _mirror_file(self, clone, service):
        common_dir = subprocess.run(
            ["git", "-C", str(clone), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return Path(common_dir) / "claims" / f"{service}.claim"

    def test_reconcile_offline_claim_then_syncs(self):
        """An offline `claim` is written local_only=true; the very next
        invocation with network access (here, `list`) must reconcile it —
        publish it for real and clear the local_only flag."""
        clone = self.clone
        bogus_origin = clone.parent / "does-not-exist-1.git"

        git(clone, "checkout", "-q", "-b", "wt/mine")
        git(clone, "remote", "set-url", "origin", str(bogus_origin))
        # Without --allow-offline an unreachable remote is a failure and
        # records nothing.
        res0 = run_claims(clone, "claim", "reconcile-svc", "--intent", "offline test")
        self.assertEqual(res0.returncode, 1, res0.stdout + res0.stderr)
        self.assertFalse(self._mirror_file(clone, "reconcile-svc").exists())
        res = run_claims(
            clone,
            "claim",
            "reconcile-svc",
            "--intent",
            "offline test",
            "--ttl-hours",
            "8",
            "--allow-offline",
        )
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        self.assertIn("recorded locally only", res.stdout + res.stderr)

        mirror_file = self._mirror_file(clone, "reconcile-svc")
        self.assertTrue(mirror_file.exists())
        self.assertIn("local_only = true", mirror_file.read_text())

        git(clone, "remote", "set-url", "origin", str(self.origin))
        res2 = run_claims(clone, "list")
        self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)
        self.assertIn("reconcile: published locally-held claim on 'reconcile-svc'", res2.stdout)

        ls = git(clone, "ls-remote", "origin", "refs/claims/reconcile-svc")
        self.assertIn("refs/claims/reconcile-svc", ls.stdout)
        self.assertNotIn("local_only", mirror_file.read_text())

        run_claims(clone, "release", "reconcile-svc")

    def test_reconcile_conflict_is_loud_not_silent(self):
        clone = self.clone
        bogus_origin = clone.parent / "does-not-exist-2.git"

        git(clone, "checkout", "-q", "-b", "wt/other")
        res0 = run_claims(clone, "claim", "conflict-svc", "--intent", "first, real")
        self.assertEqual(res0.returncode, 0, res0.stdout + res0.stderr)

        git(clone, "checkout", "-q", "-b", "wt/mine")
        git(clone, "remote", "set-url", "origin", str(bogus_origin))
        res1 = run_claims(
            clone, "claim", "conflict-svc", "--intent", "offline attempt", "--allow-offline"
        )
        self.assertEqual(res1.returncode, 3, res1.stdout + res1.stderr)
        mirror_file = self._mirror_file(clone, "conflict-svc")
        self.assertIn("local_only = true", mirror_file.read_text())

        git(clone, "remote", "set-url", "origin", str(self.origin))
        res2 = run_claims(clone, "list")
        self.assertEqual(res2.returncode, 2, res2.stdout + res2.stderr)
        combined = res2.stdout + res2.stderr
        self.assertIn("CONFLICT", combined)
        self.assertIn("conflict-svc", combined)
        self.assertIn("local_only = true", mirror_file.read_text())

        git(clone, "checkout", "-q", "wt/other")
        run_claims(clone, "release", "conflict-svc")

    def test_list_never_overwrites_local_only_mirror(self):
        clone = self.clone
        bogus_origin = clone.parent / "does-not-exist-3.git"

        git(clone, "checkout", "-q", "-b", "wt/mine")
        git(clone, "remote", "set-url", "origin", str(bogus_origin))
        res = run_claims(
            clone, "claim", "still-offline-svc", "--intent", "stays offline", "--allow-offline"
        )
        self.assertEqual(res.returncode, 3, res.stdout + res.stderr)
        mirror_file = self._mirror_file(clone, "still-offline-svc")
        self.assertIn("local_only = true", mirror_file.read_text())

        res_list = run_claims(clone, "list", "--offline")
        self.assertEqual(res_list.returncode, 0, res_list.stdout + res_list.stderr)
        self.assertIn("still-offline-svc", res_list.stdout)
        self.assertIn("LOCAL-ONLY", res_list.stdout)
        self.assertIn("local_only = true", mirror_file.read_text())

    def test_two_line_intent_is_rejected_by_the_cli(self):
        clone = self.clone
        git(clone, "checkout", "-q", "-b", "wt/mine")
        res = run_claims(clone, "claim", "svc", "--intent", 'first line\nsecond = "injected"')
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("control characters", res.stdout)
        ls = git(clone, "ls-remote", "origin", "refs/claims/*")
        self.assertEqual(ls.stdout.strip(), "")

    def test_malformed_remote_record_does_not_brick_list_or_release(self):
        clone = self.clone
        git(clone, "checkout", "-q", "-b", "wt/mine")
        # A pre-existing ref whose record is not parseable TOML, built with
        # plumbing so the working tree and index are untouched.
        env = dict(os.environ, GIT_INDEX_FILE=str(clone.parent / "tmp-index"))
        blob = subprocess.run(
            ["git", "-C", str(clone), "hash-object", "-w", "--stdin"],
            input="= = [ not toml\n",
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(clone), "read-tree", "--empty"], env=env, check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(clone),
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},claims/broken.claim",
            ],
            env=env,
            check=True,
        )
        tree = subprocess.run(
            ["git", "-C", str(clone), "write-tree"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        commit = git(clone, "commit-tree", tree, "-m", "broken").stdout.strip()
        git(clone, "push", "-q", "origin", f"{commit}:refs/claims/broken")

        res = run_claims(clone, "claim", "good", "--intent", "alongside a broken record")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        res_list = run_claims(clone, "list")
        self.assertEqual(res_list.returncode, 0, res_list.stdout + res_list.stderr)
        self.assertIn("warning", res_list.stdout)
        self.assertIn("malformed", res_list.stdout)
        self.assertIn("good", res_list.stdout)

        res_rel = run_claims(clone, "release", "good")
        self.assertEqual(res_rel.returncode, 0, res_rel.stdout + res_rel.stderr)
        ls = git(clone, "ls-remote", "origin", "refs/claims/good")
        self.assertEqual(ls.stdout.strip(), "")

        # A bulk release still releases what it can and names the bad one.
        run_claims(clone, "claim", "good2", "--intent", "bulk")
        res_bulk = run_claims(clone, "release", "--branch", "wt/mine")
        self.assertIn("released 1 claim(s)", res_bulk.stdout)
        self.assertIn("broken", res_bulk.stdout)
        self.assertNotEqual(res_bulk.returncode, 0)
        git(clone, "push", "-q", "origin", ":refs/claims/broken")

    def test_custom_remote_and_ref_prefix(self):
        clone = self.clone
        git(clone, "remote", "rename", "origin", "upstream")
        (clone / "merge-gate.toml").write_text(
            'schema = 1\n[repo]\nremote = "upstream"\n[claims]\nref_prefix = "refs/locks/"\n'
        )
        git(clone, "checkout", "-q", "-b", "wt/mine")
        res = run_claims(clone, "claim", "svc", "--intent", "custom ref prefix")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        ls = git(clone, "ls-remote", "upstream", "refs/locks/svc")
        self.assertIn("refs/locks/svc", ls.stdout)
        run_claims(clone, "release", "svc")


class PrePushHookTests(unittest.TestCase):
    """Verify scripts/git-hooks/pre-push against a real git push, with
    core.hooksPath pointed at the real hooks directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.origin = root / "origin.git"
        self.clone = root / "clone"
        subprocess.run(["git", "init", "--bare", "-q", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)], check=True)
        git(self.clone, "config", "user.email", "test-fixture")
        git(self.clone, "config", "user.name", "Test")
        git(self.clone, "config", "core.hooksPath", str(HOOKS_DIR))
        git(self.clone, "checkout", "-q", "-b", "main")
        commit_file(self.clone, "README.md", "hi\n", "init")

    def tearDown(self):
        self._tmp.cleanup()

    def _push(self, *refs, env_extra=None):
        env = dict(os.environ)
        if env_extra:
            env.update(env_extra)
        return subprocess.run(
            ["git", "push", "origin", *refs],
            cwd=str(self.clone),
            capture_output=True,
            text=True,
            env=env,
        )

    def test_direct_main_push_blocked_without_train_var(self):
        res = self._push("main")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("REFUSED", res.stdout + res.stderr)

    def test_main_push_allowed_with_train_var_scoped_to_one_command(self):
        res = self._push("main", env_extra={"MERGE_GATE_TRAIN": "1"})
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        git(self.clone, "commit", "--allow-empty", "-q", "-m", "again")
        res2 = self._push("main")
        self.assertNotEqual(res2.returncode, 0)
        self.assertIn("REFUSED", res2.stdout + res2.stderr)

    def test_train_branch_push_blocked_without_train_var(self):
        git(self.clone, "checkout", "-q", "-b", "train/local-1")
        res = self._push("train/local-1")
        self.assertNotEqual(res.returncode, 0)

    def test_claim_ref_push_and_delete_never_blocked(self):
        git(self.clone, "checkout", "-q", "-b", "wt/whatever")
        res = run_claims(self.clone, "claim", "hook-test-svc", "--intent", "hook check")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

        res2 = run_claims(self.clone, "release", "hook-test-svc")
        self.assertEqual(res2.returncode, 0, res2.stdout + res2.stderr)

        ls = git(self.clone, "ls-remote", "origin", "refs/claims/hook-test-svc")
        self.assertEqual(ls.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
