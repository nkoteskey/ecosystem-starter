"""Tests for this template's own bootstrap.sh (the stamping layer on top of
the vendored merge-gate bootstrap). Stdlib unittest; each test stamps a
throwaway git repository.

Run:
    python3 -m unittest tests/test_starter_bootstrap.py -v
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BOOTSTRAP = REPO / "bootstrap.sh"


def sh(cwd, *args, env=None):
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.run(list(args), cwd=str(cwd), capture_output=True, text=True, env=full_env)


class StarterBootstrapCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name).resolve() / "new-repo"
        self.target.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.target)], check=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def stamp(self, *args):
        return sh(self.target, "bash", str(BOOTSTRAP), str(self.target), *args)

    def test_apps_writes_config_and_registry(self):
        res = self.stamp("--name", "scratch", "--apps", "web,admin")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        keys = sh(self.target, "python3", "scripts/mergegate_config.py", "keys", "gates.frontend")
        self.assertEqual(keys.stdout.split(), ["web", "admin"])
        self.assertTrue((self.target / "claims" / "README.md").exists())
        self.assertTrue((self.target / "claims" / "shared-ui.md").exists())
        self.assertEqual((self.target / "README.md").read_text().splitlines()[0], "# scratch")
        self.assertEqual((self.target / "CLAUDE.md").read_text().splitlines()[0], "# scratch")

    def test_existing_config_is_kept_without_force_config(self):
        self.assertEqual(self.stamp("--name", "scratch", "--apps", "web").returncode, 0)
        before = (self.target / "merge-gate.toml").read_text()
        res = self.stamp("--name", "scratch", "--apps", "admin")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--force-config", res.stderr)
        self.assertEqual((self.target / "merge-gate.toml").read_text(), before)
        res = self.stamp("--name", "scratch")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("keeping existing merge-gate.toml", res.stdout)
        res = self.stamp("--name", "scratch", "--apps", "admin", "--force-config")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        keys = sh(self.target, "python3", "scripts/mergegate_config.py", "keys", "gates.frontend")
        self.assertEqual(keys.stdout.split(), ["admin"])

    def test_whitespace_inside_an_apps_entry_is_rejected(self):
        res = self.stamp("--name", "scratch", "--apps", "web, admin")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("whitespace", res.stderr)
        self.assertFalse((self.target / "merge-gate.toml").exists())

    def test_empty_apps_warns_and_keeps_shipped_config(self):
        res = self.stamp("--name", "scratch", "--apps", "")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("warning", res.stderr)
        keys = sh(self.target, "python3", "scripts/mergegate_config.py", "keys", "gates.frontend")
        self.assertIn("example-app", keys.stdout.split())

    def test_in_place_without_apps_needs_no_flag(self):
        # In-place use after "Use this template": a clone of this repo.
        clone = self.target.parent / "clone"
        subprocess.run(["git", "clone", "-q", str(REPO), str(clone)], check=True)
        res = sh(clone, "bash", "bootstrap.sh", "--name", "in-place")
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual((clone / "README.md").read_text().splitlines()[0], "# in-place")
        res = sh(clone, "bash", "bootstrap.sh", "--name", "in-place", "--apps", "web")
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("--force-config", res.stderr)


if __name__ == "__main__":
    unittest.main()
