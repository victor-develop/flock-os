# Secrets Directory (FLO-248)

This directory contains SOPS-encrypted secrets for Flock OS deployment.

## Files

- `staging.enc.yaml` - Encrypted secrets for staging environment
- `prods.enc.yaml` - Encrypted secrets for production environment
- `staging.yaml` - Plain YAML staging secrets (DO NOT COMMIT - .gitignore'd)
- `prod.yaml` - Plain YAML production secrets (DO NOT COMMIT - .gitignore'd)

## Security Rules

1. **NEVER commit plain YAML files** (`staging.yaml`, `prod.yaml`) - they are .gitignore'd
2. **ONLY commit .enc.yaml files** - these are encrypted with SOPS
3. **Keep age private keys secure** - store them in GitHub Actions secrets, never in the repo
4. **Rotate keys regularly** - if a key is compromised, re-encrypt all secrets with a new key

## Working with Secrets

### Editing secrets

1. Decrypt the file:
   ```bash
   sops --decrypt secrets/staging.enc.yaml > secrets/staging.yaml
   ```

2. Edit the plain file with your values

3. Re-encrypt:
   ```bash
   sops --encrypt secrets/staging.yaml > secrets/staging.enc.yaml
   rm secrets/staging.yaml
   ```

### Using secrets in deployment

The deploy pipeline automatically decrypts secrets using the `AGE_PRIVATE_KEY` stored in GitHub Actions environments. The `scripts/deploy/render-secrets.sh` script handles decryption and environment variable export.

For local testing:
```bash
eval "$(scripts/deploy/render-secrets.sh --env staging --export)"
```

### Required secret values

Each environment requires the following secrets (see the plain YAML files for the current structure):

- Database credentials (DB_HOST, DB_NAME, DB_USER, DB_PASSWORD)
- Redis URIs (cache, queue, socketio, adapter)
- Frappe SECRET_KEY
- Site configuration (SITE_URL, FLOCK_ENV)
- Socketio scaling settings (FLOCK_SIO_PROCESSES)
- Optional: Cloud storage keys (Dropbox, Google Drive)

## Reference

- SOPS documentation: https://github.com/getsops/sops
- age documentation: https://github.com/FiloSottile/age
- Deploy runbook: `docs/development/deploy-runbook.md`