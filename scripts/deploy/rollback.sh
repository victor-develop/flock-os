#!/usr/bin/env bash
#
# Flock OS rollback (FLO-246 Phase 6.1, acceptance gate).
#
# Rolls the deployed bench back to the previous known-good image/tag. The
# Phase 6.1 acceptance criterion is "rollback proven (deploy, break, roll
# back, verify)" — this script is the mechanism; the runbook records the drill.
#
# Strategy: the deploy orchestrator (Frappe Cloud Server plan, docker compose,
# or k8s) tags each deploy with a monotonically-numbered image tag. The current
# tag is recorded in a deploy-managed state file / env var; the prior good tag
# is the rollback target. This script:
#   1. Resolves the current + previous tags ($FLOCK_CURRENT_TAG / $FLOCK_PREVIOUS_TAG).
#   2. Re-deploys the previous tag (delegates to the orchestrator's deploy
#      command — $FLOCK_DEPLOY_CMD, or `docker compose` / `kubectl` / the
#      Frappe Cloud SSH deploy by default).
#   3. Runs scripts/deploy/smoke-staging.sh against $STAGING_URL to verify the
#      rollback is healthy.
#
# This script is orchestrator-agnostic by design — the Frappe Cloud Server plan
# is the ADR target, but the same rollback works on the self-hosted fallback.
# Wire $FLOCK_DEPLOY_CMD to your orchestrator's "deploy tag X" command.
#
# Usage:
#   scripts/deploy/rollback.sh                      # rollback to FLOCK_PREVIOUS_TAG
#   scripts/deploy/rollback.sh --to <tag>           # rollback to an explicit tag
#   scripts/deploy/rollback.sh --skip-smoke         # rollback without the post-smoke
#   scripts/deploy/rollback.sh --help
#
# Env:
#   FLOCK_CURRENT_TAG       currently-deployed image tag (required unless --to)
#   FLOCK_PREVIOUS_TAG      rollback target tag (required unless --to)
#   FLOCK_DEPLOY_CMD        command that deploys a given tag (default: prints the
#                          docker compose / kubectl / SSH pattern — see runbook)
#   STAGING_URL             post-rollback smoke target (required unless --skip-smoke)
#   STAGING_WS_URL          optional WS smoke URL override
#
# Runbook: docs/development/deploy-runbook.md -> "Rollback".
set -uo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

TO_TAG=""
SKIP_SMOKE=0

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --to) TO_TAG="$2"; shift 2 ;;
        --to=*) TO_TAG="${1#--to=}"; shift ;;
        --skip-smoke) SKIP_SMOKE=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "$PROG: unknown arg: $1" >&2; exit 2 ;;
    esac
done

# Resolve the rollback target: explicit --to wins, else FLOCK_PREVIOUS_TAG.
if [[ -z "$TO_TAG" ]]; then
    TO_TAG="${FLOCK_PREVIOUS_TAG:-}"
fi
if [[ -z "$TO_TAG" ]]; then
    echo "$PROG: no rollback target — set FLOCK_PREVIOUS_TAG or pass --to <tag>." >&2
    echo "$PROG: the deploy orchestrator must record the previous good tag at deploy time." >&2
    exit 2
fi

CURRENT_TAG="${FLOCK_CURRENT_TAG:-unknown}"
echo "Flock OS rollback (FLO-246)"
echo "---------------------------"
echo "  current tag: $CURRENT_TAG"
echo "  rollback to: $TO_TAG"
echo

if [[ "$TO_TAG" == "$CURRENT_TAG" ]]; then
    echo "$PROG: rollback target == current tag; nothing to do." >&2
    exit 1
fi

# --- 1. Re-deploy the previous tag -------------------------------------------
# Delegate to the orchestrator's "deploy a specific tag" command. If unset,
# print the canonical patterns so a human/agent can wire it without guessing —
# the three orchestrators in play (Frappe Cloud Server SSH, docker compose, k8s)
# all support image-tag pinning.
DEPLOY_CMD="${FLOCK_DEPLOY_CMD:-}"
if [[ -z "$DEPLOY_CMD" ]]; then
    echo "$PROG: FLOCK_DEPLOY_CMD is unset — pick the pattern for your orchestrator:" >&2
    cat >&2 <<'PATTERNS'
  # Frappe Cloud Server plan (SSH) — re-pull + restart the supervisor tier:
  FLOCK_DEPLOY_CMD='ssh frappe@<host> "cd /home/frappe/frappe-bench && \
      docker pull registry.flock.os/flock-os-bench:<TAG> && \
      docker stop flock-os || true && \
      docker run -d --name flock-os --env-file /etc/flock-os/prod.env \
          -p 8080:8080 -p 9000:9000 \
          registry.flock.os/flock-os-bench:<TAG>"'
  # docker compose — set the image tag + up:
  FLOCK_DEPLOY_CMD='TAG=<TAG> docker compose up -d --no-deps --force-recreate bench'
  # k8s — kubectl set image:
  FLOCK_DEPLOY_CMD='kubectl set image deployment/flock-os bench=registry.flock.os/flock-os-bench:<TAG>'
  # The literal <TAG> in your FLOCK_DEPLOY_CMD is substituted with the rollback target.
PATTERNS
    echo "$PROG: set FLOCK_DEPLOY_CMD and re-run, or invoke the orchestrator by hand." >&2
    exit 1
fi

# Substitute <TAG> → rollback target. Literal token replacement (no sed regex
# hazards — <TAG> is a fixed sentinel).
DEPLOY_RESOLVED="${DEPLOY_CMD//\<TAG\>/$TO_TAG}"
echo "==> deploying rollback tag $TO_TAG"
echo "    cmd: $DEPLOY_RESOLVED"
if ! eval "$DEPLOY_RESOLVED"; then
    echo "$PROG: deploy command failed — the rollback did NOT apply." >&2
    echo "$PROG: the prior tag ($CURRENT_TAG) is still live." >&2
    exit 1
fi
echo "    rollback deploy applied."

# --- 2. Post-rollback smoke ---------------------------------------------------
if [[ "$SKIP_SMOKE" -eq 1 ]]; then
    echo "$PROG: --skip-smoke set; skipping the post-rollback smoke."
    echo "$PROG: rollback to $TO_TAG applied. Run scripts/deploy/smoke-staging.sh by hand."
    exit 0
fi

if [[ -z "${STAGING_URL:-}" ]]; then
    echo "$PROG: STAGING_URL is unset — cannot run the post-rollback smoke." >&2
    echo "$PROG: rollback deploy applied to $TO_TAG, but health is UNVERIFIED." >&2
    echo "$PROG: set STAGING_URL and run scripts/deploy/smoke-staging.sh." >&2
    exit 1
fi

echo
echo "==> post-rollback smoke"
if "$REPO_ROOT/scripts/deploy/smoke-staging.sh" --url "$STAGING_URL" ${STAGING_WS_URL:+--ws-url "$STAGING_WS_URL"}; then
    echo
    echo "ROLLBACK: SUCCESS — rolled $CURRENT_TAG → $TO_TAG and smoke is green."
    exit 0
fi

echo
echo "ROLLBACK: APPLIED BUT UNHEALTHY — $TO_TAG is live but the smoke failed." >&2
echo "Investigate the bench logs; if $TO_TAG is broken, roll further back" >&2
echo "(FLOCK_PREVIOUS_TAG=<older tag> scripts/deploy/rollback.sh)." >&2
exit 1
