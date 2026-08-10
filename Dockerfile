# The keeper site as a container image.
#
# An IMAGE is a filesystem snapshot plus a default command -- a template, built
# once, never running. A CONTAINER is one running instance of an image. You can
# throw away the container and start another from the same image and get an
# identical process; anything you want to survive that has to live outside the
# image, which is why the database is a bind mount and not a file in here.
#
# The image is built from LAYERS: each instruction below produces one, and
# Docker caches each layer against the inputs that produced it. Change a file
# and every layer from that point down is rebuilt; everything above is reused.
# The ordering below is chosen entirely around that fact.

FROM python:3.12-slim

# "slim" is Debian with the build toolchain and docs stripped -- a fraction of
# the full image and a much smaller surface to patch. It matches the local
# Python 3.12 so a version difference can never be the thing that broke prod.
# (The alpine images are smaller still, but use musl instead of glibc and so
# cannot use most prebuilt wheels; they compile from source instead. Not worth
# it here.)

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_STATIC_MANIFEST=1

# PYTHONUNBUFFERED is the one that matters operationally: without it Python
# buffers stdout, and `docker compose logs` shows nothing until the buffer
# fills -- which looks exactly like a hung container.
# DJANGO_STATIC_MANIFEST is set here, for the build AND for the running app, so
# collectstatic writes the hashed manifest and {% static %} then reads it.

WORKDIR /app

# Dependencies first, application code second. This is the whole reason the
# COPY is split in two: requirements-prod.txt changes rarely, so the expensive
# pip layer is reused across every code-only rebuild. Copying the source first
# would invalidate the pip layer on every one-line edit and reinstall Django
# each time.
COPY requirements-prod.txt ./
RUN pip install --no-cache-dir -r requirements-prod.txt

COPY . .

# Gather our CSS/JS and Django's admin assets into STATIC_ROOT, at build time,
# so a running container never writes to its own filesystem to serve a page.
# The secret key here is a build-time throwaway: collectstatic needs settings to
# import, and settings refuse to load without one. It is not baked into the
# image in any meaningful sense -- the runtime key comes from the environment.
RUN DJANGO_SECRET_KEY=build-time-only-never-used-to-sign-anything \
    DJANGO_DEBUG=false \
    python manage.py collectstatic --noinput

# The entrypoint goes in while we are still root, because /usr/local/bin is not
# writable by anyone else.
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Run as a normal user. A container process that is root is root on the host
# kernel if it ever escapes the namespace, and nothing here needs the
# privilege. UID 1000 is deliberate: it is also the default `ubuntu` user on
# the EC2 AMI, so the bind-mounted database directory created there is writable
# by this user with no chown dance.
RUN useradd --create-home --uid 1000 keeper && chown -R keeper:keeper /app
USER keeper

# Documentation only -- EXPOSE publishes nothing. Compose puts this container
# on a private network where Caddy can reach it by name, and the port is
# deliberately NOT mapped to the host: gunicorn should be unreachable except
# through the proxy that terminates TLS.
EXPOSE 8000

ENTRYPOINT ["entrypoint.sh"]
