"""End-to-end tests for bootstrap.sh and the shell entry points.

Stdlib `unittest` only, no network. Creates a throwaway bare "origin" and a
clone, bootstraps the merge-gate scripts into the clone, and then runs the
shell scripts in the modes that need no toolchain: `full-gate.sh
--dry-run`, `standards-check.sh`, `invariants-check.sh`, `wt.sh new/ls/rm`,
`land.sh --dry-run`, and the pre-push hook.

Run:
    python3 -m unittest scripts/tests/test_bootstrap.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
BOOTSTRAP = REPO / "bootstrap.sh"


def git(cwd, *args):
    res = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {res.stderr}")
    return res


def sh(cwd, *args, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, env=full_env)


class BootstrappedRepoCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name).resolve()
        self.origin = root / "origin.git"
        self.clone = root / "clone"
        self.wt_root = root / "worktrees"
        subprocess.run(["git", "init", "--bare", "-q", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.clone)], check=True)
        git(self.clone, "config", "user.email", "test-fixture")
        git(self.clone, "config", "user.name", "Test")
        git(self.clone, "checkout", "-q", "-b", "main")
        (self.clone / "README.md").write_text("example\n")
        git(self.clone, "add", "README.md")
        git(self.clone, "commit", "-q", "-m", "init")
        res = sh(self.clone, "bash", str(BOOTSTRAP), str(self.clone))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "add merge gate")
        # The pre-push hook is installed by wt.sh; for the initial push use
        # the train variable the same way land.sh would.
        res = sh(
            self.clone, "git", "push", "-q", "-u", "origin", "main", env={"MERGE_GATE_TRAIN": "1"}
        )
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_bootstrap_copies_scripts_and_writes_config(self):
        for rel in (
            "scripts/land.sh",
            "scripts/full-gate.sh",
            "scripts/wt.sh",
            "scripts/claims.py",
            "scripts/test-baseline.py",
            "scripts/affected.py",
            "scripts/adr-check.py",
            "scripts/invariants-check.sh",
            "scripts/standards-check.sh",
            "scripts/mergegate_config.py",
            "scripts/mergegate_scan.py",
            "scripts/git-hooks/pre-push",
            "scripts/tests/test_claims.py",
            "merge-gate.toml",
            "ci/baseline.json",
            "scripts/invariants-allowlist.txt",
            "scripts/standards-allowlist.txt",
        ):
            self.assertTrue((self.clone / rel).exists(), rel)
        res = sh(self.clone, "python3", "scripts/mergegate_config.py", "validate")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_bootstrap_refuses_to_overwrite_an_existing_config(self):
        res = sh(self.clone, "bash", str(BOOTSTRAP), str(self.clone))
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("keeping existing merge-gate.toml", res.stdout)

    def test_full_gate_dry_run_touches_nothing(self):
        res = sh(self.clone, "bash", "scripts/full-gate.sh", "--dry-run", "--results", "ci-results")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("(--dry-run: nothing was actually executed)", res.stdout)
        self.assertIn("baseline-compare", res.stdout)
        self.assertFalse((self.clone / "ci-results").exists())

    def test_standards_and_invariants_run_clean_on_the_default_config(self):
        # The default merge-gate.toml ships two standards checks and two
        # invariants; on a tree with no source files both must pass.
        res = sh(self.clone, "bash", "scripts/standards-check.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("2 check(s), 0 failure(s)", res.stdout)
        res = sh(self.clone, "bash", "scripts/invariants-check.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("invariants: OK (2 checked", res.stdout)

    def test_invariant_hit_fails_unless_allowlisted(self):
        (self.clone / "merge-gate.toml").write_text(
            "schema = 1\n"
            "[invariants.checks.no_todo]\n"
            "pattern = 'todo!\\('\n"
            "paths = ['crates/**/*.rs']\n"
            "excludes = ['**/tests/**']\n"
        )
        src = self.clone / "crates" / "core" / "src"
        src.mkdir(parents=True)
        (src / "lib.rs").write_text("fn f() { todo!() }\n")
        res = sh(self.clone, "bash", "scripts/invariants-check.sh")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("no_todo: crates/core/src/lib.rs:1:", res.stdout)

        (self.clone / "scripts" / "invariants-allowlist.txt").write_text(
            "no_todo crates/core/src/lib.rs   # pre-existing, tracked separately\n"
        )
        res = sh(self.clone, "bash", "scripts/invariants-check.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("allowlisted: no_todo:", res.stdout)

    def test_standards_fail_and_warn_tiers(self):
        (self.clone / "merge-gate.toml").write_text(
            "schema = 1\n"
            "[standards.checks.money-tofixed]\n"
            "tier = 'fail'\n"
            "scope = 'ts'\n"
            "pattern = '\\.toFixed\\(2\\)'\n"
            "message = 'currency must go through the formatter'\n"
            "[standards.checks.swallowed-catch]\n"
            "tier = 'warn'\n"
            "scope = 'ts'\n"
            "pattern = 'catch\\s*\\{\\s*\\}'\n"
            "message = 'empty catch'\n"
        )
        src = self.clone / "apps" / "web" / "src"
        src.mkdir(parents=True)
        (src / "a.ts").write_text("const s = `$${x.toFixed(2)}`;\ntry {} catch {}\n")
        res = sh(self.clone, "bash", "scripts/standards-check.sh")
        self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
        self.assertIn("FAIL [money-tofixed]", res.stdout)
        self.assertIn("WARN [swallowed-catch]", res.stdout)

        (self.clone / "scripts" / "standards-allowlist.txt").write_text(
            "money-tofixed apps/web/src/a.ts   # the formatter itself\n"
        )
        res = sh(self.clone, "bash", "scripts/standards-check.sh")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)

    def test_worktree_new_ls_rm(self):
        env = {"MERGE_GATE_WT_ROOT": str(self.wt_root)}
        res = sh(self.clone, "bash", "scripts/wt.sh", "new", "feature-x", env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        wt = self.wt_root / "feature-x"
        self.assertTrue(wt.is_dir())
        self.assertEqual(
            git(wt, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "wt/feature-x"
        )
        hooks = git(self.clone, "config", "core.hooksPath").stdout.strip()
        self.assertEqual(hooks, "scripts/git-hooks")

        res = sh(self.clone, "bash", "scripts/wt.sh", "new", "../escape", env=env)
        self.assertNotEqual(res.returncode, 0)

        res = sh(self.clone, "bash", "scripts/wt.sh", "ls", env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("wt/feature-x", res.stdout)

        # Unpushed branch: refuse without --force.
        res = sh(self.clone, "bash", "scripts/wt.sh", "rm", "feature-x", env=env)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("refusing", res.stderr)
        res = sh(self.clone, "bash", "scripts/wt.sh", "rm", "feature-x", "--force", env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertFalse(wt.exists())

    def test_hook_blocks_direct_main_push_after_install(self):
        env = {"MERGE_GATE_WT_ROOT": str(self.wt_root)}
        res = sh(self.clone, "bash", "scripts/wt.sh", "new", "feature-y", env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        git(self.clone, "commit", "--allow-empty", "-q", "-m", "direct")
        res = sh(self.clone, "git", "push", "origin", "main")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("REFUSED", res.stdout + res.stderr)

    def test_land_dry_run_prints_plan_without_mutating(self):
        env = {"MERGE_GATE_WT_ROOT": str(self.wt_root)}
        res = sh(self.clone, "bash", "scripts/wt.sh", "new", "feature-z", env=env)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        wt = self.wt_root / "feature-z"
        (wt / "CHANGE.md").write_text("change\n")
        git(wt, "add", "CHANGE.md")
        git(wt, "commit", "-q", "-m", "a change")
        git(wt, "push", "-q", "-u", "origin", "wt/feature-z")

        before = git(self.clone, "rev-parse", "HEAD").stdout.strip()
        res = sh(self.clone, "bash", "scripts/land.sh", "--dry-run", "wt/feature-z")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("+ git merge --no-ff --no-edit origin/wt/feature-z", res.stdout)
        self.assertIn("MERGE_GATE_TRAIN=1 git push origin", res.stdout)
        self.assertEqual(git(self.clone, "rev-parse", "HEAD").stdout.strip(), before)
        self.assertEqual(
            git(self.clone, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main"
        )
        self.assertEqual(git(self.clone, "branch", "--list", "train/*").stdout.strip(), "")

        res = sh(self.clone, "bash", "scripts/land.sh", "--dry-run", "--no-gate", "wt/feature-z")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--i-understand-ungated", res.stderr)

        res = sh(self.clone, "bash", "scripts/land.sh", "--dry-run", "-not-a-branch")
        self.assertNotEqual(res.returncode, 0)

        shutil.rmtree(wt)


if __name__ == "__main__":
    unittest.main()
