# syntax=docker/dockerfile:1.7
#
# Flock OS production bench image (FLO-246 Phase 6.1).
#
# A reproducible, containerized Frappe `bench` that ships the FLO-121 scaled-
# socketio tier UNCHANGED: N node socketio workers (sticky-L7'd by the companion
# nginx upstream in deploy/nginx/prod.conf) + the self-healing
# @socket.io/redis-adapter wiring (flock_os/utils/realtime_setup.py re-inserts
# the guarded block on every `bench update`).
#
# Target (ADR FLO-245): a Frappe Cloud *Server plan* VM (AWS/OCI dedicated),
# where this image is the unit-of-deploy. Also runs unchanged on any
# self-hosted VM (the ADR fallback) — nothing here is Frappe-Cloud-specific.
#
# No secret ever lands in the image. site_config.json / common_site_config.json
# are rendered at container start from environment by deploy/entrypoint.sh
# (scripts/deploy/render-config.sh). Secrets come from SOPS+age or cloud SM at
# deploy time — see docs/development/deploy-runbook.md.
#
# Build (from repo root):
#   docker build -t flock-os-bench:dev -f deploy/Dockerfile .
#
# Run (rendered config + supervisor manages gunicorn/workers/scheduler/N socketio):
#   docker run --env-file .env.prod -p 8080:8080 -p 9000:9000 flock-os-bench:dev
#
# The image is intentionally a single-VM launch-band shape (ADR §Topology). HA /
# multi-VM split is a post-launch concern (split the socketio backends behind a
# dedicated LB — the nginx template already assumes non-loopback backends).

ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=20

FROM python:${PYTHON_VERSION}-slim AS base

# ---------------------------------------------------------------------------- #
# System layer: Frappe runtime deps + nginx (WS-aware LB) + supervisor (the
# multi-process manager that preserves the scaled tier across restarts — ADR
# blocker #2) + MariaDB/Redis clients + wkhtmltopdf (PDF) + fonts. Pinned to
# keep builds reproducible; apt gets security updates via debian base.
# ---------------------------------------------------------------------------- #
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CURL_CA_BUNDLE="" \
    LC_ALL=C.UTF-8 \
    LANG=C.UTF-8

