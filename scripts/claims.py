#!/usr/bin/env python3
"""claims.py — shared-service claim protocol (docs/DESIGN.md §4).

A *shared service* is (a) a workspace crate reachable via path dependencies
from at least `claims.min_consumers` of the crates listed in
`claims.app_crates`, or (b) a unit declared in `<claims.dir>/REGISTRY.toml`.
One `<claims.dir>/<service>.md` is generated per service by `sync-registry`
(never hand-edited). A *claim* on a service is never committed on a working
branch — it is the sole file of an orphan commit pushed to
`<claims.ref_prefix><service>` on the configured remote (atomic create;
compare-and-swap takeover of an expired claim), mirrored to
`$(git rev-parse --git-common-dir)/claims/<service>.claim` for offline
reads.

Stdlib only; no `tomllib` and no `match` statement, so it runs on the macOS
system `python3` and on the oldest supported CI images. The registry and
claim-record files use the TOML subset `mergegate_config.parse_toml_subset`
accepts.

Subcommands: sync-registry, services, claim, renew, release, list, check.
Run `python3 scripts/claims.py <command> --help` for per-command flags.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mergegate_config

# Bounded retry for the individual git ref pushes/fetches a bulk `release
# --branch` performs — one per touched service. A transient network failure
# on any single one must never silently drop that service from the release
# (a bulk release once released two of three claims held by the same branch
# and left the third claimed with no error surfaced). Only errors
# `is_network_error` recognizes as transient are retried; a genuine
# conflict (wrong holder, non-fast-forward) surfaces immediately.
RELEASE_RETRY_ATTEMPTS = 3
RELEASE_RETRY_BASE_DELAY = 0.5  # seconds; backoff is base_delay * 2**attempt

NETWORK_ERROR_MARKERS = [
    "could not resolve host",
    "could not read from remote",
    "connection refused",
    "connection timed out",
    "network is unreachable",
    "operation timed out",
    "temporary failure in name resolution",
    "unable to negotiate",
    "no route to host",
    "ssh: connect to host",
    "could not connect",
    "timed out",
    "unable to access",
    "failed to connect",
]

# A service id becomes part of a git ref name and a file name. Keep it to a
# conservative charset: no '/', no '..', no leading '-' or '.', nothing git
# could misparse. A ref pushed with a path instead of an id once made every
# `list` warn and a bulk `release` abort with the real claims still held.
SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def find_root() -> Path:
    res = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if res.returncode != 0:
        sys.exit("claims.py: not inside a git repository")
    return Path(res.stdout.strip())


ROOT = find_root()
try:
    CFG = mergegate_config.load_config(ROOT)
except mergegate_config.ConfigError as _e:
    sys.exit(f"claims.py: {_e}")

REMOTE: str = CFG["repo"]["remote"]
REF_PREFIX: str = CFG["claims"]["ref_prefix"]
CLAIMS_DIR: str = CFG["claims"]["dir"]
APP_CRATES: List[str] = list(CFG["claims"]["app_crates"])
MIN_CONSUMERS: int = int(CFG["claims"]["min_consumers"])
DEFAULT_TTL_HOURS: float = float(CFG["claims"]["default_ttl_hours"])
EXEMPT_ACTORS: List[str] = list(CFG["claims"]["exempt_actors"])
DOCS_PREFIXES: List[str] = list(CFG["claims"]["docs_prefixes"])
DOCS_SUFFIXES: List[str] = list(CFG["claims"]["docs_suffixes"])
LOCKFILE_NAMES: List[str] = list(CFG["claims"]["lockfile_names"])

parse_toml_lite = mergegate_config.parse_toml_subset


def valid_service_id(service: str) -> bool:
    return bool(SERVICE_ID_RE.match(service)) and ".." not in service


def valid_branch_name(branch: str) -> bool:
    if not branch or branch.startswith("-"):
        return False
    res = subprocess.run(
        ["git", "check-ref-format", "--branch", branch], capture_output=True, text=True
    )
    return res.returncode == 0


# --------------------------------------------------------------------------
# Claim record serialization
# --------------------------------------------------------------------------


def toml_string(value: Any) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def serialize_claim(record: dict) -> str:
    lines = []
    for key in ("service", "branch", "session", "claimed_at", "expires_at", "intent"):
        lines.append(f"{key} = {toml_string(record.get(key, ''))}")
    if record.get("local_only"):
        lines.append("local_only = true")
    previous = record.get("previous")
    if previous:
        lines.append("")
        lines.append("[previous]")
        for key in ("branch", "session", "claimed_at"):
            lines.append(f"{key} = {toml_string(previous.get(key, ''))}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Glob matching — implements ** across path separators, plain shell globs
# otherwise. Used against owned-path patterns from the registry.
# --------------------------------------------------------------------------


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


_GLOB_CACHE: Dict[str, re.Pattern[str]] = {}


def glob_matches(glob: str, path: str) -> bool:
    rx = _GLOB_CACHE.get(glob)
    if rx is None:
        rx = re.compile(glob_to_regex(glob))
        _GLOB_CACHE[glob] = rx
    return rx.match(path) is not None


# --------------------------------------------------------------------------
# git plumbing helpers
# --------------------------------------------------------------------------


def run(cmd: List[str], cwd: Optional[Path] = None, input_text: Optional[str] = None, env=None):
    return subprocess.run(
        cmd,
        cwd=str(cwd or ROOT),
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
    )


def current_branch() -> str:
    res = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if res.returncode != 0:
        sys.exit(f"claims.py: could not determine current branch: {res.stderr.strip()}")
    return res.stdout.strip()


def default_session() -> str:
    sid = os.environ.get("MERGE_GATE_SESSION")
    if sid:
        return sid
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    host = socket.gethostname()
    try:
        tty = os.ttyname(0)
    except OSError:
        tty = "notty"
    return f"{user}@{host}:{tty}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def is_expired(record: dict) -> bool:
    return parse_iso(record["expires_at"]) <= utcnow()


def is_network_error(err_text: str) -> bool:
    low = (err_text or "").lower()
    return any(marker in low for marker in NETWORK_ERROR_MARKERS)


def retry_on_network_error(
    fn: Callable[[], Tuple[bool, str]],
    attempts: int = RELEASE_RETRY_ATTEMPTS,
    base_delay: float = RELEASE_RETRY_BASE_DELAY,
) -> Tuple[bool, str]:
    """Call fn() up to `attempts` times. fn returns (ok, err) like the push_*
    helpers. Retried only when not ok AND is_network_error(err); a
    non-transient failure returns on the first try."""
    ok, err = False, "retry_on_network_error: fn was never called"
    for attempt in range(attempts):
        ok, err = fn()
        if ok or not is_network_error(err):
            return ok, err
        if attempt < attempts - 1:
            time.sleep(base_delay * (2**attempt))
    return ok, err


def retry_raises_on_network_error(
    fn: Callable[[], Any],
    attempts: int = RELEASE_RETRY_ATTEMPTS,
    base_delay: float = RELEASE_RETRY_BASE_DELAY,
) -> Any:
    """Call fn() up to `attempts` times; fn may raise RuntimeError (the shape
    the ls_remote_* / fetch_and_parse helpers use). Retried only when the
    message looks transient; anything else re-raises immediately."""
    last_exc: Optional[RuntimeError] = None
    for attempt in range(attempts):
        try:
            return fn()
        except RuntimeError as e:
            if not is_network_error(str(e)):
                raise
            last_exc = e
            if attempt < attempts - 1:
                time.sleep(base_delay * (2**attempt))
    assert last_exc is not None
    raise last_exc


def git_common_dir() -> Path:
    res = run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if res.returncode == 0 and res.stdout.strip():
        return Path(res.stdout.strip())
    res2 = run(["git", "rev-parse", "--git-common-dir"])
    p = Path(res2.stdout.strip())
    if not p.is_absolute():
        p = (ROOT / p).resolve()
    return p


def mirror_dir() -> Path:
    return git_common_dir() / "claims"


def mirror_write(service: str, record: dict) -> None:
    d = mirror_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{service}.claim").write_text(serialize_claim(record))


def mirror_delete(service: str) -> None:
    p = mirror_dir() / f"{service}.claim"
    if p.exists():
        p.unlink()


def claim_ref(service: str) -> str:
    return f"{REF_PREFIX}{service}"


def build_orphan_commit(service: str, content: str) -> str:
    """Commit `content` as claims/<service>.claim, alone, as an orphan
    commit — via a temp GIT_INDEX_FILE, never touching the working tree or
    the real index."""
    fd, index_path = tempfile.mkstemp(prefix="merge-gate-claim-index-")
    os.close(fd)
    os.unlink(index_path)
    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = index_path
    try:
        res = run(["git", "read-tree", "--empty"], env=env)
        if res.returncode != 0:
            raise RuntimeError(res.stderr.strip())
        blob = run(["git", "hash-object", "-w", "--stdin"], input_text=content, env=env)
        if blob.returncode != 0:
            raise RuntimeError(blob.stderr.strip())
        blob_sha = blob.stdout.strip()
        ui = run(
            [
                "git",
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob_sha},claims/{service}.claim",
            ],
            env=env,
        )
        if ui.returncode != 0:
            raise RuntimeError(ui.stderr.strip())
        tree = run(["git", "write-tree"], env=env)
        if tree.returncode != 0:
            raise RuntimeError(tree.stderr.strip())
        tree_sha = tree.stdout.strip()
        commit = run(["git", "commit-tree", tree_sha, "-m", f"claim: {service}"], env=env)
        if commit.returncode != 0:
            raise RuntimeError(commit.stderr.strip())
        return commit.stdout.strip()
    finally:
        if os.path.exists(index_path):
            os.unlink(index_path)


def push_create(service: str, sha: str) -> Tuple[bool, str]:
    res = run(["git", "push", REMOTE, f"{sha}:{claim_ref(service)}"])
    return res.returncode == 0, (res.stderr or res.stdout)


def push_force_with_lease(service: str, sha: str, old_sha: str) -> Tuple[bool, str]:
    res = run(
        [
            "git",
            "push",
            f"--force-with-lease={claim_ref(service)}:{old_sha}",
            REMOTE,
            f"{sha}:{claim_ref(service)}",
        ]
    )
    return res.returncode == 0, (res.stderr or res.stdout)


def push_delete(service: str) -> Tuple[bool, str]:
    res = run(["git", "push", REMOTE, f":{claim_ref(service)}"])
    return res.returncode == 0, (res.stderr or res.stdout)


def ls_remote_ref(service: str) -> Optional[str]:
    res = run(["git", "ls-remote", REMOTE, claim_ref(service)])
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip())
    line = res.stdout.strip()
    if not line:
        return None
    return line.split()[0]


def ls_remote_refs() -> Dict[str, str]:
    res = run(["git", "ls-remote", REMOTE, f"{REF_PREFIX}*"])
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip())
    out: Dict[str, str] = {}
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        sha, ref = line.split(maxsplit=1)
        ref = ref.strip()
        svc = ref[len(REF_PREFIX) :] if ref.startswith(REF_PREFIX) else ref.split("/")[-1]
        if not valid_service_id(svc):
            # A ref that does not decode to a valid service id can never be
            # fetched by service name; treating it as a service makes every
            # `list` warn and a bulk `release` abort. Report it once and
            # leave it out of the scan.
            print(
                f"warning — malformed claim ref '{ref}' ignored (service ids match "
                f"{SERVICE_ID_RE.pattern}); delete it with: git push {REMOTE} :{ref}"
            )
            continue
        out[svc] = sha
    return out


def fetch_and_parse(service: str) -> dict:
    res = run(["git", "fetch", REMOTE, claim_ref(service)])
    if res.returncode != 0:
        raise RuntimeError((res.stderr or res.stdout).strip())
    show = run(["git", "show", f"FETCH_HEAD:claims/{service}.claim"])
    if show.returncode != 0:
        raise RuntimeError((show.stderr or show.stdout).strip())
    return parse_toml_lite(show.stdout)


# --------------------------------------------------------------------------
# Registry: derive rule (a) crates from cargo metadata, merge with the
# declared (b) services from REGISTRY.toml, render <claims.dir>/<service>.md.
# --------------------------------------------------------------------------


def load_cargo_metadata(args) -> dict:
    metadata_json = getattr(args, "metadata_json", None)
    if metadata_json:
        return json.loads(Path(metadata_json).read_text())
    if not (ROOT / "Cargo.toml").exists():
        return {"packages": []}
    res = subprocess.run(
        ["cargo", "metadata", "--no-deps", "--format-version", "1"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        sys.exit(f"sync-registry: cargo metadata failed: {res.stderr.strip()}")
    return json.loads(res.stdout)


def derive_crate_services(
    metadata: dict,
    app_crates: Optional[List[str]] = None,
    min_consumers: Optional[int] = None,
    root: Optional[Path] = None,
) -> dict:
    """Crates reachable via path deps from >= min_consumers of app_crates."""
    app_crates = APP_CRATES if app_crates is None else app_crates
    min_consumers = MIN_CONSUMERS if min_consumers is None else min_consumers
    root = ROOT if root is None else root
    pkgs = {p["name"]: p for p in metadata.get("packages", [])}
    names = set(pkgs)
    graph: Dict[str, set] = {}
    manifest_dir: Dict[str, str] = {}
    for name, p in pkgs.items():
        deps = set()
        for dep in p.get("dependencies", []) or []:
            if dep.get("path") and dep.get("name") in names:
                deps.add(dep["name"])
        graph[name] = deps
        manifest_dir[name] = str(Path(p["manifest_path"]).parent)

    def reachable(start: str) -> set:
        seen: set = set()
        stack = [start]
        while stack:
            cur = stack.pop()
            for nxt in graph.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return seen

    counts: Dict[str, int] = {}
    reach_by_app: Dict[str, set] = {}
    for app in app_crates:
        if app not in graph:
            continue
        r = reachable(app)
        reach_by_app[app] = r
        for c in r:
            counts[c] = counts.get(c, 0) + 1

    result = {}
    for crate, n in counts.items():
        if n < min_consumers or crate in app_crates:
            continue
        consumers = sorted(a for a in app_crates if crate in reach_by_app.get(a, set()))
        rel = manifest_dir.get(crate, crate)
        if os.path.isabs(rel):
            rel = os.path.relpath(rel, root)
        result[crate] = {
            "paths": [f"{rel}/**"],
            "consumers": consumers,
            "source": f"cargo metadata: reachable from {', '.join(consumers)}",
        }
    return result


def render_service_md(service: str, kind: str, paths, consumers, source: str) -> str:
    lines = [f"# {service}", ""]
    lines.append(f"- **kind:** {kind}")
    lines.append("- **owned paths:**")
    for p in sorted(paths):
        lines.append(f"  - `{p}`")
    consumers_str = ", ".join(sorted(consumers)) if consumers else "none"
    lines.append(f"- **consumers:** {consumers_str}")
    lines.append(f"- **source:** {source}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        f"Live claim state lives in {claim_ref(service)} on {REMOTE} — never in "
        f"this file. Regenerated by scripts/claims.py sync-registry; do not "
        f"hand-edit."
    )
    return "\n".join(lines) + "\n"


def render_readme(service_ids) -> str:
    ids = ", ".join(sorted(set(service_ids)))
    apps = ", ".join(f"`{a}`" for a in APP_CRATES) or "(none configured)"
    return (
        f"# {CLAIMS_DIR}/ — shared-service claim registry\n"
        "\n"
        f"One `{CLAIMS_DIR}/<service>.md` file per shared service (never hand-edited — "
        "see the footer of each file). Regenerated by "
        "`python3 scripts/claims.py sync-registry`; `--check` fails if the "
        "checked-in files drift from what regenerating would produce. Full "
        "protocol: `docs/DESIGN.md` §4.\n"
        "\n"
        "Two kinds of service:\n"
        "\n"
        "- **crate** — a workspace crate reachable via path dependencies from "
        f">= {MIN_CONSUMERS} of the app crates ({apps}), rule (a). Derived from "
        "`cargo metadata`.\n"
        "- **declared** — a non-crate shared unit listed in "
        f"`{CLAIMS_DIR}/REGISTRY.toml`, rule (b).\n"
        "\n"
        f"Current services: {ids}\n"
        "\n"
        "## Live claim state lives in the refs, not here\n"
        "\n"
        "A claim record is never committed on a working branch. It is the "
        f"sole file of an orphan commit pushed to `{REF_PREFIX}<service>` on "
        f"`{REMOTE}`, mirrored to "
        "`$(git rev-parse --git-common-dir)/claims/<service>.claim` for "
        "offline reads.\n"
        "\n"
        "## CLI cheat-sheet\n"
        "\n"
        "```\n"
        'python3 scripts/claims.py claim <service> --intent "<one line>" '
        f"[--ttl-hours {DEFAULT_TTL_HOURS:g}] [--takeover]\n"
        "python3 scripts/claims.py renew <service> | --all-mine | --branch <b>\n"
        "python3 scripts/claims.py release <service> | --branch <b> | "
        "--all-mine [--force]\n"
        "python3 scripts/claims.py list [--json] [--offline]\n"
        "python3 scripts/claims.py services --files <f>... | --base <ref> "
        "--head <ref>\n"
        "python3 scripts/claims.py check --base <ref> --head <ref> --branch "
        "<name> [--branch <name> ...] [--actor <login>]\n"
        "python3 scripts/claims.py sync-registry [--check]\n"
        "```\n"
        "\n"
        "## Exemptions from claim enforcement (`claims.py check`)\n"
        "\n"
        "- Docs-only diffs (every changed file under a docs prefix or with a "
        "docs suffix — see `claims.docs_prefixes` / `claims.docs_suffixes`).\n"
        "- Lockfile-only diffs (`claims.lockfile_names`).\n"
        "- Diffs whose actor is in `claims.exempt_actors`.\n"
        "\n"
        "These enforce *exclusive landing*, not exclusive editing — two "
        "branches can still edit the same service concurrently; only the "
        "claim holder can land.\n"
    )


def build_expected_files(args) -> Dict[str, str]:
    files: Dict[str, str] = {}
    registry_path = ROOT / CLAIMS_DIR / "REGISTRY.toml"
    declared: Dict[str, Any] = {}
    if registry_path.exists():
        parsed = parse_toml_lite(registry_path.read_text())
        declared = parsed.get("services", {})
    for svc_id, tbl in sorted(declared.items()):
        if not valid_service_id(svc_id):
            raise ValueError(f"REGISTRY.toml: invalid service id '{svc_id}'")
        kind = tbl.get("kind", "declared")
        paths = tbl.get("paths", [])
        consumers = tbl.get("consumers", [])
        source = tbl.get("source", "")
        files[f"{svc_id}.md"] = render_service_md(svc_id, kind, paths, consumers, source)

    metadata = load_cargo_metadata(args)
    crate_services = derive_crate_services(metadata)
    for svc_id, info in sorted(crate_services.items()):
        if not valid_service_id(svc_id):
            raise ValueError(f"cargo metadata: crate name '{svc_id}' is not a valid service id")
        files[f"{svc_id}.md"] = render_service_md(
            svc_id, "crate", info["paths"], info["consumers"], info["source"]
        )

    files["README.md"] = render_readme(list(declared.keys()) + list(crate_services.keys()))
    return files


def load_registry() -> dict:
    d = ROOT / CLAIMS_DIR
    registry: dict = {}
    if not d.exists():
        return registry
    for f in sorted(d.glob("*.md")):
        if f.name == "README.md":
            continue
        text = f.read_text()
        svc = f.stem
        kind_m = re.search(r"^-\s*\*\*kind:\*\*\s*(\S+)\s*$", text, re.M)
        consumers_m = re.search(r"^-\s*\*\*consumers:\*\*\s*(.*)$", text, re.M)
        source_m = re.search(r"^-\s*\*\*source:\*\*\s*(.*)$", text, re.M)
        paths = re.findall(r"^\s*-\s*`([^`]+)`\s*$", text, re.M)
        consumers_str = consumers_m.group(1).strip() if consumers_m else ""
        consumers = (
            [] if consumers_str in ("", "none") else [c.strip() for c in consumers_str.split(",")]
        )
        registry[svc] = {
            "kind": kind_m.group(1) if kind_m else "unknown",
            "paths": paths,
            "consumers": consumers,
            "source": source_m.group(1).strip() if source_m else "",
        }
    return registry


def services_for_files(files, registry: dict) -> List[str]:
    touched = []
    for svc_id, info in registry.items():
        globs = info.get("paths", [])
        if any(glob_matches(g, f) for g in globs for f in files):
            touched.append(svc_id)
    return sorted(touched)


# --------------------------------------------------------------------------
# Diff / exemption helpers
# --------------------------------------------------------------------------


def changed_files_base_head(base: str, head: str) -> List[str]:
    # `--head HEAD` (the literal token) means "my current checkout,
    # uncommitted work included" — the ad-hoc workflow of running `check`
    # against work in progress. CI always passes a real head SHA, so the
    # two-dot working-tree-inclusive form never fires there.
    if head == "HEAD":
        res = run(["git", "diff", "--name-only", base])
    else:
        res = run(["git", "diff", "--name-only", f"{base}...{head}"])
    if res.returncode != 0:
        sys.exit(f"claims.py: git diff failed: {res.stderr.strip()}")
    return [line for line in res.stdout.splitlines() if line.strip()]


def changed_files_from_args(args) -> List[str]:
    if getattr(args, "files", None):
        return list(args.files)
    if getattr(args, "base", None) and getattr(args, "head", None):
        return changed_files_base_head(args.base, args.head)
    sys.exit("services: provide --files <f>... or --base <ref> --head <ref>")


def is_docs_file(f: str) -> bool:
    return any(f.startswith(p) for p in DOCS_PREFIXES) or any(f.endswith(s) for s in DOCS_SUFFIXES)


def is_lockfile(f: str) -> bool:
    return any(f == name or f.endswith("/" + name) for name in LOCKFILE_NAMES)


def is_docs_only(files) -> bool:
    return bool(files) and all(is_docs_file(f) for f in files)


def is_lockfile_only(files) -> bool:
    return bool(files) and all(is_lockfile(f) for f in files)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_sync_registry(args) -> int:
    try:
        expected = build_expected_files(args)
    except SystemExit:
        raise
    except Exception as e:
        print(f"sync-registry: failed to build registry: {e}")
        return 1

    claims_dir = ROOT / CLAIMS_DIR
    claims_dir.mkdir(parents=True, exist_ok=True)
    existing = {f.name for f in claims_dir.glob("*.md")}

    if args.check:
        problems = []
        for name, content in sorted(expected.items()):
            path = claims_dir / name
            if not path.exists() or path.read_text() != content:
                problems.append(f"would change: {CLAIMS_DIR}/{name}")
        extra = sorted(existing - set(expected.keys()) - {"README.md"})
        for name in extra:
            problems.append(f"stale extra file: {CLAIMS_DIR}/{name}")
        if problems:
            print("sync-registry --check: registry drift detected:")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(
            f"OK — {CLAIMS_DIR}/ matches the derived+declared registry "
            f"({len(expected) - 1} service(s))."
        )
        return 0

    for name, content in expected.items():
        (claims_dir / name).write_text(content)
    extra = existing - set(expected.keys()) - {"README.md"}
    for name in extra:
        (claims_dir / name).unlink()
    print(f"sync-registry: wrote {len(expected) - 1} service file(s) + README.md to {CLAIMS_DIR}/.")
    return 0


def cmd_services(args) -> int:
    files = changed_files_from_args(args)
    registry = load_registry()
    for svc in services_for_files(files, registry):
        print(svc)
    return 0


def reconcile_one(service: str, rec: dict) -> int:
    """Attempt to publish one locally-mirrored `local_only` claim now that
    network access might be available. Returns 0 (reconciled or nothing to
    do), 1 (still cannot reach the remote — leave it local-only), or 2 (a
    live, unexpired remote claim by a DIFFERENT branch — a genuine conflict
    that must not be silently dropped)."""
    branch = rec.get("branch", "")
    new_record = dict(rec)
    new_record.pop("local_only", None)

    try:
        remote_sha = ls_remote_ref(service)
    except RuntimeError:
        return 1

    if remote_sha is None:
        sha = build_orphan_commit(service, serialize_claim(new_record))
        ok, err = push_create(service, sha)
        if ok:
            mirror_write(service, new_record)
            print(f"reconcile: published locally-held claim on '{service}' (was local-only)")
            return 0
        if is_network_error(err):
            return 1
        # Someone else created the ref in the meantime (race) — re-check as
        # an existing ref below instead of failing outright.
        try:
            remote_sha = ls_remote_ref(service)
        except RuntimeError:
            return 1
        if remote_sha is None:
            print(f"reconcile: could not publish '{service}': {err.strip()}")
            return 1

    try:
        old_record = fetch_and_parse(service)
    except RuntimeError:
        return 1

    if old_record.get("branch") == branch:
        mirror_write(service, old_record)
        print(f"reconcile: '{service}' already held remotely by '{branch}' — synced")
        return 0

    if not is_expired(old_record):
        print(
            f"reconcile: CONFLICT on '{service}' — remote is held by "
            f"'{old_record.get('branch')}' (session {old_record.get('session')}) "
            f"until {old_record.get('expires_at')}, but the local-only claim here "
            f"is for '{branch}'. NOT auto-resolved.",
            file=sys.stderr,
        )
        return 2

    new_record["previous"] = {
        "branch": old_record.get("branch", ""),
        "session": old_record.get("session", ""),
        "claimed_at": old_record.get("claimed_at", ""),
    }
    sha = build_orphan_commit(service, serialize_claim(new_record))
    ok, err = push_force_with_lease(service, sha, remote_sha)
    if not ok:
        if is_network_error(err):
            return 1
        print(f"reconcile: could not take over '{service}' (lost the race?): {err.strip()}")
        return 1
    mirror_write(service, new_record)
    print(f"reconcile: took over expired remote claim on '{service}' with the local-only record")
    return 0


def reconcile() -> List[str]:
    """Flush every locally-mirrored `local_only` claim, if the remote is
    reachable. Returns the service ids that hit a genuine conflict."""
    d = mirror_dir()
    if not d.exists():
        return []
    pending = []
    for f in sorted(d.glob("*.claim")):
        if not valid_service_id(f.stem):
            continue
        try:
            rec = parse_toml_lite(f.read_text())
        except Exception:
            continue
        if rec.get("local_only"):
            pending.append((f.stem, rec))
    conflicts = []
    for service, rec in pending:
        if reconcile_one(service, rec) == 2:
            conflicts.append(service)
    return conflicts


def cmd_claim(args) -> int:
    if not valid_service_id(args.service):
        print(
            f"claim: refusing '{args.service}' — a service id is a bare name matching "
            f"{SERVICE_ID_RE.pattern} (see `claims.py services <paths>` to map a path to its id)"
        )
        return 1
    registry = load_registry()
    if args.service not in registry:
        print(
            f"claim: warning — '{args.service}' is not a known service in "
            f"{CLAIMS_DIR}/ (run `sync-registry` first, or check the id) — "
            f"proceeding anyway",
            file=sys.stderr,
        )

    branch = args.branch or current_branch()
    if not valid_branch_name(branch):
        print(f"claim: '{branch}' is not a valid branch name")
        return 1
    session = args.session or default_session()
    now = utcnow()
    record = {
        "service": args.service,
        "branch": branch,
        "session": session,
        "claimed_at": iso(now),
        "expires_at": iso(now + timedelta(hours=args.ttl_hours)),
        "intent": args.intent,
    }

    sha = build_orphan_commit(args.service, serialize_claim(record))
    ok, err = push_create(args.service, sha)
    if ok:
        mirror_write(args.service, record)
        print(f"claimed '{args.service}' for '{branch}' until {record['expires_at']}")
        return 0

    if is_network_error(err):
        record["local_only"] = True
        mirror_write(args.service, record)
        print(
            f"WARNING: {REMOTE} unreachable — '{args.service}' claimed locally "
            f"only (local_only=true). Re-run claims.py once online to "
            f"publish the claim: {err.strip()}",
            file=sys.stderr,
        )
        return 0

    try:
        remote_sha = ls_remote_ref(args.service)
    except RuntimeError as e:
        print(f"claim: push failed for '{args.service}' and could not verify why: {e}")
        return 1
    if remote_sha is None:
        print(f"claim: push failed for '{args.service}': {err.strip()}")
        return 1

    try:
        old_record = fetch_and_parse(args.service)
    except RuntimeError as e:
        print(f"claim: could not read existing claim for '{args.service}': {e}")
        return 1

    if old_record.get("branch") == branch:
        new_record = dict(record)
        sha2 = build_orphan_commit(args.service, serialize_claim(new_record))
        ok2, err2 = push_force_with_lease(args.service, sha2, remote_sha)
        if not ok2:
            print(f"claim: renew push failed for '{args.service}': {err2.strip()}")
            return 1
        mirror_write(args.service, new_record)
        print(
            f"renewed own claim on '{args.service}' for '{branch}' until {new_record['expires_at']}"
        )
        return 0

    if not is_expired(old_record):
        print(
            f"claim REFUSED: '{args.service}' is held by branch "
            f"'{old_record.get('branch')}' (session {old_record.get('session')}) "
            f"until {old_record.get('expires_at')}."
        )
        print(f"  intent: {old_record.get('intent', '')}")
        print("Sequence the work with the holder, or wait for expiry / takeover once expired.")
        return 2

    # Expired: take over (compare-and-swap). --takeover is accepted but not
    # required once the existing claim is actually expired.
    new_record = dict(record)
    new_record["previous"] = {
        "branch": old_record.get("branch", ""),
        "session": old_record.get("session", ""),
        "claimed_at": old_record.get("claimed_at", ""),
    }
    sha3 = build_orphan_commit(args.service, serialize_claim(new_record))
    ok3, err3 = push_force_with_lease(args.service, sha3, remote_sha)
    if not ok3:
        print(f"claim: takeover push failed for '{args.service}' (lost the race?): {err3.strip()}")
        return 1
    mirror_write(args.service, new_record)
    print(
        f"took over expired claim on '{args.service}' from "
        f"'{old_record.get('branch')}' — now held by '{branch}' until "
        f"{new_record['expires_at']}"
    )
    return 0


def renew_one(service: str, branch: str, ttl_hours: float) -> bool:
    try:
        remote_sha = ls_remote_ref(service)
    except RuntimeError as e:
        print(f"renew: could not reach {REMOTE} for '{service}': {e}")
        return False
    if remote_sha is None:
        print(f"renew: no live claim for '{service}'")
        return False
    try:
        record = fetch_and_parse(service)
    except RuntimeError as e:
        print(f"renew: could not read claim for '{service}': {e}")
        return False
    if record.get("branch") != branch:
        print(f"renew: '{service}' is held by '{record.get('branch')}', not '{branch}' — refusing")
        return False
    record["expires_at"] = iso(utcnow() + timedelta(hours=ttl_hours))
    sha = build_orphan_commit(service, serialize_claim(record))
    ok, err = push_force_with_lease(service, sha, remote_sha)
    if not ok:
        print(f"renew: push failed for '{service}': {err.strip()}")
        return False
    mirror_write(service, record)
    print(f"renewed '{service}' for '{branch}' until {record['expires_at']}")
    return True


def cmd_renew(args) -> int:
    if args.service and not valid_service_id(args.service):
        print(f"renew: invalid service id '{args.service}'")
        return 1
    bulk = args.all_mine or (args.branch and not args.service)
    if bulk:
        target_branch = args.branch or current_branch()
        try:
            refs = ls_remote_refs()
        except RuntimeError as e:
            print(f"renew: could not reach {REMOTE}: {e}")
            return 1
        mine = []
        for svc in sorted(refs):
            try:
                rec = fetch_and_parse(svc)
            except RuntimeError as e:
                print(f"renew: warning — could not read '{svc}': {e}")
                continue
            if rec.get("branch") == target_branch:
                mine.append(svc)
        if not mine:
            print(f"renew: no claims held by '{target_branch}'")
            return 0
        failed = [svc for svc in mine if not renew_one(svc, target_branch, args.ttl_hours)]
        if failed:
            print("renew: failed for:", ", ".join(failed))
            return 1
        print(f"renew: refreshed {len(mine)} claim(s) for '{target_branch}': {', '.join(mine)}")
        return 0

    if not args.service:
        print("renew: specify <service>, --all-mine, or --branch <b>")
        return 1
    target_branch = args.branch or current_branch()
    return 0 if renew_one(args.service, target_branch, args.ttl_hours) else 1


def release_one(service: str, requesting_branch: str, force: bool) -> bool:
    try:
        remote_sha = retry_raises_on_network_error(lambda: ls_remote_ref(service))
    except RuntimeError as e:
        print(f"release: could not reach {REMOTE} for '{service}' after retries: {e}")
        return False
    if remote_sha is None:
        print(f"release: '{service}' has no live claim (already released)")
        mirror_delete(service)
        return True
    try:
        record = retry_raises_on_network_error(lambda: fetch_and_parse(service))
    except RuntimeError as e:
        print(f"release: could not read claim for '{service}' after retries: {e}")
        return False
    if record.get("branch") != requesting_branch and not force:
        print(
            f"release: '{service}' is held by '{record.get('branch')}', not "
            f"'{requesting_branch}' — use --force"
        )
        return False
    ok, err = retry_on_network_error(lambda: push_delete(service))
    if not ok:
        print(f"release: failed to delete ref for '{service}' after retries: {err.strip()}")
        return False
    mirror_delete(service)
    print(f"released '{service}'")
    return True


def cmd_release(args) -> int:
    if args.service and not valid_service_id(args.service):
        print(f"release: invalid service id '{args.service}'")
        return 1
    bulk = args.all_mine or (args.branch and not args.service)
    if bulk:
        target_branch = args.branch or current_branch()
        try:
            refs = retry_raises_on_network_error(ls_remote_refs)
        except RuntimeError as e:
            print(f"release: could not reach {REMOTE}: {e}")
            return 1
        # Two-phase (scan every live ref for a branch match, then release
        # each match) is inherently non-atomic. What must never happen is a
        # transient failure during the SCAN phase silently excluding a
        # matching service from `mine`. Every scan read is retried, and a
        # service that still cannot be read is tracked as `unreadable` and
        # reported — never silently skipped.
        mine = []
        unreadable = []
        for svc in sorted(refs):
            try:
                rec = retry_raises_on_network_error(lambda svc=svc: fetch_and_parse(svc))
            except RuntimeError as e:
                print(f"release: warning — could not read '{svc}' after retries: {e}")
                unreadable.append(svc)
                continue
            if rec.get("branch") == target_branch:
                mine.append(svc)
        if not mine and not unreadable:
            print(f"release: no claims held by '{target_branch}'")
            return 0
        failed = [svc for svc in mine if not release_one(svc, target_branch, force=True)]
        released = [svc for svc in mine if svc not in failed]
        if released:
            print(
                f"release: released {len(released)} claim(s) held by "
                f"'{target_branch}': {', '.join(released)}"
            )
        if failed:
            print("release: failed for:", ", ".join(failed))
        if unreadable:
            print(
                "release: could not verify ownership after retries (left "
                "claimed — inspect manually):",
                ", ".join(unreadable),
            )
        if failed or unreadable:
            return 1
        return 0

    if not args.service:
        print("release: specify <service>, --all-mine, or --branch <b>")
        return 1
    # An explicit --branch alongside a single service is treated as an
    # operator's authorization to release on that branch's behalf.
    requesting_branch = args.branch or current_branch()
    force = args.force or bool(args.branch)
    return 0 if release_one(args.service, requesting_branch, force=force) else 1


def cmd_list(args) -> int:
    records = []
    if args.offline:
        d = mirror_dir()
        if d.exists():
            for f in sorted(d.glob("*.claim")):
                try:
                    records.append(parse_toml_lite(f.read_text()))
                except Exception:
                    continue
    else:
        try:
            refs = ls_remote_refs()
        except RuntimeError as e:
            print(f"list: could not reach {REMOTE} ({e}); try --offline")
            return 1
        seen_services = set()
        for svc, _sha in sorted(refs.items()):
            seen_services.add(svc)
            try:
                rec = fetch_and_parse(svc)
            except RuntimeError as e:
                print(f"list: warning — could not fetch '{svc}': {e}")
                continue
            mirror_write(svc, rec)
            records.append(rec)
        # Union in any local-only mirror entries not (yet) on the remote — a
        # pending offline claim must still be visible, and must never be
        # deleted or overwritten by a `list` refresh; only reconcile() may
        # clear local_only.
        d = mirror_dir()
        if d.exists():
            for f in sorted(d.glob("*.claim")):
                if f.stem in seen_services:
                    continue
                try:
                    rec = parse_toml_lite(f.read_text())
                except Exception:
                    continue
                if rec.get("local_only"):
                    records.append(rec)

    if args.json:
        print(json.dumps(records, indent=2))
        return 0

    print(f"{'SERVICE':25s} {'BRANCH':25s} {'SESSION':30s} {'EXPIRES':30s} INTENT")
    for rec in records:
        expires_disp = rec.get("expires_at", "")
        if rec.get("local_only"):
            expires_disp += "  [LOCAL-ONLY (unsynced)]"
        elif expires_disp and is_expired(rec):
            expires_disp += " (EXPIRED)"
        print(
            f"{rec.get('service', ''):25s} {rec.get('branch', ''):25s} "
            f"{rec.get('session', ''):30s} {expires_disp:30s} {rec.get('intent', '')}"
        )
    return 0


def cmd_check(args) -> int:
    files = changed_files_base_head(args.base, args.head)

    if args.actor in EXEMPT_ACTORS:
        print(f"check: exempt — actor is {args.actor}")
        return 0
    if is_docs_only(files):
        print("check: exempt — docs-only diff")
        return 0
    if is_lockfile_only(files):
        print("check: exempt — lockfile-only diff")
        return 0
    if not files:
        print("check: no changed files — OK")
        return 0

    registry = load_registry()
    touched = services_for_files(files, registry)
    if not touched:
        print("check: no shared services touched — OK")
        return 0

    problems = []
    for svc in touched:
        try:
            remote_sha = ls_remote_ref(svc)
        except RuntimeError as e:
            print(f"check: could not reach {REMOTE}: {e}")
            return 1
        if remote_sha is None:
            problems.append((svc, "unclaimed", "-"))
            continue
        try:
            record = fetch_and_parse(svc)
        except RuntimeError as e:
            print(f"check: could not read claim for '{svc}': {e}")
            return 1
        if record.get("branch") not in args.branch:
            problems.append((svc, record.get("branch", "?"), "held by another branch"))
        elif is_expired(record):
            problems.append((svc, record.get("branch", "?"), "EXPIRED"))

    branches_str = " or ".join(f"'{b}'" for b in args.branch)
    if problems:
        print(f"BLOCKED: services touched by this diff are not validly claimed by {branches_str}:")
        print(f"  {'service':25s} {'holder':25s} status")
        for svc, holder, status in problems:
            print(f"  {svc:25s} {holder:25s} {status}")
        print('Run: python3 scripts/claims.py claim <service> --intent "<one line>"')
        return 1

    print(
        f"OK: {len(touched)} shared service(s) touched, all validly claimed by "
        f"{branches_str}: {', '.join(touched)}"
    )
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claims.py", description="Shared-service claim protocol (docs/DESIGN.md §4)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync-registry", help=f"regenerate {CLAIMS_DIR}/*.md")
    p_sync.add_argument("--check", action="store_true")
    p_sync.add_argument("--metadata-json", help="cargo-metadata-shaped JSON file (test hook)")

    p_services = sub.add_parser("services", help="map changed files to service ids")
    p_services.add_argument("--files", nargs="+")
    p_services.add_argument("--base")
    p_services.add_argument("--head")

    p_claim = sub.add_parser("claim", help="acquire a claim on a service")
    p_claim.add_argument("service")
    p_claim.add_argument("--intent", required=True)
    p_claim.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    p_claim.add_argument("--branch")
    p_claim.add_argument("--session")
    p_claim.add_argument("--takeover", action="store_true")

    p_renew = sub.add_parser("renew", help="extend an owned claim's expiry")
    p_renew.add_argument("service", nargs="?")
    p_renew.add_argument("--all-mine", action="store_true")
    p_renew.add_argument("--branch")
    p_renew.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)

    p_release = sub.add_parser("release", help="release a claim")
    p_release.add_argument("service", nargs="?")
    p_release.add_argument("--branch")
    p_release.add_argument("--all-mine", action="store_true")
    p_release.add_argument("--force", action="store_true")

    p_list = sub.add_parser("list", help="list live claims")
    p_list.add_argument("--json", action="store_true")
    p_list.add_argument("--offline", action="store_true")

    p_check = sub.add_parser("check", help="enforce claims for a diff")
    p_check.add_argument("--base", required=True)
    p_check.add_argument("--head", required=True)
    p_check.add_argument(
        "--branch",
        action="append",
        required=True,
        help="repeatable — a touched service's live claim holder must be ONE OF the "
        "given branches (a train checks every landed candidate's branch at once)",
    )
    p_check.add_argument("--actor")

    return parser


HANDLERS = {
    "sync-registry": cmd_sync_registry,
    "services": cmd_services,
    "claim": cmd_claim,
    "renew": cmd_renew,
    "release": cmd_release,
    "list": cmd_list,
    "check": cmd_check,
}


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Every invocation that might have network access first tries to flush
    # any locally-mirrored `local_only` claim from a prior offline `claim`.
    # Skipped only for `list --offline`. A genuine conflict aborts the whole
    # invocation with exit 2 — loud, never silently dropped.
    skip_reconcile = args.command == "list" and getattr(args, "offline", False)
    if not skip_reconcile:
        conflicts = reconcile()
        if conflicts:
            print(
                f"claims.py: reconcile aborted — {len(conflicts)} local-only "
                f"claim(s) conflict with a live remote claim: {', '.join(conflicts)}",
                file=sys.stderr,
            )
            print(
                "Resolve manually: inspect with `claims.py list`, then "
                "release/re-claim as appropriate.",
                file=sys.stderr,
            )
            return 2

    return HANDLERS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
