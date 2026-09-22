# CLAUDE.md — template

This file is the operational reference for anyone (or any agent) working
in the repository: which documents are which, the rules that must not be
broken and the incidents that taught them, and the loop a change goes
through before it lands. Copy it to `CLAUDE.md`, fill in the brackets, and
delete this paragraph.

> **Start here: [`docs/NORTH_STAR.md`](./docs/NORTH_STAR.md).** If anything
> in this file or any other document conflicts with it, the north star wins
> until it is revised there.

## Doc taxonomy

There is exactly **one** north star. Every other document is one of the
kinds below; kinds do not compete with each other and none of them compete
with the north star.

| Kind | Filename pattern | Purpose | Lifespan |
|---|---|---|---|
| **North star** | `docs/NORTH_STAR.md` | The charter: thesis, participants, non-negotiables, backbone, economics, guardrails, legal posture. | Long-lived; revised in place with a changelog. |
| **Operational reference** | `CLAUDE.md` | This file: conventions, hard rules, the review loop, pointers. | Long-lived; updated as conventions evolve. |
| **Compendium** | `docs/COMPENDIUM.md` | Living current-state record: what each component *is* and how components feed each other. | Long-lived; updated when a surface ships or retires. |
| **Spec** | `docs/spec/<TOPIC>_SPEC.md` | The decided design for one subsystem. Derives from the north star and cites it. | Until the subsystem is retired. |
| **Plan** | `docs/<area>/<TOPIC>_PLAN.md` | Work in progress on one area. Derives from the north star; replaceable. | Until the work ships. |
| **Decision record** | `adr/NNNN-<title>.md` | One architecturally significant decision, its alternatives, and its cost. Immutable once accepted. | Forever. |
| **Open questions** | `adr/OPEN-QUESTIONS.md` | What the sources leave undecided, each traced to its source. | Entries promoted to records as they resolve. |
| **Standard** | `docs/standards/<AREA>_STANDARDS.md` | Rules for how code in one area is written, each rule citing the incident that produced it. | Long-lived. |
| **Live status** | `STATUS.md` | What has been done, what is next, what is in progress. | Continuously updated. |
| **Audit** | `docs/audits/<YYYY-MM-DD>-<topic>.md` | Dated snapshot of a read-only review. Input for plans; never canonical. | Frozen at write time. |

Rules:

1. Only `docs/NORTH_STAR.md` may use "north star" in its name. Any new
   architecture or strategy document is a `_PLAN.md` or `_SPEC.md`.
2. Every plan and spec opens with a header stating it derives from the
   north star and that the north star wins on conflict.
3. A plan that needs to contradict the north star revises the north star
   first, in the open, then follows it.
4. No document is archived until its binding decisions exist as decision
   records and its current-state claims live in a living document. Then it
   moves to `docs/archive/` with a one-line pointer stub.

## Hard rules

Every rule below cites the incident that produced it. A rule without an
incident is a preference and belongs in a standards document, not here. A
new incident that reveals a missing rule adds the rule *and* the incident
in the same change.

Write each rule as: **the rule**, in one sentence; **the incident**, dated,
in one or two sentences, with what was observed and what it cost; **the
check**, if one exists — the test, grep invariant or gate that now
enforces it.

- **[Rule].** [One sentence.]
  *Incident (YYYY-MM-DD):* [what happened, what it cost].
  *Check:* [`scripts/invariants-check.sh` id `…` | test `…` | none yet].

Examples of the shape (replace with your own):

- **Every external network call sits behind a trait with a stub
  implementation.** *Incident (YYYY-MM-DD):* a test suite that reached a
  real vendor endpoint passed on one developer's machine and failed on
  every other; two days were lost to "works for me". *Check:* invariant
  `trait_seam` in `merge-gate.toml`.
- **A lock is never held across an `await`.** *Incident (YYYY-MM-DD):* a
  background refresh held a mutex while awaiting a fetch; the UI thread
  waited on the same mutex and the app hung until force-quit. *Check:*
  none mechanical; reviewer's checklist item.
- **Tests never touch the real data directory.** *Incident (YYYY-MM-DD):*
  a test that wrote to the default data path deleted a developer's real
  library. *Check:* every test uses the isolated-directory fixture; a grep
  invariant fails on the default-path constant inside `tests/`.
- **Nothing reaches `main` except through the gate.** *Incident
  (YYYY-MM-DD):* two sessions pushed conflicting edits to the same shared
  crate directly to `main`; the second broke every consumer. *Check:*
  `scripts/git-hooks/pre-push` refuses direct pushes; a branch ruleset
  requires the gate's commit status.

## Parallel sessions, shared-service claims, and the merge gate

1. **Every session works in its own worktree** (`scripts/wt.sh new
   <name>`). The primary checkout is the landing checkout; sessions never
   edit it.
2. **Claim a shared service before editing it** (`python3
   scripts/claims.py claim <service> --intent "…"`). What is shared is
   derived from the dependency graph plus `claims/REGISTRY.toml`; the
   claim is an atomic ref on the remote. Only the holder can land.
3. **Nothing reaches `main` except through the gate** (`scripts/land.sh`).
   The gate is one script (`scripts/full-gate.sh`), configured in
   `merge-gate.toml`, with a zero-regression baseline recorded by test
   name. Mechanism: `docs/MERGE_GATE.md`.

## The change loop

Every non-trivial change goes through four stages, and the person or agent
in each stage is not the one in the previous stage. Where one person does
all four, the stages still happen in order, with a written artefact at
each hand-off.

| Stage | Role | Produces | Reads |
|---|---|---|---|
| **1. Plan** | *planner* | A short plan: the change, the files it touches, the tests that will prove it, the non-negotiables it could brush against. | North star, relevant spec, decision records, open questions. |
| **2. Implement** | *implementer* | The change and its tests, on a worktree branch, with claims held. | The plan. Deviations from the plan are written down, not silently made. |
| **3. Adversarial review** | *reviewer* | A findings list: what is wrong, what is missing, what could be quoted against the project. The reviewer's job is to break it, not to approve it. | The diff, the plan, the hard rules. Never the implementer's summary alone. |
| **4. Final** | *planner* | Arbitration of the findings (fix / accept with reason / reject), then the landing. | The findings and the fixes. |

Rules for the loop:

- The reviewer reads the code, runs the tests, and tries the failure
  modes. "Looks fine" is not a review.
- A finding is fixed, or accepted with a written reason, before landing.
  Silent disagreement is not an option.
- A change that touches a non-negotiable, a payment path, or personal
  data gets a second reviewer.
- The implementer never lands their own change without stage 3 having
  happened. The gate enforces the mechanics; the loop enforces the
  judgement.

## Conventions

- **Naming:** [crate/package/file naming rules].
- **Tests:** every bug fix adds the test that would have caught it; tests
  read as intent, not as coverage.
- **Errors:** no silent catch; every failure a user could notice is
  surfaced to the user.
- **Dependencies:** pinned; a new dependency is a decision, reviewed as
  one; `cargo deny` / the equivalent runs in CI.
- **Commit messages:** imperative subject; a body that says why; trailers
  the gate understands (`Baseline-Drop:`, `[adr-nonsubstantive]`) only
  when their conditions are met.

## Pointers

- North star: `docs/NORTH_STAR.md`
- Decision records: `adr/` (read before proposing an architectural change)
- Open questions: `adr/OPEN-QUESTIONS.md`
- Standards: `docs/standards/`
- Production flip checklist: `docs/PRODUCTION_FLIP_CHECKLIST.md`
- Merge gate: `docs/MERGE_GATE.md`, `merge-gate.toml`
- Live status: `STATUS.md`
