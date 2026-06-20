#!/usr/bin/env bash
#
# Generate a fresh age keypair for Flock OS secrets (FLO-248 Phase 6.1 slice 2).
#
# SOPS+age encrypts the committed ciphertext bundles (secrets/*.enc.yaml) for an
# age RECIPIENT (public key). The matching PRIVATE key decrypts them. The private
# key is the one true secret of the whole system — generate it once, store it in
# your password manager + the CI SOPS_AGE_KEY secret, and NEVER commit it.
#
# This script is the operator bootstrap. Run it:
#   - on first onboarding, to create the repo's age keypair, OR
#   - during a key rotation (see docs/development/secrets-runbook.md -> Rotate).
#
# Usage:
#   scripts/dev/gen-age-key.sh                         # staging key -> secrets/.age-key.staging
#   scripts/dev/gen-age-key.sh --env prod              # prod key    -> secrets/.age-key.prod
#   scripts/dev/gen-age-key.sh --out ~/keys/flock.key  # custom path
#   scripts/dev/gen-age-key.sh --print-recipient       # only print the public recipient
#
# After generating, copy the printed `age1...` recipient into .sops.yaml for the
# matching path_regex, then re-encrypt the bundles so they target the new key:
#   SOPS_AGE_KEY_FILE=secrets/.age-key.staging sops --rotate --in-place secrets/staging.enc.yaml
#
# Runbook: docs/development/secrets-runbook.md -> "Bootstrap + rotate the age key".
set -euo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

ENV_NAME="staging"
OUT_FILE=""
PRINT_ONLY=0

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env) ENV_NAME="$2"; shift 2 ;;
        --env=*) ENV_NAME="${1#--env=}"; shift ;;
        --out) OUT_FILE="$2"; shift 2 ;;
        --out=*) OUT_FILE="${1#--out=}"; shift ;;
        --print-recipient) PRINT_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "$PROG: unknown arg: $1" >&2; exit 2 ;;
    esac
done

case "$ENV_NAME" in
    staging|prod) ;;
    *) echo "$PROG: --env must be 'staging' or 'prod' (got '$ENV_NAME')" >&2; exit 2 ;;
esac

command -v age-keygen >/dev/null 2>&1 || { echo "$PROG: age-keygen not found (brew install age)." >&2; exit 1; }

if [[ -z "$OUT_FILE" ]]; then
    OUT_FILE="$REPO_ROOT/secrets/.age-key.$ENV_NAME"
fi

# The PRIVATE key. Default path is gitignored (secrets/.age-key*). Refuse to write
# anywhere inside the repo except the gitignored secrets/ key paths — a private key
# anywhere else in the tree is a leak waiting to happen.
case "$OUT_FILE" in
    "$REPO_ROOT/secrets/"*.age-key*|"$REPO_ROOT/secrets/.age-key"*) : ;;  # allowed (gitignored)
    /*|"~"/*) : ;;  # absolute / home path outside the repo is the operator's choice
    *) echo "$PROG: refusing to write private key to a repo-relative path ('$OUT_FILE')." >&2
       echo "$PROG: use an absolute path or the default secrets/.age-key.$ENV_NAME." >&2
       exit 2 ;;
esac

if [[ "$PRINT_ONLY" -eq 1 ]]; then
    # Read the recipient from an existing key file; do not generate.
    [[ -f "$OUT_FILE" ]] || { echo "$PROG: no key at $OUT_FILE — drop --print-recipient to generate." >&2; exit 1; }
    age-keygen -y "$OUT_FILE"
    exit 0
fi

if [[ -f "$OUT_FILE" ]]; then
    echo "$PROG: key already exists at $OUT_FILE — refusing to overwrite." >&2
    echo "$PROG: to rotate, move the old key aside first (see secrets-runbook.md -> Rotate)." >&2
    exit 1
fi

umask 077
age-keygen -o "$OUT_FILE" >/dev/null 2>&1
RECIPIENT="$(age-keygen -y "$OUT_FILE")"

cat >&2 <<EOF
$PROG: generated age keypair for env '$ENV_NAME'

  private key : $OUT_FILE   (NEVER commit — secrets/.age-key* is gitignored)
  recipient   : $RECIPIENT  (PUBLIC — paste into .sops.yaml for the $ENV_NAME rule)

Next steps:
  1. Store the private key in your password manager (1Password / pass).
  2. Set it as the SOPS_AGE_KEY GitHub Actions secret on the $ENV_NAME environment
     (repo Settings -> Environments -> $ENV_NAME -> secrets). The CI decrypt step
     (render-secrets.sh) fails loud until this is set.
  3. Paste the recipient above into .sops.yaml's $ENV_NAME path_regex rule.
  4. Re-encrypt the bundle against the new key:
        SOPS_AGE_KEY_FILE=$OUT_FILE sops --rotate --in-place secrets/$ENV_NAME.enc.yaml
  5. Fill real secret values:
        SOPS_AGE_KEY_FILE=$OUT_FILE sops secrets/$ENV_NAME.enc.yaml
See docs/development/secrets-runbook.md for the full bootstrap + rotation flow.
EOF
