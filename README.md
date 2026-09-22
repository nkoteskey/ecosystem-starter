# ecosystem-starter

A template repository for a product that is built as a small number of
applications over shared code, where the architecture and the rules are
written down first and enforced mechanically afterwards. It is meant to be
used as a hosting-site template ("Use this template"), then stamped with
`bootstrap.sh`.

## What it does

It gives a new repository, on day one:

- **A method for writing the north star** (`NORTH_STAR_TEMPLATE.md`): for
  each section — thesis, participants, non-negotiables, architectural
  backbone, economic model, anti-drift guardrails, legal posture — what to
  write, why it matters, and one worked example for a fictional product.
- **An operational reference** (`CLAUDE_TEMPLATE.md`): the document
  taxonomy, the hard-rules pattern where every rule cites the incident
  that produced it, and the plan → implement → adversarial review → final
  loop with planner / implementer / reviewer roles.
- **Decision records** (`adr/`): README with the numbering and
  immutability rules, `0000-template.md`, and the `OPEN-QUESTIONS.md`
  pattern for what the sources leave undecided.
- **A production flip checklist** (`docs/PRODUCTION_FLIP_CHECKLIST.md`):
  one row per capability built dark, with the condition, verifier and
  evidence required before it is switched on.
- **A standards skeleton** (`docs/standards/README.md`): the file list and
  the rule shape (rule, incident, check, allowed exceptions).
- **The merge gate** (`scripts/`, `merge-gate.toml`, `claims/`): the
  vendored `jams-merge-gate` scripts — worktrees, shared-service claims as
  atomic refs, a local train, a zero-regression baseline by test name, and
  the policy checks (decision-record immutability, grep invariants,
  standards patterns). Mechanism in `docs/MERGE_GATE.md`.
- **`bootstrap.sh`**: stamps the repository name, adds the remote, and
  writes `merge-gate.toml` for the app directories you name.

## Why it exists

The hard part of a new repository is not the first commit; it is the
tenth week, when three people are landing changes to the same shared
crate and nobody can say which document is authoritative. Writing the
charter first, and making the checks that enforce it part of the landing
path from the start, costs a day. Retrofitting them costs a quarter.

## How to run it

```sh
# after "Use this template" and cloning (in place; the shipped merge-gate.toml
# is replaced only because --apps is given together with --force-config):
./bootstrap.sh --name my-product --remote https://example.invalid/me/my-product.git \
  --apps web,admin --force-config
# or keep the shipped one-app config and edit it by hand:
./bootstrap.sh --name my-product --remote https://example.invalid/me/my-product.git
$EDITOR docs/NORTH_STAR.md CLAUDE.md          # fill in the method sections
bash scripts/full-gate.sh --dry-run           # see what the gate would run
bash scripts/wt.sh new first-change           # a worktree; installs the pre-push hook
```

Or into an existing repository:

```sh
/path/to/ecosystem-starter/bootstrap.sh /path/to/repo --apps web
```

Requirements: bash 3.2+, git 2.31+, python3 (3.9+). `cargo` and `npm` only for
the gates that use them.

To make this repository a template on the hosting site, enable the
"template repository" setting; nothing here depends on secrets or on the
repository's name.

## What it will not do

- It does not write your north star. The template is a method with a
  worked example for a fictional product; every sentence in your
  `docs/NORTH_STAR.md` has to be yours.
- It does not contain product code, an application skeleton, or a UI
  framework choice. `merge-gate.toml` assumes a Rust workspace with
  optional TypeScript frontends under `apps/<name>/`; change it if your
  layout differs.
- It does not replace hosted branch protection. See
  [docs/MERGE_GATE.md](docs/MERGE_GATE.md) for what the pre-push hook
  does and does not defend against.

## How this repository is reviewed

CI lints the scripts (`ruff`, `shellcheck`), runs the gate's own test
suite, validates the shipped `merge-gate.toml`, and runs
`full-gate.sh --dry-run` against this repository. The templates are prose;
review them for clarity and for anything product-specific that leaked in.

`SOURCE` names the upstream commit the vendored scripts were extracted
from; `scripts/VENDORED` says how to upgrade them.

## License

MIT — see `LICENSE`.
