#!/usr/bin/env python3
"""mergegate_config.py — loads and validates `merge-gate.toml`.

Every other script in this directory reads its repository-specific values
(app directories, crate roots, gate steps, grep invariants, remote name)
through this module, so the scripts themselves stay generic. The schema is
documented in `docs/CONFIG.md`; `DEFAULTS` below is the authoritative list
of keys and their fallback values.

The file is always `<repo-root>/merge-gate.toml`. There is deliberately no
environment-variable override for its location: `land.sh` reads the remote
it pushes to from this file, and an override would let a stray variable
point a landing at a different remote.

Stdlib only, and no `tomllib` (it needs Python 3.11; the macOS system
`python3` is 3.9 and CI runs the suite on 3.9). The parser below accepts
the TOML subset the config uses:

- `[table]` and `[table.sub.table]` headers (bare keys only);
- `key = value` with a bare key;
- basic strings (`"..."`, escapes `\\"` `\\\\` `\\n` `\\t`), literal
  strings (`'...'`, no escapes — use these for regular expressions);
- integers, floats, `true`/`false`;
- arrays of those scalars, single- or multi-line, trailing comma allowed;
- `#` comments.

Inline tables, dotted keys, arrays of tables, dates and multi-line strings
are not supported and are reported as errors rather than silently ignored.

CLI (used by the shell scripts):

    mergegate_config.py get <dotted.key> [--default <value>]
    mergegate_config.py keys <dotted.table>
    mergegate_config.py validate
    mergegate_config.py dump

`get` prints a scalar on one line, or an array one element per line, and
exits 1 when the key is absent and no `--default` was given. `keys` prints
the sub-table names of a table, one per line, in file order.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONFIG_FILENAME = "merge-gate.toml"
SCHEMA_VERSION = 1

DEFAULTS: Dict[str, Any] = {
    "schema": SCHEMA_VERSION,
    "repo": {
        "remote": "origin",
        "main_branch": "main",
        "train_prefix": "train/local-",
        "status_context": "full gate (scripts/full-gate.sh)",
        "baseline_file": "ci/baseline.json",
        "results_dir": "ci-results",
        "hooks_path": "scripts/git-hooks",
    },
    "worktree": {
        "root": "",
        "branch_prefix": "wt/",
        "bootstrap_copy": [],
        "bootstrap_npm_dirs": [],
    },
    "claims": {
        "dir": "claims",
        "ref_prefix": "refs/claims/",
        "app_crates": [],
        "min_consumers": 2,
        "default_ttl_hours": 8,
        "exempt_actors": ["dependabot[bot]"],
        "docs_prefixes": ["docs/"],
        "docs_suffixes": [".md"],
        "lockfile_names": ["Cargo.lock", "package-lock.json"],
    },
    "affected": {
        "crates_dir": "crates",
        "apps_dir": "apps",
        "app_rust_subdir": "src-tauri",
        "frontend_apps": [],
        "frontend_src_subdir": "src",
        "mobile_dir": "",
        "workspace_files": ["Cargo.toml", "Cargo.lock", "rust-toolchain.toml"],
        "everything_prefixes": [".github/", "scripts/"],
        "everything_exclude_prefixes": ["scripts/tests/"],
        "integration_paths": [],
        "packages": {},
    },
    "adr": {
        "dir": "adr",
        "ignored_basenames": ["OPEN-QUESTIONS.md", "README.md", "TEMPLATE.md", "0000-template.md"],
        "nonsubstantive_marker": "[adr-nonsubstantive]",
        "section_headings": [
            "## Context",
            "## Decision Drivers",
            "## Considered Options",
            "## Decision",
            "## Consequences",
        ],
    },
    "baseline": {
        "retry_suite_prefix": "cargo:",
    },
    "gates": {
        "rust": True,
        "cargo_fmt": True,
        "cargo_clippy_args": ["--all-targets", "--all-features", "--", "-D", "warnings"],
        "cargo_test_args": ["--all", "--no-fail-fast"],
        "scripts_selftest": True,
        "standards": True,
        "adr": True,
        "invariants": True,
        "claims": True,
        "pre": {},
        "frontend": {},
        "extra": {},
        "fuzz": {"dir": "", "nightly": "", "targets": []},
    },
    "invariants": {
        "roots": ["crates", "apps"],
        "allowlist": "scripts/invariants-allowlist.txt",
        "checks": {},
    },
    "standards": {
        "ts_root": "apps",
        "ts_globs": ["**/src/**/*.ts", "**/src/**/*.tsx"],
        "rust_roots": ["apps", "crates"],
        "rust_globs": ["**/src-tauri/src/**/*.rs", "crates/**/src/**/*.rs"],
        "allowlist": "scripts/standards-allowlist.txt",
        "checks": {},
    },
}

FRONTEND_DEFAULTS: Dict[str, Any] = {
    "dir": "",
    "install": "npm ci",
    "typecheck": "npx --no-install tsc --noEmit",
    "build": "npm run build",
    "test": 'npx --no-install vitest run --reporter=json --outputFile="$MERGE_GATE_OUT"',
    "test_blocking": False,
}

EXTRA_DEFAULTS: Dict[str, Any] = {
    "command": "",
    "blocking": True,
    "cargo_log": False,
}

INVARIANT_DEFAULTS: Dict[str, Any] = {
    "pattern": "",
    "paths": [],
    "excludes": [],
    "description": "",
}

STANDARD_DEFAULTS: Dict[str, Any] = {
    "tier": "fail",
    "scope": "ts",
    "pattern": "",
    "message": "",
}


class ConfigError(Exception):
    """Raised for a malformed or invalid `merge-gate.toml`."""


# ── TOML subset parser ───────────────────────────────────────────────────────

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_INT_RE = re.compile(r"^[+-]?[0-9](?:_?[0-9])*$")
_FLOAT_RE = re.compile(r"^[+-]?[0-9](?:_?[0-9])*\.[0-9](?:_?[0-9])*$")


class _Reader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.line = 1

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def advance(self, n: int = 1) -> str:
        chunk = self.text[self.pos : self.pos + n]
        self.line += chunk.count("\n")
        self.pos += n
        return chunk

    def eof(self) -> bool:
        return self.pos >= len(self.text)

    def skip_ws_and_comments(self, newlines: bool) -> None:
        while not self.eof():
            c = self.peek()
            if c in " \t\r" or (c == "\n" and newlines):
                self.advance()
            elif c == "#":
                while not self.eof() and self.peek() != "\n":
                    self.advance()
            else:
                break

    def error(self, msg: str) -> ConfigError:
        return ConfigError(f"line {self.line}: {msg}")


def _parse_basic_string(r: _Reader) -> str:
    r.advance()  # opening quote
    out: List[str] = []
    escapes = {'"': '"', "\\": "\\", "n": "\n", "t": "\t", "r": "\r"}
    while True:
        if r.eof():
            raise r.error("unterminated string")
        c = r.advance()
        if c == '"':
            return "".join(out)
        if c == "\n":
            raise r.error("newline inside a basic string")
        if c == "\\":
            e = r.advance()
            if e not in escapes:
                raise r.error(f"unsupported escape '\\{e}'")
            out.append(escapes[e])
        else:
            out.append(c)


def _parse_literal_string(r: _Reader) -> str:
    r.advance()  # opening quote
    out: List[str] = []
    while True:
        if r.eof():
            raise r.error("unterminated literal string")
        c = r.advance()
        if c == "'":
            return "".join(out)
        if c == "\n":
            raise r.error("newline inside a literal string")
        out.append(c)


def _parse_bare_scalar(r: _Reader) -> Any:
    start = r.pos
    while not r.eof() and r.peek() not in " \t\r\n,]#":
        r.advance()
    token = r.text[start : r.pos]
    if token == "true":
        return True
    if token == "false":
        return False
    if _INT_RE.match(token):
        return int(token.replace("_", ""))
    if _FLOAT_RE.match(token):
        return float(token.replace("_", ""))
    raise r.error(
        f"unsupported value '{token}' (dates, inline tables and bare words are not allowed)"
    )


def _parse_value(r: _Reader) -> Any:
    c = r.peek()
    if c == '"':
        return _parse_basic_string(r)
    if c == "'":
        return _parse_literal_string(r)
    if c == "[":
        return _parse_array(r)
    if c == "{":
        raise r.error("inline tables are not supported")
    if c == "":
        raise r.error("missing value")
    return _parse_bare_scalar(r)


def _parse_array(r: _Reader) -> List[Any]:
    r.advance()  # [
    items: List[Any] = []
    while True:
        r.skip_ws_and_comments(newlines=True)
        if r.eof():
            raise r.error("unterminated array")
        if r.peek() == "]":
            r.advance()
            return items
        value = _parse_value(r)
        if isinstance(value, list):
            raise r.error("nested arrays are not supported")
        items.append(value)
        r.skip_ws_and_comments(newlines=True)
        if r.peek() == ",":
            r.advance()
        elif r.peek() == "]":
            r.advance()
            return items
        else:
            raise r.error("expected ',' or ']' in array")


def _parse_table_header(r: _Reader) -> List[str]:
    r.advance()  # [
    if r.peek() == "[":
        raise r.error("arrays of tables ([[...]]) are not supported")
    start = r.pos
    while not r.eof() and r.peek() != "]":
        if r.peek() == "\n":
            raise r.error("unterminated table header")
        r.advance()
    if r.eof():
        raise r.error("unterminated table header")
    raw = r.text[start : r.pos]
    r.advance()  # ]
    parts = [p.strip() for p in raw.split(".")]
    for p in parts:
        if not _BARE_KEY_RE.match(p):
            raise r.error(f"invalid table name segment '{p}' (bare keys only)")
    return parts


def parse_toml_subset(text: str) -> Dict[str, Any]:
    """Parse the supported TOML subset into nested dicts (file order kept)."""
    root: Dict[str, Any] = {}
    current = root
    r = _Reader(text)
    while True:
        r.skip_ws_and_comments(newlines=True)
        if r.eof():
            return root
        if r.peek() == "[":
            parts = _parse_table_header(r)
            node = root
            for p in parts:
                nxt = node.get(p)
                if nxt is None:
                    nxt = {}
                    node[p] = nxt
                elif not isinstance(nxt, dict):
                    raise r.error(f"'{'.'.join(parts)}' is already a value, not a table")
                node = nxt
            current = node
            r.skip_ws_and_comments(newlines=False)
            if not r.eof() and r.peek() != "\n":
                raise r.error("unexpected text after table header")
            continue
        # key = value
        start = r.pos
        while not r.eof() and r.peek() not in " \t=\n":
            r.advance()
        key = r.text[start : r.pos]
        if not _BARE_KEY_RE.match(key):
            raise r.error(f"invalid key '{key}' (bare keys only; dotted keys are not supported)")
        r.skip_ws_and_comments(newlines=False)
        if r.peek() != "=":
            raise r.error(f"expected '=' after key '{key}'")
        r.advance()
        r.skip_ws_and_comments(newlines=False)
        value = _parse_value(r)
        if key in current:
            raise r.error(f"duplicate key '{key}'")
        current[key] = value
        r.skip_ws_and_comments(newlines=False)
        if not r.eof() and r.peek() != "\n":
            raise r.error(f"unexpected text after value for '{key}'")


# ── loading + merging ────────────────────────────────────────────────────────


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _apply_named_defaults(cfg: Dict[str, Any]) -> None:
    for name, tbl in cfg["gates"]["frontend"].items():
        cfg["gates"]["frontend"][name] = _deep_merge(FRONTEND_DEFAULTS, tbl)
    for name, tbl in cfg["gates"]["extra"].items():
        cfg["gates"]["extra"][name] = _deep_merge(EXTRA_DEFAULTS, tbl)
    for name, tbl in cfg["gates"]["pre"].items():
        cfg["gates"]["pre"][name] = _deep_merge({"command": ""}, tbl)
    for name, tbl in cfg["invariants"]["checks"].items():
        cfg["invariants"]["checks"][name] = _deep_merge(INVARIANT_DEFAULTS, tbl)
    for name, tbl in cfg["standards"]["checks"].items():
        cfg["standards"]["checks"][name] = _deep_merge(STANDARD_DEFAULTS, tbl)
    for name, tbl in cfg["affected"]["packages"].items():
        cfg["affected"]["packages"][name] = _deep_merge(
            {"path": "", "affects_all_frontends": False}, tbl
        )


def repo_root(cwd: Optional[Path] = None) -> Path:
    res = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        raise ConfigError("not inside a git repository")
    return Path(res.stdout.strip())


def config_from_dict(file_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults merged with an already-parsed override dict, validated.
    Used by tests and by tools that build a config in memory."""
    cfg = _deep_merge(DEFAULTS, file_cfg)
    _apply_named_defaults(cfg)
    problems = validate(cfg)
    if problems:
        raise ConfigError("; ".join(problems))
    return cfg


