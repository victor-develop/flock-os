#!/usr/bin/env bash
#
# Seed the Flock OS §8 smoke fixtures (FLO-112 / FLO-53 §8 WS gate).
#
# Materializes the runtime fixtures `load/README.md` -> "Runtime fixtures"
# assumes (tenant org -> branch -> group -> gathering, plus the scoped leader
# user) so the WS room-join scope gate resolves `gathering-smoke` ->
# `branch-smoke` and `flock_os.realtime_views.can_join_event_room` returns true
# for the leader. The `Flock Gathering` DocType (FLO-54) already ships in the
# app; without a seeded gathering the resolver raises -> the gate fails closed
# -> the smoke records zero broadcasts.
#
# These are runtime smoke rows, NOT canonical catalog fixtures — intentionally
# not run on `bench migrate` (no prod pollution). This is a thin wrapper over
# the idempotent seeder `flock_os.utils.smoke_fixtures.execute`; re-runs are a
# no-op. Run it once on the bench the k6 smoke + the broadcast producer target.
#
# Usage:
#   scripts/dev/seed-smoke-fixtures.sh [site]
#
#   site defaults to the current bench site (`bench --site <site>`); pass one
#   explicitly in a multi-site bench. Runbook: docs/development/ws-broadcast-delivery.md.
#
set -euo pipefail

SITE_ARG=("${@:+--site}" "$@")  # empty -> bench picks the current site

bench "${SITE_ARG[@]}" execute flock_os.utils.smoke_fixtures.execute

echo "seed-smoke-fixtures: done (idempotent). Verify with:"
echo "  bench ${SITE_ARG[*]} execute frappe.client.get_value \\\\"
echo "    --kwargs \"{doctype:'Flock Gathering', filters:{name:'gathering-smoke'}, fieldname:'branch'}\""
