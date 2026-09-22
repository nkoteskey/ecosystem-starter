# The merge gate in this repository

The scripts under `scripts/` are vendored from the `jams-merge-gate`
project; `scripts/VENDORED` records which version. Their mechanism —
worktrees, claims as atomic refs, the local train, the zero-regression
baseline by test name, the gate list — is documented in that project's
`docs/DESIGN.md`, and every configurable value is in `merge-gate.toml`
(schema in that project's `docs/CONFIG.md`).

Day to day:

```sh
bash scripts/wt.sh new <name>                              # a worktree per session
python3 scripts/claims.py claim <service> --intent "…"     # before editing a shared service
bash scripts/full-gate.sh --dry-run                        # what the gate would run
bash scripts/land.sh wt/<name>                             # land, from the primary checkout on main
```

To upgrade the vendored scripts, run the newer `bootstrap.sh` against this
repository; it overwrites the scripts and leaves `merge-gate.toml`, the
allowlists and `ci/baseline.json` alone.
