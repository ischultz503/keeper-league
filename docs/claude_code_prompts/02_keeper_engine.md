# Claude Code Prompt — Phase 2: Keeper Engine

Paste everything below the line into Claude Code once Phase 1 is reviewed and committed.

---

Re-read CLAUDE.md (the Product vision and Roadmap sections changed) and docs/keeper_rules_v3.md. Rules doc is the source of truth. Teaching mode as before: small steps, explain the Django concepts as you go.

Build Phase 2 — the keeper engine and its data model. No draft board UI yet (that's Phase 3); this phase is the invisible machinery plus admin tooling and a rules page.

## 1. New models

- `DraftSlot`: season FK, team FK, slot (1–10, unique per season). The locked draft order. (Isaac will provide the 2026 order; make it admin-editable and leave it empty for now.)
- `DraftPick`: season FK, round (int), original_team FK (whose slot it is), current_team FK (who owns it now, after trades), forfeited (bool, default False). Add a management command `generate_picks --season 2026 --rounds N` that creates all picks from DraftSlots (original_team = current_team initially). Snake order is derived from slot + round parity — add a helper/property `overall_position` that computes it; don't store it.
- `PickTrade`: season FK, pick FK, from_team FK, to_team FK, note, date. Saving one updates the pick's current_team. Register in admin — this is how Isaac records trades (e.g., Marcus's 2026 R4 → Isaac).
- `KeeperSelection`: season FK, team FK, roster_entry FK (the player, from the prior season's roster), cost_round (int, computed at save), burned_pick FK to DraftPick, created via admin only. Unique: one selection per player per season; max 3 per team enforced in validation.
- Eligibility on `RosterEntry`: add `eligible` (nullable boolean: None = not yet reviewed, treated as ineligible by validation but displayed as "pending") and `eligibility_note` (short text, e.g. "started 6 wks"). Commissioner sets these in admin; list-editable for speed (~170 rows).

## 2. The engine — pure Python, this is the heart of the app

Create `league/keeper_engine.py` with plain functions (no request objects, no rendering) implementing rules doc sections 2–5. Key functions, roughly:

- `current_cost(roster_entry, times_kept_before)` → cost round this year: base cost (draft round 1–8, else 8), escalated one round earlier per prior consecutive keep. Raise/flag if escalation passes Round 1 or keeps exceed 3. (Escalation won't fire until 2027 — build and test it now anyway. Keep history = prior seasons' KeeperSelections for that player, following the player across teams.)
- `resolve_burned_picks(team, season, keeper_costs: list[int], chosen_picks: dict | None)` → which DraftPicks get forfeited, applying rules doc section 3: use an owned, unforfeited pick in the cost round; if none, walk to the next-earlier owned pick (missing-pick rule); two keepers landing on the same round → second walks earlier (collision). Process keepers in ascending cost-round order (most expensive first) so collisions resolve deterministically. When a team owns multiple picks in a round, default to the team's original pick but accept an explicit choice. If no pick exists at or earlier than the cost round → that keeper is impossible; report it, don't crash.
- `validate_keeper_set(team, season, roster_entries)` → structured result (valid: bool, errors: list, warnings: list, burned_picks). Checks: ≤3 keepers; each eligible (`eligible is True`); ≤1 keeper with *current-year* cost in rounds 1–2; if 3 keepers, ≥1 with current-year cost ≥8; keep-count limits; burned-pick resolution succeeds.

Write thorough unit tests for the engine — this is the phase's real deliverable. Cover at least: base costs (R5→5, R8→8, R9→8, undrafted→8); escalation chains incl. R2→R1→impossible and the cap at 3 keeps; missing-pick walk (the real case: team traded away its R4, keeps an R4-cost player → burns R3); collision (two R8-cost keepers → R8+R7); collision + missing-pick combined; multiple-picks-in-round choice; composition limits using current-year cost (an escalated R3→R2 player counts toward the rounds-1–2 limit); 3-keeper set with no R8+ cost → invalid; ineligible and pending players → invalid.

## 3. Commissioner entry via admin

Customize the KeeperSelection admin so that after the (real-world) deadline Isaac can enter each team's keepers: on save, run `validate_keeper_set` for that team's full set and block save with clear error messages if illegal; auto-set cost_round and burned_pick (honoring an explicit pick choice when a team has two picks in a round); mark burned picks forfeited. Deleting a selection un-forfeits its pick. Add a simple admin action or link "validate team's keepers" that shows the structured result.

## 4. Two small pages

- `/rules/` — render docs/keeper_rules_v3.md as clean HTML (add the `markdown` package to requirements; read the file at request time so doc edits show up without a deploy). Add it to the site nav.
- `/my-keepers/` — for the logged-in manager: their roster with, per player: eligibility status, current-year keeper cost, and keep count; plus a plain-language note that keepers are declared by text to the commissioner by the deadline, and this page is for planning. (Interactive set-picking comes with the board in Phase 3 — keep this read-only.)

## 5. Wrap up

`makemigrations` + `migrate`, `manage.py check`, full test suite green, then stop and summarize what exists and where the engine's entry points are for Phase 3. Commit.

Reminders: no voluntary overpay (earlier picks burn only via the forced rules); no in-app declaration flow anywhere — managers text the commissioner; keeper business logic stays in keeper_engine.py, not in views/admin/templates.
