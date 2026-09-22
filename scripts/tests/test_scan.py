"""Unit tests for scripts/mergegate_scan.py (the shared regex/glob scanner).

Stdlib `unittest` only, no network. Each test builds a small real git
repo so `git ls-files --others` sees the fixture files.

Run:
    python3 -m unittest scripts/tests/test_scan.py -v
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCAN_PY = Path(__file__).resolve().parent.parent / "mergegate_scan.py"


class ScanCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name).resolve()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, rel: str, content: str) -> None:
        p = self.repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)

    def scan(self, *args):
        return subprocess.run(
            [sys.executable, str(SCAN_PY), *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
        )

    def test_globs_excludes_and_line_numbers(self):
        self.write("crates/a/src/lib.rs", "fn a() {}\nlet x = forbidden();\n")
        self.write("crates/a/tests/t.rs", "forbidden();\n")
        self.write("apps/b/src-tauri/src/main.rs", "forbidden();\n")
        self.write("apps/b/src/index.ts", "forbidden();\n")
        self.write("docs/x.md", "forbidden\n")
        res = self.scan(
            "--pattern",
            r"forbidden\(",
            "--root",
            "crates",
            "--root",
            "apps",
            "--glob",
            "crates/**/*.rs",
            "--glob",
            "apps/**/src-tauri/**/*.rs",
            "--exclude",
            "**/tests/**",
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        lines = sorted(res.stdout.splitlines())
        self.assertEqual(
            lines,
            [
                "apps/b/src-tauri/src/main.rs:1:forbidden();",
                "crates/a/src/lib.rs:2:let x = forbidden();",
            ],
        )

    def test_ignored_files_are_skipped(self):
        self.write(".gitignore", "target/\n")
        self.write("crates/a/target/gen.rs", "forbidden();\n")
        self.write("crates/a/src/lib.rs", "ok\n")
        res = self.scan("--pattern", "forbidden", "--root", "crates", "--glob", "**/*.rs")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")

    def test_bad_pattern_is_a_scan_failure_not_a_clean_result(self):
        res = self.scan("--pattern", "(", "--root", "crates", "--glob", "**/*.rs")
        self.assertEqual(res.returncode, 2)
        self.assertIn("does not compile", res.stderr)

    def test_missing_root_is_simply_empty(self):
        res = self.scan("--pattern", "x", "--root", "nope", "--glob", "**/*")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout, "")


if __name__ == "__main__":
    unittest.main()
