# How to write a north star

A north star is the one document every other document in the repository
defers to. It says what the product is, who it serves, what cannot change,
how it is built, how money moves, how you will notice when you are drifting,
and what legal shape you are operating in. Plans, specs and decision
records derive from it and cite it; when they conflict with it, it wins
until it is revised in place.

This file is a method, not a manifesto. Each section below says **what to
write**, **why it matters**, and shows **one worked example** for a
fictional product — a neighbourhood food co-op app called *Crate*, where
local growers list surplus produce, members order it, and volunteers
deliver it by bike — so the shape is visible without the words being
reusable. Do not copy the example; write your own.

Keep the whole document under roughly ten pages. If a section is growing
past a page, it is a spec, not a north star; move the detail out and leave
a pointer.

Naming rule: exactly one file in the repository is called `NORTH_STAR.md`.
Every other architecture or strategy document is a `*_PLAN.md` or
`*_SPEC.md`, and each opens with a line saying it derives from the north
star and that the north star wins on conflict.

---

## 1. The thesis, in one paragraph

**What to write.** One paragraph, plain language, no product names you
have not introduced yet. State the problem, the mechanism, and the
outcome. If you cannot write it in one paragraph, you do not yet know what
you are building.

**Why it matters.** Every later argument about scope ("should we add X?")
is settled by asking whether X serves this paragraph. A thesis that is
three pages long settles nothing.

**Worked example.**

> Small growers within cycling distance of a town produce more than they
> can sell at market and less than a wholesaler will collect. Crate lets a
> grower list what is spare on the day, lets members who have pre-paid a
> monthly share claim it, and routes a volunteer rider to move it the same
> evening. Growers get paid for produce that would have composted; members
> get food picked that morning; the co-op keeps nothing but its running
> costs. The software's only job is to make the daily match fast enough
> that the produce is still fresh.

---

## 2. Participants and their roles

**What to write.** List every kind of participant — human or software —
and for each, one sentence on what they contribute and one on what they
receive. Include the participants you do not control (a payment
processor, a delivery partner, an operating system) and mark them as such.
If two participants have the same role, merge them; if one participant
has two roles, split them.

