# Secrets runbook — Flock OS (FLO-248 Phase 6.1 slice 2)

> How Flock OS stores, edits, rotates, and deploys secrets with **SOPS + age**.
> Companion to the deploy runbook (`docs/development/deploy-runbook.md`) and the
> Phase 6.1 epic ([FLO-246](/FLO/issues/FLO-246)). This slice closes the
> "zero secrets in the repo" acceptance criterion: the repo holds ONLY
> ciphertext; plaintext exists only in memory at deploy time.

## TL;DR

```bash
# Install once (local dev / operator machine):
brew install sops age jq

# Edit a secret bundle (opens decrypted in $EDITOR, re-encrypts on save):
SOPS_AGE_KEY_FILE=secrets/.age-key.staging sops secrets/staging.enc.yaml

# Render the bundle to env without exposing values (CI gate):
scripts/deploy/render-secrets.sh --env staging --check

# Render to a dotenv file (deploy / local bring-up):
scripts/deploy/render-secrets.sh --env staging --out /tmp/flock.env
set -a; . /tmp/flock.env; set +a
```

## The model

```
                     age PRIVATE key (the ONE true secret)
                     ┌─────────────────────────────────────────────┐
                     │  • human: 1Password / pass / password mgr   │
                     │  • CI:    SOPS_AGE_KEY env secret per env   │
                     │  • NEVER in the repo, NEVER in a prompt     │
                     └──────────────────────┬──────────────────────┘
                                            │ decrypt
   repo (committed ciphertext only)         ▼
   ┌──────────────────────┐    sops -d    ┌──────────────────────────┐
   │ secrets/staging.enc  │ ────────────► │ plaintext env (in memory)│ ──► render-config.sh
   │ secrets/prod.enc     │               │ only for the deploy run  │     renders site_config.json
   │ .sops.yaml (recipient)│              └──────────────────────────┘
   └──────────────────────┘
```

- **SOPS** (Secrets OPerationS) encrypts each value in a YAML file with AES256-GCM; the data key itself is wrapped by **age**. The result (`secrets/<env>.enc.yaml`) is safe to commit — that's the whole point.
- **age** is the key-wrapping KMS. The **public recipient** (an `age1...` string) lives in `.sops.yaml` (committed). The matching **private key** is the only thing an attacker would need, and it never enters the repo.
- **`render-secrets.sh`** decrypts the bundle for a given env and emits env vars (dotenv file / `export` lines) that `scripts/deploy/render-config.sh` (slice 1) consumes to render `site_config.json` / `common_site_config.json`.

## The files

| Path | Tracked? | Purpose |
| --- | --- | --- |
| `.sops.yaml` | yes | Creation rules — maps `secrets/<env>.enc.yaml` to its age recipient. |
| `secrets/staging.enc.yaml` | yes | Staging secret bundle (SOPS ciphertext). |
| `secrets/prod.enc.yaml` | yes | Production secret bundle (SOPS ciphertext). |
| `secrets/.age-key*` | **no** (gitignored) | age private keys for local dev. |
| `scripts/deploy/render-secrets.sh` | yes | Decrypt → env renderer (the deploy-time gate). |
| `scripts/dev/gen-age-key.sh` | yes | Operator bootstrap: generate/rotate an age keypair. |
| `docs/development/secrets-runbook.md` | yes | This document. |

The committed bundles currently hold **placeholder values** (`CHANGE_ME_*`). Onboarding (below) replaces them with real secrets and rotates to a production key.

## Bootstrap + rotate the age key

> Run this once on first onboarding, and again on every key rotation.

Staging and prod use **separate** age keys — a staging key compromise must never read prod.

```bash
# 1. Generate a fresh keypair for the environment (writes the PRIVATE key to a
#    gitignored path and prints the PUBLIC recipient):
scripts/dev/gen-age-key.sh --env staging
#   -> private key : secrets/.age-key.staging   (gitignored)
#   -> recipient   : age1...                    (PUBLIC)

# 2. Store the private key out-of-band:
#    - your password manager (1Password / pass), AND
#    - the SOPS_AGE_KEY GitHub Actions secret on the `staging` environment
#      (repo Settings → Environments → staging → New secret).

# 3. Paste the recipient into .sops.yaml's staging path_regex rule.

# 4. Re-encrypt the bundle against the new key (sops --rotate re-wraps the data
#    key for the new recipient without touching the values):
SOPS_AGE_KEY_FILE=secrets/.age-key.staging sops --rotate --in-place secrets/staging.enc.yaml

# 5. Fill real secret values (opens decrypted in $EDITOR, re-encrypts on save):
SOPS_AGE_KEY_FILE=secrets/.age-key.staging sops secrets/staging.enc.yaml

# Repeat for prod with `--env prod` and a SEPARATE key.
```

Verify the rotation round-tripped before committing:

```bash
SOPS_AGE_KEY_FILE=secrets/.age-key.staging scripts/deploy/render-secrets.sh --env staging --check
```

## Edit a secret

```bash
# Decrypts in memory, opens $EDITOR, re-encrypts in place on save. Nothing
# plaintext is written to disk. Add/rotate a value, save, quit.
SOPS_AGE_KEY_FILE=secrets/.age-key.staging sops secrets/staging.enc.yaml

# Add a new optional key (e.g. a backup target) — it flows through to env
# automatically because render-secrets.sh exports every non-_meta key.
```

Commit the re-encrypted `secrets/staging.enc.yaml`. The diff will be ciphertext-only (new AES nonce/tag on every changed value) — review the change via `sops -d` locally, not by reading the ciphertext.

