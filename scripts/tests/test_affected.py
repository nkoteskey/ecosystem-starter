"""Unit tests for scripts/affected.py.

Stdlib `unittest` only, no network. Builds a real temporary git repo (two
commits) per test so `changed_files()` exercises real `git diff
--name-only <merge-base>..<head>`, and feeds a hand-built `cargo metadata
--no-deps`-shaped dict (as affected.py's `--metadata-json` flag would load
from disk) so the crate-dependency-graph logic never needs a real `cargo`
invocation or real Cargo.toml files.

Run:
    python3 -m unittest scripts/tests/test_affected.py -v
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPTS_DIR / "affected.py"
sys.path.insert(0, str(SCRIPTS_DIR))
import mergegate_config  # noqa: E402


def _load_module():
    spec = importlib.util.spec_from_file_location("affected", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


aff = _load_module()

APPS = ["app-a", "app-b", "app-c"]

CFG = mergegate_config.config_from_dict(
    {
        "affected": {
            "frontend_apps": APPS,
            "mobile_dir": "apps/mobile",
            "integration_paths": ["docs/design/feature-chains.toml", "crates/chain-audit/"],
            "packages": {
                "shared-ui": {"path": "packages/shared-ui", "affects_all_frontends": True},
                "embed": {"path": "packages/embed"},
            },
        }
    }
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True)
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


class GitRepoCase(unittest.TestCase):
    """Two-commit fixture repo: `base` then a second commit with the
    per-test changes, checked out as `head` (same as HEAD)."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        _git(self.repo, "init", "-q", "-b", "main")
        _git(self.repo, "config", "user.email", "test-fixture")
        _git(self.repo, "config", "user.name", "Test")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, content: str = "x\n") -> None:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def commit(self, msg: str) -> str:
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", msg)
        return _git(self.repo, "rev-parse", "HEAD").strip()

    def diff(self, base: str, head: str = "HEAD"):
        return aff.changed_files(base, head, cwd=self.repo)


def _pkg(name: str, rel_dir: str, workspace_root: Path, deps=()) -> dict:
    """deps: iterable of (dep_rel_dir, kind) where kind is None/'dev'/'build'."""
    dependencies = []
    for dep_rel_dir, kind in deps:
        dependencies.append(
            {
                "name": Path(dep_rel_dir).name,
                "path": str(workspace_root / dep_rel_dir),
                "kind": kind,
            }
        )
    return {
        "name": name,
        "manifest_path": str(workspace_root / rel_dir / "Cargo.toml"),
        "dependencies": dependencies,
    }


def _graph(workspace_root: Path) -> aff.Graph:
    """A small synthetic workspace:

    core            (crates/core)              -- no deps
    signals         (crates/signals)           -- normal dep on core
    test-only-crate (crates/test-only-crate)   -- DEV dep on signals
    app-a-crate     (apps/app-a/src-tauri)     -- normal deps on core, signals
    app-b-crate     (apps/app-b/src-tauri)     -- normal dep on core
    app-c-crate     (apps/app-c/src-tauri)     -- normal dep on signals
    """
    packages = [
        _pkg("core", "crates/core", workspace_root),
        _pkg("signals", "crates/signals", workspace_root, deps=[("crates/core", None)]),
        _pkg(
            "test-only-crate",
            "crates/test-only-crate",
            workspace_root,
            deps=[("crates/signals", "dev")],
        ),
        _pkg(
            "app-a-crate",
            "apps/app-a/src-tauri",
            workspace_root,
            deps=[("crates/core", None), ("crates/signals", None)],
        ),
        _pkg("app-b-crate", "apps/app-b/src-tauri", workspace_root, deps=[("crates/core", None)]),
        _pkg(
            "app-c-crate",
            "apps/app-c/src-tauri",
            workspace_root,
            deps=[("crates/signals", None)],
        ),
    ]
    metadata = {"workspace_root": str(workspace_root), "packages": packages}
    return aff.Graph(metadata, CFG["affected"])


class TestReverseClosure(GitRepoCase):
    def test_changing_foundation_crate_closes_over_everything(self):
        self.write("crates/core/src/lib.rs", "fn a(){}\n")
        self.write("crates/signals/src/lib.rs", "fn b(){}\n")
        base = self.commit("base")
        self.write("crates/core/src/lib.rs", "fn a(){ /* changed */ }\n")
        head = self.commit("change core")

        files = self.diff(base, head)
        result = aff.analyze(files, _graph(self.repo), CFG)

        for crate in (
            "core",
            "signals",
            "test-only-crate",
            "app-a-crate",
            "app-b-crate",
            "app-c-crate",
        ):
            self.assertIn(crate, result["crates"])
        self.assertTrue(result["rust"])

    def test_changing_leaf_crate_does_not_affect_unrelated_crates(self):
        self.write("crates/core/src/lib.rs", "fn a(){}\n")
        base = self.commit("base")
        self.write("crates/test-only-crate/src/lib.rs", "fn c(){}\n")
        head = self.commit("add test-only-crate")

        files = self.diff(base, head)
        result = aff.analyze(files, _graph(self.repo), CFG)

        self.assertEqual(result["crates"], ["test-only-crate"])


