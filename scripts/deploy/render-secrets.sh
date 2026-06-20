#!/usr/bin/env bash
#
# Render Flock OS deploy secrets from the SOPS+age bundle (FLO-248 Phase 6.1 slice 2).
#
# This is the secret-manager half of the zero-secrets-in-repo gate. The repo
# holds ONLY ciphertext (secrets/<env>.enc.yaml). This script decrypts the
# bundle for the requested environment using the age PRIVATE key and turns it
# into the env vars that scripts/deploy/render-config.sh (slice 1) consumes to
# render site_config.json / common_site_config.json. The age private key is
# NEVER in the repo — it comes from:
#   - CI:    the SOPS_AGE_KEY GitHub Actions secret (per-environment).
#   - human: SOPS_AGE_KEY_FILE (default secrets/.age-key, gitignored) for local
#            dev, or SOPS_AGE_KEY in your password manager.
#
# Required tools: sops, age, jq (already a render-config.sh dep).
#
# Usage:
#   scripts/deploy/render-secrets.sh --env <staging|prod> --check
#   scripts/deploy/render-secrets.sh --env staging  --out /tmp/flock.env
#   scripts/deploy/render-secrets.sh --env prod     --eval      # sourceable: eval "$(... --eval)"
#   scripts/deploy/render-secrets.sh --env staging  --print-env # redacted
#   scripts/deploy/render-secrets.sh --help
#
# --check:     decrypt + validate every required key is present WITHOUT emitting
#              any secret. Use in CI / pre-deploy gates (mirrors render-config.sh --check).
# --out FILE:  write a dotenv (KEY=value) file, chmod 0600, for `set -a; . FILE; set +a`.
# --eval:      emit `export KEY='value';` lines to stdout for `eval "$(...)"`.
# --print-env: print which keys are set, REDACTED (never raw values).
#
# FLOCK_ENV (env var) is used when --env is omitted. The bundle's _meta.env must
# match the requested env — a staging bundle on a prod render aborts loudly.
#
# Runbook: docs/development/secrets-runbook.md.
set -euo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SECRETS_DIR="$REPO_ROOT/secrets"

# Must stay in sync with scripts/deploy/render-config.sh REQUIRED_VARS. A drift
# here is a deploy-blocker (a present-but-unvalidated secret silently weakens
# the gate), so both lists are deliberately duplicated and cross-referenced.
REQUIRED_VARS=(
    DB_HOST DB_NAME DB_USER DB_PASSWORD
    REDIS_CACHE_URI REDIS_QUEUE_URI REDIS_SOCKETIO_URI
    FLOCK_SIO_ADAPTER_REDIS SECRET_KEY SITE_URL FLOCK_ENV
)

ENV_NAME="${FLOCK_ENV:-}"
MODE=""
OUT_FILE=""

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env) ENV_NAME="$2"; shift 2 ;;
        --env=*) ENV_NAME="${1#--env=}"; shift ;;
        --check) MODE="check"; shift ;;
        --eval) MODE="eval"; shift ;;
        --print-env) MODE="print"; shift ;;
        --out) OUT_FILE="$2"; MODE="out"; shift 2 ;;
        --out=*) OUT_FILE="${1#--out=}"; MODE="out"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "$PROG: unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$MODE" ]]; then
    echo "$PROG: no mode given — pass one of --check, --out FILE, --eval, --print-env" >&2
    usage
    exit 2
fi

if [[ -z "$ENV_NAME" ]]; then
    echo "$PROG: --env <staging|prod> is required (or set FLOCK_ENV)" >&2
    exit 2
fi

case "$ENV_NAME" in
    staging|prod) ;;
    *) echo "$PROG: env must be 'staging' or 'prod' (got '$ENV_NAME')" >&2; exit 2 ;;
esac

BUNDLE="$SECRETS_DIR/${ENV_NAME}.enc.yaml"
if [[ ! -f "$BUNDLE" ]]; then
    echo "$PROG: bundle not found: $BUNDLE" >&2
    echo "$PROG: create it with sops — see docs/development/secrets-runbook.md." >&2
    exit 1
fi

command -v sops >/dev/null 2>&1 || { echo "$PROG: sops not found (brew install sops)." >&2; exit 1; }
command -v jq   >/dev/null 2>&1 || { echo "$PROG: jq not found (brew install jq)." >&2; exit 1; }

