#!/usr/bin/env bash
#
# Flock OS site restore ([FLO-288](/FLO/issues/FLO-288) Phase 6.2).
#
# Restores a backup archive (from scripts/dev/backup.sh) into a target bench
# site. **Non-destructive by design:**
#   - requires `--confirm` (refuses to run on a bare invocation);
#   - if the target site already exists AND has data, additionally requires
#     `--force` (bench restore drops + recreates the DB, which is destructive).
# A missing target site is created first via `bench new-site --install-app flock_os`.
#
# VM-independent: `bench restore` + `bench new-site` are the same on the local
# bench and prod (runbook: docs/operations/backup-restore.md).
#
# Usage:
#   scripts/dev/restore.sh <archive-dir> --site <target> --confirm [options]
#     <archive-dir>         archive produced by backup.sh (has manifest.json)
#     --site <name>         target site to restore INTO
#     --confirm             REQUIRED safety flag (acknowledge the overwrite)
#     --force               also required when restoring over a site WITH data
#     --bench-dir <path>    bench root (default: $BENCH_DIR)
#     --db-root-username <u>  MariaDB root user for new-site/restore (frappe_root)
#     --db-root-password <p>  MariaDB root password (default: $MARIADB_ROOT_PASSWORD)
#     --admin-password <p>    Administrator password for a freshly created site
#     -h, --help
#
# Env (lower priority than flags; $REPO_ROOT/.env auto-loaded if present):
#   BENCH_DIR, SITE_ADMIN_PASSWORD, MARIADB_ROOT_PASSWORD
set -uo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

usage() { sed -n '3,/^$/p' "${BASH_SOURCE[0]}" >&2; }

load_env() {
	local env="$REPO_ROOT/.env"
	[[ -f "$env" ]] || return 0
	set -a
	# shellcheck disable=SC1090
	. "$env"
	set +a
}
load_env

# site_has_data <site_config.json> — return 0 if the site's DB has any rows in
# tabDocType (i.e. not a freshly-created empty site). Reads DB creds from the
# site_config; returns 1 (no data) on any error so an unreadable empty site is
# treated as overwritable. frappe-free + version-independent (raw information_schema).
site_has_data() {
	local cfg="$1"
	[[ -f "$cfg" ]] || return 1
	python3 - "$cfg" <<'PY'
import json, subprocess, sys
cfg = json.load(open(sys.argv[1]))
db = cfg.get("db_name")
pw = cfg.get("db_password")
if not db or pw is None:
    sys.exit(1)
def q(sql):
    return subprocess.run(
        ["mysql", "-h", "127.0.0.1", "-u", db, f"-p{pw}", db, "-N", "-B", "-e", sql],
        capture_output=True, text=True,
    )
r = q("SELECT COUNT(*) FROM information_schema.tables "
      "WHERE table_schema=DATABASE() AND table_name='tabDocType'")
if r.returncode != 0:
    sys.exit(1)
# tabDocType present + non-empty => has data.
try:
    if int(r.stdout.strip()) == 0:
        sys.exit(1)  # no tabDocType table (empty site) -> no data
except ValueError:
    sys.exit(1)
r2 = q("SELECT COUNT(*) FROM `tabDocType`")
sys.exit(0 if r2.returncode == 0 and r2.stdout.strip() not in ("0", "") else 1)
PY
}

ARCHIVE=""
SITE=""
BENCH_DIR="${BENCH_DIR:-}"
DB_ROOT_USER="frappe_root"
DB_ROOT_PW="${MARIADB_ROOT_PASSWORD:-}"
ADMIN_PW="${SITE_ADMIN_PASSWORD:-}"
CONFIRM=0
FORCE=0

while [[ $# -gt 0 ]]; do
	case "$1" in
		--site) SITE="$2"; shift 2 ;;
		--site=*) SITE="${1#--site=}"; shift ;;
		--confirm) CONFIRM=1; shift ;;
		--force) FORCE=1; shift ;;
		--bench-dir) BENCH_DIR="$2"; shift 2 ;;
		--bench-dir=*) BENCH_DIR="${1#--bench-dir=}"; shift ;;
		--db-root-username) DB_ROOT_USER="$2"; shift 2 ;;
		--db-root-username=*) DB_ROOT_USER="${1#--db-root-username=}"; shift ;;
		--db-root-password) DB_ROOT_PW="$2"; shift 2 ;;
		--db-root-password=*) DB_ROOT_PW="${1#--db-root-password=}"; shift ;;
		--admin-password) ADMIN_PW="$2"; shift 2 ;;
		--admin-password=*) ADMIN_PW="${1#--admin-password=}"; shift ;;
		-h|--help) usage; exit 0 ;;
		-*) echo "$PROG: unknown arg: $1" >&2; exit 2 ;;
		*)
			[[ -z "$ARCHIVE" ]] || { echo "$PROG: unexpected extra arg: $1" >&2; exit 2; }
			ARCHIVE="$1"; shift ;;
	esac
done

