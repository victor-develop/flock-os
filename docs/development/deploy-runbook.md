# Deploy runbook — Flock OS (FLO-246 Phase 6.1)

> The operational companion to the deploy pipeline artifacts (`deploy/`,
> `scripts/deploy/`, `.github/workflows/deploy.yml`). Covers how to deploy,
> how to roll back, how to run the staging smoke, and how the FLO-121 scaled-
> socketio tier survives the deploy/restart cycle. Target environment: a
> Frappe Cloud **Server plan** VM (ADR [FLO-245](/FLO/issues/FLO-245)); the
> same steps run on the self-hosted fallback.

## TL;DR

```bash
# Master green → staging auto-deploys (the deploy.yml workflow does it).
# Promote staging → prod (manual gate, CEO/QA sign-off):
#   Actions tab → "Deploy" workflow → Run workflow → check "promote_to_prod".
# Roll back:
STAGING_URL=https://staging.flock-os.example \
FLOCK_CURRENT_TAG=sha-abc-123 FLOCK_PREVIOUS_TAG=sha-def-456 \
FLOCK_DEPLOY_CMD='TAG=<TAG> docker compose up -d --no-deps --force-recreate bench' \
scripts/deploy/rollback.sh
# Smoke by hand:
scripts/deploy/smoke-staging.sh --url https://staging.flock-os.example
```

## What gets deployed

The unit of deploy is the **`flock-os-bench` container image** (`deploy/Dockerfile`).
It is a complete Frappe bench with the FLO-121 scaled-socketio tier baked in:

- gunicorn (web) + bench worker (queues) + bench schedule (scheduler)
- **N node socketio workers** (`scripts/dev/scale-socketio.sh start N --lb nginx`)
  behind the **nginx sticky-L7 LB** (`deploy/nginx/prod.conf`)
- the self-healing `@socket.io/redis-adapter` wiring (armed at build; re-armed
  on every `bench migrate` by the `after_migrate` hook)
- nginx front edge (web reverse proxy + the socketio sticky-L7 LB)

The image contains **zero secrets**. At container start, `deploy/entrypoint.sh`
runs `scripts/deploy/render-config.sh` which renders `site_config.json` and
`common_site_config.json` from environment (set by the secret manager).

## Environments + secrets

Two GitHub Actions environments back the two-stage pipeline:

