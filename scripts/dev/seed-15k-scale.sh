#!/usr/bin/env bash
#
# Seed the FLO-365 15k-scale dataset on the local bench.
#
# Materializes ~15,000 attendees across a multi-branch org/group tree plus
# attendance (via the canonical BulkAttendanceService) and registrations, so the
# DB/application hot paths can be profiled at event volume. Idempotent: purges
# tagged rows first so the drill is repeatable on a clean bench.
#
# Companion profiler: scripts/dev/profile-15k-scale.sh
# Findings doc:        docs/operations/scale-15k-findings.md
#
# Usage:
#   scripts/dev/seed-15k-scale.sh [site] [--members N] [--branches N] [--attendance N] [--no-purge]
#
# Run from the bench dir (or pass --site). See FLO-365.
#
set -euo pipefail

SITE_ARG=("${@:+--site}" "$@")

bench "${SITE_ARG[@]}" execute flock_os.utils.scale_seed.execute

echo "seed-15k-scale: done. Profile next:"
echo "  scripts/dev/profile-15k-scale.sh ${*:-}"
