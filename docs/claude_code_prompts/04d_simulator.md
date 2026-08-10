# Claude Code Prompt — Phase 4 Part D: Draft Simulator (amended)

Supersedes Part D in `04_scenarios.md`. Paste below the line into Claude Code once Part C
(`c0e4732`) is committed. Part E in `04_scenarios.md` is unchanged and still runs after this.

Three things changed from the original Part D text, all found by reading the code Part C
actually shipped:

1. A prerequisite (D0) — the `Player` table currently holds only 2025 rostered players, so
   simulated picks would draw from last year's rosters with no rookies and no free agents.
2. Simulate must be a **POST returning JSON**, never a GET with the sandbox in the query
   string. The original wording didn't say, and the obvious reading leaks.
3. Sandbox checkbox state has to survive the redirect that every prediction lock causes.

---

Re-read `CLAUDE.md`, `docs/keeper_rules_v3.md`, and `league/views.py` (the board view and
`keeper_preview` in particular — the new endpoint mirrors its access-control shape). Teaching
mode as before: explain the Django and JS concepts as you introduce them, and say what you
chose and why when a call is not obvious. Keeper math stays in `league/keeper_engine.py`.
Simulation logic goes in a new module `league/draft_sim.py` — no request or session objects
in it, so it is unit-testable from fixtures alone. Commit after each part.

## Part D0 — Make the draft pool real (do this first)

`import_adp` currently only calls `bulk_update` on existing `Player` rows; it never creates
any. Every `Player` came from `import_rosters`, so the table is exactly the ~160 players
someone rostered in 2025. A draft simulator drawing from that pool has no 2026 rookies and
no free agents in it — the top of the board will be visibly, embarrassingly wrong, and it
will look like a simulator bug rather than a data gap.

- Add a `--create-missing` flag to `import_adp`. With it set, an ADP row that matches no
  existing `Player` creates one (name, position, `nfl_team`, `adp`, `adp_updated`) instead of
  landing in the unmatched report. Default OFF — the current conservative behaviour stays the
  default, and creating rows is opt-in.
- Add `--create-missing-limit N` (default something like 250) so a full-NFL feed does not
  create a thousand kicker rows. Create in ADP order, best first.
- These new players have no `RosterEntry`, which is correct — they are free agents. Before
  you finish, grep for code that assumes every `Player` has a roster entry and say what you
  found. Fix anything that breaks.
- Adjust `report()` so "stranded: our player with no ADP" is still the actionable signal and
  isn't drowned by the newly created rows.
- Tests: `--create-missing` creates the right rows and is idempotent on re-run; without the
  flag, behaviour is byte-identical to today; the limit is respected.

Tell me the exact command to run afterwards, and what to eyeball in its output.

## Part D1 — `league/draft_sim.py`

A pure module. Suggested shape, adapt if the code wants otherwise — but keep it model-in /
dataclass-out, matching `keeper_engine`'s existing style rather than inventing a second one:

```
@dataclass(frozen=True)
class SimPick:
    pick_id: int
    round: int
    team_id: int
    overall: int
    player_id: int | None
    source: str   # 'keeper' | 'prediction' | 'sandbox' | 'sim' | 'burned' | 'empty'

def simulate_draft(*, slots, picks, burned_pick_ids, taken_player_ids, pool, rounds) -> list[SimPick]
```

Rules, and put every one of them in a small on-page legend so nobody has to read the source
to know what they're looking at:

- Walk the picks in true snake order (`engine.snake_overall`). A cell whose pick is burned by
  a keeper, prediction, or sandbox selection yields no simulated pick — that team simply does
  not pick there. Burning a cell must **not** shift anyone else's position; the slot is dead,
  not removed.
- Otherwise take the best available by ADP. Players with no ADP sort last. Tie-break by
  `player_id` so the same inputs always produce the same board — a simulator that reshuffles
  on reload is useless for comparing scenarios.
- Positional caps: in rounds 1–8, a team never takes a second QB or a second TE (counting
  kept, predicted, sandboxed, and already-sim-filled players), and never takes a K or DEF.
  In rounds 9+ allow a second QB/TE; allow K and DEF only in the final two rounds.
- Never sim-fill a player who is kept, predicted, or sandboxed anywhere in the league.
- Pool exhaustion yields `source='empty'`, not an exception.
- If a user's predicted set for some team is illegal (the engine already flags these, see
  `_place_predictions`), simulate from whatever the engine resolved anyway. A warned column
  still simulates; it does not opt out.

Unit tests for all of the above, plus a determinism test.

## Part D2 — The simulate endpoint

`POST /board/simulate/` → `simulate` view → JSON. **No GET variant, and the sandbox selection
never goes in a query string.**

The reason matters, so here it is explicitly: the sandbox is the manager's real keeper plan.
The whole design keeps it out of the database (rules §1 — no plan may exist anywhere before
the deadline). A URL like `?sim=1&sandbox=12,45` would put that same plan into browser
history, the `Referer` header on every outbound link, and gunicorn's access log as plaintext
on the EC2 box — strictly worse than the database write we deliberately avoided, because
nobody thinks to protect a log file. POST body only.

- `@login_required` + `@require_POST`. The view **never writes** — no model saves at all,
  same contract as `keeper_preview`.
- Body: `{"entry_ids": [...]}` — the user's own sandbox picks. Filter them to
  `team=own_team, season=roster_season` exactly as `keeper_preview` does; a mismatched count
  is a 403, not a silent drop. Cap at `MAX_KEEPERS_PER_TEAM`.
- Predictions come from the DB filtered to `request.user`. Never accept a prediction or a
  team id from the client.
- Run sandbox + each team's predictions through `engine.validate_keeper_set` to get burned
  picks, then hand those to `simulate_draft`.
- Response: the fill list (`pick_id`, player name, position, `nfl_team`, `source`) plus the
  burned pick ids. Nothing about any other user's predictions, ever.
- Tests: user A's simulation never reflects user B's predictions; another team's entry id in
  `entry_ids` is refused; nothing is persisted (assert the row counts are unchanged).

## Part D3 — Board wiring

- A "Simulate draft" button and a "Clear simulation" button near the round toggle. Extend
  `board.js` — the CSRF-header pattern it already uses for `keeper_preview` is the pattern
  here; reuse it rather than writing a second one.
- Sim-filled cells render in muted/desaturated position colours, clearly quieter than locked
  cells. Clear simulation removes fills and leaves locks and sandbox checkboxes alone.
- Fills are painted client-side into existing cells — do not re-render the table server-side.
- If the fetch fails, say so in the UI. A silently empty board looks like "no results."

## Part D4 — Sandbox state across reloads

Every prediction lock is a form POST + 302, which reloads the board and clears the sandbox
checkboxes. Persist the checked `entry_id`s in `sessionStorage`, keyed by season, and restore
them on load (then re-run the preview so the sidebar result matches).

`sessionStorage`, not `localStorage` — it dies with the tab, which is the right lifetime for
something we are being careful not to store. Add a one-line note near the sandbox that the
selection lives in this browser tab only, so nobody is surprised on a shared computer.

## Done means

Full suite green, `manage.py check` clean, no pending migrations, and a commit per part.

Out of scope: persisting simulations, sharing them, any in-app declaration flow, Part E
(reveal interplay) — that runs next from `04_scenarios.md`.
