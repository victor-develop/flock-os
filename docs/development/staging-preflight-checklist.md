# Staging pre-flight checklist — Flock OS (FLO-274 / FLO-246 Phase 6.1)

> **The step-through the CEO / board / DevOps run once the staging VM is live to
> prove the Phase 6.1 pipeline's end-to-end contract holds before any prod
> promotion talk.** Each step is one copy-paste command with its expected
> output — no judgement calls, no "it should kind of work". If every box ticks
> green, the acceptance criterion "a reachable staging URL over TLS" of
> [FLO-246](/FLO/issues/FLO-246) / [FLO-249](/FLO/issues/FLO-249) is provable.
>
> This checklist is **no-spend, agent-actionable to draft** — it is pure
> documentation. Execution needs the VM, which is blocked on board endorsement
> ([approval 609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc)) +
> a Frappe Cloud account (see
> [`provision-staging-vm.md` → Prerequisites/blockers](provision-staging-vm.md#prerequisites--blockers)).
>
> Companions: this checklist is the **execute** layer over the
> [provisioning runbook](provision-staging-vm.md) (bring-up), the
> [deploy pipeline](deploy-pipeline.md) (mechanics), and the
> [deploy runbook](deploy-runbook.md) (operations).

## Before you start

- [ ] Board acceptance of [approval 609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc)
      is recorded.
- [ ] Frappe Cloud account exists with a payment method on file (CEO).
- [ ] [`provision-staging-vm.md` → Acceptance checklist §9](provision-staging-vm.md#9-acceptance-checklist-flo-249)
      is fully green (VM provisioned, SSH key on VM, site registered, DNS/TLS,
      managed DB, dedicated adapter Redis, GitHub `staging` environment wired).

Set your staging URL once; every step below reads it:

```bash
export STAGING_URL=https://staging.<your-domain>     # the DNS name wired in provision §4
export SOPS_AGE_KEY_FILE=secrets/.age-key.staging    # the staging age key (gitignored)
```

> Run all commands from the repo root on the DevOps workstation (the same place
> the GitHub Action's secrets were created). Replace `<your-domain>` with the
> actual staging hostname.

## 1. Site up (DNS + TLS)

Proves DNS routes through Cloudflare (orange-cloud), the Frappe Cloud edge
terminated a Let's Encrypt cert, and nginx + gunicorn answered.

```bash
# 1a. DNS resolves to the Cloudflare edge (not the raw VM IP):
dig +short "${STAGING_URL#https://}"           # → one or more Cloudflare IPs (104.x / 172.x)
```

- [ ] `dig` returns Cloudflare edge IPs (`104.x` / `172.x`), not the raw VM IP.

```bash
# 1b. HTTP reachability — Frappe root may 302 to /app or /login; 2xx/3xx is green:
curl -sIL --max-time 15 "$STAGING_URL/" | grep -iE '^HTTP' | tail -1
```

- [ ] `HTTP/… 2xx` or `3xx` (a `302` to `/login` is healthy). `4xx`/`5xx`/`000`
      means nginx/gunicorn is misconfigured — see
      [deploy-runbook.md → Troubleshooting](deploy-runbook.md#troubleshooting).

```bash
# 1c. TLS — issuer is Let's Encrypt, dates span today:
echo | openssl s_client -connect "${STAGING_URL#https://}:443" \
    -servername "${STAGING_URL#https://}" 2>/dev/null \
  | openssl x509 -noout -issuer -dates
```

- [ ] `issuer=` mentions **Let's Encrypt**, and `notBefore` ≤ today ≤ `notAfter`.

## 2. WebSocket endpoint reachable

Proves Cloudflare WebSockets is on, the FLO-121 scaled-socketio tier is up, and
the `@socket.io/redis-adapter` is armed (a WS handshake completes end-to-end).

```bash
# 2a. The /socket.io path answers (engine.io polling handshake, expect a JSON sid blob):
curl -s --max-time 15 "$STAGING_URL/socket.io/?EIO=4&transport=polling" | head -c 80; echo
```

- [ ] Response contains a `sid` JSON fragment (e.g. `{"sid":"…","upgrades":["websocket"],…}`).
      A `4xx`/`5xx` or HTML error means the socketio upstream is down or nginx
      isn't proxying `/socket.io`.

```bash
# 2b. (Confirm Cloudflare WebSockets is enabled.)
#     Dashboard: Cloudflare → your domain → Network → WebSockets = ON.
```

- [ ] WebSockets toggle is **ON** in the Cloudflare dashboard (default on all
      plans). If off, every WS upgrade fails at the edge.

## 3. Secrets rendered (off-image decrypt + config gate)

Proves the committed SOPS+age ciphertext bundle decrypts cleanly with the
staging key and the full secret→config template contract holds — the **same
gate** `deploy.yml` runs before any rolling change. Runs entirely off-image, no
VM needed beyond the key + bundle.

```bash
# 3a. Decrypt gate — every required key present in secrets/staging.enc.yaml:
scripts/deploy/render-secrets.sh --env staging --check
```

- [ ] Prints `render-secrets: --check OK (env=staging, …)`. If it fails with
      `sops decrypt failed`, the age key doesn't match `.sops.yaml`'s recipient
      — re-rotate (see [secrets-runbook.md](secrets-runbook.md)). If it fails
      with `missing required keys`, open the bundle with `sops` and add them.

```bash
# 3b. Config-contract gate — every template placeholder has a value:
scripts/deploy/render-secrets.sh --env staging --out /tmp/flock.env
set -a; . /tmp/flock.env; set +a
scripts/deploy/render-config.sh --check
rm -f /tmp/flock.env   # plaintext — clean up immediately
```

- [ ] Prints `render-config: --check OK (all required env present)`. A failure
      names the missing var (redacted); add it to the bundle and re-run.

> If both §3a and §3b pass off-image, the in-pipeline decrypt step will pass
> too (it runs the identical commands with `SOPS_AGE_KEY` from the GitHub
> secret).

## 4. Smoke script green

The four-probe gate the deploy workflow runs post-deploy, by hand. This is the
single command that closes Phase 6.1's "reachable staging URL" acceptance.

```bash
scripts/deploy/smoke-staging.sh
```

- [ ] **`[1/4] OK`** — HTTP reachability + TLS (2xx/3xx on the site root).
- [ ] **`[2/4] OK`** — `/api/method/ping` returns `pong` (gunicorn + bench boot +
      site config rendered).
- [ ] **`[3/4] OK`** — WS handshake completed (scaled-socketio tier up + adapter
      armed).
- [ ] **`[4/4] OK`** — engagement assets serve 200 (proves `bench build --app
      flock_os` ran + the web worker restarted; FLO-617).
- [ ] Final line reads **`SMOKE: PASS`** (exit 0). A `SMOKE: FAIL` blocks prod
      promotion — do **not** proceed; triage via
      [deploy-runbook.md → Troubleshooting](deploy-runbook.md#troubleshooting).

## 5. (Optional) End-to-end deploy parity check

If you want to prove the *pipeline* (not just the live site) is wired end-to-end
before relying on the auto-deploy, run one manual deploy and watch it go green:

1. Confirm CI (`.github/workflows/ci.yml`) is green on the tip of `master`.
2. Trigger the **Deploy** workflow from the Actions tab (or push a trivial
   change to `master`) and watch the jobs:
   `lint-and-test` → `build-image` → `deploy-staging` → "Post-deploy smoke".
3. Confirm the smoke step shows `[1/4] [2/4] [3/4] [4/4]` all `OK`.

- [ ] A manual/automatic `master` deploy completes with all four jobs green and
      the post-deploy smoke `PASS`.

> How the workflow targets the VM and renders secrets is documented in
> [`deploy-pipeline.md`](deploy-pipeline.md).

## Sign-off

When every box above is checked:

- [ ] Post the staging URL + the green smoke output to the
      [FLO-249](/FLO/issues/FLO-249) thread — that is the Phase 6.1 acceptance
      evidence ("a reachable staging URL over TLS").
- [ ] Update [`provision-staging-vm.md`](provision-staging-vm.md) with the real
      hostname/SKU once provisioned.
- [ ] [FLO-249](/FLO/issues/FLO-249) → `done`; this auto-unblocks
      [FLO-250](/FLO/issues/FLO-250) (live staging smoke + rollback drill) and
      [FLO-251](/FLO/issues/FLO-251) (prod promotion), which are first-class
      blocked on it.

## Out of scope

- Actual VM provisioning / cloud spend → [`provision-staging-vm.md`](provision-staging-vm.md).
- Rollback drill + supervisor triage → [FLO-250](/FLO/issues/FLO-250) /
  [`deploy-runbook.md`](deploy-runbook.md).
- Prod promotion smoke → [FLO-251](/FLO/issues/FLO-251).
- Observability / alerting / restore drill → Phase 6.2.
