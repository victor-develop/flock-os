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

# Frappe's `bench new-site` scopes the site DB user to the connecting client's
# host IP (e.g. `_e0fa89170b204789@172.18.0.6`). In a multi-container bench the
# web + socketio workers connect from DIFFERENT IPs on the docker network, so
# the init-scoped user denies them (Access denied for ...@172.18.0.9). Broaden
# the user to @'%' so every container on the backend network can connect with
# the password already in site_config.json (standard frappe-docker fix).
broaden_db_user() {
	local cfg="sites/$SITE_NAME/site_config.json"
	local db_name db_pass
	db_name="$(env/bin/python -c "import json;print(json.load(open('$cfg'))['db_name'])")"
	db_pass="$(env/bin/python -c "import json;print(json.load(open('$cfg'))['db_password'])")"
	echo "init: broadening DB user '$db_name' to @'%' for multi-container access ..."
	mariadb -h "${DB_HOST:-mariadb}" -P "${DB_PORT:-3306}" -u root -p"$MARIADB_ROOT_PASSWORD" <<SQL 2>/dev/null
CREATE USER IF NOT EXISTS '$db_name'@'%' IDENTIFIED BY '$db_pass';
ALTER USER '$db_name'@'%' IDENTIFIED BY '$db_pass';
GRANT ALL PRIVILEGES ON \`$db_name\`.* TO '$db_name'@'%';
FLUSH PRIVILEGES;
SQL
}

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
	broaden_db_user
else
	echo "init: site $SITE_NAME exists -> migrate (re-applies realtime wirings) ..."
	bench --site "$SITE_NAME" migrate
fi

# Runtime fixture the §8 gate requires: the branch-scope join gate resolves the
# gathering's branch, so gathering-smoke + the scoped leader MUST exist for the
# smoke user (leader@flock.os) or joins fail closed (looks like an FLO-107
# regression). Idempotent; see ws-broadcast-delivery.md -> Runtime fixture.
#
# Run from the bench ROOT (not sites/) with an explicit sites_path: frappe.init()
# defaults sites_path to cwd, and importing flock_os.permissions (which the
# fixtures pull in) resolves the nested flock_os.flock_os module correctly only
# from the bench root (a setuptools editable-install namespace quirk under the
# sites/ cwd). Also pre-create the log dirs frappe's rotating handler writes to.
echo "init: seeding gathering-smoke runtime fixture ..."
# Frappe's rotating log handler resolves to $HOME/logs/<site>_database.log
# (e.g. /home/frappe/logs/flock_os.localhost_database.log) — that is ONE level
# above the bench root, NOT under it, so the relative `logs` dirs created below
# do not cover it. Without this absolute mkdir the first frappe.connect() in the
# fixture block throws FileNotFoundError on the database.log handler and the
# smoke leader / gathering-smoke fixtures never seed (init then exits 0 with the
# non-fatal WARN, leaving the gate with no leader@flock.os to authenticate).
mkdir -p logs sites/flock_os.localhost/logs sites/logs "${HOME}/logs"
env/bin/python - <<'PY' || echo "init: WARN: smoke fixtures skipped/failed (non-fatal for the WS connect gate; create the smoke leader manually if running the gate)."
import os
import frappe
import traceback
frappe.init(os.environ.get("SITE_NAME", "flock_os.localhost"),
            sites_path=os.path.join(os.getcwd(), "sites"))
for _d in ("logs", "flock_os.localhost/logs"):
    os.makedirs(os.path.join(os.getcwd(), "sites", _d), exist_ok=True)
    os.makedirs(os.path.join(os.getcwd(), _d), exist_ok=True)
# Absolute $HOME/logs (see mkdir note above) — belt-and-braces in case CWD
# differs from the bench root when this runs.
os.makedirs(os.path.join(os.environ.get("HOME", "/home/frappe"), "logs"), exist_ok=True)
frappe.connect()
try:
    from flock_os.utils.smoke_fixtures import execute
    print("init: smoke fixtures ->", execute())
    frappe.db.commit()
except Exception:
    traceback.print_exc()
PY

echo "init: site $SITE_NAME ready."
