# Keeper League Rules — changelog

History of how the rules doc got to its current wording. Lifted out of
`keeper_rules_v3.md` so the rules page shows the rules and nothing else;
kept because section 8 asks that rule changes stay documented.

## Fourth Draft (8.13.2026), superseding the Third Draft (8.9.2026)

The first **rule change** in this document rather than a clarification, and the
first carried by a vote under section 8. Three proposals were put to the league
at `/rules-vote/`, all three on section 4 (roster composition). **All ten
managers voted.**

1. **Proposal 1 — PASSED.** The limit of one keeper with a current-year cost in
   **Rounds 1–2** is **removed**. Any number of premium keepers is now legal;
   the cost rules are the only deterrent, since two Round-2-cost keepers take
   your Round 2 and — by the same-round collision rule — your Round 1 as well.
   (Two Round-1-cost keepers remain impossible: section 3 refuses a keep when
   you own no pick in the cost round or earlier. That is arithmetic, not a
   composition limit.)
2. **Proposal 2 — PASSED.** The requirement that a **3-keeper set include one
   keeper costing Round 8 or later** is **removed**. An all-premium trio is now
   legal if you can pay for it.
3. **Proposal 3 — FAILED.** The proposed cap of **at most one keeper with a
   Round-8-or-later cost** was rejected. Late-round stacking stays legal:
   three Round-8-cost keepers still cost picks **8, 7 and 6** under the
   collision rule, and the league has now looked straight at that and declined
   to change it. If someone does stack three sleepers, the fallback already
   drafted in `docs/league_votes.md` is Option C — steeper collision pricing
   for the second late keeper — not the cap that was just voted down.

**Combined effect: section 4 had exactly two rules and both were repealed, so
there are now no roster-composition limits at all.** The section is kept (and
says so) rather than deleted, because the numbering of sections 5–8 is
referenced throughout the rules and the code. What still constrains a keeper
set is only: at most three keepers (section 1), escalation and the 3-keep
maximum (section 2), owning the picks to pay for it (section 3), and
eligibility (section 5).

The worked example "Composition limit (Isaac)" was replaced accordingly: Jeanty
and Bowers are now both keepable, at the price of Isaac's Rounds 2 and 1.

## Third Draft (8.9.2026), superseding the Second Draft (8.20.2025)

Clarifications that filled gaps the old doc did not address, not rule changes.

1. Declaration deadline set to **7 days before the draft**; declarations submitted privately by text to the commissioner, final once submitted, revealed to the league after the deadline (was TBD).
2. **Missing-pick rule added**: no pick in the cost round → forfeit next-earlier owned pick (was unaddressed).
3. **Cost follows the player**: base cost is fixed on draft day and unaffected by drops/waivers/trades — closes the drop-and-re-add loophole (old wording could be read as any waiver add costing Round 8).
4. **Same-round collision rule added**: second keeper at the same cost round costs the next-earlier pick (was unaddressed).
5. **Keep history follows the player** across trades (was implied, now explicit).
6. Composition limits defined by **current-year keeper cost** after escalation (was ambiguous "draft cost").
7. Escalation wording clarified: cost moves **one round earlier** per repeat keep (matches the existing chart).
8. Eligibility clarified: playoff weeks count as started weeks; **IR counts as rostered**.
9. **Draft order section added**: reverse final finish for playoff teams (picks 5–10); consolation teams earn choice of picks 1–4 by consolation finish; snake format; slots tradeable. For the 2026 draft only, the order is Yahoo's final 2025 standings reversed (locked table above), since these rules were adopted after the 2025 season.
10. **Trade rules added**: only next season's picks tradeable; pick trading freezes at the declaration deadline.
11. **Governance section added**: commissioner rulings, offseason-only majority-vote rule changes.
