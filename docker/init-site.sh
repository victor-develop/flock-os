#!/usr/bin/env bash
#
# One-shot site provisioning for the docker WS tier (FLO-347).
#
# Mounted into the image at /home/frappe/init-site.sh and run by the `init`
# compose service before `web` + the socketio workers start. Idempotent: safe to
# re-run on every `docker-ws-tier.sh up`. Mirrors scripts/bootstrap.sh steps
# [5/6] + [6/6] (new-site / migrate / fixtures) but against the docker network.
set -euo pipefail

BENCH_DIR="${BENCH_DIR:-/home/frappe/frappe-bench}"
cd "$BENCH_DIR"

SITE_NAME="${SITE_NAME:-flock_os.localhost}"
: "${MARIADB_ROOT_PASSWORD:?MARIADB_ROOT_PASSWORD must be set (docker/.env.docker)}"
ADMIN_PASSWORD="${SITE_ADMIN_PASSWORD:-admin}"

# Wait for MariaDB TCP readiness (compose healthcheck already gates this, but a
# defensive poll makes the script runnable standalone against an existing DB).
echo "init: waiting for mariadb at ${DB_HOST:-mariadb}:${DB_PORT:-3306} ..."
for _ in $(seq 1 60); do
	if mysqladmin ping -h "${DB_HOST:-mariadb}" -P "${DB_PORT:-3306}" \
			-u root -p"$MARIADB_ROOT_PASSWORD" --silent 2>/dev/null; then
		break
	fi
	sleep 1
done

if [ ! -d "sites/$SITE_NAME" ]; then
	echo "init: creating site $SITE_NAME (install-app flock_os) ..."
	bench new-site "$SITE_NAME" \
		--db-root-username root \
		--db-root-password "$MARIADB_ROOT_PASSWORD" \
		--admin-password "$ADMIN_PASSWORD" \
		--db-host "${DB_HOST:-mariadb}" \
		--install-app flock_os
	bench --site "$SITE_NAME" use "$SITE_NAME"
else
	echo "init: site $SITE_NAME exists -> migrate (re-applies realtime wirings) ..."
	bench --site "$SITE_NAME" migrate
fi

# Runtime fixture the §8 gate requires: the branch-scope join gate resolves the
# gathering's branch, so gathering-smoke MUST exist in branch-smoke for the
# smoke user (leader@flock.os) or joins fail closed (looks like an FLO-107
# regression). Idempotent; see ws-broadcast-delivery.md -> Runtime fixture.
echo "init: seeding gathering-smoke runtime fixture ..."
bench --site "$SITE_NAME" execute flock_os.utils.smoke_fixtures.execute || \
	echo "init: WARN: smoke fixtures skipped/failed (non-fatal for the WS connect gate)."

echo "init: site $SITE_NAME ready."