## Rotate a single secret value

Same as editing — open with `sops`, change the value, save. For a **credential rotation** (e.g. DB password changed on the DB side), coordinate the order:

1. Create the new credential on the resource (DB/Redis).
2. `sops secrets/<env>.enc.yaml` → update the value → save → commit.
3. Merge → the deploy pipeline re-renders + restarts the bench with the new value.

Never rotate the value and the resource out of order; a 1-commit lead time is fine because the bench reads the rendered config at container start.

## Required keys (the render-config contract)

Every bundle MUST carry these (validated by `render-secrets.sh --check`, mirrored from `scripts/deploy/render-config.sh`):

`DB_HOST`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `REDIS_CACHE_URI`, `REDIS_QUEUE_URI`, `REDIS_SOCKETIO_URI`, `FLOCK_SIO_ADAPTER_REDIS`, `SECRET_KEY`, `SITE_URL`, `FLOCK_ENV`

Optional keys: `DB_PORT`, `FLOCK_SIO_PROCESSES`, `MUTE_EMAILS`, `DROPBOX_*`, `GDRIVE_*`.

`FLOCK_ENV` is both the **selector** (which bundle to load) and a **self-check** (`_meta.env` inside the bundle must match the requested env — a staging bundle on a prod render aborts loudly).

## How CI decrypts

The Deploy workflow (`.github/workflows/deploy.yml`) installs `sops` + `age` on the runner, then for each environment:

1. Reads `SOPS_AGE_KEY` (the environment secret set in step 2 of Bootstrap).
2. Runs `scripts/deploy/render-secrets.sh --env <env> --check` — the decrypt gate. If the key is missing or a required key is absent, the deploy **fails loud** before any rolling change.
3. Renders a dotenv to `$RUNNER_TEMP/flock.env` and re-runs `scripts/deploy/render-config.sh --check` against it, proving the full secret → config contract holds.
4. Exposes `FLOCK_ENV_FILE` so the deploy command (`FLOCK_DEPLOY_CMD`) can ship the rendered env to the host (e.g. `docker run --env-file`).

Until `SOPS_AGE_KEY` is onboarded on an environment, that environment's deploy fails at the decrypt gate — this is intentional and matches the rest of the pipeline's fail-loud posture.

## Deliver secrets to the running bench

The image contains zero secrets; `deploy/entrypoint.sh` calls `render-config.sh` which reads env at container start. Pick one delivery path per orchestrator:

```bash
# Frappe Cloud Server (SSH + docker): render on the host from a checked-out bundle.
ssh frappe@<host '
  cd /home/frappe/flock-os &&
  sudo SOPS_AGE_KEY_FILE=/etc/flock-os/age.key \
    scripts/deploy/render-secrets.sh --env prod --out /etc/flock-os/prod.env &&
  docker run -d --name flock-os --env-file /etc/flock-os/prod.env \
    ghcr.io/<org>/flock-os-bench:<TAG>'

# docker compose (self-hosted): render in the deploy step, pass --env-file.
scripts/deploy/render-secrets.sh --env prod --out ./prod.env
docker compose --env-file ./prod.env up -d --no-deps --force-recreate bench
```

Never bake the rendered `.env` into the image or commit it (`.gitignore` drops `secrets/*.env`, `*.rendered.env`).

## Rotate the age key (compromise / scheduled)

1. Generate a new keypair: `scripts/dev/gen-age-key.sh --env staging`.
2. Update `.sops.yaml` to the new recipient.
3. `sops --rotate --in-place secrets/staging.enc.yaml` (re-wraps for the new key).
4. Distribute the new private key (password manager + CI secret); revoke the old.
5. Verify: `scripts/deploy/render-secrets.sh --env staging --check`.
6. Commit. The old ciphertext becomes un-decryptable by the revoked key — which is the goal.

## Emergency: lost private key

If the private key for an environment is lost and no copy exists in the password manager, the bundle is unrecoverable (by design — age has no escrow). Recover by:

1. Generate a new keypair (`scripts/dev/gen-age-key.sh --env <env>`).
2. Update `.sops.yaml` to the new recipient.
3. **Recreate** `secrets/<env>.enc.yaml` from the last known plaintext (password manager / resource console), re-encrypting fresh:
   ```bash
   # write the new plaintext values, then encrypt in place:
   $EDITOR secrets/staging.enc.yaml   # plaintext values
   sops --encrypt --in-place secrets/staging.enc.yaml
   ```
4. Distribute the new key, verify, commit.

This is why the password manager copy is mandatory — it is your escrow.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `render-secrets: sops decrypt failed` | `SOPS_AGE_KEY`/`SOPS_AGE_KEY_FILE` doesn't match the recipient in `.sops.yaml`. Regenerate or re-rotate. |
| `render-secrets: env mismatch` | The bundle's `_meta.env` disagrees with `--env`. You pointed the wrong bundle at the render; rename or fix `_meta.env`. |
| `render-secrets: bundle is missing required keys` | Open the bundle with `sops` and add the listed key(s); save. |
| CI: `no age key` / decrypt gate fails | `SOPS_AGE_KEY` not set on the GitHub environment. See Bootstrap step 2. |
| `error loading config: no matching creation rules` | The file path doesn't match `.sops.yaml`'s `path_regex`. Bundles live at `secrets/<env>.enc.yaml`. |

## Out of scope

- Per-secret audit logging / HMAC alerting → Phase 6.2 observability.
- Cloud KMS backing (AWS KMS / GCP KMS) instead of age → revisit if the board moves off the Frappe Cloud Server plan.
