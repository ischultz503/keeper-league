# Claude Code Prompt — Phase 3b: Board Fixes + Polish

Paste below the line into Claude Code. Small patch phase: one placement bug and a visual pass. No new features.

---

Two items for the draft board. Rules doc still governs (docs/keeper_rules_v3.md).

## 1. Candidate placement bug (real bug — add a test)

Other teams' cells currently place candidates by base cost round. That's wrong when a team no longer owns a pick in that round: Marcus's R4 was traded to Isaac, so JSN (R4 cost) shows in a cell Marcus can't use — keeping JSN deterministically burns Marcus's R3 (missing-pick rule), regardless of his other keepers.

Fix: place each candidate in the cell that `resolve_burned_picks` returns for that player as a **solo keeper** for their team. For most players this equals base cost; for players whose team lacks the cost-round pick, it shifts to the next-earlier owned pick. When the placed round differs from the player's cost round, annotate the entry (e.g., "R4 cost → burns R3" on hover/tap or a small marker). Same logic drives the sidebar's cost display if it differs.

Add engine-backed view tests: JSN appears in Marcus's R3 cell (not R4); a normal player appears in his base-cost cell; Isaac's R4-cost candidates resolve into one of his two owned R4 picks.

Note: multi-keeper chain placements for OTHER teams remain out of scope (Phase 4 scenarios). This fix is only the deterministic solo-keep placement.

## 2. Visual polish (keep the single CSS file, no frameworks)

The board works but reads rough. A restrained pass:

- Tighten the grid: consistent row heights, smaller/denser type in cells, position chips (RB/WR/etc.) matching the sidebar's style so names scan fast.
- Sticky header row (team names) AND sticky round column while scrolling in both directions.
- Clearer cell states: subtle background tints — own column highlighted, traded picks with a distinct badge style, "+N more" expander styled as a quiet control, pending-eligibility players greyed.
- Snake direction arrows made subtle (muted, small) — informative, not decorative clutter.
- Sidebar: clearer selected state on checked players, and the burned-cell highlight color should match a swatch shown next to the selected player in the sidebar so the mapping is obvious.
- Empty cells stay visually quiet; the board should read as "names float on a calm grid," not a wall of borders.
- Keep it working on phone (horizontal scroll + sticky round column already in place — don't regress it).

Screenshot-worthy is the bar: this page is what the league will judge the whole site by.

Run the full test suite + `manage.py check`, commit.
