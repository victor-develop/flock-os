#!/usr/bin/env bash
#
# Profile the Flock OS hot paths against the FLO-365 15k-scale dataset.
#
# Runs flock_os.utils.scale_profile.execute on the local bench: captures timing
# + EXPLAIN plans for the attendance/registration/realtime hot paths, flags N+1
# / full scans / unbounded reads, and verifies the in-app throttle under burst.
# Seeds first if no scale data is present (one-shot repeatable).
#
# Prerequisite: scripts/dev/seed-15k-scale.sh (auto-run if missing).
# Output:       pretty-printed JSON report in the bench log + feeds the
#               findings doc docs/operations/scale-15k-findings.md.
#
# Usage:
#   scripts/dev/profile-15k-scale.sh [site]
#
set -euo pipefail

SITE_ARG=("${@:+--site}" "$@")

bench "${SITE_ARG[@]}" execute flock_os.utils.scale_profile.execute

echo "profile-15k-scale: done. Report logged above; update docs/operations/scale-15k-findings.md."
