# Deploy pipeline — Flock OS (FLO-274 / FLO-246 Phase 6.1)

> **How the slice-1 deploy workflow ([`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml),
> [FLO-247](/FLO/issues/FLO-247)) targets the staging VM and promotes to prod.**
> This is the *pipeline mechanics* reference: the job graph, the
> environment/secret wiring, the orchestrator hook that ships the image to the
> host, and the staging→prod promotion gate.
>
> Companions (read together, each owns one concern):
>
> - **[`provision-staging-vm.md`](provision-staging-vm.md)** — the **one-time
>   bring-up** of the VM the pipeline deploys *onto* (SKU, SSH, DNS, TLS, DB,
>   Redis, the GitHub `staging` environment first-wire). The pipeline below
>   assumes that runbook's acceptance checklist is green.
> - **[`deploy-runbook.md`](deploy-runbook.md)** — **day-2 operations**: how to
>   deploy, roll back, run the smoke by hand, and keep the FLO-121 scaled-
>   socketio tier alive across restarts.
> - **[`secrets-runbook.md`](secrets-runbook.md)** — the **SOPS+age** key/edit/
>   rotate flow this pipeline's secret gate depends on ([FLO-248](/FLO/issues/FLO-248)).
> - **[`staging-preflight-checklist.md`](staging-preflight-checklist.md)** — the
>   step-through checklist the CEO/board/DevOps run once the VM is live to prove
>   the pipeline's end-to-end contract holds.

## TL;DR

```
push to master
   │
   ▼
┌──────────────┐    ┌───────────────┐    ┌─────────────────┐
│ lint-and-test│───►│  build-image  │───►│ deploy-staging  │──┐
│ (CI reuse)   │    │ (push to GHCR)│    │ + smoke [1..3/3]│  │
└──────────────┘    └───────────────┘    └─────────────────┘  │
                                                          record
                                                       FLOCK_PREVIOUS_TAG
                                                              │
                    manual workflow_dispatch                  ▼
                    (CEO/QA sign-off)              ┌───────────────────────┐
                 ┌──────────────────────────────► │ promote-to-prod       │
                 │   (same artifact, no rebuild)  │ + smoke [1..3/3]      │
                 │                                └───────────────────────┘
```

- **Staging is automatic.** Any push to `master` with a green CI gate builds the
  image, ships it to the VM via `$FLOCK_DEPLOY_CMD`, and runs the smoke. No human
  in the loop for staging.
- **Prod is a manual promotion.** A `workflow_dispatch` with `promote_to_prod`
  re-deploys the **exact tag that passed the staging smoke** (artifact parity —
  no rebuild), behind a required `production` environment reviewer.
- **Secrets never ride the workflow as per-secret values.** The only deploy
  secret an environment needs is `SOPS_AGE_KEY`; the rest decrypt from the
  committed `secrets/<env>.enc.yaml` ciphertext at deploy time.

## The job graph

`.github/workflows/deploy.yml` is a single four-job DAG. Every job fails loud
(fast) rather than silent — a masked misconfiguration is worse than a stopped
deploy.

| Job | Runs when | Owns | Blocks prod? |
|-----|-----------|------|--------------|
| `lint-and-test` | always (reuses `.github/workflows/ci.yml`) | the "master green" signal | yes — `build-image` needs it |
| `build-image` | `master` push only | build + push `flock-os-bench:<tag>` + `:latest` to the registry | yes — `deploy-staging` needs the tag |
| `deploy-staging` | `master` push only | decrypt gate → `FLOCK_DEPLOY_CMD` → smoke → record prior tag | yes — prod promotes the staging-green tag |
| `promote-to-prod` | `workflow_dispatch` + `promote_to_prod == true` | decrypt gate (prod key) → promote tag → prod smoke | n/a (this *is* the prod gate) |

**Concurrency:** one deploy in flight per environment
(`deploy-staging` group vs `deploy-prod` group), `cancel-in-progress: false`. A
racing second deploy is the classic cause of a half-migrated bench — the lock
prevents it.

### Image tagging (traceability)

`build-image` resolves the tag in priority order:

1. `inputs.target_tag` (manual override — pin a hotfix or re-deploy an old tag).
2. Else `sha-<short-sha>-<run-id>` — monotonically traceable back to the commit
   *and* unique per run.

The same tag is pushed as `:latest` for convenience, but every deploy records
the explicit `sha-…` tag it ran so rollback is never ambiguous.

## What gets deployed

The unit of deploy is the **`flock-os-bench` container image**
(`deploy/Dockerfile`) — a complete Frappe bench with the FLO-121 scaled-socketio
tier baked in:

- gunicorn (web) + bench worker (queues) + bench schedule (scheduler)
- **N node socketio workers** behind the **nginx sticky-L7 LB**
  (`deploy/nginx/prod.conf`), armed with the `@socket.io/redis-adapter` at build
  and re-armed on every `bench migrate` by the `after_migrate` hook.
- the front-edge nginx (web reverse proxy + socketio sticky-L7 LB).

**The image contains zero secrets.** At container start, `deploy/entrypoint.sh`
runs `scripts/deploy/render-config.sh`, which renders `site_config.json` +
`common_site_config.json` from environment the orchestrator injected. See
[`deploy-runbook.md` → "What gets deployed"](deploy-runbook.md#what-gets-deployed)
for the full component breakdown.

## How the workflow targets the VM — `FLOCK_DEPLOY_CMD`

The workflow is **orchestrator-agnostic by design**: it does not hard-code SSH,
docker, or kubectl. Instead it `eval`s a single environment variable,
`FLOCK_DEPLOY_CMD`, with the literal `<TAG>` substituted to the image tag:

```yaml
- name: Deploy tag … to staging
  env:
    FLOCK_DEPLOY_CMD: ${{ vars.FLOCK_DEPLOY_CMD }}
    FLOCK_IMAGE_TAG: ${{ needs.build-image.outputs.tag }}
  run: |
    [[ -z "$FLOCK_DEPLOY_CMD" ]] && { echo "FLOCK_DEPLOY_CMD is empty …"; exit 1; }
    eval "${FLOCK_DEPLOY_CMD//\<TAG\>/$FLOCK_IMAGE_TAG}"
```

If `FLOCK_DEPLOY_CMD` is empty, the step **fails loud** — an empty deploy command
would silently "succeed" and mask a misconfigured environment.

`FLOCK_DEPLOY_CMD` is a GitHub Actions **environment variable** (not a secret —
its *shape* is public; only the hostname inside is private). Set it on **both**
the `staging` and `production` environments (repo Settings → Environments).
Canonical shapes:

```bash
# Frappe Cloud Server plan (SSH + docker) — the ADR target:
FLOCK_DEPLOY_CMD='ssh -i /tmp/flock_deploy_key -o StrictHostKeyChecking=no frappe@<staging-vm> \
    "cd /home/frappe && \
     docker pull ghcr.io/<org>/flock-os-bench:<TAG> && \
     docker stop flock-os || true && \
     docker run -d --name flock-os --env-file /etc/flock-os/staging.env \
         -p 8080:8080 -p 9000:9000 \
         ghcr.io/<org>/flock-os-bench:<TAG>"'

# docker compose (self-hosted VM):
FLOCK_DEPLOY_CMD='TAG=<TAG> docker compose up -d --no-deps --force-recreate bench'

# kubernetes:
FLOCK_DEPLOY_CMD='kubectl set image deployment/flock-os bench=ghcr.io/<org>/flock-os-bench:<TAG>'
```

> **Open wiring note:** `deploy.yml` references `/tmp/flock_deploy_key` inside
> the SSH shape above but does not yet write it from the `FLOCK_DEPLOY_SSH_KEY`
> secret before the `eval`. Provisioning must add a step that materializes the
> key (e.g. `echo "$FLOCK_DEPLOY_SSH_KEY" > /tmp/flock_deploy_key && chmod 600`)
> or bake it into the hook. Tracked against the slice-1 pipeline
> ([FLO-246](/FLO/issues/FLO-246)), not this doc.

The full first-time wiring (SSH keygen, key distribution, the VM bring-up the
hook targets) is in [`provision-staging-vm.md` → §2 + §7d](provision-staging-vm.md#2-ssh-key-setup--keygen-draftable-now--upload-blocked-on-provisioning).

## Secrets rendering (SOPS+age → render-config)

The pipeline's secret contract is **decrypt → validate → render**, executed
*before* any rolling change so a missing/mismatched secret stops the deploy
rather than producing a half-configured bench.

### The chain

```
 GitHub Actions env secret SOPS_AGE_KEY  (the ONE age private key for the env)
            │
            ▼
 secrets/<env>.enc.yaml  ──sops -d──►  plaintext env (in $RUNNER_TEMP, in memory only)
 (committed ciphertext)                   │
                                          ├─► render-secrets.sh --check   (decrypt gate)
                                          ├─► render-secrets.sh --out     (dotenv)
                                          └─► render-config.sh  --check   (config contract gate)
                                                       │
                                                       ▼
                                          site_config.json / common_site_config.json
                                          (rendered by entrypoint.sh at container start)
```

### The two gates (both must pass)

The `deploy-staging` and `promote-to-prod` jobs each run **two** fail-loud
checks back-to-back before `$FLOCK_DEPLOY_CMD`:

1. **Decrypt gate** — `scripts/deploy/render-secrets.sh --env <env> --check`
   decrypts `secrets/<env>.enc.yaml` with `SOPS_AGE_KEY` and confirms every
   *required* key is present. Fails if the key doesn't match the recipient in
   `.sops.yaml`, or if a required key is absent.
2. **Config-contract gate** — sources the rendered dotenv, then runs
   `scripts/deploy/render-config.sh --check`, which proves the full
   secret→config template contract holds (every placeholder in
   `deploy/templates/*.tmpl` has a value) **without writing anything**.

```yaml
- name: Decrypt secrets + render-config check (SOPS+age)
  env:
    SOPS_AGE_KEY: ${{ secrets.SOPS_AGE_KEY }}
  run: |
    ENV_FILE="$RUNNER_TEMP/flock.env"
    scripts/deploy/render-secrets.sh --env staging --check
    scripts/deploy/render-secrets.sh --env staging --out "$ENV_FILE"
    echo "FLOCK_ENV_FILE=$ENV_FILE" >> "$GITHUB_ENV"
    set -a; . "$ENV_FILE"; set +a
    scripts/deploy/render-config.sh --check
```

The rendered `FLOCK_ENV_FILE` is exposed to later steps so `$FLOCK_DEPLOY_CMD`
can ship it to the host (`--env-file` / `scp`). `$RUNNER_TEMP` is auto-cleaned
by the runner — plaintext never persists.

### Environment + secret inventory

Two GitHub Actions environments back the two-stage pipeline. Staging and prod
use **separate** age keys — a staging key compromise must never read prod.

| Environment | Purpose | Secret(s) | Variable(s) |
|-------------|---------|-----------|-------------|
| `staging` | master-green auto-deploy target | `SOPS_AGE_KEY` (staging), `STAGING_URL`, `STAGING_WS_URL`, `FLOCK_DEPLOY_SSH_KEY` | `FLOCK_DEPLOY_CMD` |
| `production` | manual-promotion target (required reviewers ON) | `SOPS_AGE_KEY` (**prod** key), `PROD_URL`, `PROD_WS_URL` | `FLOCK_DEPLOY_CMD` |
| repo (shared) | registry auth | `FLOCK_IMAGE_REGISTRY_TOKEN` (falls back to `GITHUB_TOKEN`) | `FLOCK_IMAGE_REGISTRY` (default `ghcr.io`) |

The **actual deploy secrets** (DB, Redis, `SECRET_KEY`, …) live as SOPS
ciphertext in `secrets/<env>.enc.yaml` — *not* as individual GitHub Actions
secrets. For the full per-secret classification (board/human-provided vs
agent-generated) see
[`provision-staging-vm.md` → §7c "Secret classification"](provision-staging-vm.md#7c-secret-classification--boardhuman-provided-vs-agent-generated),
and [`secrets-runbook.md`](secrets-runbook.md) for the key/edit/rotate flow.

> **Zero secrets in the repo.** `.env.example` is example-only; the deploy
> templates (`deploy/templates/*.tmpl`) carry placeholders, never values; and
> `secrets/` holds only `*.enc.yaml` ciphertext (plaintext + age private keys
> are gitignored).

## The staging→prod promotion path

Promotion is a **manual gate with artifact parity** — prod never gets an artifact
that didn't first pass the staging smoke.

1. **Staging smoke is green** for the latest auto-deploy; the workflow recorded
   that tag as `FLOCK_PREVIOUS_TAG` on the `staging` environment (see
   "Rollback target" below).
2. **A required reviewer on the `production` environment** (CEO/QA) triggers the
   promotion: Actions tab → **Deploy** workflow → **Run workflow** → check
   `promote_to_prod`. The reviewer approval *is* the ADR §manual-promotion gate.
3. `promote-to-prod` resolves the target tag (the staging `FLOCK_PREVIOUS_TAG`,
   or an explicit `inputs.target_tag` for a hotfix pin), runs the **prod**
   decrypt+render gate against the prod bundle/key, then `eval`s the
   `production`-environment `FLOCK_DEPLOY_CMD`.
4. **No rebuild.** Prod re-deploys the *same image tag* staging smoked — exact
   artifact parity, so a staging-passes/prod-fails divergence is impossible
   except for environment-specific config (which the prod gate catches).
5. A **post-promotion smoke** runs against `$PROD_URL` / `$PROD_WS_URL` (the
   same `smoke-staging.sh`, env-overridden). If it fails, follow the rollback
   path in [`deploy-runbook.md` → "How to roll back"](deploy-runbook.md#how-to-roll-back).

```yaml
promote-to-prod:
  if: github.event_name == 'workflow_dispatch' && inputs.promote_to_prod == true
  environment:
    name: production   # MUST have required reviewers enabled — CEO/QA sign-off
```

> **Enable required reviewers on `production`.** This is the one repo setting
> that turns the promotion from "anyone with write access" into the intended
> CEO/QA gate. Repo Settings → Environments → `production` → Required reviewers.

## Rollback target (`FLOCK_PREVIOUS_TAG`)

Every green staging deploy records its tag as the `FLOCK_PREVIOUS_TAG`
environment variable on the `staging` environment (create-or-upsert):

```yaml
- name: Record the prior tag for rollback
  uses: actions/github-script@v7
  with:
    script: |
      const tag = '${{ needs.build-image.outputs.tag }}';
      await github.rest.actions.createEnvironmentVariable({
        owner: context.repo.owner, repo: context.repo.repo,
        environment_name: 'staging', name: 'FLOCK_PREVIOUS_TAG', value: tag,
      }).catch(e => github.rest.actions.updateEnvironmentVariable({
        owner: context.repo.owner, repo: context.repo.repo,
        environment_name: 'staging', name: 'FLOCK_PREVIOUS_TAG', value: tag,
      }));
```

`promote-to-prod` reads that variable to pick the promotion tag, and
`scripts/deploy/rollback.sh` reads it as the default rollback target. The
rollback flow itself (deploy → break → roll back → re-smoke) is documented in
[`deploy-runbook.md` → "How to roll back"](deploy-runbook.md#how-to-roll-back).

## Post-deploy smoke (the pipeline's quality gate)

Both `deploy-staging` and `promote-to-prod` end with
`scripts/deploy/smoke-staging.sh` — the same three-probe gate used by hand and
in the pre-flight checklist:

- **`[1/3]` HTTP reachability + TLS** — `curl` the site root, expect 2xx/3xx.
- **`[2/3]` Frappe API liveness** — `/api/method/ping` returns `pong` (proves
  gunicorn + bench boot + site config rendered cleanly).
- **`[3/3]` WebSocket connect** — a WS handshake completes against the
  scaled-socketio tier (proves the FLO-121 N-worker sticky-L7 tier is up and
  the `@socket.io/redis-adapter` is armed).

A smoke failure fails the workflow → blocks prod promotion. The probe source +
per-probe troubleshooting is in
[`deploy-runbook.md` → Troubleshooting](deploy-runbook.md#troubleshooting); the
step-through (with expected output) is in
[`staging-preflight-checklist.md`](staging-preflight-checklist.md).

## Troubleshooting the pipeline itself

| Symptom | Cause / fix |
|---------|-------------|
| `FLOCK_DEPLOY_CMD is empty` | Set it as an environment variable on the GitHub environment (Settings → Environments → staging/production). See "How the workflow targets the VM" above. |
| Decrypt gate: `sops decrypt failed` | `SOPS_AGE_KEY` doesn't match the recipient in `.sops.yaml`. Re-rotate the bundle. See [`secrets-runbook.md` → Troubleshooting](secrets-runbook.md#troubleshooting). |
| Decrypt gate: `bundle is missing required keys` | Open `secrets/<env>.enc.yaml` with `sops` and add the listed keys; save → commit. |
| Config gate: `missing required env vars` | A template placeholder has no value. `render-config.sh --print-env` shows which (redacted). |
| Smoke `[3/3] FAIL` (WS handshake) | Scaled-socketio tier down or `FLOCK_SIO_ADAPTER_REDIS` unreachable. See [`deploy-runbook.md` → Troubleshooting](deploy-runbook.md#troubleshooting). |
| `build-image` denied: packages | `FLOCK_IMAGE_REGISTRY_TOKEN` missing/invalid, or the workflow lacks `packages: write`. |
| Two deploys raced | Should be impossible (concurrency lock). If seen, confirm `cancel-in-progress: false` and the group key are intact. |

## Out of scope

- **VM bring-up** (SKU, SSH upload, DNS, TLS, managed DB/Redis) →
  [`provision-staging-vm.md`](provision-staging-vm.md).
- **Day-2 operations** (hand deploy, rollback drill, supervisor/nginx triage) →
  [`deploy-runbook.md`](deploy-runbook.md).
- **Key/edit/rotate** the SOPS bundles → [`secrets-runbook.md`](secrets-runbook.md).
- Observability / alerting / restore drill → Phase 6.2.
- Real launch-partner onboarding + 15k-in-prod → Phase 6.3.
