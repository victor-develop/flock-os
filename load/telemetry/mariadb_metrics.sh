#!/usr/bin/env bash
# Flock OS – MariaDB runtime telemetry wrapper (FLO-49 / FLO-10 §8, D5 trigger).
#
# Emits a prometheus-ish time series from the queries in mariadb_metrics.sql:
# slow-query count, live connections, and the InnoDB buffer-pool hit ratio.
# Run on a sample cadence during a k6 smoke (slow-query delta == rate).
#
# Config via env (defaults match the Homebrew Frappe dev site):
#   MARIADB_HOST, MARIADB_PORT, MARIADB_USER, MARIADB_PASSWORD, MARIADB_DB
set -euo pipefail

MARIADB_HOST="${MARIADB_HOST:-127.0.0.1}"
MARIADB_PORT="${MARIADB_PORT:-3306}"
MARIADB_USER="${MARIADB_USER:-root}"
MARIADB_PASSWORD="${MARIADB_PASSWORD:?set MARIADB_PASSWORD (see .env)}"
MARIADB_DB="${MARIADB_DB:-_flock_os}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mysql_cli() {
  MYSQL_PWD="$MARIADB_PASSWORD" mysql -h "$MARIADB_HOST" -P "$MARIADB_PORT" \
    -u "$MARIADB_USER" -N -B "$@"
}

TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "# flock mariadb telemetry @ $TS"

slow=$(mysql_cli -e "SHOW GLOBAL STATUS LIKE 'Slow_queries';" | awk '{print $2}')
threads=$(mysql_cli -e "SHOW GLOBAL STATUS LIKE 'Threads_connected';" | awk '{print $2}')
maxused=$(mysql_cli -e "SHOW GLOBAL STATUS LIKE 'Max_used_connections';" | awk '{print $2}')
read_req=$(mysql_cli -e "SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_read_requests';" | awk '{print $2}')
phys_reads=$(mysql_cli -e "SHOW GLOBAL STATUS LIKE 'Innodb_buffer_pool_reads';" | awk '{print $2}')

echo "flock_mariadb_slow_queries_total ${slow:-0}"
echo "flock_mariadb_threads_connected ${threads:-0}"
echo "flock_mariadb_max_used_connections ${maxused:-0}"

# Hit ratio = 1 - physical_reads / read_requests (D5 revisit trigger < ~99%).
# Guard: only compute when read_requests is a positive integer (note the
# `var=value` binding form for awk -v — `var value` is not valid awk syntax).
hit="0"
if [[ "${read_req:-}" =~ ^[0-9]+$ && "$read_req" -gt 0 ]]; then
  hit=$(awk -v rr="$read_req" -v pr="${phys_reads:-0}" 'BEGIN { printf "%.4f", 1 - (pr / rr) }')
fi
echo "flock_mariadb_buffer_pool_hit_ratio ${hit}"