# The age PRIVATE key must be supplied out-of-band. SOPS reads either the
# SOPS_AGE_KEY env (the key body) or SOPS_AGE_KEY_FILE (a path to it). For local
# dev default to the gitignored secrets/.age-key produced by gen-age-key.sh.
if [[ -z "${SOPS_AGE_KEY:-}" && -z "${SOPS_AGE_KEY_FILE:-}" ]]; then
    if [[ -f "$SECRETS_DIR/.age-key" ]]; then
        export SOPS_AGE_KEY_FILE="$SECRETS_DIR/.age-key"
    else
        echo "$PROG: no age key — set SOPS_AGE_KEY or SOPS_AGE_KEY_FILE" >&2
        echo "$PROG: (CI uses the SOPS_AGE_KEY secret; local dev uses secrets/.age-key" >&2
        echo "$PROG:  from scripts/dev/gen-age-key.sh). See docs/development/secrets-runbook.md." >&2
        exit 1
    fi
fi

# Decrypt once into memory. --output-type json gives jq something to chew on.
# The plaintext lives only in this var + the pipes below — never logged.
if ! SECRETS_JSON="$(sops --decrypt --output-type json "$BUNDLE" 2>/dev/null)"; then
    echo "$PROG: sops decrypt failed for $BUNDLE" >&2
    echo "$PROG: check the age key matches the recipient in .sops.yaml." >&2
    exit 1
fi

# Self-check: the bundle's declared env must match the requested env. Prevents
# a staging bundle accidentally shipping to a prod render (or vice versa).
BUNDLE_ENV="$(printf '%s' "$SECRETS_JSON" | jq -r '._meta.env // empty')"
if [[ "$BUNDLE_ENV" != "$ENV_NAME" ]]; then
    echo "$PROG: env mismatch — requested '$ENV_NAME' but bundle _meta.env is '${BUNDLE_ENV:-<missing>}'" >&2
    echo "$PROG: this guard stops staging secrets landing on a prod deploy. Aborting." >&2
    exit 1
fi

# Validate required keys are present + non-empty. Drift-proof against the
# render-config.sh contract: if a key is missing here, render-config.sh would
# fail downstream anyway — fail earlier, with a clearer message.
missing=()
for v in "${REQUIRED_VARS[@]}"; do
    val="$(printf '%s' "$SECRETS_JSON" | jq -r --arg k "$v" '.[$k] // empty')"
    if [[ -z "$val" ]]; then missing+=("$v"); fi
done
if (( ${#missing[@]} > 0 )); then
    echo "$PROG: bundle '$BUNDLE' is missing required keys: ${missing[*]}" >&2
    echo "$PROG: edit the bundle: SOPS_AGE_KEY_FILE=secrets/.age-key sops $BUNDLE" >&2
    exit 1
fi

emit_check() {
    echo "$PROG: --check OK (env=$ENV_NAME, bundle=$BUNDLE, all required keys present):"
    for v in "${REQUIRED_VARS[@]}"; do echo "  $v=<set>"; done
}

emit_print() {
    echo "# render-secrets resolved bundle (env=$ENV_NAME, redacted):"
    for v in "${REQUIRED_VARS[@]}"; do
        val="$(printf '%s' "$SECRETS_JSON" | jq -r --arg k "$v" '.[$k] // empty')"
        if [[ -z "$val" ]]; then printf '%s=(unset)\n' "$v"; else printf '%s=<set,%d chars>\n' "$v" "${#val}"; fi
    done
}

case "$MODE" in
    check)
        emit_check
        ;;
    print)
        emit_print
        ;;
    eval)
        # Sourceable form: `eval "$(scripts/deploy/render-secrets.sh --env staging --eval)"`
        # Skips _meta. jq -Rs wraps the raw string in a JSON string literal,
        # which is also valid as a bash single-quoted value for these secret
        # shapes (no expansion needed). Export every secret key.
        for k in $(printf '%s' "$SECRETS_JSON" | jq -r 'keys[] | select(. != "_meta")'); do
            val="$(printf '%s' "$SECRETS_JSON" | jq -r --arg k "$k" '.[$k] // empty')"
            [[ -z "$val" ]] && continue
            printf "export %s=%s;\n" "$k" "$(printf '%s' "$val" | jq -Rs .)"
        done
        ;;
    out)
        # Dotenv form: KEY=value, chmod 0600. Consume with: set -a; . "$OUT_FILE"; set +a
        umask 077
        : > "$OUT_FILE"
        for k in $(printf '%s' "$SECRETS_JSON" | jq -r 'keys[] | select(. != "_meta")'); do
            val="$(printf '%s' "$SECRETS_JSON" | jq -r --arg k "$k" '.[$k] // empty')"
            [[ -z "$val" ]] && continue
            printf "%s=%s\n" "$k" "$(printf '%s' "$val" | jq -Rs .)" >> "$OUT_FILE"
        done
        chmod 0600 "$OUT_FILE"
        echo "$PROG: wrote $OUT_FILE ($(wc -l < "$OUT_FILE" | tr -d ' ') keys, chmod 0600) for env=$ENV_NAME"
        ;;
esac

echo "$PROG: secrets for '$ENV_NAME' decrypted from bundle — none were read from the repo as plaintext." >&2
