# Architecture Decision Records

This directory is the durable log of architecturally significant decisions.
A record captures *why* a decision was made — the alternatives that were on
the table, why the chosen one won, and what it costs — so that a future
contributor can understand the shape of the system without reverse-
engineering it from the code.

> **Relationship to the north star.** Records derive from
> [`docs/NORTH_STAR.md`](../docs/NORTH_STAR.md). The north star says *what*
> the product is and what cannot drift; records say which *decisions*
> implement that. If a record conflicts with the north star, the north
> star wins until it is revised — at which point the record is superseded,
> never silently edited. Records do not replace specs or plans; they record
> the decision points those documents elaborate.

## Numbering

- Each record is a file `NNNN-kebab-case-title.md`, `NNNN` zero-padded,
  four digits, strictly sequential from `0001`. `0000-template.md` is the
  template and is not a record.
- Numbers are assigned once and never reused. A superseded or rejected
  record keeps its number and its file forever.
- The next number is `max + 1` regardless of earlier statuses. Gaps are
  neither created nor filled. `scripts/adr-check.py` enforces this.

## Status lifecycle

| Status | Meaning |
|---|---|
| **Proposed** | Drafted, under discussion. May still change. |
| **Accepted** | In force. **Immutable** (below). |
| **Superseded** | Replaced by a later record, named in the header. Kept for history. |
| **Rejected** | Considered and deliberately not adopted. Kept so it is not re-litigated. |
| **Deprecated** | The subsystem it governed was retired. |

The header also records **how** a record reached Accepted: *Prospective*
(ratified by review before the work shipped) or *Retrospective — ratified
by shipped implementation* (the decision was made and built first; the
record documents it after the fact, citing the code and documents that
ratify it).

## Immutability rule

**An accepted record is immutable.** Its Context, Decision Drivers,
Considered Options, Decision and Consequences are never edited to reflect
a change of mind. The point of the log is that it records what was decided
*and when*.

If a decision changes:

1. Write a new record (next number) whose Context explains what changed.
2. In the new record's header, add `Supersedes: ADR-NNNN`.
3. In the old record, change only its Status line to `Superseded by
   ADR-MMMM`. This is the one permitted edit to an accepted record.

Typo and broken-link fixes that leave the five canonical sections, the
title, the Status line and the heading set byte-identical are allowed with
`[adr-nonsubstantive]` in the commit message of the commit that touches
the file. `scripts/adr-check.py` enforces all of this mechanically.

## Recording a retrospective decision

1. **Trace it to a source.** Cite the document or code that records the
   decision. Do not invent a decision you cannot trace. If it was never
   settled, it belongs in `OPEN-QUESTIONS.md`.
2. **Reconstruct the alternatives honestly.** If the losing options are
   not documented, say so rather than fabricating them.
3. **State the cost.** *Consequences → Negative* must name what the
   decision costs. A record with no downside is incomplete.
4. Header: `Status: Accepted` / `Acceptance: Retrospective — ratified by
   shipped implementation`. Date is the authoring date; the original
   decision date goes in Context.

## Using records when proposing a change

Read this directory before proposing an architectural change. A proposal
must not contradict an accepted record without first drafting a
superseding record for review.

## Index

```sh
grep -H '^# ' adr/[0-9]*.md | sed 's/adr\///; s/:# / — /'
```
