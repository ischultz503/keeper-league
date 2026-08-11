# Open Questions for League Vote

Running list of rule questions to put to the league. Per the governance rule, changes pass by majority vote and take effect between seasons. The site implements the current written rules until a vote changes them.

**Three of these are on the ballot now.** They are live at `/rules-vote/` on the site, seeded by `python manage.py seed_rules_poll`. The site records the votes and shows the counts; it does not decide who won — Section 8 says "majority vote" without saying majority of what, so the commissioner records each outcome by hand. A passed change is then applied by editing `docs/keeper_rules_v3.md` and adding an entry to `docs/keeper_rules_changelog.md`. The app never rewrites the rules doc.

Items are numbered by ballot position while a vote is running.

---

## 1. Allow more than one keeper costing Rounds 1–2

**The situation:** Section 4's first bullet allows only one keeper with a current-year cost in Rounds 1–2. The question is whether the cost rules already do that job on their own.

**Question: should the Rounds 1–2 limit be dropped?**

**Option A — leave it as-is.** One premium keeper per team, full stop. No rule change.

**Option B — delete the bullet.** No limit on how many keepers carry a Rounds 1–2 cost. The price is the deterrent: two Round-2-cost keepers cost your Round 2 and, by the same-round collision rule, your Round 1 as well — you do not pick until Round 3. Escalation makes it self-limiting, since a Round 1 cost cannot be kept again at all and a Round 2 cost becomes a Round 1 the next year and then dies.

**Against:** it concentrates keeper value at the top, and with Proposal 2 also passed the most aggressive legal set becomes Round 1 + Round 2 + Round 3 — three high picks returned, first pick in Round 4. Disclosure: the current rule is what stops the commissioner keeping both Jeanty and Bowers this year, so he benefits from it passing.

**Worth knowing:** two Round-1-cost keepers stay impossible whatever happens. The second would need a pick earlier than Round 1, and Section 3 refuses a keep when you own no pick in the cost round or any earlier round.

**Status:** ON THE BALLOT — Proposal 1 of the 2026 vote (Option B).

---

## 2. Drop the requirement that a 3-keeper set include a Round 8+ keeper

**The situation:** Section 4's second bullet says a 3-keeper set must include at least one keeper costing Round 8 or later. It is the only thing currently preventing an all-premium trio.

**Question: should the Round 8+ requirement be dropped?**

**Option A — leave it as-is.** Every 3-keeper set keeps at least one cheap player. No rule change.

**Option B — delete the bullet.** A Round 3 + Round 4 + Round 5 set costs picks 3, 4 and 5, which is a real price, and there is no reason the rules should refuse it. As written the rule punishes teams that drafted well in the middle rounds by making them hold a scrub they don't want in order to keep two players they do.

**Against:** it is the only ceiling on how top-heavy a keeper set can be. Remove it and the ceiling is set entirely by what a team is willing to pay.

**Worth knowing:** this and Proposal 3 are not opposites. If Proposal 2 fails and Proposal 3 passes, a 3-keeper set must contain **exactly one** Round-8-or-later keeper — at least one from the rule that stayed, at most one from the rule that arrived. Coherent, possibly even good, but it should be voted for on purpose rather than arrived at by accident.

**Status:** ON THE BALLOT — Proposal 2 of the 2026 vote (Option B).

---

## 3. Late-round keeper stacking

**The situation:** As written, the rules allow keeping up to THREE players who all carry a Round 8 cost (late-round picks and waiver pickups). The composition rules don't prevent it, and the same-round collision rule prices them at picks 8, 7, and 6. Keeping three sleepers for your 6th–8th round picks is strong value and someone will eventually do it.

**Question: should stacking late-round keepers be limited?**

**Option A — leave it as-is.** Three sleepers cost picks 8, 7, and 6. Rewards good waiver-wire work and late-round drafting. No rule change.

**Option B — max one keeper with a Round 8+ cost.** Simple and airtight. Combined with the existing rules, a 3-keeper set would then be: at most one early (rounds 1–2), at most one late (8+), so always at least one mid-rounder. Downside: a team with two legit sleeper hits must give one back to the pool, and a legitimate Round 8 draft pick is caught exactly as hard as a waiver pickup. The Round 8 line is also arbitrary — two Round-6-cost keepers cost picks 6 and 5 and stay perfectly legal, which is nearly as cheap for nearly as much value.

**Option C — steeper collision pricing for late-rounders.** Keep the flexibility but tax it: the second Round-8-cost keeper costs a Round 4 pick (instead of Round 7 via the normal collision rule). Exact price to be agreed. Downside: more complex, and a second special-case in the rules.

**Definition, so this doesn't become an argument in 2027:** "current-year cost" means **after escalation**, the same convention Section 4 already uses. A waiver pickup on his second keep costs Round 7, so he no longer counts against this cap. That is intentional — he isn't cheap any more.

**Status:** ON THE BALLOT — Proposal 3 of the 2026 vote (**Option B only**). Option C is deliberately not on the ballot and stays written down here as the fallback if Proposal 3 fails; the ballot's own text points managers at it and asks them to say so in the note box if they prefer it.

---

*(Add new items above this line as they come up.)*