RUN apt-get update && apt-get upgrade -y && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        curl \
        git \
        jq \
        mariadb-client \
        redis-tools \
        nginx \
        supervisor \
        wkhtmltopdf \
        fonts-noto-core \
        libjpeg-dev \
        zlib1g-dev \
        libtiff-dev \
        libfreetype6-dev \
        liblcms2-dev \
        libwebp-dev \
        tcl-dev \
        tk-dev \
        libffi-dev \
        libldap2-dev \
        libsasl2-dev \
        libxslt1-dev \
        pkg-config \
        xfonts-75dpi \
        xfonts-base \
        gettext \
        bash-completion \
        vim-tiny \
        less \
        netcat-openbsd \
        procps \
    && rm -rf /var/lib/apt/lists/*

# Node 20 (LTS) — Frappe v15 build pipeline + the socketio workers require it.
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g yarn npx

FROM base AS app

# ---------------------------------------------------------------------------- #
# Frappe bench bootstrap.
#
# `bench init` writes a Procfile by default; we replace it post-init with our
# scaled-tier Procfile (deploy/Procfile) so a `bench start` (or the supervisor)
# brings up N socketio workers, not the single-process default.
# ---------------------------------------------------------------------------- #
ARG FRAPPE_BRANCH=version-15
ENV FRAPPE_BRANCH=${FRAPPE_BRANCH} \
    BENCH_DIR=/home/frappe/frappe-bench \
    BENCH_NAME=flock-os

RUN useradd -m -s /bin/bash frappe \
    && mkdir -p /home/frappe/sites \
    && chown -R frappe:frappe /home/frappe

USER frappe
WORKDIR /home/frappe

# Install bench CLI at the user level (kept off the system Python to avoid
# version drift with the Frappe app deps). Pin bench to a v15-compatible wheel.
RUN pip install --user --no-cache-dir 'frappe-bench<6,>=5.0' 'click<8.2'

ENV PATH="/home/frappe/.local/bin:${PATH}"

# Initialise the bench WITHOUT a default site and WITHOUT the default Procfile
# (we ship our own). `--frappe-branch` pins the framework to v15; `--skip-redis-
# config-generation` because Redis is a separate managed/compose service, not
# installed in this image.
RUN bench init frappe-bench \
        --frappe-branch "${FRAPPE_BRANCH}" \
        --no-procfile \
        --skip-redis-config-generation \
        --skip-assets \
        --verbose

WORKDIR /home/frappe/frappe-bench

# ---------------------------------------------------------------------------- #
# Install the flock_os app from the repo (this repo IS the app — see setup.py).
# `bench get-app` symlinks the source into apps/flock_os so edits during local
# dev hot-reload; in the image we want a real copy, so we COPY then link.
# ---------------------------------------------------------------------------- #
COPY --chown=frappe:frappe setup.py setup.cfg MANIFEST.in pyproject.toml requirements.txt package.json /opt/flock_os/
COPY --chown=frappe:frappe flock_os /opt/flock_os/flock_os
COPY --chown=frappe:frappe realtime /opt/flock_os/realtime
COPY --chown=frappe:frappe load /opt/flock_os/load

USER root
RUN mkdir -p /home/frappe/frappe-bench/apps \
    && ln -sf /opt/flock_os /home/frappe/frappe-bench/apps/flock_os \
    && chown -R frappe:frappe /home/frappe/frappe-bench/apps/flock_os

USER frappe

# Install the app's Python + Node deps into the bench venv, then register the
# app on the bench so hooks.py / fixtures / patches.txt are picked up.
RUN cd /home/frappe/frappe-bench/apps/flock_os \
    && pip install --no-cache-dir -e . \
    && cd /home/frappe/frappe-bench \
    && bench setup requirements --python \
    && bench setup requirements --node

# The FLO-121 scaled-socketio tier needs @socket.io/redis-adapter at the
# flock_os app root (the wire-socketio-redis-adapter.sh self-healing block
# requires it from there). ensure_adapter_dep() in scale-socketio.sh does the
# same at runtime; bake it into the image so first boot is clean.
RUN cd /home/frappe/frappe-bench/apps/flock_os \
    && npm install --no-audit --no-fund @socket.io/redis-adapter @redis/client

# Build assets (JS/CSS bundles). `bench build` compiles the app's webpack/esbuild
# outputs so nginx can serve them as static (unmetered on Frappe Cloud).
RUN cd /home/frappe/frappe-bench \
    && bench build --app flock_os

# ---------------------------------------------------------------------------- #
# Deploy the FLO-121 scaled-tier wiring into the vendored Frappe realtime
# server. This is the SAME self-healing wiring the dev tier uses — re-run on
# every `bench update` by the flock_os `after_migrate` hook. Running it at build
# time guarantees the baked image has the adapter armed even before the first
# migrate fires.
# ---------------------------------------------------------------------------- #
COPY --chown=frappe:frappe scripts/dev/wire-socketio-redis-adapter.sh /home/frappe/frappe-bench/apps/flock_os/scripts/dev/wire-socketio-redis-adapter.sh
RUN cd /home/frappe/frappe-bench/apps/flock_os \
    && bash scripts/dev/wire-socketio-redis-adapter.sh --bench /home/frappe/frappe-bench \
    && bash scripts/dev/wire-socketio-redis-adapter.sh --check --bench /home/frappe/frappe-bench

# ---------------------------------------------------------------------------- #
# Deploy configs: our scaled-tier Procfile (replaces the bench default), the
# multi-process supervisor (survives Frappe Cloud dashboard restarts — ADR #2),
# the prod sticky-L7 nginx, and the entrypoint that renders site_config from env.
# ---------------------------------------------------------------------------- #
COPY --chown=frappe:frappe deploy/Procfile /home/frappe/frappe-bench/Procfile
COPY --chown=frappe:frappe deploy/supervisord.conf /etc/supervisor/conf.d/flock-os.conf
COPY --chown=frappe:frappe deploy/nginx/prod.conf /etc/nginx/sites-available/flock-os.conf
COPY --chown=frappe:frappe deploy/templates/site_config.json.tmpl /opt/flock_os-deploy/site_config.json.tmpl
COPY --chown=frappe:frappe deploy/templates/common_site_config.json.tmpl /opt/flock_os-deploy/common_site_config.json.tmpl
COPY --chown=frappe:frappe deploy/entrypoint.sh /usr/local/bin/flock-os-entrypoint
COPY --chown=frappe:frappe scripts/deploy/render-config.sh /usr/local/bin/flock-os-render-config
COPY --chown=frappe:frappe scripts/dev/scale-socketio.sh /usr/local/bin/flock-os-scale-socketio

USER root
RUN chmod 0755 /usr/local/bin/flock-os-entrypoint \
    && chmod 0755 /usr/local/bin/flock-os-render-config \
    && chmod 0755 /usr/local/bin/flock-os-scale-socketio \
    && ln -sf /etc/nginx/sites-available/flock-os.conf /etc/nginx/sites-enabled/flock-os.conf \
    && rm -f /etc/nginx/sites-enabled/default \
    && mkdir -p /var/log/nginx /var/lib/nginx /run/nginx \
    && chown -R www-data:www-data /var/log/nginx /var/lib/nginx /run/nginx

# A sentinel site config so `bench` commands work pre-render. The real config
# is written by the entrypoint at container start (overwriting this).
RUN mkdir -p /home/frappe/frappe-bench/sites \
    && printf '{"db_name":"_sentinel","db_host":"127.0.0.1"}\n' > /home/frappe/frappe-bench/sites/common_site_config.json \
    && chown -R frappe:frappe /home/frappe/frappe-bench/sites

USER frappe
WORKDIR /home/frappe/frappe-bench

# 8080 = Frappe web (gunicorn behind nginx). 9000 = the scaled-socketio LB
# (nginx sticky-L7 in front of N socketio backends on 9001..900N). Both are
# documented in docs/development/deploy-runbook.md.
EXPOSE 8080 9000

# No site is baked in (multi-site capable). The entrypoint renders
# site_config.json + common_site_config.json from environment, runs
# `bench migrate`, then starts supervisor (gunicorn + workers + scheduler +
# N socketio workers) and nginx.
ENTRYPOINT ["/usr/local/bin/flock-os-entrypoint"]
CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
