# Claude Code Prompt — Phase 3: Draft Board

Paste everything below the line into Claude Code once Phase 2 is committed and the 2026 data is seeded (draft slots, generated picks, the Marcus/Isaac trade).

---

Re-read CLAUDE.md and docs/keeper_rules_v3.md. Teaching mode as before. Phase 3 is the draft board — the centerpiece of the site. Keep all keeper math in `league/keeper_engine.py`; views and the JSON endpoint stay thin.

## 1. The board page — `/board/`

A grid: **rows = rounds, columns = the 10 teams in draft-slot order** (slot 1 leftmost). Login required. Add to nav as "Draft Board".

- Indicate snake direction per row (e.g., a subtle arrow in the row header; odd rounds left→right, even rounds right→left).
- Each cell is one DraftPick. States:
  - **Normal** — empty planning cell.
  - **Traded** — pick sits in its *original team's* column but shows a badge with the current owner ("→ Isaac"). Both 2026 trade cells (Marcus's R4, Isaac's R13) must render this way.
  - **Kept (revealed)** — after reveal, shows the kept player's name + position; visually distinct (filled/dark).
- **Other teams' cells, pre-reveal:** list the players whose *current-year base cost* lands in that round for that team (from last season's roster + engine cost, eligible players only; show `eligible=None` "pending" players greyed with a "pending" marker). Cap at 3 names per cell with a "+N more" expander (pure CSS or a few lines of JS). Do NOT render collision/missing-pick chain possibilities for other teams — base cost cells only.
- **Reveal toggle:** add a `keepers_revealed` boolean (or reveal datetime) to Season, admin-editable. Pre-reveal: sandbox + candidate lists. Post-reveal: all KeeperSelections shown in their burned cells across the board; candidate lists hidden.

## 2. My-keeper sandbox (the interactive part)

Sidebar on the board page, only for the logged-in manager's own team:

- Their eligible players (eligible=True) listed with current-year cost and keep count. Checkboxes, max 3 selectable (enforce in JS and server-side).
- On every toggle, JS calls `POST /api/keeper-preview/` with the selected roster-entry IDs. The view calls `validate_keeper_set` and returns JSON: `{valid, errors: [...], warnings: [...], burned: [{pick_id, round, via}]}` where `via` says "base cost" / "collision" / "missing pick" so the UI can explain *why* a cell burned.
- JS paints the response: burned cells highlighted in the user's column(s) with the player name and the `via` reason on hover/tap; errors shown in the sidebar in plain language.
- **Nothing is ever saved.** This is a sandbox; declarations happen by text to the commissioner. Say so in a one-line note under the sidebar.
- Vanilla JS in one static file (~100–150 lines is the right ballpark), no frameworks, no build step. Use `fetch`; handle Django's CSRF token properly for the POST — explain how that works when you wire it, it's a concept Isaac should learn.
- Login required on the endpoint; a manager can only preview their *own* team's keepers (derive team from request.user, never trust a team id from the client — explain this while you're at it).

## 3. Polish

- The grid must be readable on a phone: horizontal scroll with a sticky round-number column is fine; don't attempt responsive reflow of a 10-column grid.
- Keep the single CSS file approach; cell states via CSS classes, not inline styles.

## 4. Tests

- Endpoint: rejects anonymous users; rejects >3 selections; returns correct burned picks for (a) a simple set, (b) the two-R8 collision, (c) Marcus keeping JSN → burns his R3 (missing-pick, because his R4 was traded to Isaac); manager cannot preview another team's roster entries.
- Board view: renders; traded picks badged with current owner; revealed state shows keeper selections and hides candidates.

`manage.py check` + full suite green, then stop and summarize. Commit.

Out of scope (Phase 4): predictions/highlights on other teams' cells, ADP, scenario autofill. Don't build placeholders for them.
