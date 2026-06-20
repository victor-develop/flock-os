# Staging cloud VM provisioning runbook — Flock OS (FLO-249 Phase 6.1 slice 3)

> The concrete, step-by-step bring-up of the **staging Frappe Cloud Server
> plan VM** that the slice-1 deploy pipeline ([`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml))
> targets. This is the **hard external gate** for Phase 6.1 — the moment a
> reachable staging URL over TLS exists, the acceptance criteria of
> [FLO-246](/FLO/issues/FLO-246) are provable and slices 4 ([FLO-250](/FLO/issues/FLO-250))
> and 5 ([FLO-251](/FLO/issues/FLO-251)) unblock.
>
> Target is fixed by the Phase 6.0 ADR ([FLO-245](/FLO/issues/FLO-245) document
> `adr`): a **Frappe Cloud Server plan** VM (AWS or OCI dedicated) running one
> prod `bench` + the [FLO-121](/FLO/issues/FLO-121) scaled-socketio tier behind
> nginx, fronted by **Cloudflare** (orange-cloud) with **Let's Encrypt TLS** at
> the Frappe Cloud edge. Economics in the [hosting-quote](/FLO/issues/FLO-231#document-hosting-quote).
>
> Companion runbook: [`deploy-runbook.md`](deploy-runbook.md) (how to deploy /
> roll back once the VM exists). This runbook ends where that one starts.

## TL;DR

```bash
# 0. Prereqs (HUMAN — see "Prerequisites/blockers" below):
#    - board acceptance of approval 609ccd5d
#    - a Frappe Cloud account with a payment method on file

# 1. Provision the Server plan VM in Frappe Cloud dashboard (AWS/OCI dedicated).
# 2. Upload the deploy SSH key; note the VM hostname.
# 3. Register the staging site + point DNS at the Frappe Cloud edge:
#       staging.<your-domain>  →  <frappe-cloud-edge-host>   (Cloudflare orange-cloud ON)
# 4. Let Frappe Cloud issue the Let's Encrypt cert (automatic, edge-side).
# 5. Wire the GitHub Actions `staging` environment:
#       FLOCK_DEPLOY_CMD + the staging secret set (see "Wire FLOCK_DEPLOY_CMD").
# 6. First deploy: push to master (auto) and verify the smoke is green:
STAGING_URL=https://staging.<your-domain> scripts/deploy/smoke-staging.sh
```

## Prerequisites / blockers

Two real-world gates that **only humans can clear**. An agent cannot create the
commercial relationships; this runbook is drafted ahead of time so the moment
they clear, provisioning is a straight-line execution.

| # | Blocker | Status | Unblock owner | Action |
|---|---------|--------|---------------|--------|
| 1 | Board endorsement of [FLO-231](/FLO/issues/FLO-231) Phase 6 north star (includes the ~$1,500 launch-window hosting budget per the [hosting-quote](/FLO/issues/FLO-231#document-hosting-quote)) | **pending** ([approval 609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc)) | **board** | Accept the approval |
| 2 | A Frappe Cloud account with a payment method on file | **not present** | **CEO** (or board delegate) | Create the account at [frappe.io/cloud](https://frappe.io/cloud), add a card |

> Why an agent can't self-serve: no Frappe Cloud credentials exist in the
> company, no payment method is on file. These are real-world commercial
> relationships, not code/infra. Do **not** attempt to script around them.

### How to read this runbook (draft vs execute)

Sections are flagged so it is obvious which lines are **draftable now** (no
infra, no account) vs which **execute only after the gate clears**:

- **BLOCKED-ON-PROVISIONING** — the step needs the Frappe Cloud VM/account,
  i.e. it cannot run until both blockers in the table above resolve. These are
  the lines that move from "drafted" to "executed" the moment
  [FLO-249](/FLO/issues/FLO-249) unblocks.
- **draftable now** — the step can be prepared today (keypairs generated,
  bundle shape validated, DNS/Cloudflare planned, commands rehearsed) so
  execution is a straight-line copy-paste once the VM exists.

Every §1–§8 section header carries one of these flags.

## 1. Pick the staging Server plan SKU — BLOCKED-ON-PROVISIONING

Per the [ADR](/FLO/issues/FLO-245) and the [hosting-quote](/FLO/issues/FLO-231#document-hosting-quote),
**staging accepts a smaller dedicated VM than prod** (staging only has to host
the topology + run the smoke + the [FLO-250](/FLO/issues/FLO-250) load rehearsal;
it does not face real 15k traffic until [FLO-251](/FLO/issues/FLO-251) promotes).

**Recommended staging SKU** — pick **one** of the following from the
[Frappe Cloud Server plan catalog](https://frappe.io/cloud/pricing) (the exact
SKU names rotate; match the spec, not the label):

| Option | Provider | vCPU | RAM | Disk | Notes |
|--------|----------|------|-----|------|-------|
| **Staging A (preferred)** | AWS dedicated | ≥ 4 | 16 GB | ≥ 80 GB SSD | Hits the ADR minimum (`≥4 core / 16–32 GB`). Same provider family as prod → no provider-specific surprise at promotion. |
| Staging B (cost floor) | DigitalOcean / Hetzner dedicated | ≥ 4 | 16 GB | ≥ 80 GB SSD | ADR permits DO/Hetzner for staging. Use only if AWS capacity/budget blocks A. |
| Staging C (over-spec, avoid) | OCI dedicated | ≥ 4 | 32 GB | ≥ 80 GB SSD | Only if a single SKU is shared staging+prod for 6.3 load rehearsal. Otherwise overkill for staging. |

> The launch-band ADR constraint is the **topology fit**, not raw size: the VM
> must host one `bench` + **N (≈4) node socketio workers** + nginx + a dedicated
> adapter Redis + MariaDB. Anything below 4 vCPU / 16 GB will thrash the
> socketio tier under the [FLO-250](/FLO/issues/FLO-250) rehearsal smoke. Do
> **not** pick a Frappe Cloud shared *Site plan* — it does not expose the bench
> topology the [FLO-121](/FLO/issues/FLO-121) tier needs (ADR §Provider).

Record the chosen plan in the [FLO-249](/FLO/issues/FLO-249) thread when you
provision (provider, vCPU/RAM/disk, monthly $) so prod sizing in
[FLO-251](/FLO/issues/FLO-251) can reuse the data point.

## 2. SSH key setup — keygen draftable now · upload BLOCKED-ON-PROVISIONING

The deploy pipeline ships the image over SSH + `docker pull`
([`deploy-runbook.md` → Wire FLOCK_DEPLOY_CMD](deploy-runbook.md#wire-flock_deploy_cmd)),
so a dedicated **deploy key** (not a personal key) must be authorized on the VM.

> The `ssh-keygen` below is **draftable now** (step 2 storing the private key
> in the GitHub secret is also draftable). Steps 1 and 3 (upload to VM, smoke
> from VM) are **BLOCKED-ON-PROVISIONING** — they need the VM up.

```bash
# On the DevOps workstation (or wherever the GitHub Action's secret was created):
ssh-keygen -t ed25519 -f ~/.ssh/flock_os_staging_deploy -C "flock-os-staging-deploy"
# → ~/.ssh/flock_os_staging_deploy      (PRIVATE — goes into the GitHub secret)
# → ~/.ssh/flock_os_staging_deploy.pub  (PUBLIC  — goes onto the VM)
```

1. Upload the **public** key in the Frappe Cloud dashboard (Server plan → SSH
   keys) **or** append it to `~/.ssh/authorized_keys` for the bench user once
   the VM is up:
   ```bash
   ssh frappe@<staging-vm> 'mkdir -p ~/.ssh && chmod 700 ~/.ssh && \
       tee -a ~/.ssh/authorized_keys' < ~/.ssh/flock_os_staging_deploy.pub
   ssh frappe@<staging-vm> 'chmod 600 ~/.ssh/authorized_keys'
   ```
2. Store the **private** key as the GitHub Actions `staging` environment secret
   `FLOCK_DEPLOY_SSH_KEY` (repo Settings → Environments → `staging` → Secrets).
   Never commit it; `.env.example` stays example-only.
3. Smoke-test the key from a clean shell before wiring CI:
   ```bash
   ssh -i ~/.ssh/flock_os_staging_deploy frappe@<staging-vm> 'echo ok; uname -a'
   ```

## 3. Register the staging site on the VM — BLOCKED-ON-PROVISIONING

On a Frappe Cloud **Server plan**, the VM ships with a bench pre-installed. The
staging site is created through the dashboard (recommended) or `bench`:

**Dashboard (preferred):** Frappe Cloud → your Server plan → "New Site" → name
it `flock-os-staging` (or your chosen site name). Frappe Cloud auto-wires
nginx + the SSL hook + backups.

**Manual (if you need a non-default site name or custom bench):**
```bash
ssh -i ~/.ssh/flock_os_staging_deploy frappe@<staging-vm>
cd ~/frappe-bench
sudo bench new-site flock-os-staging \
    --db-host 127.0.0.1 --db-root-password "$MYSQL_ROOT_PASSWORD"
sudo bench --site flock-os-staging install-app flock_os
```

The containerized-deploy path (the [slice-1](/FLO/issues/FLO-246) pipeline)
**replaces** the in-VM bench with the image — the site registration above is the
**first-time** bring-up; subsequent deploys are tag-pushed via `$FLOCK_DEPLOY_CMD`
and the site row persists in MariaDB. Do not re-run `bench new-site` after CI is
wired.

## 4. DNS wiring (Cloudflare orange-cloud) — BLOCKED-ON-PROVISIONING

The ADR puts Cloudflare in front for CDN + rate-limit + DDoS on the
Gunicorn-billed public surface. WebSocket traffic rides the same hostname
(Cloudflare supports WS on orange-clouded records).

1. In the **Cloudflare** dashboard for your domain, add (or update) the
   staging record:
   | Type | Name | Target | Proxy | TTL |
   |------|------|--------|-------|-----|
   | `CNAME` | `staging` | `<frappe-cloud-edge-host>` | **Proxied (orange-cloud)** | Auto |
   `<frappe-cloud-edge-host>` is the hostname Frappe Cloud assigned to the
   Server plan VM (visible in the dashboard). It is **not** the raw VM IP —
   Frappe Cloud's edge terminates TLS, so we point DNS at the edge.
2. Under **SSL/TLS** → set mode to **Full (strict)** (Frappe Cloud has a valid
   LE cert; "Flexible" would downgrade to HTTP on the origin leg and break the
   WS upgrade).
3. Under **Network** → confirm **WebSockets** is enabled (it is by default on
   all plans). WS fan-out is the [FLO-121](/FLO/issues/FLO-121) hot path; if
   this is off, every socketio connection will fail at the upgrade step.
4. (Optional, recommended) Under **Rules** → **Cache Rules**, add a rule that
   **bypasses** cache for `/socket.io/*` and `/api/*` — those must reach the
   bench live. Static assets (`/assets/*`) cache fine.

```bash
# Verify DNS resolves through Cloudflare:
dig +short staging.<your-domain>   # → one or more Cloudflare IPs (104.x / 172.x)
```

## 5. TLS (Let's Encrypt at the Frappe Cloud edge) — BLOCKED-ON-PROVISIONING

TLS is **automatic** on Frappe Cloud Server plans — the edge issues and renews a
Let's Encrypt certificate for any hostname that resolves to it. There is no
manual `certbot` step.

To trigger issuance:

1. Confirm the DNS record from §4 is proxied (orange) **and** the staging
   hostname resolves to the Frappe Cloud edge.
2. In the Frappe Cloud dashboard → your Server plan → **Domains** → **Add
   Domain** → enter `staging.<your-domain>`. Frappe Cloud runs the HTTP-01
   challenge against LE and installs the cert at the edge within ~60s.
3. Verify end-to-end over TLS:
   ```bash
   curl -sI https://staging.<your-domain> | head -5
   echo | openssl s_client -connect staging.<your-domain>:443 -servername staging.<your-domain> 2>/dev/null \
       | openssl x509 -noout -issuer -dates
   # issuer should mention Let's Encrypt; dates should span today.
   ```

If Frappe Cloud's edge cert ever needs replacing (custom cert, wildcard, etc.),
use the dashboard's **Custom SSL** upload — out of scope for staging.

## 6. Managed MariaDB + dedicated Redis provisioning — BLOCKED-ON-PROVISIONING

Per the [ADR](/FLO/issues/FLO-245) §Redis/DB:

- **MariaDB** — managed by Frappe Cloud (Frappe v15-supported, 10.6+). On a
  Server plan this is provisioned with the VM; note the connection details
  (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`) from the dashboard.
  For staging the default managed instance is sufficient — no custom tuning is
  needed at this band.
- **Redis** — Frappe Cloud provisions the standard `redis_cache` + `redis_queue`
  instances. **You must add a third, dedicated adapter Redis** for
  `@socket.io/redis-adapter` — the shared `redis_socketio` stalls under the
  adapter's pub/sub burst ([FLO-127](/FLO/issues/FLO-127) §2). On a Server plan:
  ```bash
  ssh -i ~/.ssh/flock_os_staging_deploy frappe@<staging-vm>
  # Frappe Cloud VMs ship redis-server; create a dedicated DB for the adapter:
  sudo nano /etc/redis/redis-adapter.conf    # port 6389, maxmemory 256mb, no eviction
  sudo systemctl enable --now redis-adapter
  redis-cli -p 6389 ping                      # → PONG
  ```
  The adapter URI becomes `FLOCK_SIO_ADAPTER_REDIS=redis://127.0.0.1:6389`
  (or `rediss://...` if you TLS-wrap it). Keep cache (`redis_cache`) and queue
  (`redis_queue`) on their default DBs.

> **No Redis Cluster at the launch band** (ADR §Redis). A single healthy
> adapter instance carries the 15k fan-out because the adapter runs on pub/sub,
> not data sharding. Cluster is a >15k / HA concern for Phase 6.3+.

## 7. Wire `FLOCK_DEPLOY_CMD` + the SOPS+age staging secret bundle

> The age-keypair bootstrap and the `FLOCK_DEPLOY_CMD` shape (§7a, §7d, §7e)
> are **draftable now** — they need neither the VM nor board approval. Filling
> the bundle with real DB/Redis values (§7b) is **BLOCKED-ON-PROVISIONING**
> (those values come from the Frappe Cloud dashboard once the VM exists).

The deploy workflow
([`.github/workflows/deploy.yml`](../../.github/workflows/deploy.yml)) does
**not** read per-secret GitHub Actions values. Per [FLO-248](/FLO/issues/FLO-248)
(slice 2, done), the source of truth is the **committed SOPS+age ciphertext
bundle** `secrets/staging.enc.yaml`, decrypted at deploy time by
[`scripts/deploy/render-secrets.sh`](../../scripts/deploy/render-secrets.sh)
using a single GitHub Actions secret: the age **private key** `SOPS_AGE_KEY`.
Full model + bootstrap: [`secrets-runbook.md`](secrets-runbook.md). The
workflow then runs [`render-config.sh`](../../scripts/deploy/render-config.sh)
`--check` to prove the full secret → config contract holds before any rolling
change.

### 7a. Provision the staging age keypair (draftable now)

```bash
# Generate the staging age keypair (private key gitignored; prints the PUBLIC recipient):
scripts/dev/gen-age-key.sh --env staging
#   -> secrets/.age-key.staging   (PRIVATE — gitignored; copy to password manager + GitHub secret)
#   -> recipient age1...          (PUBLIC  — paste into .sops.yaml's staging rule)

# Paste the recipient into .sops.yaml, then re-wrap the bundle for the new key:
SOPS_AGE_KEY_FILE=secrets/.age-key.staging sops --rotate --in-place secrets/staging.enc.yaml

# Distribute the PRIVATE key (human with repo-admin access):
#   - password manager (1Password / pass) — the mandatory escrow copy, AND
#   - GitHub Actions `staging` environment secret `SOPS_AGE_KEY`
#     (repo Settings → Environments → staging → New secret).
```

Staging and prod use **separate** age keys — a staging key compromise must
never read prod (see `.sops.yaml`).

### 7b. Fill the staging secret bundle — BLOCKED-ON-PROVISIONING

Once §1–§6 are done and the VM/managed DB/Redis exist, fill the real values:

```bash
SOPS_AGE_KEY_FILE=secrets/.age-key.staging sops secrets/staging.enc.yaml
# opens decrypted in $EDITOR; set values per the table below; save re-encrypts in place.
```

### 7c. Secret classification — board/human-provided vs agent-generated

Every secret/variable the deploy pipeline consumes. **board/human-provided**
= a value that originates from the Frappe Cloud dashboard, a commercial
relationship, or a board decision (cannot be agent-generated).
**agent-generated** = producible by DevOps without external input.

| Secret / var | Where it lives | Classification | Source |
|--------------|----------------|----------------|--------|
| `SOPS_AGE_KEY` | GitHub Actions `staging` secret | **board/human-distributed** (keypair agent-generated via `gen-age-key.sh`; private key placed in password manager + GitHub secret by a human with repo-admin access) | `scripts/dev/gen-age-key.sh` → human distributes |
| `DB_HOST` | `secrets/staging.enc.yaml` | **board/human-provided** | Frappe Cloud dashboard (managed MariaDB endpoint) |
| `DB_NAME` | bundle | agent-generated | Convention: `flock_os_staging` (set at `bench new-site`) |
| `DB_USER` | bundle | **board/human-provided** | Frappe Cloud managed MariaDB user |
| `DB_PASSWORD` | bundle | **board/human-provided** | Frappe Cloud dashboard (rotate via dashboard; never agent-set on a managed DB) |
| `DB_PORT` *(optional)* | bundle | agent-generated | Default `3306` |
| `REDIS_CACHE_URI` | bundle | **board/human-provided** (host) + agent-generated (DB index) | Frappe Cloud default Redis; pick DB `/1` |
| `REDIS_QUEUE_URI` | bundle | same | DB `/2` |
| `REDIS_SOCKETIO_URI` | bundle | same | DB `/3` |
| `FLOCK_SIO_ADAPTER_REDIS` | bundle | agent-generated | Dedicated adapter Redis stood up in §6 (`redis://127.0.0.1:6389`) |
| `SECRET_KEY` | bundle | agent-generated | `openssl rand -hex 50` (rotate per ADR §secrets) |
| `SITE_URL` | bundle | **board/human-provided** | Depends on the chosen domain (`https://staging.<your-domain>`) |
| `FLOCK_ENV` | bundle | agent-generated | Literal `staging` (also the bundle selector + `_meta.env` self-check) |
| `FLOCK_DEPLOY_CMD` | GitHub Actions `staging` **variable** (not secret) | agent-generated (shape) + **board/human-provided** (`<staging-vm>` hostname) | See §7d |
| `FLOCK_DEPLOY_SSH_KEY` | GitHub Actions `staging` secret | agent-generated (keygen in §2) + **board/human-distributed** (private key placed in the secret by a human with repo-admin access) | `ssh-keygen` in §2 |
| `FLOCK_IMAGE_REGISTRY_TOKEN` | GitHub Actions repo secret | **board/human-provided** | GitHub PAT for ghcr.io (or rely on the `GITHUB_TOKEN` fallback) |
| `FLOCK_IMAGE_REGISTRY` *(optional)* | GitHub Actions repo variable | agent-generated | Default `ghcr.io` |
| `STAGING_URL` | GitHub Actions `staging` secret | **board/human-provided** | Mirrors `SITE_URL`; consumed by the smoke step |
| `STAGING_WS_URL` *(optional)* | GitHub Actions `staging` secret | **board/human-provided** | Defaults to `wss://$STAGING_URL` |

> **Never** put any of these in `.env.example`, a prompt, or a commit message.
> Plaintext lives only in the password manager + the in-memory render at deploy
> time. The committed `secrets/staging.enc.yaml` is ciphertext only.

### 7d. Set `FLOCK_DEPLOY_CMD` (the orchestrator hook)

`FLOCK_DEPLOY_CMD` is a GitHub Actions **environment variable** (not a secret —
its shape is public; only the hostname inside is private). The workflow
substitutes `<TAG>` with the image tag and `eval`s it. For a Frappe Cloud
Server plan VM, the hook is **SSH + docker**:

In the GitHub repo: Settings → Environments → `staging` → **Environment
variables** → add:
```
FLOCK_DEPLOY_CMD=ssh -i /tmp/flock_deploy_key -o StrictHostKeyChecking=no frappe@<staging-vm> \
    "cd /home/frappe && \
     docker pull ghcr.io/<org>/flock-os-bench:<TAG> && \
     docker stop flock-os || true && \
     docker run -d --name flock-os \
         --env-file /etc/flock-os/staging.env \
         -p 8080:8080 -p 9000:9000 \
         ghcr.io/<org>/flock-os-bench:<TAG>"
```
(`<org>` is the GitHub owner; `<staging-vm>` is the Frappe Cloud edge host.)
Replicate the same shape on the `production` environment when
[FLO-251](/FLO/issues/FLO-251) opens, pointing at prod resources.

> **Open wiring note:** `deploy.yml` references `/tmp/flock_deploy_key` inside
> `FLOCK_DEPLOY_CMD` but does not yet write it from `FLOCK_DEPLOY_SSH_KEY`
> before the `eval`. Provisioning must add a step that materializes the key
> (e.g. `echo "$FLOCK_DEPLOY_SSH_KEY" > /tmp/flock_deploy_key && chmod 600`)
> or bake it into the hook. Tracked against the slice-1 pipeline
> ([FLO-246](/FLO/issues/FLO-246)), not this runbook.

### 7e. Pre-flight the secret gate (off-image, no VM needed for §7a)

The age-keypair + `FLOCK_DEPLOY_CMD` shape can be validated before the VM
exists. The full decrypt → render chain can be exercised end-to-end once the
bundle holds real values (§7b). This is the **same gate** `deploy.yml` runs
before any rolling change:

```bash
# Decrypt gate (works as soon as §7a is done, even with placeholder values):
SOPS_AGE_KEY_FILE=secrets/.age-key.staging \
  scripts/deploy/render-secrets.sh --env staging --check
# -> "render-secrets: --check OK (env=staging, bundle=..., all required keys present)"

# Full chain (decrypt -> render-config) once §7b filled the real values:
SOPS_AGE_KEY_FILE=secrets/.age-key.staging \
  scripts/deploy/render-secrets.sh --env staging --out /tmp/flock.env
set -a; . /tmp/flock.env; set +a
scripts/deploy/render-config.sh --check
# -> "render-config: --check OK (all required env present)"
```

## 8. First deploy + verification — BLOCKED-ON-PROVISIONING

Once §1–§7 are in place, the first staging deploy is just a merge to `master`
(the deploy workflow is automatic on push to `master`):

1. Confirm CI (`.github/workflows/ci.yml`) is green on the tip of `master`.
2. Merge (or push) to `master`. Watch the **Deploy** workflow:
   - `lint-and-test` → `build-image` → `deploy-staging` → "Post-deploy smoke".
3. The smoke step (`scripts/deploy/smoke-staging.sh`) runs three probes:
   `[1/3]` HTTP 2xx/3xx on `$STAGING_URL`, `[2/3]` `/api/ping` returns `pong`,
   `[3/3]` a WS handshake succeeds against the scaled-socketio tier. All three
   must pass; a failure blocks prod promotion ([FLO-251](/FLO/issues/FLO-251)).
4. Record the green smoke output (and the staging URL) in the
   [FLO-249](/FLO/issues/FLO-249) thread — that is the Phase 6.1 acceptance
   evidence ("a reachable staging URL over TLS").
5. Hand off to [FLO-250](/FLO/issues/FLO-250) (live staging smoke + rollback
   drill) — the FLO-249 close-out unblocks it.

Manual smoke (sanity check, same probes as CI):
```bash
STAGING_URL=https://staging.<your-domain> scripts/deploy/smoke-staging.sh
```

## 9. Acceptance checklist (FLO-249)

- [ ] Board acceptance of [approval 609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc).
- [ ] Frappe Cloud account + payment method on file (CEO).
- [ ] Staging Server plan VM provisioned (SKU recorded in thread).
- [ ] Deploy SSH key generated, public key on VM, private key in GitHub secret.
- [ ] Staging site registered; DNS `staging.<your-domain>` → Frappe Cloud edge
      (Cloudflare proxied/orange, SSL/TLS **Full (strict)**, WebSockets on).
- [ ] Let's Encrypt cert issued at the edge; `curl -sI https://staging...` is 2xx/3xx.
- [ ] Managed MariaDB reachable; **dedicated adapter Redis** on port 6389 (or
      equivalent) healthy.
- [ ] GitHub `staging` environment wired: `FLOCK_DEPLOY_CMD` var +
      `SOPS_AGE_KEY` secret; `secrets/staging.enc.yaml` filled with real values;
      `render-secrets.sh --env staging --check` + `render-config.sh --check`
      both pass off-image.
- [ ] First `master` push → Deploy workflow green; `smoke-staging.sh` `[1/3] [2/3] [3/3]` all pass.
- [ ] Staging URL posted to the [FLO-249](/FLO/issues/FLO-249) thread; this
      runbook updated with the real hostname/SKU once provisioned.

When every box is checked, FLO-249 → `done`; FLO-250 + FLO-251 unblock
automatically (they are first-class-blocked on this issue).

## 10. Rollback / operations

Once the VM is live, day-2 operations (rollback, scaled-socketio tier health,
nginx sticky-L7 Cloudflare caveat, troubleshooting matrix) live in the
companion runbook: **[`deploy-runbook.md`](deploy-runbook.md)**. This document
is the **one-time bring-up**; that one is the **steady-state operator guide**.

## Out of scope

- Real launch-partner onboarding + 15k-in-prod → Phase 6.3.
- First real event → Phase 6.4.
- Observability / alerting / restore drill → Phase 6.2.
- Prod promotion (same shape, `production` environment, CEO/QA sign-off) →
  [FLO-251](/FLO/issues/FLO-251).