| Environment | Purpose | Required secrets |
|-------------|---------|------------------|
| `staging` | master-green auto-deploy target | `DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `REDIS_CACHE_URI`, `REDIS_QUEUE_URI`, `REDIS_SOCKETIO_URI`, `FLOCK_SIO_ADAPTER_REDIS`, `SECRET_KEY`, `STAGING_URL`, `STAGING_WS_URL` (optional), `FLOCK_IMAGE_REGISTRY_TOKEN` |
| `production` | manual-promotion target | the same set, pointing at prod resources; `PROD_URL`, `PROD_WS_URL` instead of `STAGING_*` |

Plus **environment variables** (non-secret) on each environment:

| Variable | Purpose |
|----------|---------|
| `FLOCK_DEPLOY_CMD` | the orchestrator-specific "deploy tag `<TAG>`" command (see below) |
| `FLOCK_IMAGE_REGISTRY_TOKEN` (secret) | registry auth (use the GitHub token for ghcr.io) |
| `FLOCK_IMAGE_REGISTRY` (repo var) | registry host (default `ghcr.io`) |

**Zero secrets in the repo.** `.env.example` is example-only; the deploy
templates (`deploy/templates/*.tmpl`) carry placeholders, never values. The
`scripts/deploy/render-config.sh --check` step in the deploy workflow fails the
deploy loudly if any required secret is missing.

## Wire `FLOCK_DEPLOY_CMD`

The deploy workflow calls `$FLOCK_DEPLOY_CMD` with `<TAG>` substituted to the
target image tag. Pick the pattern for your orchestrator:

```bash
# Frappe Cloud Server plan (SSH + docker):
FLOCK_DEPLOY_CMD='ssh frappe@<host> "cd /home/frappe && \
    docker pull ghcr.io/<org>/flock-os-bench:<TAG> && \
    docker stop flock-os || true && \
    docker run -d --name flock-os --env-file /etc/flock-os/staging.env \
        -p 8080:8080 -p 9000:9000 \
        ghcr.io/<org>/flock-os-bench:<TAG>"'

# docker compose (self-hosted VM):
FLOCK_DEPLOY_CMD='TAG=<TAG> docker compose up -d --no-deps --force-recreate bench'

# kubernetes:
FLOCK_DEPLOY_CMD='kubectl set image deployment/flock-os \
    bench=ghcr.io/<org>/flock-os-bench:<TAG>'
```

Set it as an environment variable on BOTH the `staging` and `production`
GitHub environments (repo Settings → Environments).

## How to deploy

### Staging (automatic)

1. Merge a slice into `master` (the per-slice worktree workflow —
   `docs/development/per-slice-worktrees.md`).
2. The `CI` workflow runs the lint+test gate.
3. CI green → the `Deploy` workflow builds + pushes the image, runs the
   `--check` secret gate, then invokes `FLOCK_DEPLOY_CMD` for the `staging`
   environment.
4. Post-deploy smoke (`scripts/deploy/smoke-staging.sh`) runs against
   `$STAGING_URL`. If it fails, the workflow fails — staging is NOT healthy
   and prod promotion is blocked.
5. On success, the tag is recorded as `FLOCK_PREVIOUS_TAG` on the `staging`
   environment (the rollback target for the next deploy).

### Production (manual promotion gate)

1. The `staging` environment has a green smoke from the latest auto-deploy.
2. **A required reviewer on the `production` environment** (CEO/QA) approves
   the promotion: Actions → "Deploy" → Run workflow → check `promote_to_prod`.
3. The workflow promotes the **same tag that passed the staging smoke** (no
   rebuild — exact artifact parity) and runs the prod smoke.

## How to roll back

`scripts/deploy/rollback.sh` re-deploys `FLOCK_PREVIOUS_TAG` (or an explicit
`--to <tag>`) and re-runs the smoke:

```bash
STAGING_URL=https://staging.flock-os.example \
FLOCK_CURRENT_TAG=sha-bad-999 FLOCK_PREVIOUS_TAG=sha-good-888 \
FLOCK_DEPLOY_CMD='TAG=<TAG> docker compose up -d --no-deps --force-recreate bench' \
scripts/deploy/rollback.sh
```

- `FLOCK_CURRENT_TAG` and `FLOCK_PREVIOUS_TAG` come from the deploy
  orchestrator's state (the workflow records `FLOCK_PREVIOUS_TAG` on the
  `staging` environment after each green deploy).
- The rollback re-runs the smoke. If the rolled-back tag is also unhealthy,
  roll further back: `--to <older-tag>`.
- The Phase 6.1 acceptance gate records one **rollback drill** (deploy →
  break → roll back → verify) as evidence; capture the commands + smoke
  output in the issue thread.

## The FLO-121 scaled-socketio tier across deploys

The tier is **self-healing across `bench update` and across restarts**:

1. **Image build** (`deploy/Dockerfile`): runs
   `scripts/dev/wire-socketio-redis-adapter.sh` against the vendored Frappe
   realtime `index.js`, arming the `@socket.io/redis-adapter` block. The image
   ships with the adapter armed.
2. **`bench migrate`** (entrypoint step 3): the flock_os `after_migrate` hook
   (`flock_os/utils/realtime_setup.py`) re-runs the wiring script. Even if a
   framework upgrade rewrote `index.js`, the adapter block is re-inserted
   before any socketio worker boots.
3. **Supervisor** (`deploy/supervisord.conf`): the `socketio-tier` program
   runs `scripts/dev/scale-socketio.sh start N --lb nginx`, which brings up
   N node backends + the nginx sticky-L7 LB. Supervisor auto-restarts the
   tier if it dies — this is the ADR blocker #2 (the scaled tier survives a
   Frappe Cloud dashboard restart, not just gunicorn).

If a dashboard restart collapses the tier to a single socketio, SSH in and:

```bash
docker exec -it flock-os supervisorctl restart socketio-tier
# or, on a bare-VM deploy:
sudo supervisorctl restart socketio-tier
```

## Render site config (manual)

To render the config off-image (debugging, a fresh VM bring-up):

```bash
DB_HOST=... DB_NAME=... DB_USER=... DB_PASSWORD=... \
REDIS_CACHE_URI=... REDIS_QUEUE_URI=... REDIS_SOCKETIO_URI=... \
FLOCK_SIO_ADAPTER_REDIS=... SECRET_KEY=... SITE_URL=https://... FLOCK_ENV=staging \
scripts/deploy/render-config.sh --sites-dir ./sites --site flock_os

# Or just check the secret set without writing:
scripts/deploy/render-config.sh --check
```

## nginx sticky-L7 (Cloudflare caveat)

The prod nginx upstream uses `ip_hash` for sticky L7 — correct for a direct
Frappe Cloud deploy. **Cloudflare collapses every viewer to a handful of CF
source IPs**, so a Cloudflare-fronted deploy must swap `ip_hash` for a
sticky-cookie module:

```nginx
# Replace `ip_hash;` in deploy/nginx/prod.conf with a sticky cookie (requires
# nginx compiled with the sticky module, or lua-nginx-module). Frappe Cloud
# Server plans ship a sticky-capable nginx; verify with `nginx -V`.
sticky_cookie_affinity;
# or the lua variant:
# set $sticky_cookie "";
# access_by_lua_block {
#   ...
# }
```

Document the chosen variant in the environment's runbook when the staging URL
goes live. The default `ip_hash` is the safe direct-deploy baseline.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `render-config: missing required env vars` | The secret manager didn't inject a required var. `scripts/deploy/render-config.sh --print-env` shows which (redacted). |
| Smoke `[1/3] FAIL` (HTTP non-2xx/3xx) | nginx or gunicorn misconfigured. `docker exec flock-os supervisorctl status` + `tail /var/log/flock-os/nginx.err.log`. |
| Smoke `[2/3] FAIL` (ping != pong) | gunicorn is up but the site config is broken. Check `site_config.json` rendered correctly + `bench --site flock_os migrate` succeeded. |
| Smoke `[3/3] FAIL` (WS handshake) | The scaled-socketio tier is down. `supervisorctl status socketio-tier`; `tail /var/log/flock-os/socketio-tier.err.log`. Common cause: `FLOCK_SIO_ADAPTER_REDIS` unreachable (FLO-127 §2). |
| `FLOCK_DEPLOY_CMD is empty` | Set it on the GitHub environment (Settings → Environments → staging/production → environment variables). |
| Rollback failed mid-deploy | The prior tag is still live; investigate `$FLOCK_DEPLOY_CMD` output. Do NOT re-run with the broken current tag. |

## Out of scope

- Observability / alerting / restore drill → Phase 6.2.
- Real launch-partner onboarding + 15k-in-prod → Phase 6.3.
- First real event → Phase 6.4.