class TestAppCrateMapping(GitRepoCase):
    def test_app_src_tauri_change_maps_to_that_app_crate_only(self):
        self.write("apps/app-a/src-tauri/src/main.rs", "fn main(){}\n")
        base = self.commit("base")
        self.write("apps/app-a/src-tauri/src/main.rs", "fn main(){ /* x */ }\n")
        head = self.commit("change app-a")

        files = self.diff(base, head)
        result = aff.analyze(files, _graph(self.repo), CFG)

        self.assertEqual(result["crates"], ["app-a-crate"])
        self.assertTrue(result["apps"]["app-a"])
        self.assertFalse(result["apps"]["app-b"])
        self.assertFalse(result["apps"]["app-c"])
        # Rust-only change: frontend flags stay false (separate bucket).
        self.assertFalse(any(result["frontend"].values()))


class TestDocsOnlyAndLockfileOnly(GitRepoCase):
    def test_docs_only_diff(self):
        self.write("README.md", "hello\n")
        base = self.commit("base")
        self.write("docs/design/some-note.md", "note v1\n")
        self.write("CHANGELOG.md", "v1\n")
        head = self.commit("docs edits")

        files = self.diff(base, head)
        result = aff.analyze(files, _graph(self.repo), CFG)

        self.assertTrue(result["docs_only"])
        self.assertFalse(result["rust"])
        self.assertFalse(result["integration"])
        self.assertFalse(any(result["frontend"].values()))

    def test_lockfile_only_diff_still_forces_rust_and_that_apps_frontend(self):
        self.write("Cargo.lock", "v1\n")
        base = self.commit("base")
        self.write("Cargo.lock", "v2\n")
        self.write("apps/app-b/package-lock.json", "{}\n")
        head = self.commit("lockfile bump")

        files = self.diff(base, head)
        result = aff.analyze(files, _graph(self.repo), CFG)

        self.assertTrue(result["lockfile_only"])
        # Cargo.lock is also a workspace-file rust trigger (forces all crates)
        self.assertTrue(result["rust"])
        # apps/app-b/package-lock.json matches the frontend config glob
        self.assertTrue(result["frontend"]["app-b"])
        self.assertFalse(result["docs_only"])


class TestPackagesAndEverythingBuckets(GitRepoCase):
    def test_shared_package_change_affects_all_frontends(self):
        self.write("packages/shared-ui/src/index.ts", "export {}\n")
        base = self.commit("base")
        self.write("packages/shared-ui/src/index.ts", "export const x = 1;\n")
        head = self.commit("shared-ui edit")

        files = self.diff(base, head)
        result = aff.analyze(files, _graph(self.repo), CFG)

        self.assertTrue(result["frontend"]["shared-ui"])
        for a in APPS:
            self.assertTrue(result["frontend"][a])
        self.assertFalse(result["frontend"]["embed"])
        self.assertTrue(result["integration"])  # any frontend affected -> integration

    def test_mobile_dir_sets_only_the_mobile_flag(self):
        self.write("apps/mobile/app-a/src/x.ts", "1\n")
        base = self.commit("base")
        self.write("apps/mobile/app-a/src/x.ts", "2\n")
        head = self.commit("mobile edit")

        files = self.diff(base, head)
        result = aff.analyze(files, _graph(self.repo), CFG)

        self.assertTrue(result["frontend"]["mobile"])
        self.assertFalse(result["frontend"]["app-a"])

    def test_integration_path_sets_integration_without_rust(self):
        self.write("docs/design/feature-chains.toml", "v1\n")
        base = self.commit("base")
        self.write("docs/design/feature-chains.toml", "v2\n")
        head = self.commit("chains edit")

        files = self.diff(base, head)
        result = aff.analyze(files, _graph(self.repo), CFG)

        self.assertTrue(result["integration"])
        self.assertFalse(result["rust"])

    def test_scripts_change_forces_everything_except_scripts_tests(self):
        self.write("scripts/standards-check.sh", "#!/usr/bin/env bash\n")
        base = self.commit("base")
        self.write("scripts/standards-check.sh", "#!/usr/bin/env bash\necho hi\n")
        head = self.commit("scripts edit")

        files = self.diff(base, head)
        result = aff.analyze(files, _graph(self.repo), CFG)

        self.assertTrue(result["rust"])
        self.assertTrue(result["frontend"]["mobile"])
        self.assertTrue(result["frontend"]["shared-ui"])
        self.assertTrue(result["frontend"]["embed"])
        self.assertTrue(result["integration"])

    def test_scripts_tests_change_does_not_force_everything(self):
        self.write("scripts/tests/test_something.py", "x = 1\n")
        base = self.commit("base")
        self.write("scripts/tests/test_something.py", "x = 2\n")
        head = self.commit("scripts/tests edit")

        files = self.diff(base, head)
        result = aff.analyze(files, _graph(self.repo), CFG)

        self.assertFalse(result["rust"])
        self.assertFalse(any(result["frontend"].values()))
        self.assertFalse(result["integration"])


class TestGithubOutput(unittest.TestCase):
    def test_keys_are_derived_from_config_names(self):
        result = {
            "rust": True,
            "crates": ["core"],
            "crate_args": "-p core",
            "apps": {"app-a": True},
            "frontend": {"app-a": False, "shared-ui": True},
            "integration": True,
            "docs_only": False,
            "lockfile_only": False,
        }
        lines = aff.github_output_lines(result)
        self.assertIn("app_app_a=true", lines)
        self.assertIn("fe_shared_ui=true", lines)
        self.assertIn("integration=true", lines)


if __name__ == "__main__":
    unittest.main()