**Why it matters.** Most drift comes from a feature that quietly serves a
participant the thesis never named (an advertiser, a data buyer, an
investor's dashboard). Naming the roles precisely makes that visible.

**Worked example.**

| Participant | Contributes | Receives | Controlled by us? |
|---|---|---|---|
| Grower | Same-day listing of surplus, honest quantities | Payment at the listed price, weekly | No — a member of the co-op |
| Member | A pre-paid monthly share; claims within a window | Produce, a receipt, the grower's name | No |
| Rider | An evening of cycling; confirms pickup and drop | A per-delivery stipend; nothing else | No |
| The app | Matching, routing, receipts, ledgers | Nothing — it holds no balance | Yes |
| Payment processor | Card capture and payouts | Its published fee | No — a vendor |
| Co-op committee | Sets the share price and the stipend once a quarter | Read-only reports | No — governance, not software |

---

## 3. The non-negotiables

**What to write.** A numbered list, each item one sentence, each item a
thing that would make the product something else if it changed. Not
preferences ("we like Rust") — constraints ("no participant's data leaves
their device unencrypted"). Aim for between five and fifteen. For each,
add a half-sentence on what would be lost if it were broken.

**Why it matters.** These are the tests a future maintainer — or a future
you under commercial pressure — runs a proposal against. If a proposal
breaks one, the answer is "revise the north star first, in the open," not
"just this once."

**Worked example.**

1. The app never holds money. Payments go from member to processor to
   grower; the co-op's cut is invoiced separately. *Otherwise the co-op
   becomes a money transmitter and the committee becomes a bank.*
2. A grower's listing is their word. The app never adjusts a quantity or
   price on their behalf. *Otherwise growers stop trusting the receipt.*
3. Riders' locations are known only during a delivery and are deleted
   when it is confirmed. *Otherwise volunteering means being tracked.*
4. No ranking, no featured growers, no paid placement. Listings are shown
   in the order they were posted. *Otherwise the app starts choosing
   winners.*
5. Everything a member sees about a grower, the grower can see about
   themselves. *Otherwise there is a hidden profile.*
6. The app works without an account for browsing and with a co-op
   membership number for everything else; there are no passwords and no
   third-party logins. *Otherwise the co-op outsources identity.*

---

## 4. The architectural backbone

**What to write.** The three to five structural decisions that everything
else is built on: where computation runs, where data lives, what the
trust boundary is, what you will not run. Diagram optional; a labelled
list is enough. Then name the seam — the interface behind which every
external dependency sits — so it can be replaced or stubbed. Finish with a
paragraph on the "early posture": which of these you are deliberately not
building yet and why that is safe.

**Why it matters.** The backbone is what a new contributor needs to hold
in their head to make a change without asking. It is also what an
architectural decision record checks itself against.

**Worked example.**

- **On-device first.** Listing, claiming and routing all run in the member
  and grower apps; the only shared state is the day's listing board and
  the ledger of claims.
- **One small shared board.** The board is a replicated, append-only log
  that every app mirrors. There is no application server; the co-op runs
  one relay node so phones can find each other, and that relay stores
  nothing it can read.
- **Money is someone else's problem.** Card capture and payouts are behind
  a `Payments` trait with exactly one production implementation and one
  stub. No code outside that module knows the processor's name.
- **Identity is a membership number plus a device key.** The device key
  signs every listing and claim; the membership number is what the
  committee recognises. Losing a phone means re-pairing, not recovering a
  password.
- **Early posture:** for the first year the relay is a single machine
  under the committee's desk, backups are a nightly export the treasurer
  emails to themselves, and the routing is greedy nearest-first. Each of
  these is replaceable behind its seam without touching the apps.

---

## 5. The economic model

**What to write.** Every flow of money, one subsection each: who pays,
who is paid, what the software's cut is (a number or an explicit zero),
where the operating costs come from, and what is free. Then one paragraph
on the mechanism that settles a flow (what event triggers payment, what
record proves it). If a number is a placeholder, say so and say who
decides it.

**Why it matters.** Vague economics are where trust dies. A participant
who can compute their own payout from the rules in this section will stay;
one who cannot will assume the worst.

**Worked example.**

**Flow 1 — Member to grower.** A member's monthly share is captured by the
processor on the first of the month. Each claim debits the share at the
grower's listed price. Growers are paid out weekly, the sum of their
fulfilled claims minus the processor's published per-payout fee. The app
takes nothing from this flow.

**Flow 2 — Member to co-op.** A flat monthly membership fee, set by the
committee, invoiced separately from the share. This pays for the relay
node, the processor's fixed costs, and the rider stipend pool.

**Flow 3 — Co-op to rider.** A per-delivery stipend from the pool, paid
weekly, computed from confirmed drops. Riders are volunteers; the stipend
is a cost reimbursement and the amount is on the committee's minutes, not
in the app.

**What is free.** Browsing the board. Listing produce. Cancelling a claim
before the pickup window.

**Settlement mechanism.** A claim is a signed record (member key, listing
id, quantity, time). A fulfilment is a second signed record from the rider
at the drop. The weekly payout is the sum of fulfilments per grower; any
member can recompute their own statement from the board.

---

## 6. Anti-drift guardrails

**What to write.** The mechanisms — not intentions — by which the
repository notices it is departing from the sections above. Typical
guardrails: a decision-record log that cannot be edited in place; grep
invariants that fail CI when a forbidden pattern appears; a checklist any
"go to production" flip must pass; a standing question list for things the
north star has not decided. For each, say where it lives and what runs it.

**Why it matters.** A north star that is only read on day one is a
memory, not a constraint. Guardrails convert its sentences into failing
checks.

**Worked example.**

- **Decision records** in `adr/`: numbered, immutable once accepted,
  superseded rather than edited. CI refuses a change that edits an
  accepted record's substance (`scripts/adr-check.py`).
- **Grep invariants** in `merge-gate.toml`: no network client constructed
  outside the `Payments` and `Relay` modules; no listener bound anywhere;
  no "featured" or "sponsored" string in the listing code. CI fails on a
  new hit (`scripts/invariants-check.sh`).
- **Production flip checklist** in `docs/PRODUCTION_FLIP_CHECKLIST.md`:
  every row is a condition with a named verifier; nothing is switched on
  in production until its row is green.
- **Open questions** in `adr/OPEN-QUESTIONS.md`: what the north star has
  not decided, each traced to the sentence that leaves it open. A question
  becomes a decision record when it is answered; it is never answered
  silently in code.
- **This document itself** is revised in place, in the open, with a
  dated changelog at the bottom. A plan that needs the north star to
  change edits the north star first.

---

## 7. Legal posture

**What to write.** Not legal advice — the *shape* of the operation as you
understand it, so that engineering decisions do not accidentally change
it: whether you hold money, whether you are a marketplace or a vendor,
what personal data you collect and under which basis, what jurisdiction
you operate in, what you will refuse to build until a qualified person has
looked. Mark every item that is unverified as unverified. Date the section.

**Why it matters.** The most expensive drift is the kind where a feature
quietly makes you a different kind of entity. Writing the posture down
lets a reviewer ask "does this change it?" of every proposal.

**Worked example.**

- **Money.** The co-op does not hold member funds; the processor does,
  under its own licence. The share is a pre-payment for goods, not stored
  value. *Unverified with an accountant; a question stands until it is.*
- **Marketplace or vendor.** The co-op is a marketplace: growers sell,
  members buy, the co-op facilitates. Receipts name the grower. Product
  liability sits with the grower; the membership agreement says so.
- **Personal data.** Membership number, a delivery address, a device
  key, and rider location for the duration of a delivery. Basis: the
  membership contract. Retention: addresses for the membership term,
  locations until the drop is confirmed, nothing else.
- **Jurisdiction.** One country, one region, one co-op. Anything that
  would make this cross a border — a second town, a payout to a grower
  abroad — is a question for the committee, not a feature.
- **Not until reviewed.** Any form of credit to members; any reselling of
  claims between members; any data shared with the town council.

---

## Changelog

Keep a dated, one-line-per-change log here. A north star without a
changelog invites the question "which version did we agree to?".

- YYYY-MM-DD — first version.
