#!/usr/bin/env bash
#
# Flock OS operator restore entry point ([FLO-885](/FLO/issues/FLO-885) Phase 6.2 AC#1).
#
# Thin delegation wrapper: the canonical implementation lives at
# scripts/dev/restore.sh (referenced by docs/operations/backup-restore.md and
# the prior FLO-288 / FLO-353 commits). This wrapper establishes scripts/ops/
# as the operator-facing entry point without duplicating logic (DRY) — every
# flag and env var passes through unchanged.
#
# Usage: scripts/ops/restore.sh <archive-dir> --site <target> --confirm [options]
#        (see scripts/dev/restore.sh --help)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/../dev/restore.sh" "$@"
