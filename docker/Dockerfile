# Flock OS — prod-equivalent Frappe bench image (FLO-347 / FLO-10 §8).
#
# A single image that can run as the gunicorn WEB server, a SOCKETIO realtime
# worker (node), or the Frappe scheduler/worker — the same one-image-many-roles
# shape frappe-docker uses. docker-compose picks the role via `command:`.
#
# This mirrors scripts/bootstrap.sh (the reproducible host setup) so the
# containerized bench is identical to the developer bench: same Frappe v15,
# same flock_os app installed editable from the copied repo, same realtime
# wirings (join handler + auth cache + redis adapter) pre-applied to vendored
# apps/frappe/realtime/index.js. The image is built ONCE (`docker compose
# build`); the live §8 15k gate then runs against the multi-container tier.
#
# Why self-contained (not FROM frappe/frappe-worker): this is fully auditable,
# has no dependency on the undocumented internal layout of the frappe-docker
# image, and is byte-for-byte the same setup scripts/bootstrap.sh performs on
# the host — so prod topology == dev topology == CI topology.
#
# Build context = repo root (see docker/docker-compose.yml `build.context`),
# so the whole flock_os app + scripts/ + realtime/ are COPYable in.
#
# Runbook: docs/development/ws-broadcast-delivery.md -> Prod-equivalent docker tier.

# ---- stage 1: system deps + bench init -------------------------------------
FROM python:3.12-slim AS bench

ENV DEBIAN_FRONTEND=noninteractive \
	PYTHONUNBUFFERED=1 \
	PIP_NO_CACHE_DIR=1

# Frappe v15 system prerequisites: MariaDB client libs (Frappe connects over
# TCP), Node.js (realtime + asset build), build toolchain (Python wheels that
# have C extensions like lxml/psycopg/Pillow), curl/git, and the redis CLI for
# runbook probes. `mariadb-client` provides `mysql` + the libmariadbclient
# runtime that the python `mariadb` wheel links.
RUN apt-get update && apt-get install -y --no-install-recommends \
		build-essential \
		curl \
		git \
		mariadb-client \
		libmariadb-dev \
		pkg-config \
		redis-tools \
		nodejs \
		npm \
		ca-certificates \
	&& rm -rf /var/lib/apt/lists/*

# frappe-bench (the CLI) drives init/get-app/new-site the same way bootstrap.sh
# does on the host. Pinned via pip so the bench layout matches the host setup.
RUN pip install --upgrade pip uv \
	&& pip install frappe-bench

# A non-root `frappe` user owns the bench so gunicorn/socketio never run as root
# (prod shape + the image is the same one the team would ship to staging).
RUN useradd -m -s /bin/bash frappe
USER frappe
WORKDIR /home/frappe

# `bench init` the runtime bench with Frappe v15. --skip-assets (the gate needs
# no built JS — the realtime path + the k6 smoke are backend-only) and
# --no-backups (avoid the macOS-only crontab step bootstrap.sh also skips).
RUN bench init frappe-bench \
		--frappe-branch version-15 \
		--skip-assets \
		--no-backups

WORKDIR /home/frappe/frappe-bench

# ---- stage 2: install the flock_os app + pre-wire realtime -----------------
# COPY the app source as the `frappe` user (owned so the editable install +
# symlink land cleanly). The build context is the repo root.
COPY --chown=frappe:frappe . /tmp/flock_os

# Install flock_os from the copied repo (editable), then symlink it into the
# bench apps dir exactly like bootstrap.sh does — so the version-controlled
# realtime/ adapter + handlers resolve from the app root, and the realtime
# wirings (which require() ../../flock_os/...) find their targets.
RUN "$HOME/frappe-bench/env/bin/python" -m pip install -q -e /tmp/flock_os \
	&& if [ ! -e apps/flock_os ]; then ln -s /tmp/flock_os apps/flock_os; fi

# Declare flock_os in apps.txt so `bench` loads it (get-app normally writes
# this; the symlink path bypasses get-app so we do it explicitly).
RUN mkdir -p sites && printf 'flock_os\n' >> sites/apps.txt 2>/dev/null || \
	printf 'flock_os\n' > sites/apps.txt

# Node deps for the scaled realtime tier: @socket.io/redis-adapter lives in the
# flock_os app root (repo root) so the realtime modules resolve it via Node's
# walk-up resolution. @redis/client is already a frappe dep, but pin it so the
# adapter's createClient factory resolves even if frappe's layout shifts.
RUN cd apps/flock_os && npm install --no-audit --no-fund

# Pre-apply the three realtime wirings to vendored apps/frappe/realtime/index.js
# (join handler FLO-107, auth cache FLO-116, redis adapter FLO-121). These are
# the same idempotent marker-guarded scripts the after_migrate hook re-runs; we
# bake them into the image so a fresh container is wired on first boot without
# needing a migrate. Each is independently reversible + idempotent.
RUN bash apps/flock_os/scripts/dev/wire-socketio-handler.sh --bench "$PWD" \
	&& bash apps/flock_os/scripts/dev/wire-socketio-auth-cache.sh --bench "$PWD" \
	&& bash apps/flock_os/scripts/dev/wire-socketio-redis-adapter.sh --bench "$PWD"

# The entrypoint renders sites/common_site_config.json from compose env (DB
# host, Redis service names) at container start, then execs the compose
# `command:` (gunicorn / node socketio / scheduler). init-site.sh is the
# one-shot site provisioning the `init` compose service runs (new-site +
# migrate + smoke fixtures). Both are baked in so the image is self-contained.
COPY --chown=frappe:frappe docker/entrypoint.sh /home/frappe/entrypoint.sh
COPY --chown=frappe:frappe docker/init-site.sh /home/frappe/init-site.sh
RUN chmod +x /home/frappe/entrypoint.sh /home/frappe/init-site.sh

ENTRYPOINT ["/home/frappe/entrypoint.sh"]
# Default role = web (gunicorn). docker-compose overrides per service.
CMD ["gunicorn"]