# --- validate -----------------------------------------------------------------
[[ -n "$ARCHIVE" ]] || { echo "$PROG: <archive-dir> is required." >&2; usage; exit 2; }
[[ "$CONFIRM" -eq 1 ]] || { echo "$PROG: --confirm is required (restore is destructive)." >&2; exit 2; }
[[ -n "$SITE" ]] || { echo "$PROG: --site <target> is required." >&2; exit 2; }
if [[ -z "$BENCH_DIR" ]]; then
	echo "$PROG: --bench-dir (or BENCH_DIR env) is required." >&2; exit 2
fi
[[ -d "$BENCH_DIR/sites" ]] || { echo "$PROG: $BENCH_DIR is not a bench (no sites/)." >&2; exit 2; }

ARCHIVE="$(cd "$ARCHIVE" 2>/dev/null && pwd)" || { echo "$PROG: archive $ARCHIVE not found." >&2; exit 2; }
[[ -f "$ARCHIVE/manifest.json" ]] || { echo "$PROG: $ARCHIVE has no manifest.json (not a backup.sh archive)." >&2; exit 2; }

DB_FILE="$(find "$ARCHIVE" -maxdepth 1 -type f -name '*-database*.sql*' -print -quit 2>/dev/null || true)"
PRIVATE_FILE="$(find "$ARCHIVE" -maxdepth 1 -type f -name '*-private-files*.tar*' -print -quit 2>/dev/null || true)"
# Public files archive is named "<site>-files.tar"; exclude the private-files tar.
PUBLIC_FILE="$(find "$ARCHIVE" -maxdepth 1 -type f -name '*-files*.tar*' ! -name '*private*' -print -quit 2>/dev/null || true)"
[[ -n "$DB_FILE" ]] || { echo "$PROG: no database dump (*-database*.sql*) in $ARCHIVE." >&2; exit 2; }

echo "Flock OS restore (FLO-288)"
echo "--------------------------"
echo "  archive: $ARCHIVE"
echo "  db dump: $(basename "$DB_FILE")"
[[ -n "$PRIVATE_FILE" ]] && echo "  private: $(basename "$PRIVATE_FILE")"
[[ -n "$PUBLIC_FILE" ]] && echo "  public:  $(basename "$PUBLIC_FILE")"
echo "  target site: $SITE"
echo

root_args=()
[[ -z "$DB_ROOT_PW" ]] || root_args+=(--db-root-username "$DB_ROOT_USER" --db-root-password "$DB_ROOT_PW")

# --- target site: create if missing, else guard overwrite ---------------------
SITE_DIR="$BENCH_DIR/sites/$SITE"
if [[ ! -d "$SITE_DIR" ]]; then
	echo "==> target site $SITE does not exist — creating (bench new-site --install-app flock_os)"
	new_args=(--install-app flock_os)
	[[ -z "$ADMIN_PW" ]] || new_args+=(--admin-password "$ADMIN_PW")
	if ! (cd "$BENCH_DIR" && command bench new-site "$SITE" "${root_args[@]}" "${new_args[@]}"); then
		echo "$PROG: bench new-site $SITE failed." >&2
		exit 1
	fi
else
	# Site exists — only proceed if empty or --force.
	if [[ "$FORCE" -ne 1 ]]; then
		if site_has_data "$SITE_DIR/site_config.json"; then
			echo "$PROG: site $SITE already has data — refusing to overwrite without --force." >&2
			echo "$PROG: re-run with --force to drop + recreate its database from this backup." >&2
			exit 1
		fi
	fi
fi

# --- restore ------------------------------------------------------------------
echo "==> bench restore"
restore_args=(--site "$SITE" restore "$DB_FILE")
# bench restore drops + recreates the target DB, so it also needs root creds.
[[ -n "$DB_ROOT_PW" ]] && restore_args+=(--db-root-username "$DB_ROOT_USER" --db-root-password "$DB_ROOT_PW")
[[ -n "$PRIVATE_FILE" ]] && restore_args+=(--with-private-files "$PRIVATE_FILE")
[[ -n "$PUBLIC_FILE" ]] && restore_args+=(--with-public-files "$PUBLIC_FILE")
# --force on restore bypasses the app-version/downgrade warning so a restore into
# an already-migrated target does not block the drill.
[[ "$FORCE" -eq 1 ]] && restore_args+=(--force)
if ! (cd "$BENCH_DIR" && command bench "${restore_args[@]}"); then
	echo "$PROG: bench restore failed — target $SITE is in an indeterminate state." >&2
	exit 1
fi

# --- migrate the restored site onto the installed app schema -----------------
# bench restore loads the dump as-is; run migrate so the restored schema matches
# the currently-installed flock_os app (no-op when versions already align).
echo "==> bench migrate (align restored schema to installed app)"
if ! (cd "$BENCH_DIR" && command bench --site "$SITE" migrate); then
	echo "$PROG: WARN — bench migrate had errors; the restored data is loaded but schema may lag." >&2
fi

echo
echo "RESTORE: OK into site $SITE"
exit 0
