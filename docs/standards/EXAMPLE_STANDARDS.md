# Example standards (fictional product)

> Derives from `NORTH_STAR.md`; the north star wins on conflict.
> Mechanical checks: `merge-gate.toml` ids listed per rule.

This file shows the shape of one rule. Delete it once a real standards
file exists.

## Rule 1 — A listing is shown in posting order; no code path sorts, ranks or filters the board by anything but the posting time

**Rule.** The board query is `ORDER BY posted_at`; no other ordering, no
scoring column, no "featured" flag, in any layer. A filter the member
chose (distance, produce type) is applied after ordering and shown as a
filter chip they can remove.

**Incident (YYYY-MM-DD).** A well-meant change sorted the board by "most
claimed first" to help new members. Growers noticed their listings
dropping to the bottom within an hour of posting and stopped listing
small quantities. Two weeks of reduced supply before the cause was found.

**Check.** `invariants.checks.no_board_ranking` (pattern
`ORDER BY (?!posted_at)` over `crates/board/**/*.rs`) and
`standards.checks.featured-string` (pattern `featured|sponsored|promoted`
over the listing UI, fail tier).

**Allowed exceptions.** The admin export, which sorts by grower name for
the treasurer's weekly statement — `scripts/invariants-allowlist.txt`
entry `no_board_ranking crates/board/src/export.rs # treasurer export,
not a member-facing surface`.
