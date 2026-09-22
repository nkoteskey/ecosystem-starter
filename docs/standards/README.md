# Standards

Each file in this directory holds the rules for how code in one area is
written. A rule is not a preference: **every rule cites the incident that
produced it**, dated, with what was observed and what it cost. A proposed
rule with no incident goes in a plan as a suggestion, not here.

Where a rule can be checked mechanically, its check lives in
`merge-gate.toml` under `[standards.checks.<id>]` (a defect pattern that
fails or warns) or `[invariants.checks.<id>]` (an architectural rule over
the whole tree), and the rule names its check id. Where it cannot, the rule
says so and names the reviewer's checklist item that covers it.

## Files — create as needed

None of these exist yet; create one the first time an incident produces a
rule for that area. Suggested split:

| File | Would cover |
|---|---|
| `BACKEND_STANDARDS.md` | Error handling, locking, async, storage, network seams, identifiers. |
| `FRONTEND_STANDARDS.md` | State ownership, rendering of ids and money, error surfacing, accessibility. |
| `HARDENING_STANDARDS.md` | Input validation at trust boundaries, secrets handling, dependency pinning, fuzzing expectations. |
| `DATA_COMPATIBILITY.md` | Versioned formats, the n-1 read rule, migration expectations. |
| `OBSERVABILITY_STANDARDS.md` | What is logged, what is never logged, what a health endpoint reports. |
| `DEPENDENCY_RADAR.md` | The periodic sweep: what to re-evaluate, how often, and where the last sweep is recorded. |

`EXAMPLE_STANDARDS.md` in this directory shows one complete rule in the
shape below, for the fictional product used in the templates.

Start each file with the same header:

```markdown
# <Area> standards

> Derives from `NORTH_STAR.md`; the north star wins on conflict.
> Mechanical checks: `merge-gate.toml` ids listed per rule.

## Rule 1 — <one-line rule>

**Rule.** <What to do, in one or two sentences.>

**Incident (YYYY-MM-DD).** <What happened; what it cost.>

**Check.** `standards.checks.<id>` | `invariants.checks.<id>` | reviewer checklist.

**Allowed exceptions.** <Where the pattern is legitimately used, with the allowlist entry that records it.>
```

## Allowlists

`scripts/standards-allowlist.txt` and `scripts/invariants-allowlist.txt`
record the sites where a forbidden pattern is legitimately used, each with
a justification. Removing the justification means removing the entry. The
lists only shrink.