def load_config(root: Optional[Path] = None) -> Dict[str, Any]:
    """Defaults merged with `<root>/merge-gate.toml` (if present), validated."""
    if root is None:
        root = repo_root()
    path = Path(root) / CONFIG_FILENAME
    file_cfg: Dict[str, Any] = {}
    if path.exists():
        try:
            file_cfg = parse_toml_subset(path.read_text())
        except ConfigError as e:
            raise ConfigError(f"{path}: {e}") from e
    try:
        return config_from_dict(file_cfg)
    except ConfigError as e:
        raise ConfigError(f"{path}: {e}") from e


# ── validation ───────────────────────────────────────────────────────────────

_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REL_PATH_RE = re.compile(r"^(?!/)(?!.*(^|/)\.\.(/|$)).*$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_RESERVED_REF_NAMESPACES = ("refs/heads/", "refs/tags/", "refs/remotes/", "refs/notes/")


def _git_check_ref_format(*args: str) -> bool:
    res = subprocess.run(["git", "check-ref-format", *args], capture_output=True, text=True)
    return res.returncode == 0


def _valid_branch(name: str) -> bool:
    return bool(name) and not name.startswith("-") and _git_check_ref_format("--branch", name)


def _valid_ref(name: str) -> bool:
    return bool(name) and not name.startswith("-") and _git_check_ref_format(name)


def _valid_worktree_root(value: str) -> bool:
    if value == "":
        return True
    if not (value.startswith("~/") or value.startswith("/")):
        return False
    return not any(part == ".." for part in value.split("/"))


def _rel_nonempty(value: str) -> bool:
    return bool(value) and bool(_REL_PATH_RE.match(value))


def _expect(problems: List[str], cond: bool, msg: str) -> None:
    if not cond:
        problems.append(msg)


def _is_str_list(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def validate(cfg: Dict[str, Any]) -> List[str]:
    """Return a list of problems (empty when the config is usable)."""
    p: List[str] = []
    _expect(p, cfg.get("schema") == SCHEMA_VERSION, f"schema must be {SCHEMA_VERSION}")

    repo = cfg["repo"]
    _expect(
        p, bool(_SAFE_NAME_RE.match(str(repo["remote"]))), "repo.remote is not a plain remote name"
    )
    _expect(p, bool(str(repo["main_branch"])), "repo.main_branch must be set")
    _expect(p, "/" not in str(repo["main_branch"]), "repo.main_branch must not contain '/'")
    _expect(
        p,
        _valid_branch(str(repo["main_branch"])),
        "repo.main_branch is not a valid branch name (git check-ref-format)",
    )
    _expect(
        p,
        _valid_ref(f"refs/heads/{repo['train_prefix']}x"),
        "repo.train_prefix is not a valid branch-name prefix (git check-ref-format)",
    )
    _expect(
        p,
        0 < len(str(repo["status_context"])) <= 255
        and not _CONTROL_RE.search(str(repo["status_context"])),
        "repo.status_context must be one non-empty line (at most 255 characters)",
    )
    _expect(
        p,
        str(repo["train_prefix"]).endswith("-") or str(repo["train_prefix"]).endswith("/"),
        "repo.train_prefix should end with '-' or '/'",
    )
    _expect(p, not str(repo["train_prefix"]).startswith("/"), "repo.train_prefix must be relative")
    for key in ("baseline_file", "results_dir", "hooks_path"):
        _expect(p, bool(_REL_PATH_RE.match(str(repo[key]))), f"repo.{key} must be a relative path")

    wt = cfg["worktree"]
    _expect(p, str(wt["branch_prefix"]).endswith("/"), "worktree.branch_prefix must end with '/'")
    _expect(
        p,
        _valid_ref(f"refs/heads/{wt['branch_prefix']}x"),
        "worktree.branch_prefix is not a valid branch-name prefix (git check-ref-format)",
    )
    _expect(
        p,
        _valid_worktree_root(str(wt["root"])),
        "worktree.root must be empty, absolute, or start with '~/', and contain no '..'",
    )
    _expect(p, _is_str_list(wt["bootstrap_copy"]), "worktree.bootstrap_copy must be a string array")
    _expect(
        p,
        _is_str_list(wt["bootstrap_npm_dirs"]),
        "worktree.bootstrap_npm_dirs must be a string array",
    )

    cl = cfg["claims"]
    _expect(
        p,
        str(cl["ref_prefix"]).startswith("refs/") and str(cl["ref_prefix"]).endswith("/"),
        "claims.ref_prefix must look like 'refs/<name>/'",
    )
    _expect(
        p,
        not any(str(cl["ref_prefix"]).startswith(r) for r in _RESERVED_REF_NAMESPACES),
        "claims.ref_prefix must not be under refs/heads, refs/tags, refs/remotes or refs/notes",
    )
    _expect(
        p,
        _valid_ref(f"{cl['ref_prefix']}x"),
        "claims.ref_prefix is not a valid ref namespace (git check-ref-format)",
    )
    _expect(p, _rel_nonempty(str(cl["dir"])), "claims.dir must be a non-empty relative path")
    _expect(p, _is_str_list(cl["app_crates"]), "claims.app_crates must be a string array")
    _expect(
        p,
        isinstance(cl["min_consumers"], int) and cl["min_consumers"] >= 1,
        "claims.min_consumers must be an integer >= 1",
    )
    _expect(
        p,
        isinstance(cl["default_ttl_hours"], (int, float)) and cl["default_ttl_hours"] > 0,
        "claims.default_ttl_hours must be a positive number",
    )

    af = cfg["affected"]
    _expect(p, _is_str_list(af["frontend_apps"]), "affected.frontend_apps must be a string array")
    for name, tbl in af["packages"].items():
        _expect(p, bool(_SAFE_NAME_RE.match(name)), f"affected.packages.{name}: bad name")
        _expect(
            p,
            bool(tbl["path"]) and bool(_REL_PATH_RE.match(tbl["path"])),
            f"affected.packages.{name}.path must be a relative path",
        )

    adr = cfg["adr"]
    _expect(p, bool(_REL_PATH_RE.match(str(adr["dir"]))), "adr.dir must be a relative path")
    _expect(
        p,
        _is_str_list(adr["section_headings"]) and len(adr["section_headings"]) > 0,
        "adr.section_headings must be a non-empty string array",
    )

    gates = cfg["gates"]
    _expect(
        p, _is_str_list(gates["cargo_test_args"]), "gates.cargo_test_args must be a string array"
    )
    _expect(
        p,
        _is_str_list(gates["cargo_clippy_args"]),
        "gates.cargo_clippy_args must be a string array",
    )
    for name, tbl in gates["frontend"].items():
        _expect(p, bool(_SAFE_NAME_RE.match(name)), f"gates.frontend.{name}: bad name")
        _expect(
            p,
            bool(tbl["dir"]) and bool(_REL_PATH_RE.match(tbl["dir"])),
            f"gates.frontend.{name}.dir must be a relative path",
        )
        _expect(
            p,
            isinstance(tbl["test_blocking"], bool),
            f"gates.frontend.{name}.test_blocking must be a boolean",
        )
    for kind in ("pre", "extra"):
        for name, tbl in gates[kind].items():
            _expect(p, bool(_SAFE_NAME_RE.match(name)), f"gates.{kind}.{name}: bad name")
            _expect(p, bool(tbl["command"]), f"gates.{kind}.{name}.command must be set")
    fuzz = gates["fuzz"]
    _expect(p, _is_str_list(fuzz["targets"]), "gates.fuzz.targets must be a string array")
    if fuzz["dir"] or fuzz["targets"]:
        _expect(
            p,
            bool(fuzz["dir"]) and bool(fuzz["nightly"]) and bool(fuzz["targets"]),
            "gates.fuzz needs dir, nightly and targets together",
        )

    inv = cfg["invariants"]
    _expect(
        p,
        _is_str_list(inv["roots"]) and len(inv["roots"]) > 0,
        "invariants.roots must be non-empty",
    )
    _expect(
        p,
        _rel_nonempty(str(inv["allowlist"])),
        "invariants.allowlist must be a non-empty relative path",
    )
    for name, tbl in inv["checks"].items():
        _expect(p, bool(_SAFE_NAME_RE.match(name)), f"invariants.checks.{name}: bad name")
        _expect(p, bool(tbl["pattern"]), f"invariants.checks.{name}.pattern must be set")
        _expect(
            p,
            _is_str_list(tbl["paths"]) and len(tbl["paths"]) > 0,
            f"invariants.checks.{name}.paths must be non-empty",
        )
        try:
            re.compile(tbl["pattern"])
        except re.error as e:
            p.append(f"invariants.checks.{name}.pattern does not compile: {e}")

    st = cfg["standards"]
    _expect(
        p,
        _rel_nonempty(str(st["allowlist"])),
        "standards.allowlist must be a non-empty relative path",
    )
    for name, tbl in st["checks"].items():
        _expect(p, bool(_SAFE_NAME_RE.match(name)), f"standards.checks.{name}: bad name")
        _expect(
            p,
            tbl["tier"] in ("fail", "warn"),
            f"standards.checks.{name}.tier must be 'fail' or 'warn'",
        )
        _expect(
            p,
            tbl["scope"] in ("ts", "rust"),
            f"standards.checks.{name}.scope must be 'ts' or 'rust'",
        )
        _expect(p, bool(tbl["pattern"]), f"standards.checks.{name}.pattern must be set")
    return p


# ── access helpers ───────────────────────────────────────────────────────────


def get_path(cfg: Dict[str, Any], dotted: str) -> Tuple[bool, Any]:
    node: Any = cfg
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "\n".join(format_value(v) for v in value)
    if isinstance(value, dict):
        return "\n".join(value.keys())
    return str(value)


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    p_get = sub.add_parser("get", help="print one value (arrays one element per line)")
    p_get.add_argument("key")
    p_get.add_argument("--default", default=None)

    p_keys = sub.add_parser("keys", help="print the sub-table names of a table")
    p_keys.add_argument("table")

    sub.add_parser("validate", help="exit 0 if merge-gate.toml is usable")
    sub.add_parser("dump", help="print the merged config as JSON")

    args = ap.parse_args(argv)
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"merge-gate.toml: {e}", file=sys.stderr)
        return 1

    if args.command == "validate":
        print("merge-gate.toml: OK")
        return 0
    if args.command == "dump":
        print(json.dumps(cfg, indent=2, sort_keys=True))
        return 0
    if args.command == "keys":
        found, value = get_path(cfg, args.table)
        if not found or not isinstance(value, dict):
            return 0
        for k in value:
            print(k)
        return 0
    found, value = get_path(cfg, args.key)
    if not found:
        if args.default is None:
            print(f"merge-gate.toml: key '{args.key}' not found", file=sys.stderr)
            return 1
        print(args.default)
        return 0
    out = format_value(value)
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
