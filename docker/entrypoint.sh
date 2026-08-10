#!/bin/sh
# Container start-up: bring the schema up to date, then serve.
#
# This file MUST keep Unix (LF) line endings. A CRLF here makes the kernel look
# for an interpreter literally named "/bin/sh\r", and the container dies with
# the famously unhelpful "exec format error" or "no such file or directory" --
# on a file you can plainly see exists. .gitattributes pins it; see there.
set -eu

# Migrate on start. The trade-off, stated plainly: this is safe here because
# exactly one container runs, so exactly one process can be migrating. With
# replicas it is wrong -- several containers would race the same migration, and
# with SQLite they would race the same file. The right shape at that point is a
# separate one-shot migration step in the deploy, not this line.
echo "==> Applying migrations"
python manage.py migrate --noinput

echo "==> Starting gunicorn"
# Workers are processes, not threads: each one holds its own copy of Django and
# handles one request at a time. Three is generous for ten managers, and the
# usual (2 x cores) + 1 rule of thumb lands there on a small EC2 instance.
#
# Logs go to stdout/stderr on purpose. In containers the log IS the output
# stream -- `docker compose logs` reads it, and nothing has to rotate a file
# inside a filesystem that gets thrown away.
exec gunicorn keeper_site.wsgi:application \
    --bind 0.0.0.0:8000 \
    --workers "${GUNICORN_WORKERS:-3}" \
    --timeout 60 \
    --access-logfile - \
    --error-logfile -
