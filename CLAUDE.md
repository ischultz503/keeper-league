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

## Repo layout

- `data/raw/` — original draft spreadsheets (e.g., `2025 draft picks.xlsx`)
- `data/processed/rosters_2025.csv` — tidy roster data: Team, Owner, Player_Name, Player_Position, round, overall_pick, Draft_pick ("Undrafted" for undrafted)
- `src/roster_processing.py`, `notebooks/` — the pandas pipeline that produced the CSV
- `docs/keeper_rules_v3.md` — league rules / app spec
- `docs/django_orientation.md` — Django learning notes for Isaac
- Django project lives at repo root once scaffolded: project package `keeper_site/`, app `league/`

## League facts

10 teams. Owners: Isaac (Zimbo Baggins), Chris, Sonny, Marcus, Rimler, Ricky, Pechman, Jake, Nick, Luke. 6-team playoff, 4-team consolation bracket. Snake draft, ~13+ rounds.

## Roadmap (one phase at a time — don't jump ahead)

1. **Scaffold** (current): Django project + `league` app; models for Team, Player, Season, RosterEntry; management command to import `rosters_2025.csv`; admin registration; login-required team page and league overview showing rosters with base keeper cost (draft round, or Round 8 for Round 9+/undrafted).
2. **Keeper engine**: keeper cost/escalation logic, pick ownership + trades, missing-pick forfeiture, roster composition validation, keeper declaration UI with deadline.
3. **League views**: declared-keepers reveal, draft board with forfeited slots, keeper history over seasons.
4. **Docker**: containerize with a production server (gunicorn + whitenoise for static files); Docker Compose with a Caddy service for HTTPS.
5. **Deploy**: single EC2 instance + Docker Compose, Elastic IP, domain, security groups (22 locked to Isaac's IP, 80/443 open), Caddy/Let's Encrypt HTTPS. Bonus: nightly SQLite backup to S3.
6. **Cognito (optional, last)**: swap Django login for Cognito hosted UI via OIDC; everything else unchanged.

## Conventions

- Run tests and `python manage.py check` before declaring a step done.
- Write migrations via `makemigrations` — never hand-edit the DB.
- Commit at the end of each working step with a clear message.
- Keeper business logic goes in plain Python functions/methods (easy to unit test), not buried in views or templates.
