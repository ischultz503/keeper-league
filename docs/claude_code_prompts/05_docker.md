# Claude Code Prompt — Phase 5: Docker (author-only — no local Docker)

Paste below the line after 04d + Part E are committed and the eligibility import has run.

Context: my dev machine cannot run Docker Desktop. This phase AUTHORS the container setup and verifies everything that can be verified without running Docker; the actual build/run happens on EC2 in Phase 6 (I deploy by SSH). Teaching mode matters extra here — Docker is new-ish to me: explain images vs containers, layers, and what each Dockerfile line does as you write it, knowing I won't see it run until we're on the server.

---

Read CLAUDE.md (deployment decisions settled: single EC2, Docker Compose, gunicorn + Caddy, SQLite, no ALB).

## 1. Settings hygiene first

- Move SECRET_KEY, DEBUG, and ALLOWED_HOSTS into environment variables read in settings.py (python-dotenv already loads .env locally). Generate a fresh SECRET_KEY for me to put in .env; DEBUG defaults False unless env says otherwise; ALLOWED_HOSTS from a comma-separated env var.
- Add whitenoise (middleware + STATIC_ROOT + compressed manifest storage). Explain why: gunicorn doesn't serve static files; whitenoise keeps us to one app container.
- Verify `runserver` still works locally with DEBUG=true in .env, and run `collectstatic` locally once to prove the static pipeline is sound. Full test suite green.

## 2. Dockerfile (app container)

- Python slim base matching my local version; install requirements first, then copy code (explain layer caching).
- `collectstatic` at build time; non-root user.
- Entrypoint: `migrate` then gunicorn 0.0.0.0:8000, 2-3 workers. Explain the migrate-on-start tradeoff (fine for single-instance SQLite; wrong with replicas).
- `.dockerignore`: .venv, .env, db.sqlite3, data/raw, notebooks, .git, __pycache__.

## 3. docker-compose.yml + Caddyfile

- `web`: build from Dockerfile; env_file .env; bind mount ./db/ (holding db.sqlite3) over the database location — explain why the DB lives outside the image (survives rebuilds).
- `caddy`: official image, ports 80/443, volumes for Caddyfile + caddy data/config dirs (cert persistence — Let's Encrypt rate limits if certs don't survive restarts).
- Caddyfile: site block using a `{$DOMAIN}` env placeholder, `reverse_proxy web:8000`. Explain compose networking (service name = hostname).
- Settings must read the SQLite path from an env var (default: current location) so the container can point it at the mounted dir without breaking local dev.

## 4. Validation without Docker

You can't run docker locally, so: lint-check the Dockerfile/compose file by review, ensure entrypoint scripts have LF line endings (Windows CRLF breaks them in Linux containers — add a .gitattributes rule forcing LF for .sh files; explain this, it's the classic Windows-authored-container bug), and write docs/deploy_notes.md covering: what each file does, the commands Phase 6 will run on the server, how to run manage.py inside the container, how the db mount works.

Commit. Out of scope: EC2 provisioning, DNS, S3 backups (Phase 6). No Postgres, no nginx — stack is deliberately gunicorn + Caddy + SQLite.
