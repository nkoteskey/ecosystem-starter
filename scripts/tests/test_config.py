"""Unit tests for scripts/mergegate_config.py (the merge-gate.toml loader).

Stdlib `unittest` only, no network.

Run:
    python3 -m unittest scripts/tests/test_config.py -v
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parent.parent / "mergegate_config.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mergegate_config_mod", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mc = _load_module()


class TestParser(unittest.TestCase):
    def test_tables_strings_arrays_and_comments(self):
        text = (
            "# leading comment\n"
            "schema = 1\n"
            "[repo]\n"
            'remote = "upstream"  # trailing comment\n'
            "[gates.frontend.web]\n"
            'dir = "apps/web"\n'
            "test_blocking = true\n"
            "[claims]\n"
            'app_crates = ["app-a", "app-b",\n'
            '  "app-c",  # multi-line, trailing comma\n'
            "]\n"
            "min_consumers = 2\n"
            "default_ttl_hours = 8.5\n"
        )
        parsed = mc.parse_toml_subset(text)
        self.assertEqual(parsed["schema"], 1)
        self.assertEqual(parsed["repo"]["remote"], "upstream")
        self.assertEqual(parsed["gates"]["frontend"]["web"]["dir"], "apps/web")
        self.assertIs(parsed["gates"]["frontend"]["web"]["test_blocking"], True)
        self.assertEqual(parsed["claims"]["app_crates"], ["app-a", "app-b", "app-c"])
        self.assertEqual(parsed["claims"]["min_consumers"], 2)
        self.assertEqual(parsed["claims"]["default_ttl_hours"], 8.5)

    def test_literal_strings_keep_backslashes(self):
        text = "[invariants.checks.x]\npattern = '(\\bfoo\\b|\\s+bar)'\n"
        parsed = mc.parse_toml_subset(text)
        self.assertEqual(parsed["invariants"]["checks"]["x"]["pattern"], "(\\bfoo\\b|\\s+bar)")

    def test_basic_string_escapes(self):
        text = 'a = "say \\"hi\\"\\\\n"\n'
        parsed = mc.parse_toml_subset(text)
        self.assertEqual(parsed["a"], 'say "hi"\\n')

    def test_hash_inside_string_is_not_a_comment(self):
        text = 'source = "issue #12 — the # is inside a string"\n'
        parsed = mc.parse_toml_subset(text)
        self.assertEqual(parsed["source"], "issue #12 — the # is inside a string")

    def test_table_order_is_preserved(self):
        text = "[gates.pre.zeta]\ncommand = 'z'\n[gates.pre.alpha]\ncommand = 'a'\n"
        parsed = mc.parse_toml_subset(text)
        self.assertEqual(list(parsed["gates"]["pre"].keys()), ["zeta", "alpha"])

    def test_rejects_unsupported_syntax(self):
        for bad in (
            "a = {x = 1}\n",
            "a.b = 1\n",
            "[[tables]]\n",
            "a = 1\na = 2\n",
            "a = 2026-01-01\n",
            "a = [[1]]\n",
            'a = "unterminated\n',
            "a = 1 extra\n",
        ):
            with self.assertRaises(mc.ConfigError, msg=bad):
                mc.parse_toml_subset(bad)


class TestDefaultsAndValidation(unittest.TestCase):
    def test_empty_override_yields_defaults(self):
        cfg = mc.config_from_dict({})
        self.assertEqual(cfg["repo"]["remote"], "origin")
        self.assertEqual(cfg["claims"]["app_crates"], [])
        self.assertEqual(cfg["gates"]["frontend"], {})

    def test_named_tables_get_per_entry_defaults(self):
        cfg = mc.config_from_dict({"gates": {"frontend": {"web": {"dir": "apps/web"}}}})
        web = cfg["gates"]["frontend"]["web"]
        self.assertEqual(web["install"], "npm ci")
        self.assertIs(web["test_blocking"], False)

    def test_validation_catches_bad_values(self):
        cases = [
            {"schema": 2},
            {"repo": {"remote": "../evil"}},
            {"repo": {"main_branch": "refs/heads/main"}},
            {"repo": {"baseline_file": "/abs/path.json"}},
            {"claims": {"ref_prefix": "claims/"}},
            {"claims": {"min_consumers": 0}},
            {"gates": {"frontend": {"web": {"dir": "../outside"}}}},
            {"gates": {"extra": {"x": {}}}},
            {"gates": {"fuzz": {"dir": "fuzz"}}},
            {"invariants": {"checks": {"x": {"pattern": "(", "paths": ["**/*.rs"]}}}},
            {"standards": {"checks": {"x": {"tier": "maybe", "pattern": "a"}}}},
        ]
        for override in cases:
            with self.assertRaises(mc.ConfigError, msg=json.dumps(override)):
                mc.config_from_dict(override)

    def test_get_path_and_format_value(self):
        cfg = mc.config_from_dict({"claims": {"app_crates": ["a", "b"]}})
        found, value = mc.get_path(cfg, "claims.app_crates")
        self.assertTrue(found)
        self.assertEqual(mc.format_value(value), "a\nb")
        self.assertEqual(mc.format_value(True), "true")
        found, _ = mc.get_path(cfg, "claims.nope")
        self.assertFalse(found)


class TestCli(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "merge-gate.toml").write_text(
            "schema = 1\n[repo]\nremote = 'upstream'\n"
            "[gates.frontend.web]\ndir = 'apps/web'\n"
            "[gates.frontend.admin]\ndir = 'apps/admin'\n"
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(MODULE_PATH), *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )

    def test_get_keys_validate(self):
        res = self.run_cli("get", "repo.remote")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.strip(), "upstream")

        res = self.run_cli("get", "repo.nope", "--default", "fallback")
        self.assertEqual(res.stdout.strip(), "fallback")

        res = self.run_cli("get", "repo.nope")
        self.assertEqual(res.returncode, 1)

        res = self.run_cli("keys", "gates.frontend")
        self.assertEqual(res.stdout.split(), ["web", "admin"])

        res = self.run_cli("validate")
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_invalid_file_fails_validate(self):
        (self.repo / "merge-gate.toml").write_text("schema = 1\n[repo]\nremote = 'a b'\n")
        res = self.run_cli("validate")
        self.assertEqual(res.returncode, 1)
        self.assertIn("repo.remote", res.stderr)


if __name__ == "__main__":
    unittest.main()
