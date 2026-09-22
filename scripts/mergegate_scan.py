#!/usr/bin/env python3
"""mergegate_scan.py — regex scan over tracked (and untracked, non-ignored)
files selected by `**`-aware globs. Shared by `invariants-check.sh` and
`standards-check.sh` so both use one regex dialect (Python `re`, the same
one `mergegate_config.py` validates config patterns with) on every host.

Usage:
    mergegate_scan.py --pattern <regex> [--ignore-case]
                      --root <dir>... --glob <glob>... [--exclude <glob>...]

Prints `path:line:text` for every matching line, path relative to the
repository root. Exit 0 whether or not anything matched; exit 2 on a
usage or I/O error (so a caller can tell "nothing found" from "the scan
itself failed" — a broken pattern must never read as a clean tree).

Glob semantics: `**` matches across `/`, `*` and `?` stay within one path
segment, everything else is literal. A glob is matched against the whole
repo-relative path.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional


def glob_to_regex(pattern: str) -> str:
    out = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*" and i + 1 < n and pattern[i + 1] == "*":
            i += 2
            if i < n and pattern[i] == "/":
                i += 1
                out.append("(?:.*/)?")
            else:
                out.append(".*")
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(c))
            i += 1
    return "^" + "".join(out) + "$"


def list_files(root: Path, dirs: List[str]) -> List[str]:
    existing = [d for d in dirs if (root / d).exists()]
    if not existing:
        return []
    proc = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *existing],
        cwd=str(root),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace").strip())
    return [p for p in proc.stdout.decode(errors="replace").split("\0") if p]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pattern", required=True)
    ap.add_argument("--ignore-case", action="store_true")
    ap.add_argument("--root", action="append", required=True)
    ap.add_argument("--glob", action="append", required=True)
    ap.add_argument("--exclude", action="append", default=[])
    args = ap.parse_args(argv)

    try:
        rx = re.compile(args.pattern, re.IGNORECASE if args.ignore_case else 0)
    except re.error as e:
        print(f"mergegate_scan: pattern does not compile: {e}", file=sys.stderr)
        return 2

    top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if top.returncode != 0:
        print("mergegate_scan: not inside a git repository", file=sys.stderr)
        return 2
    root = Path(top.stdout.strip())

    includes = [re.compile(glob_to_regex(g)) for g in args.glob]
    excludes = [re.compile(glob_to_regex(g)) for g in args.exclude]

    try:
        candidates = list_files(root, args.root)
    except RuntimeError as e:
        print(f"mergegate_scan: git ls-files failed: {e}", file=sys.stderr)
        return 2

    for rel in sorted(set(candidates)):
        if not any(rx_i.match(rel) for rx_i in includes):
            continue
        if any(rx_e.match(rel) for rx_e in excludes):
            continue
        path = root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError as e:
            print(f"mergegate_scan: cannot read {rel}: {e}", file=sys.stderr)
            return 2
        for lineno, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                print(f"{rel}:{lineno}:{line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
