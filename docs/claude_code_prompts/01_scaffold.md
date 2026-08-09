# Claude Code Prompt — Phase 1: Scaffold

Paste everything below the line into Claude Code in VS Code (with the keeper_league folder open). Future phase prompts will live alongside this file.

---

Read CLAUDE.md and docs/keeper_rules_v3.md first — CLAUDE.md explains how I want you to work (I'm learning Django, so teach as you go, small steps).

Scaffold Phase 1 of the keeper league site:

1. Django project named `keeper_site` at the repo root, with one app named `league`. Use the existing `.venv` and keep `requirements.txt` updated.

2. Models in `league/models.py`:
   - `Season`: year (unique integer).
   - `Team`: name, owner_name, and a nullable one-to-one link to Django's User model (each manager will get a login).
   - `Player`: name, position (choices: QB/RB/WR/TE/K/DEF).
   - `RosterEntry`: season FK, team FK, player FK, draft_round (nullable int — null means undrafted), overall_pick (nullable int), plus a `base_keeper_cost` property implementing the rule from docs/keeper_rules_v3.md section 2: draft round if drafted in rounds 1–8, otherwise (round 9+ or undrafted) Round 8.
   - Unique-together: a player appears once per season.

3. A management command `import_rosters` that loads `data/processed/rosters_2025.csv` into these models (create the 2025 Season, Teams with owners, Players, RosterEntries). "Undrafted" in Draft_pick / blank round means undrafted. Make it idempotent (safe to run twice).

4. A management command `seed_users` that creates a Django user per team (username = lowercase owner name, e.g. `isaac`), sets an unusable password placeholder I can reset via the admin, links each user to their Team, and creates a superuser prompt reminder in its output.

5. Register everything in the Django admin.

6. Views + templates (server-rendered, one shared `base.html`, minimal clean CSS — no frontend frameworks):
   - `/` — league overview: all 10 teams with owner names, linking to team pages.
   - `/teams/<id>/` — team detail: full roster table with position, draft round, overall pick, and base keeper cost; sort by draft round with undrafted last.
   - `/my-team/` — redirects the logged-in user to their own team's page.
   - All views require login; use Django's built-in login/logout views with a simple login template.

7. Run `makemigrations`, `migrate`, both import commands, and `python manage.py check`. Tell me how to create my superuser and start the dev server, then stop so I can click around before we build more.

Add a few unit tests for `base_keeper_cost` (drafted round 5 → 5, round 9 → 8, round 8 → 8, undrafted → 8). Commit when everything passes.

While you work: name the Django concepts you're using and briefly explain any file you create that isn't boilerplate. Where a choice is conventional, say "this is the standard Django way." Do not build keeper declaration logic, pick trading, escalation, Docker, or Cognito yet — that's later phases.
