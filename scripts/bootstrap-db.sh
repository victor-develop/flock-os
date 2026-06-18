#!/usr/bin/env bash
#
# One-time MariaDB preparation for Flock OS / Frappe on macOS (Homebrew).
#
# Homebrew MariaDB enables unix_socket auth, so the OS user (`mac`) is a
# passwordless superuser via the local socket, but Frappe connects over TCP
# (127.0.0.1) and therefore needs a password-authenticated root-equivalent user.
#
# This script:
#   1. Loads ./../.env (MARIADB_ROOT_PASSWORD).
#   2. Connects via the unix socket as the OS superuser.
#   3. Idempotently creates `frappe_root`@`%` (TCP, any host) with that password
#      and full privileges + GRANT OPTION, so `bench new-site
#      --db-root-username frappe_root` works over TCP.
#
# We use a dedicated `frappe_root` user (not `root`) because Homebrew MariaDB
# ships a `root`@`localhost` account that always shadows `root`@`%` for local
# TCP connections. A dedicated name avoids that host-matching conflict.
#
# The password is generated locally and stored ONLY in .env (gitignored).
# No secret is written to the repo or printed.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

ENV_FILE="$REPO_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.example to .env and set MARIADB_ROOT_PASSWORD first." >&2
  exit 1
fi

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a

if [ -z "${MARIADB_ROOT_PASSWORD:-}" ]; then
  echo "ERROR: MARIADB_ROOT_PASSWORD is empty in $ENV_FILE." >&2
  exit 1
fi

# The OS user that Homebrew MariaDB trusts via unix_socket.
OS_USER="$(id -un)"

echo "==> Creating TCP-accessible frappe_root@%% for Frappe (auth via socket as '$OS_USER')..."

# mysql CLI quoting of the password safely.
mysql -u "$OS_USER" -h localhost --protocol=socket -S "${MARIADB_SOCKET:-/tmp/mysql.sock}" <<SQL
-- Homebrew MariaDB ships anonymous ''@'localhost' / ''@'<host>' accounts whose
-- host is more specific than '<user>'@'%' and therefore SHADOW every named user
-- on local TCP connections (login fails with "using password: YES"). Drop them.
DROP USER IF EXISTS ''@'localhost';
DROP USER IF EXISTS ''@'127.0.0.1';
DROP USER IF EXISTS ''@'$(hostname)';
DROP USER IF EXISTS ''@'$(hostname -s 2>/dev/null || echo localhost)';
DROP USER IF EXISTS ''@'%';

CREATE USER IF NOT EXISTS 'frappe_root'@'%' IDENTIFIED BY '${MARIADB_ROOT_PASSWORD}';
ALTER USER 'frappe_root'@'%' IDENTIFIED BY '${MARIADB_ROOT_PASSWORD}';
GRANT ALL PRIVILEGES ON *.* TO 'frappe_root'@'%' WITH GRANT OPTION;
FLUSH PRIVILEGES;
SQL

echo "==> Verifying TCP login as frappe_root@127.0.0.1 ..."
if mysql -u frappe_root -h 127.0.0.1 -P 3306 -p"${MARIADB_ROOT_PASSWORD}" -e "SELECT 'frappe_root@% TCP OK' AS status, VERSION() AS version;" >/dev/null 2>&1; then
  echo "    OK: frappe_root@%% can connect over TCP."
else
  echo "    ERROR: frappe_root@%% TCP login failed." >&2
  exit 1
fi

echo "==> MariaDB is ready for Frappe (bench new-site --db-root-username frappe_root)."
