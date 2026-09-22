#!/usr/bin/env python3
"""adr-check.py — mechanical enforcement of the ADR immutability rule.

Implements docs/DESIGN.md §5.4 for the range `merge-base(base,head)..head`:

For every `<adr.dir>/NNNN-*.md` **modified** in the range whose version AT
THE MERGE-BASE has a `- **Status:**` line beginning `Accepted`:

  - Allowed without a marker: the only changed line is the Status line, its
    new value is `Superseded by ADR-MMMM`, and a NEW file
    `<adr.dir>/MMMM-*.md` is added in the same range containing
    `Supersedes:` naming ADR-NNNN.
  - Allowed with the nonsubstantive marker (default `[adr-nonsubstantive]`)
    in a commit message of the range that ITSELF touches that ADR file (a
    marker on an unrelated commit does not unlock anything): EVERY `## `
    section body — canonical or not — stays byte-identical between the
    merge-base and head versions, EVERY `- **Key:**` metadata line (Status,
    Acceptance, Date, Deciders, Supersedes, Sources, …) is unchanged, the
    H1 title is unchanged, and no `## ` heading is added, removed, or
    renamed. What may change is only the text outside any section and
    outside the metadata lines: in practice the derivation blockquote and
    any prose between the header and the first section (a broken link, a
    typo there). A typo inside a section body is fixed by a superseding
    record, not in place.
  - Everything else fails.

Deleting or renaming any `<adr.dir>/NNNN-*.md` fails, regardless of its
status. A newly added ADR's number must be `max_existing + 1` (several new
ADRs in one range may occupy a contiguous run). The basenames in
`adr.ignored_basenames` are unconstrained.

All content is read via `git show <ref>:<path>` — the working tree is never
consulted, so this is safe to run against a ref that is not checked out.

Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mergegate_config

STATUS_LINE_RE = re.compile(r"^- \*\*Status:\*\*\s*(.+?)\s*$")
METADATA_LINE_RE = re.compile(r"^- \*\*([^*]+):\*\*.*$")
SUPERSEDED_BY_RE = re.compile(r"^Superseded by ADR-(\d{4})\s*$")
TITLE_LINE_RE = re.compile(r"^# .*$")
HEADING_LINE_RE = re.compile(r"^## .*$")


class Rules:
    """The configurable parts of the check, resolved once per run."""

    def __init__(self, adr_cfg: Dict[str, Any]):
        self.dir: str = adr_cfg["dir"].strip("/")
        self.numbered_re = re.compile(rf"^{re.escape(self.dir)}/(\d{{4}})-.*\.md$")
        self.ignored_basenames = set(adr_cfg["ignored_basenames"])
        # Kept for configuration compatibility and reporting; the marker path
        # compares EVERY section body, not only these.
        self.section_headings: List[str] = list(adr_cfg["section_headings"])
        self.marker: str = adr_cfg["nonsubstantive_marker"]


def default_rules() -> Rules:
    return Rules(mergegate_config.DEFAULTS["adr"])


def _run(cmd: List[str], cwd: Optional[Path] = None) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed ({' '.join(cmd)}): {proc.returncode}\n{proc.stderr}")
    return proc.stdout


def merge_base(base: str, head: str, cwd: Optional[Path] = None) -> str:
    return _run(["git", "merge-base", base, head], cwd=cwd).strip()


def show(ref: str, path: str, cwd: Optional[Path] = None) -> Optional[str]:
    """git show <ref>:<path>, or None if the path does not exist at ref."""
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def name_status(
    mb: str, head: str, adr_dir: str, cwd: Optional[Path] = None
) -> List[Tuple[str, str, Optional[str]]]:
    """Returns (status, path, new_path_or_None) for <adr_dir>/** entries.

    status is one of 'A', 'M', 'D', or 'R' (renames collapsed from R100 etc).
    For 'R', path is the OLD path and new_path is the new one.
    """
    out = _run(
        ["git", "diff", "--name-status", "-M", f"{mb}..{head}", "--", f"{adr_dir}/"],
        cwd=cwd,
    )
    entries = []
    for line in out.splitlines():
        if not line.strip():
            continue
        fields = line.split("\t")
        status = fields[0]
        if status.startswith("R"):
            entries.append(("R", fields[1], fields[2]))
        else:
            entries.append((status[0], fields[1], None))
    return entries


def ls_tree_numbered(ref: str, rules: Rules, cwd: Optional[Path] = None) -> Dict[int, str]:
    """<adr.dir>/NNNN-*.md files present at ref -> {number: path}."""
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "--", f"{rules.dir}/"],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return {}
    out = {}
    for line in proc.stdout.splitlines():
        m = rules.numbered_re.match(line.strip())
        if m and Path(line.strip()).name not in rules.ignored_basenames:
            out[int(m.group(1))] = line.strip()
    return out


def commit_hashes_touching_path(
    mb: str, head: str, path: str, cwd: Optional[Path] = None
) -> List[str]:
    out = _run(["git", "log", "--format=%H", f"{mb}..{head}", "--", path], cwd=cwd)
    return [h.strip() for h in out.splitlines() if h.strip()]


def commit_message_at(commit_hash: str, cwd: Optional[Path] = None) -> str:
    return _run(["git", "log", "-1", "--format=%B", commit_hash], cwd=cwd)


def has_nonsubstantive_marker_for_path(
    mb: str, head: str, path: str, marker: str, cwd: Optional[Path] = None
) -> bool:
    """The marker only unlocks a file when it appears in a commit that
    ITSELF touches that file — a marker on an unrelated commit elsewhere in
    the range does not count."""
    for h in commit_hashes_touching_path(mb, head, path, cwd=cwd):
        if marker in commit_message_at(h, cwd=cwd):
            return True
    return False


def extract_header_signature(text: str) -> Dict[str, object]:
    """The parts of an ADR file the nonsubstantive path must leave untouched
    besides the section bodies: the H1 title, every `- **Key:**` metadata
    line in order (Status, Acceptance, Date, Deciders, Supersedes, Sources,
    and any other), and the full ordered list of '## ' headings (so a
    heading cannot be silently added, removed or renamed)."""
    lines = text.splitlines()
    title = next((ln for ln in lines if TITLE_LINE_RE.match(ln)), None)
    metadata = [ln for ln in lines if METADATA_LINE_RE.match(ln)]
    headings = [ln for ln in lines if HEADING_LINE_RE.match(ln)]
    return {"title": title, "metadata": metadata, "headings": headings}


def base_status_value(text: str) -> Optional[str]:
    for line in text.splitlines():
        m = STATUS_LINE_RE.match(line)
        if m:
            return m.group(1)
    return None


def extract_sections(text: str) -> Dict[str, str]:
    """Every `## ` section's body, keyed by heading (a repeated heading gets
    a numeric suffix so nothing is silently merged). Text before the first
    section is not a section and is not returned."""
    out: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []
    for line in text.splitlines():
        if HEADING_LINE_RE.match(line):
            if current is not None:
                out[current] = "\n".join(buf)
            key = line.strip()
            n = 2
            while key in out:
                key = f"{line.strip()} #{n}"
                n += 1
            current = key
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf)
    return out


def only_status_line_changed(base_text: str, head_text: str) -> Optional[int]:
    """Returns the superseding ADR number if the only diff between base_text
    and head_text is the Status line changing to 'Superseded by ADR-MMMM',
    else None."""
    base_lines = base_text.splitlines()
    head_lines = head_text.splitlines()
    if len(base_lines) != len(head_lines):
        return None
    diffs = [i for i, (a, b) in enumerate(zip(base_lines, head_lines)) if a != b]
    if len(diffs) != 1:
        return None
    i = diffs[0]
    if not STATUS_LINE_RE.match(base_lines[i]):
        return None
    m = STATUS_LINE_RE.match(head_lines[i])
    if not m:
        return None
    sup = SUPERSEDED_BY_RE.match(m.group(1))
    if not sup:
        return None
    return int(sup.group(1))


def new_adr_supersedes(
    head: str,
    mmmm: int,
    nnnn: int,
    added_paths: List[str],
    rules: Rules,
    cwd: Optional[Path] = None,
) -> bool:
    target_prefix = f"{rules.dir}/{mmmm:04d}-"
    candidates = [p for p in added_paths if p.startswith(target_prefix) and p.endswith(".md")]
    if not candidates:
        return False
    supersedes_re = re.compile(rf"Supersedes:\**\s*ADR-0*{nnnn}\b")
    for path in candidates:
        text = show(head, path, cwd=cwd)
        if text and supersedes_re.search(text):
            return True
    return False


def check(
    base: str, head: str, cwd: Optional[Path] = None, rules: Optional[Rules] = None
) -> Tuple[bool, List[str], int]:
    """Returns (ok, failure_reasons, accepted_checked_count)."""
    rules = rules or default_rules()
    mb = merge_base(base, head, cwd=cwd)
    entries = name_status(mb, head, rules.dir, cwd=cwd)

    failures: List[str] = []
    accepted_checked = 0

    def basename_ignored(path: str) -> bool:
        return Path(path).name in rules.ignored_basenames

    added_paths = [p for (s, p, _np) in entries if s == "A" and not basename_ignored(p)]
    modified_paths = [p for (s, p, _np) in entries if s == "M" and not basename_ignored(p)]
    deleted_or_renamed = [
        (s, p, np)
        for (s, p, np) in entries
        if s in ("D", "R") and rules.numbered_re.match(p) and not basename_ignored(p)
    ]

    for s, p, np in deleted_or_renamed:
        if s == "D":
            failures.append(f"{p}: DELETED — an ADR file must never be removed (immutability rule)")
        else:
            failures.append(
                f"{p}: RENAMED to {np} — an ADR file must never be renamed (numbers are permanent)"
            )

    for path in modified_paths:
        if not rules.numbered_re.match(path):
            continue
        base_text = show(mb, path, cwd=cwd)
        head_text = show(head, path, cwd=cwd)
        if base_text is None:
            continue
        status = base_status_value(base_text)
        if not status or not status.startswith("Accepted"):
            continue  # only Accepted-at-base ADRs are immutability-protected

        accepted_checked += 1
        m = rules.numbered_re.match(path)
        assert m is not None
        nnnn = int(m.group(1))

        # Path 1: status-pointer-only edit to "Superseded by ADR-MMMM" plus a
        # new superseding ADR added in the same range.
        mmmm = only_status_line_changed(base_text, head_text or "")
        if mmmm is not None and new_adr_supersedes(head, mmmm, nnnn, added_paths, rules, cwd=cwd):
            continue

        # Path 2: nonsubstantive marker (in a commit that itself touches this
        # file) + every section body byte-identical + every metadata line,
        # the title and the heading set unchanged.
        if has_nonsubstantive_marker_for_path(mb, head, path, rules.marker, cwd=cwd):
            base_sections = extract_sections(base_text)
            head_sections = extract_sections(head_text or "")
            all_headings = list(base_sections) + [
                h for h in head_sections if h not in base_sections
            ]
            non_identical = [
                h for h in all_headings if base_sections.get(h) != head_sections.get(h)
            ]

            base_sig = extract_header_signature(base_text)
            head_sig = extract_header_signature(head_text or "")
            problems: List[str] = []
            if base_sig["headings"] != head_sig["headings"]:
                problems.append("a '## ' heading was added, removed, or renamed")
            if non_identical:
                problems.append("section(s) not byte-identical: " + ", ".join(non_identical))
            if base_sig["title"] != head_sig["title"]:
                problems.append("H1 title line changed")
            base_meta = base_sig["metadata"]
            head_meta = head_sig["metadata"]
            assert isinstance(base_meta, list) and isinstance(head_meta, list)
            if base_meta != head_meta:
                changed = sorted(
                    {
                        (METADATA_LINE_RE.match(ln).group(1) if METADATA_LINE_RE.match(ln) else ln)
                        for ln in set(base_meta) ^ set(head_meta)
                    }
                )
                problems.append("metadata line(s) changed: " + ", ".join(changed))

            if not problems:
                continue
            failures.append(
                f"{path}: {rules.marker} marker present (on a commit touching "
                f"this file) but " + "; ".join(problems)
            )
            continue

        if mmmm is not None:
            failures.append(
                f"{path}: Status line points to 'Superseded by ADR-{mmmm:04d}' but no "
                f"{rules.dir}/{mmmm:04d}-*.md file was added in this range with a "
                f"'Supersedes: ADR-{nnnn:04d}' header"
            )
        else:
            failures.append(
                f"{path}: Accepted ADR modified beyond the Status line, with no "
                f"{rules.marker} commit-message marker and no valid "
                f"Superseded-by handoff"
            )

    # Numbering sequence for newly added numbered ADRs.
    base_numbers = ls_tree_numbered(mb, rules, cwd=cwd)
    max_existing = max(base_numbers) if base_numbers else 0
    added_numbers = sorted(
        int(m.group(1)) for p in added_paths if (m := rules.numbered_re.match(p))
    )
    if added_numbers:
        expected = list(range(max_existing + 1, max_existing + 1 + len(added_numbers)))
        if added_numbers != expected:
            failures.append(
                f"new ADR numbering: added {added_numbers} but expected a contiguous "
                f"run starting at {max_existing + 1:04d} ({expected}) — "
                f"max existing at merge-base is {max_existing:04d}"
            )

    return (len(failures) == 0, failures, accepted_checked)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--cwd", help="run git as if invoked from this directory")
    args = ap.parse_args(argv)

    cwd = Path(args.cwd).resolve() if args.cwd else None

    try:
        cfg = mergegate_config.load_config(mergegate_config.repo_root(cwd))
    except mergegate_config.ConfigError as e:
        print(f"adr-check: {e}", file=sys.stderr)
        return 1

    try:
        ok, failures, accepted_checked = check(
            args.base, args.head, cwd=cwd, rules=Rules(cfg["adr"])
        )
    except RuntimeError as e:
        print(f"adr-check: error: {e}", file=sys.stderr)
        return 1

    if not ok:
        print("ADR immutability: FAIL", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1

    print(f"ADR immutability: OK ({accepted_checked} accepted ADRs checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
