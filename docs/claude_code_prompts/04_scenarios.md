# Claude Code Prompt — Phase 4: Scenario Layer (Predictions + ADP)

Paste below the line into Claude Code after Phase 3b is committed. This is the biggest UX phase — take it in the numbered order, committing after each part works.

---

Re-read CLAUDE.md and docs/keeper_rules_v3.md. Teaching mode as before; keeper math stays in `league/keeper_engine.py`; simulation logic goes in a new plain-Python module `league/draft_sim.py` (unit-testable, no request objects).

## Part A — FantasyPros ADP import (do this first; the board display needs its data)

- Add fields to Player: `nfl_team` (short code, e.g. "KC"), `adp` (nullable float), `adp_updated` (datetime).
- Management command `import_adp` that calls the FantasyPros API using `FANTASYPROS_API_KEY` from the environment (loaded via python-dotenv; add it to requirements if not present). Half-PPR/standard redraft ADP, whichever endpoint matches our league scoring — check the API docs and say what you chose. Match API players to our Player rows by name + position with normalization (suffixes, punctuation); print unmatched names for manual review rather than guessing. `--csv path` flag as a fallback importer for a FantasyPros ADP export.
- Never log or print the key. Idempotent; safe to re-run weekly.

## Part B — Board layout changes (applies to the existing board)

- **Default view: rounds 1–8 only**, sized so ALL 10 columns fit the viewport width with no horizontal scrolling, cells roughly square-ish. A "show all rounds" toggle expands to the full 13+ (the R13 trade badge lives there); toggle state can be a simple query param or JS class flip.
- Every player name on the board and sidebar now renders as: Name · position chip · NFL team (from Part A). No player images.
- **Position color coding** (Isaac provided a reference: a FantasyPros-style live draft board). Match that aesthetic: QB / RB / WR / TE / K / DEF each get a distinct color (reference used teal-green RB, blue WR, red-pink QB, orange TE); resolved cells are SOLID position-color rounded rectangles containing the player name (bold, truncate long names), a smaller "POS – TEAM" subline, and the pick number (e.g. "3.4") small in a corner. Unresolved/empty cells stay dark/muted with just the pick number and snake arrow — the board reads as bright filled cells floating on a calm dark grid. Pre-resolution candidate lists use small position-colored chips per name. Define the palette once in CSS variables. No player photos/avatars.
- Selected/locked keeper pop: bold cell outline + high-contrast text on the position color, clearly louder than sim-filled cells (which render in muted/desaturated versions of the position colors). Must be obvious at a glance which cells are YOUR locked calls vs autofill.

## Part C — Predictions (click-to-lock on other teams' cells)

- Clicking any other team's cell opens a small popover listing that cell's candidates (name, position chip, NFL team, cost annotation). Choosing one **locks** it: cell fills solid, bolded. Clicking a locked cell offers unlock.
- Locks persist: new model `KeeperPrediction` (user FK, season FK, roster_entry FK, created/updated). Strictly private per user — every query filters by request.user; write tests proving user A never sees user B's predictions.
- A user's own team is EXCLUDED from click-to-lock: own-team planning stays in the existing sandbox checkboxes and is **never persisted** (deliberate — real keeper plans must not live in the DB). The sandbox selection lives only in page state.
- Locked predictions run through the engine per team (their full predicted set, so collisions/missing-picks resolve) — a predicted set that's illegal shows a warning marker on that team's column rather than blocking the lock.

## Part D — Simulate ("Finalize all keepers")

- A "Simulate draft" button: takes the user's locked predictions + their own sandbox selection, marks all those cells as keepers (bold, position-colored), then autofills every remaining cell in draft order (snake) from ADP via `draft_sim.py`.
- Autofill rules (state them in a small legend on the page): best available by ADP; skip QB if that team already has one (kept, predicted, or sim-filled); same for TE; no K or DEF in rounds 1–8. Sim-filled cells render greyed/muted with the player name + position chip — visually clearly "projection, not fact."
- "Clear simulation" resets fills but keeps locks. Simulation results are not persisted — recomputed on demand (it's fast: 10 teams × ≤13 rounds).
- Unit-test `draft_sim.py`: respects ADP order, positional caps, skips kept/predicted players, handles players missing ADP (sort last).

## Part E — Reveal interplay

Post-reveal (Season.keepers_revealed): real KeeperSelections override everything — locked cells show actual keepers, predictions become read-only history (a subtle "you called it" check where prediction matched reality is welcome if cheap), and Simulate still works for the remaining non-keeper cells.

Full suite + `manage.py check` green, commit per part. Out of scope: any in-app declaration flow; sharing predictions between users; ADP auto-refresh scheduling (manual command run is fine).
