# Keeper League Website

Django web app for a 10-team fantasy football keeper league. Managers log in and see their own team's keeper options, costs, and league-wide keeper context. Eventually deployed with Docker on AWS EC2, with AWS Cognito for per-team logins.

## About the developer (important — read this)

Isaac is learning Django. He knows Python well (pandas, Streamlit, Jupyter) but this is his first Django project, and later his first Docker/EC2/Cognito deployment.

**Work in teaching mode:**
- Explain WHAT you're doing and WHY as you go — especially Django concepts (models, migrations, views, templates, URL routing, the ORM, the admin).
- Prefer small, reviewable steps over big generated dumps of code. After each meaningful step, pause and summarize what changed.
- When there's a standard Django idiom, use it and name it, so Isaac learns the conventional way.
- Don't add dependencies, apps, or abstractions beyond what the current phase needs. Ask before adding anything not listed under Tech decisions.

## The spec

**`docs/keeper_rules_v3.md` is the source of truth** for all keeper logic: costs, escalation, pick forfeiture, roster composition limits, eligibility, draft order. Read it before implementing any keeper-related feature. If code and rules doc disagree, the rules doc wins.

## Tech decisions (settled — don't relitigate)

- Django with server-rendered templates. No REST framework, no React/SPA, minimal JS.
- SQLite for the database (10 users, tiny data — this is fine, even in production; revisit only if it becomes a real problem).
- Django's built-in auth through launch — the site ships and runs on it. AWS Cognito (OIDC) is an optional final phase after deployment, swapping only the login flow (design auth code so this swap is contained).
- Deployment: single EC2 instance, Elastic IP + domain, Docker Compose running gunicorn + Caddy (Caddy terminates HTTPS via Let's Encrypt automatically). Deliberately NO load balancer — one instance, 10 users.
- Plain CSS or a single lightweight CSS file; no frontend build tooling.
- pandas/openpyxl only in data-import management commands, not in request handling.
- Windows dev machine; virtualenv at `.venv`; dependencies tracked in `requirements.txt`.
- Secrets/config live in a git-ignored `.env` file at repo root, loaded with `python-dotenv`; `settings.py` reads `os.environ`. `FANTASYPROS_API_KEY` is there now; move `SECRET_KEY`/`DEBUG` there when touching settings. Never commit `.env` or print key values.
- ADP data (Phase 4) comes from the FantasyPros API using that key; CSV import is the fallback.

## Locked 2026 draft order (final — Yahoo 2025 final standings reversed)

1 Ricky, 2 Jake, 3 Isaac, 4 Sonny, 5 Luke, 6 Pechman, 7 Rimler, 8 Nick, 9 Chris, 10 Marcus. (Also in the rules doc.)

**2026 pick trades (complete list per Isaac):** Marcus's Round 4 → Isaac; Isaac's Round 13 → Marcus (one trade, seed via PickTrade in Phase 2).

## Repo layout

- `data/raw/` — original draft spreadsheets (e.g., `2025 draft picks.xlsx`)
- `data/processed/rosters_2025.csv` — tidy roster data: Team, Owner, Player_Name, Player_Position, round, overall_pick, Draft_pick ("Undrafted" for undrafted)
- `src/roster_processing.py`, `notebooks/` — the pandas pipeline that produced the CSV
- `docs/keeper_rules_v3.md` — league rules / app spec
- `docs/django_orientation.md` — Django learning notes for Isaac
- Django project lives at repo root once scaffolded: project package `keeper_site/`, app `league/`

## League facts

10 teams. Owners: Isaac (Zimbo Baggins), Chris, Sonny, Marcus, Rimler, Ricky, Pechman, Jake, Nick, Luke. 6-team playoff, 4-team consolation bracket. Snake draft, ~13+ rounds. League platform: Yahoo.

## Product vision

The centerpiece is a **draft board**: the locked 2026 draft order as a grid (10 slots × rounds). Before the keeper deadline it's a planning tool — each manager sees their own keeper options and which board slots a chosen keeper set would burn (computed by the engine: cost round + collision + missing-pick rules), plus every other team's *possible* keepers per slot (public info derived from last year's draft). Managers privately highlight/predict what others will keep and (later) autofill remaining picks from ADP to simulate scenarios.

**Keeper declarations do NOT happen in the app.** Managers text the commissioner privately by the deadline (keeps keepers truly secret — nothing in the DB to leak). After the deadline the commissioner enters all declarations via the admin, the engine validates each set, and the board flips to "revealed" for everyone. In-app declarations are a possible far-future add-on, not part of the plan.

Keeper legality is scenario-dependent: a player's cost round is fixed, but the board slot burned depends on pick inventory and the rest of the keeper set (collision → next-earlier pick; missing pick → next-earlier owned pick). There is NO voluntary overpay — earlier picks burn only when the rules force it.

## Roadmap (one phase at a time — don't jump ahead)

1. **Scaffold** (done pending Isaac's review): Django project + `league` app; Season/Team/Player/RosterEntry models; `import_rosters` + `seed_users` commands; admin; login-required league overview + team pages with base keeper cost.
2. **Keeper engine** (next): draft order + pick ownership + trade models; eligibility flags; pure-Python keeper engine (current cost with escalation, burned-pick computation with collision + missing-pick chains, composition + eligibility validation) with thorough unit tests; commissioner keeper entry via admin with validation; rules tab rendering `docs/keeper_rules_v3.md`.
3. **Draft board**: the grid view — own-team keeper planning (select set → see burned slots live), other teams' possible keepers per slot, post-deadline revealed board.
4. **Scenario layer**: private per-user predictions/highlights on others' slots; ADP import via CSV; autofill projected picks to simulate draft scenarios.
5. **Docker**: containerize (gunicorn + whitenoise); Docker Compose with Caddy for HTTPS.
6. **Deploy**: single EC2 + Docker Compose, Elastic IP, domain, security groups (22 locked to Isaac's IP, 80/443 open), Caddy/Let's Encrypt. Bonus: nightly SQLite backup to S3.
7. **Optional, last**: Cognito login via OIDC; Yahoo API integration to auto-compute eligibility (started 4+ weeks / rostered 9+ weeks) — manual admin flags until then.

## Conventions

- Run tests and `python manage.py check` before declaring a step done.
- Write migrations via `makemigrations` — never hand-edit the DB.
- Commit at the end of each working step with a clear message.
- Keeper business logic goes in plain Python functions/methods (easy to unit test), not buried in views or templates.
