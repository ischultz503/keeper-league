# Deployment notes — the container setup

Phase 5 authored these files; nothing here has been built or run yet, because
this dev machine cannot run Docker. Phase 6 does that on the EC2 box. This
document is the bridge: what each file is for, what to run on the server, and
the handful of things that will bite if they are forgotten.

## The stack, in one paragraph

Two containers. **web** runs gunicorn, which runs Django and serves its own
static files through whitenoise. **caddy** is the only thing the internet can
reach: it holds ports 80 and 443, gets a Let's Encrypt certificate for the
domain automatically, and forwards requests to `web:8000` over a private
network. SQLite lives on the host and is mounted into the app container, so
rebuilding the image never touches the league's data. No load balancer, no
Postgres, no nginx — ten users and one instance (see CLAUDE.md).

```
internet ──443──▶ caddy ──▶ web:8000 (gunicorn ─▶ Django)
                                  │
                            /data (bind mount) ──▶ ./db/db.sqlite3 on the host
```

## The files

| File | What it is |
| --- | --- |
| `Dockerfile` | How to build the app image: Python 3.12-slim, prod requirements, code, `collectstatic`, non-root user. |
| `docker/entrypoint.sh` | What a container does on start: `migrate`, then `exec gunicorn`. Must stay LF. |
| `docker-compose.yml` | The two services, the database bind mount, the published ports, the Caddy volumes. |
| `Caddyfile` | One site block: `{$DOMAIN}` → `reverse_proxy web:8000`, with automatic HTTPS. |
| `.dockerignore` | What never enters the build context — `.env`, `*.pem`, `db.sqlite3`, `.venv`, `.git`. |
| `.gitattributes` | Forces LF on container files so Windows checkouts don't break the entrypoint. |
| `requirements-prod.txt` | Pinned runtime dependencies only. No pandas, no jupyter. |
| `.env.example` | Every environment variable, with notes. Copy to `.env` and fill in. |

## Settings that changed in Phase 5

All read from the environment (`keeper_site/settings.py`):

- `DJANGO_SECRET_KEY` — **required in production; the app refuses to start
  without it.** Debug and test runs generate a throwaway. There is no committed
  fallback, deliberately.
- `DJANGO_DEBUG` — defaults to **false**. Forgetting it fails closed.
- `DJANGO_ALLOWED_HOSTS` / `DJANGO_CSRF_TRUSTED_ORIGINS` — comma-separated.
  The second one matters behind TLS: Django checks the `Origin` header on POSTs
  against it, so without `https://your.domain` in there every login and every
  keeper form fails CSRF.
- `DJANGO_DB_PATH` — where SQLite lives. Compose sets it to `/data/db.sqlite3`.
- `DJANGO_STATIC_MANIFEST` — on in the image; makes `{% static %}` resolve
  through the hashed manifest that `collectstatic` writes.
- With `DEBUG` off, Django also turns on `SECURE_SSL_REDIRECT`, secure cookies,
  a one-hour HSTS, and trusts Caddy's `X-Forwarded-Proto`.

## First deployment (Phase 6 will run these)

```bash
# On the server, in the repo directory:
cp .env.example .env
nano .env          # secret key, DOMAIN, ALLOWED_HOSTS, CSRF_TRUSTED_ORIGINS, ACME_EMAIL

mkdir -p db        # the host directory the database lives in

docker compose build
docker compose up -d
docker compose logs -f          # watch the migration run and gunicorn start
```

The DNS record must already point at the Elastic IP when Caddy first starts, or
the certificate challenge fails and it retries with backoff.

### Getting the data in

`docker compose up` runs `migrate`, which creates an **empty** database at
`db/db.sqlite3` — schema, no league. Two ways to fill it:

```bash
# Preferred: copy the local database up, before the first `up`.
scp db.sqlite3 ubuntu@<elastic-ip>:~/keeper_league/db/db.sqlite3

# Or start empty and seed inside the container:
docker compose exec web python manage.py seed_users
docker compose exec web python manage.py seed_draft_order
docker compose exec web python manage.py generate_picks
docker compose exec web python manage.py createsuperuser
```

`import_rosters` does **not** work in the container — it reads `.xlsx` through
pandas, which is not installed there on purpose. Run roster imports locally and
copy the database up. `import_adp` does work (it only needs `requests`), so
weekly ADP refreshes can happen on the server.

## Everyday commands

```bash
docker compose ps                       # what is running
docker compose logs -f web              # gunicorn + Django logs
docker compose exec web python manage.py <anything>
docker compose exec web python manage.py shell
docker compose restart web              # restart without rebuilding

# Deploy a change:
git pull && docker compose up -d --build
```

`docker compose exec` runs inside the *already running* container;
`docker compose run --rm web ...` starts a throwaway one, which is what you
want if the app is failing to boot.

## How the database mount works

`./db:/data` in `docker-compose.yml` means: the host directory `./db` appears
inside the container as `/data`. `DJANGO_DB_PATH=/data/db.sqlite3` points Django
at it. The file is a normal file on the EC2 filesystem — `ls -l db/`, back it
up with `cp`, and it survives every `down`, `build` and `up`.

Two details worth knowing:

- The **directory** must be writable, not just the file. SQLite writes
  `db.sqlite3-wal` and `-journal` alongside the database, and a read-only
  directory produces "attempt to write a readonly database" even when the
  database file itself is writable.
- The container runs as UID 1000, chosen because that is also the `ubuntu` user
  on the standard EC2 AMI. A `db/` directory created by `ubuntu` is therefore
  already owned by the right UID. If permissions do go wrong:
  `sudo chown -R 1000:1000 db/`.

## Things that will bite

**CRLF in `entrypoint.sh`.** `core.autocrlf=true` on the Windows dev machine
rewrites LF to CRLF on checkout. Linux then reads the shebang literally, looks
for an interpreter called `/bin/sh\r`, and the container exits with "exec format
error" or "no such file or directory" — naming a file you can see is there.
`.gitattributes` pins `eol=lf` for these paths. If it ever recurs:
`file docker/entrypoint.sh` should say "ASCII text", not "with CRLF line
terminators".

**Let's Encrypt rate limits.** Certificates and ACME account keys live in the
`caddy_data` named volume so they survive restarts. Do not `docker compose down
-v` casually — `-v` deletes named volumes, and re-requesting certificates
repeatedly will lock the domain out of issuance for up to a week.

**Migrate-on-start.** The entrypoint migrates before serving. That is safe
because exactly one container runs. It would be wrong with replicas — several
containers would race the same migration against the same SQLite file — and the
fix at that point is a separate one-shot migration step, not this line.

**`manage.py check --deploy` warnings.** Three are expected and accepted:
`W009` about key length (only when using a short throwaway key — a real
`get_random_secret_key()` value is 50 characters), and `W005`/`W021` about HSTS
subdomains and preload. Preload especially is a one-way door: browsers cache it
for months and it cannot be quickly undone if the certificate breaks. Revisit
once the domain has been stable for a while.

## Not in this phase

EC2 provisioning, security groups, DNS, and the nightly S3 backup of
`db/db.sqlite3` are Phase 6.
