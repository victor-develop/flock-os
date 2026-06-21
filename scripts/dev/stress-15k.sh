#!/usr/bin/env bash
#
# FLO-454 / FLO-365 — local 15k DB/app stress drill.
#
# Seeds ~15,000 attendees across a multi-branch / nested-group tree on the
# local bench, profiles the four hot paths (bulk attendance, registration,
# broadcast, room-join), verifies the Redis sliding-window throttle, and prints
# the findings snapshot. Re-runs are idempotent (stress-* namespace truncation).
#
# Usage:
#   scripts/dev/stress-15k.sh [container] [site]
#
#   container  defaults to flock-ws-web-1 (the docker-compose web service)
#   site       defaults to flock_os.localhost
#
# No cloud, no board budget — this is the no-board Phase 6.1 validation slice.
# Runbook: docs/development/flo-454-stress-findings.md
#
set -euo pipefail

CONTAINER="${1:-flock-ws-web-1}"
SITE="${2:-flock_os.localhost}"

echo "FLO-454: executing 15k DB/app stress on container '$CONTAINER' (site: $SITE)..."

docker exec "$CONTAINER" bash -lc "
  cd /home/frappe/frappe-bench/sites && ../env/bin/python -c '
import frappe, json, sys
frappe.init(site=\"$SITE\")
frappe.connect()
from flock_os.utils.stress_seed import execute
result = execute()
frappe.db.commit()
frappe.destroy()
print(\"STRESS_RESULT_JSON_START\")
print(json.dumps(result, indent=2, default=str))
print(\"STRESS_RESULT_JSON_END\")
'
" 2>&1 | tee /tmp/flo-454-stress-output.txt

echo ""
echo "FLO-454: stress drill complete. Output saved to /tmp/flo-454-stress-output.txt"
