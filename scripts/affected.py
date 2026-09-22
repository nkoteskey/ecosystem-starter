#!/usr/bin/env python3
"""affected.py — compute the affected crate / app / frontend surface for a diff.

The fast CI lane needs to know, for a given base..head range, which Cargo
crates need `clippy -p <affected> -D warnings` + `cargo test -p <affected>`,
and which frontend packages need their build+test steps. This is the single
source of truth for that mapping — a CI `changes` job (via
`--github-output`) and any local invocation (`--json`) use it.

Stdlib only. Requires a `cargo` on PATH unless `--metadata-json` points at a
pre-computed `cargo metadata --no-deps --format-version 1` dump (used by the
test suite and for fully offline runs).

Usage:
    python3 scripts/affected.py --base origin/main [--head HEAD] [--json]
    python3 scripts/affected.py --base origin/main --github-output

Rules (paths are configurable in merge-gate.toml `[affected]`):
  - <crates_dir>/<c>/**                     -> crate c (name resolved via cargo
                                               metadata, not the directory name)
  - <apps_dir>/<a>/<app_rust_subdir>/**     -> that app's Rust crate, for a in
                                               frontend_apps
  - workspace_files, or any other *.rs / Cargo.toml outside the two trees
    above                                    -> every workspace crate
  - affected crates = changed crates + the reverse-dependency closure over
    path dependencies of kind normal, build OR dev (a dev-dependency edge
    is included too — a test-only reverse dependency still breaks on a
    changed dependency's test run)
  - <apps_dir>/<a>/<frontend_src_subdir>/**, index.html, package.json,
    package-lock.json, and the postcss/tailwind/tsconfig/vite/vitest config
    files directly under <apps_dir>/<a>/  -> frontend a
  - packages.<p>.path/**                    -> frontend flag p (and every
                                               frontend app when
                                               affects_all_frontends = true)
  - <mobile_dir>/**                         -> frontend flag `mobile`
  - everything_prefixes (minus everything_exclude_prefixes) -> everything
  - docs prefixes/suffixes                  -> nothing (docs_only when the
                                               WHOLE diff is such files)
  - integration = any Rust affected, any frontend affected, OR a path in
    integration_paths touched
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mergegate_config

FRONTEND_CONFIG_RE = re.compile(
    r"^("
    r"index\.html"
    r"|package\.json"
    r"|package-lock\.json"
    r"|postcss\.config\..+"
    r"|tailwind\.config\..+"
    r"|tsconfig(\..+)?\.json"
    r"|vite\.config\..+"
    r"|vitest\.config\..+"
    r")$"
)


# ── git plumbing ─────────────────────────────────────────────────────────────


def _run(cmd: List[str], cwd: Optional[Path] = None) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"command failed ({' '.join(cmd)}): exit {proc.returncode}")
    return proc.stdout


def merge_base(base: str, head: str, cwd: Optional[Path] = None) -> str:
    return _run(["git", "merge-base", base, head], cwd=cwd).strip()


def changed_files(base: str, head: str, cwd: Optional[Path] = None) -> List[str]:
    mb = merge_base(base, head, cwd=cwd)
    out = _run(["git", "diff", "--name-only", f"{mb}..{head}"], cwd=cwd)
    return sorted(line for line in out.splitlines() if line.strip())


# ── cargo metadata plumbing ──────────────────────────────────────────────────


def load_metadata(metadata_json: Optional[str], cwd: Optional[Path] = None) -> dict:
    if metadata_json:
        return json.loads(Path(metadata_json).read_text())
    root = cwd or Path.cwd()
    if not (root / "Cargo.toml").exists():
        return {"workspace_root": str(root), "packages": []}
    out = _run(["cargo", "metadata", "--no-deps", "--format-version", "1"], cwd=cwd)
    return json.loads(out)


class Graph:
    def __init__(self, metadata: dict, affected_cfg: Dict[str, Any]):
        self.cfg = affected_cfg
        self.workspace_root = Path(metadata["workspace_root"])
        packages = metadata["packages"]
        self.dir_to_name: Dict[str, str] = {
            str(Path(p["manifest_path"]).parent): p["name"] for p in packages
        }
        self.names: Set[str] = set(self.dir_to_name.values())
        self.forward: Dict[str, Set[str]] = {n: set() for n in self.names}
        self.reverse: Dict[str, Set[str]] = {n: set() for n in self.names}
        for p in packages:
            name = p["name"]
            for dep in p.get("dependencies") or []:
                path = dep.get("path")
                if not path:
                    continue
                dep_name = self.dir_to_name.get(path)
                if not dep_name:
                    continue
                self.forward[name].add(dep_name)
                self.reverse.setdefault(dep_name, set()).add(name)

    def reverse_closure(self, seed: Set[str]) -> Set[str]:
        seen: Set[str] = set(seed)
        stack = list(seed)
        while stack:
            n = stack.pop()
            for dependent in self.reverse.get(n, ()):
                if dependent not in seen:
                    seen.add(dependent)
                    stack.append(dependent)
        return seen

    def crate_dir_map(self) -> Dict[str, str]:
        """<crates_dir>/<dirname> -> crate name."""
        out = {}
        crates_root = self.workspace_root / self.cfg["crates_dir"]
        for d, name in self.dir_to_name.items():
            try:
                rel = Path(d).relative_to(crates_root)
            except ValueError:
                continue
            if len(rel.parts) == 1:
                out[rel.parts[0]] = name
        return out

    def app_crate_dir_map(self) -> Dict[str, str]:
        """<apps_dir>/<a>/<app_rust_subdir> -> crate name, for a in frontend_apps."""
        out = {}
        for a in self.cfg["frontend_apps"]:
            d = str(self.workspace_root / self.cfg["apps_dir"] / a / self.cfg["app_rust_subdir"])
            if d in self.dir_to_name:
                out[a] = self.dir_to_name[d]
        return out


# ── classification ───────────────────────────────────────────────────────────


def _is_docs(f: str, claims_cfg: Dict[str, Any]) -> bool:
    return any(f.startswith(p) for p in claims_cfg["docs_prefixes"]) or any(
        f.endswith(s) for s in claims_cfg["docs_suffixes"]
    )


def _is_lockfile(f: str, claims_cfg: Dict[str, Any]) -> bool:
    return any(f == n or f.endswith("/" + n) for n in claims_cfg["lockfile_names"])


def _under(path_parts: List[str], prefix: str) -> bool:
    prefix_parts = [p for p in prefix.split("/") if p]
    return path_parts[: len(prefix_parts)] == prefix_parts


def analyze(files: List[str], graph: Graph, cfg: Dict[str, Any]) -> dict:
    af = cfg["affected"]
    cl = cfg["claims"]
    frontend_apps: List[str] = list(af["frontend_apps"])
    packages: Dict[str, Dict[str, Any]] = af["packages"]
    crate_dirmap = graph.crate_dir_map()
    app_dirmap = graph.app_crate_dir_map()

    changed_crates: Set[str] = set()
    rust_all = False
    fe_direct = {a: False for a in frontend_apps}
    fe_mobile = False
    fe_packages = {p: False for p in packages}
    everything = False
    integration_extra = False

    crates_dir = af["crates_dir"]
    apps_dir = af["apps_dir"]
    rust_subdir = af["app_rust_subdir"]
    src_subdir = af["frontend_src_subdir"]
    mobile_dir = af["mobile_dir"]

    for f in files:
        parts = f.split("/")
        matched = False

        if parts[0] == crates_dir and len(parts) >= 2:
            matched = True
            crate_name = crate_dirmap.get(parts[1])
            if crate_name:
                changed_crates.add(crate_name)

        elif (
            parts[0] == apps_dir
            and len(parts) >= 3
            and parts[1] in frontend_apps
            and parts[2] == rust_subdir
        ):
            matched = True
            crate_name = app_dirmap.get(parts[1])
            if crate_name:
                changed_crates.add(crate_name)

        elif mobile_dir and _under(parts, mobile_dir):
            matched = True
            fe_mobile = True

        elif parts[0] == apps_dir and len(parts) >= 2 and parts[1] in frontend_apps:
            a = parts[1]
            rest = parts[2:]
            is_src = bool(rest) and rest[0] == src_subdir
            is_config = len(rest) == 1 and FRONTEND_CONFIG_RE.match(rest[0]) is not None
            if is_src or is_config:
                matched = True
                fe_direct[a] = True

        if not matched:
            for pkg_name, pkg in packages.items():
                if _under(parts, pkg["path"]):
                    matched = True
                    fe_packages[pkg_name] = True
                    if pkg["affects_all_frontends"]:
                        for a in frontend_apps:
                            fe_direct[a] = True
                    break

        if f in af["integration_paths"] or any(
            _under(parts, p) for p in af["integration_paths"] if p.endswith("/")
        ):
            integration_extra = True
            matched = True

        if not matched:
            in_everything = any(_under(parts, p) for p in af["everything_prefixes"])
            excluded = any(_under(parts, p) for p in af["everything_exclude_prefixes"])
            if in_everything and not excluded:
                matched = True
                everything = True

        if not matched and f in af["workspace_files"]:
            matched = True
            rust_all = True

        if not matched and (f.endswith(".rs") or f.endswith("Cargo.toml")):
            # Any stray Rust source/manifest outside the two trees handled
            # above forces the whole workspace (a new top-level crate dir
            # not yet wired into crate_dirmap, an xtask, a fuzz dir).
            matched = True
            rust_all = True

    if everything:
        rust_all = True
        for a in frontend_apps:
            fe_direct[a] = True
        fe_mobile = True
        for p in fe_packages:
            fe_packages[p] = True

    crate_seed = set(graph.names) if rust_all else changed_crates
    closure = graph.reverse_closure(crate_seed) if crate_seed else set()

    rust = bool(closure)
    apps_out = {a: (app_dirmap.get(a) in closure) for a in frontend_apps}
    frontend_out: Dict[str, bool] = {a: fe_direct[a] for a in frontend_apps}
    if mobile_dir:
        frontend_out["mobile"] = fe_mobile
    for p in packages:
        frontend_out[p] = fe_packages[p]
    integration = rust or any(frontend_out.values()) or integration_extra

    docs_only = bool(files) and all(_is_docs(f, cl) for f in files)
    lockfile_only = bool(files) and all(_is_lockfile(f, cl) for f in files)

    return {
        "changed_files": files,
        "crates": sorted(closure),
        "crate_args": " ".join(f"-p {c}" for c in sorted(closure)),
        "rust": rust,
        "apps": apps_out,
        "frontend": frontend_out,
        "integration": integration,
        "docs_only": docs_only,
        "lockfile_only": lockfile_only,
    }


# ── output ────────────────────────────────────────────────────────────────


def _bool_str(b: bool) -> str:
    return "true" if b else "false"


def _key(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "_", name)


def github_output_lines(result: dict) -> List[str]:
    lines = [
        f"rust={_bool_str(result['rust'])}",
        f"crates={','.join(result['crates'])}",
        f"crate_args={result['crate_args']}",
    ]
    for a, v in result["apps"].items():
        lines.append(f"app_{_key(a)}={_bool_str(v)}")
    for a, v in result["frontend"].items():
        lines.append(f"fe_{_key(a)}={_bool_str(v)}")
    lines.append(f"integration={_bool_str(result['integration'])}")
    lines.append(f"docs_only={_bool_str(result['docs_only'])}")
    lines.append(f"lockfile_only={_bool_str(result['lockfile_only'])}")
    return lines


def write_github_output(result: dict, path: str) -> None:
    with open(path, "a") as fh:
        for line in github_output_lines(result):
            fh.write(line + "\n")


def print_summary(result: dict) -> None:
    n = len(result["changed_files"])
    print(f"affected: {n} changed file(s)", file=sys.stderr)
    print(
        f"  rust={result['rust']} crates=[{', '.join(result['crates']) or '-'}]",
        file=sys.stderr,
    )
    apps_on = [a for a, v in result["apps"].items() if v]
    print(f"  app crates affected: [{', '.join(apps_on) or '-'}]", file=sys.stderr)
    fe_on = [a for a, v in result["frontend"].items() if v]
    print(f"  frontend affected: [{', '.join(fe_on) or '-'}]", file=sys.stderr)
    print(
        f"  integration={result['integration']} docs_only={result['docs_only']} "
        f"lockfile_only={result['lockfile_only']}",
        file=sys.stderr,
    )


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True, help="base ref (e.g. origin/main)")
    ap.add_argument("--head", default="HEAD", help="head ref (default HEAD)")
    ap.add_argument("--json", action="store_true", help="print JSON to stdout")
    ap.add_argument(
        "--github-output",
        action="store_true",
        help="append key=value lines to $GITHUB_OUTPUT",
    )
    ap.add_argument(
        "--metadata-json",
        help="use this file instead of running `cargo metadata` (offline/tests)",
    )
    ap.add_argument(
        "--cwd",
        help="run git/cargo as if invoked from this directory (default: current dir)",
    )
    args = ap.parse_args(argv)

    cwd = Path(args.cwd).resolve() if args.cwd else None

    try:
        cfg = mergegate_config.load_config(mergegate_config.repo_root(cwd))
    except mergegate_config.ConfigError as e:
        print(f"affected: {e}", file=sys.stderr)
        return 1

    files = changed_files(args.base, args.head, cwd=cwd)
    metadata = load_metadata(args.metadata_json, cwd=cwd)
    graph = Graph(metadata, cfg["affected"])
    result = analyze(files, graph, cfg)

    print_summary(result)

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))

    if args.github_output:
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if not gh_out:
            print(
                "warning: --github-output given but $GITHUB_OUTPUT is unset; skipping",
                file=sys.stderr,
            )
        else:
            write_github_output(result, gh_out)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
