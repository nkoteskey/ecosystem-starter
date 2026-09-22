# Production flip checklist

> Derives from [`NORTH_STAR.md`](./NORTH_STAR.md); the north star wins on
> conflict.

Every capability that is built "dark" — present in the code but switched
off in production — has a row here. A row is the *condition* under which
the switch may be flipped, who verifies it, and how. Nothing is flipped
until its row is green, and the flip commit links the row.

Rows are never deleted. A capability that is retired keeps its row with
status `RETIRED` and a pointer to the decision record.

## How to read a row

| Column | Meaning |
|---|---|
| **Id** | Stable, never reused (`R1`, `R2`, …). Referenced from code comments and decision records. |
| **Capability** | What the switch enables, in one line, with the flag or config key that controls it. |
| **Condition** | What must be true before flipping. A fact, not an intention. |
| **Verifier** | The role that confirms the condition (not a person's name). |
| **Evidence** | Where the confirmation lives: a test, an audit file, a signed-off review, a vendor letter. |
| **Status** | `DARK` (built, off) · `READY` (condition met, not yet flipped) · `LIVE` (flipped, with date) · `RETIRED`. |

## Rows

| Id | Capability | Condition | Verifier | Evidence | Status |
|---|---|---|---|---|---|
| R1 | Real payment processor behind the `Payments` trait (`PAYMENTS_BACKEND=live`) | Processor contract signed; sandbox round-trip test green; refund path exercised once end to end | Treasurer + reviewer | `docs/audits/<date>-payments-sandbox.md` | DARK |
| R2 | Release signing key in the updater config | A key exists that is not the placeholder; its public half is in the repo; a signed build installs over an unsigned one | Release owner | `scripts/check-release-config.py --release` green | DARK |
| R3 | Third-party crash reporting | Privacy review confirms no payload field can carry user content; opt-in switch exists and defaults off | Privacy reviewer | `docs/audits/<date>-crash-reporting.md` | DARK |
| R4 | Public listing board reachable outside the pilot group | Abuse-handling path exists (report, hide, ban); rate limit on listings; committee sign-off | Committee + reviewer | Minutes reference + test names | DARK |

## Rules

1. A `DARK` capability's code path is reachable in tests and in a
   development build, never in a production build without the flag.
2. Flipping is a commit that changes exactly the flag and this file's row,
   with the evidence linked. It goes through the merge gate like any other
   change.
3. A row's condition may be tightened at any time; loosening one is a
   decision record.
4. `standards-check` may carry a warn-tier check for placeholder values
   (`R2` above is the usual example) so a forgotten placeholder is visible
   on every gate run without permanently reddening main.
