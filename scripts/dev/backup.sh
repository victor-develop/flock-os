#!/usr/bin/env bash
#
# Flock OS site backup ([FLO-288](/FLO/issues/FLO-288) Phase 6.2).
#
# Wraps `bench backup --with-files` into a self-describing, versioned, timestamped
# archive directory: site_config.json + DB dump + public/private files + a
# JSON manifest (file list, sha256, app versions). Idempotent: every run produces
# a NEW timestamped archive (never overwrites), so re-runs accumulate history.
#
# VM-independent: `bench backup --with-files` mechanics are identical on the
# local bench and in prod, so this wrapper is the single backup entry point for
# both (runbook: docs/operations/backup-restore.md).
#
# The archive lives OUTSIDE the repo (default $BENCH_DIR/backups). It contains
# site_config.json (DB credentials) and uploaded files, so it MUST never be
# committed. Keep backups off the tracked tree.
#
# Usage:
#   scripts/dev/backup.sh [options]
#     --site <name>        site to back up (default: $SITE_NAME / current site)
#     --bench-dir <path>   bench root (default: $BENCH_DIR)
#     --out <path>         archive root (default: $FLOCK_BACKUP_DIR / $BENCH_DIR/backups)
#     --compress           gzip the file archives (passes --compress to bench)
#     -h, --help
#
# Env (lower priority than flags; $REPO_ROOT/.env auto-loaded if present):
#   BENCH_DIR, SITE_NAME, FLOCK_BACKUP_DIR
#
# Prints the absolute archive directory path on success. Exit non-zero on failure.
set -uo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

usage() { sed -n '3,/^$/p' "${BASH_SOURCE[0]}" >&2; }

# Auto-load .env if present (does not clobber vars already set in the env).
load_env() {
	local env="$REPO_ROOT/.env"
	[[ -f "$env" ]] || return 0
	set -a
	# shellcheck disable=SC1090
	. "$env"
	set +a
}
load_env

SITE="${SITE_NAME:-}"
BENCH_DIR="${BENCH_DIR:-}"
OUT="${FLOCK_BACKUP_DIR:-}"
COMPRESS=0

while [[ $# -gt 0 ]]; do
	case "$1" in
		--site) SITE="$2"; shift 2 ;;
		--site=*) SITE="${1#--site=}"; shift ;;
		--bench-dir) BENCH_DIR="$2"; shift 2 ;;
		--bench-dir=*) BENCH_DIR="${1#--bench-dir=}"; shift ;;
		--out) OUT="$2"; shift 2 ;;
		--out=*) OUT="${1#--out=}"; shift ;;
		--compress) COMPRESS=1; shift ;;
		-h|--help) usage; exit 0 ;;
		*) echo "$PROG: unknown arg: $1" >&2; exit 2 ;;
	esac
done

# --- resolve bench root -------------------------------------------------------
if [[ -z "$BENCH_DIR" ]]; then
	echo "$PROG: --bench-dir (or BENCH_DIR env) is required." >&2
	echo "$PROG: it is the bench runtime root (the dir containing sites/)." >&2
	exit 2
fi
if [[ ! -d "$BENCH_DIR/sites" ]]; then
	echo "$PROG: $BENCH_DIR does not look like a bench (no sites/ dir)." >&2
	exit 2
fi

# --- resolve site -------------------------------------------------------------
resolve_site() {
	if [[ -n "$SITE" ]]; then printf '%s' "$SITE"; return; fi
	local cur="$BENCH_DIR/sites/currentsite.txt"
	if [[ -f "$cur" ]] && [[ -s "$cur" ]]; then cat "$cur"; return; fi
	return 1
}
SITE="$(resolve_site)" || { echo "$PROG: no --site and no current site set." >&2; exit 2; }
SITE_CONFIG="$BENCH_DIR/sites/$SITE/site_config.json"
[[ -f "$SITE_CONFIG" ]] || { echo "$PROG: site $SITE not found (no site_config.json)." >&2; exit 2; }

# --- resolve archive root -----------------------------------------------------
[[ -n "$OUT" ]] || OUT="$BENCH_DIR/backups"
mkdir -p "$OUT" || { echo "$PROG: cannot create archive root $OUT." >&2; exit 2; }

TS="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$(cd "$OUT" && pwd)/$SITE-$TS"
mkdir -p "$ARCHIVE" || { echo "$PROG: cannot create archive $ARCHIVE." >&2; exit 2; }

echo "Flock OS backup (FLO-288)"
echo "-------------------------"
echo "  bench:  $BENCH_DIR"
echo "  site:   $SITE"
echo "  archive: $ARCHIVE"
echo

# --- run bench backup (all files into the archive dir) ------------------------
BACKUP_ARGS=(--site "$SITE" backup --with-files --backup-path "$ARCHIVE")
[[ "$COMPRESS" -eq 1 ]] && BACKUP_ARGS+=(--compress)
echo "==> bench backup --with-files"
if ! (cd "$BENCH_DIR" && command bench "${BACKUP_ARGS[@]}"); then
	echo "$PROG: bench backup failed — archive at $ARCHIVE is incomplete." >&2
	exit 1
fi

# --- copy site_config.json (needed for restore; contains DB creds) -------------
cp "$SITE_CONFIG" "$ARCHIVE/site_config.json"

# --- normalize dump for cross-database portability ----------------------------
# Two mysqldump artifacts break restore into a DIFFERENTLY-named database (a DR
# landmine — a recovery site's DB name always differs from the source):
#
#   (1) SEQUENCE nextval() DEFAULTs are qualified with the SOURCE DB name:
#         ``nextval(`_sourcedb`.`event_attendance_summary_id_seq`)``
#       Loaded under the target site user, that triggers ERROR 1142 (no access
#       to the source DB's sequence). Strip the qualifier so the sequence
#       resolves against whichever DB loads the dump (the dump creates sequences
#       unqualified, earlier in the file). The exact regex is the unit-tested
#       ``NEXTVAL_QUALIFIED_RE`` in flock_os/utils/backup_drill.py — keep in sync.
#
#   (2) mysqldump's ``--add-locks`` wraps each table's INSERTs in
#         ``LOCK TABLES `tabX` WRITE;`` … ``UNLOCK TABLES;``
#       For a table whose DEFAULT pulls a SEQUENCE, MariaDB then raises
#       ERROR 1100 ("Table …seq was not locked with LOCK TABLES") because the
#       sequence is not in the lock set. The LOCK directives are an optional
#       import-speed optimization; dropping them is safe for a single-session
#       restore and removes the failure. (Equivalent to ``--skip-add-locks``.)
#
# Both transforms are pure and idempotent; a dump with no sequences / no locks is
# unchanged. ``restore-drill.sh`` is the end-to-end proof the normalized dump
# restores into a differently-named DB with row-count parity.
SQL_GZ="$(find "$ARCHIVE" -maxdepth 1 -type f -name '*-database*.sql.gz' -print -quit 2>/dev/null || true)"
if [[ -n "$SQL_GZ" ]]; then
	tmp_sql="$(mktemp)"
	if gunzip -c "$SQL_GZ" \
		| sed -E \
			-e 's/nextval\(`[^`]+`\.`([^`]+)`\)/nextval(`\1`)/g' \
			-e '/^LOCK TABLES /d' \
			-e '/^UNLOCK TABLES;/d' \
			>"$tmp_sql" 2>/dev/null; then
		gzip -c "$tmp_sql" >"$SQL_GZ"
	fi
	rm -f "$tmp_sql"
fi

# --- manifest (file list + sha256 + versions) ---------------------------------
# Generated via python3 for correct JSON; reads the archive dir after backup.
echo "==> writing manifest"
if ! python3 - "$ARCHIVE" "$SITE" "$TS" "$BENCH_DIR" <<'PY'; then
import hashlib, json, os, subprocess, sys
from datetime import datetime, timezone

archive, site, ts, bench_dir = sys.argv[1:5]


def sha256(path):
	h = hashlib.sha256()
	with open(path, "rb") as fh:
		for chunk in iter(lambda: fh.read(1 << 20), b""):
			h.update(chunk)
	return h.hexdigest()


files = []
for name in sorted(os.listdir(archive)):
	p = os.path.join(archive, name)
	if os.path.isfile(p):
		files.append({"name": name, "bytes": os.path.getsize(p), "sha256": sha256(p)})


def app_versions():
	try:
		out = subprocess.run(
			["bench", "version"], cwd=bench_dir, capture_output=True, text=True, timeout=60
		)
	except Exception:
		return {}
	apps = {}
	for line in out.stdout.splitlines():
		parts = line.split()
		if len(parts) >= 2:
			apps[parts[0]] = parts[1]
	return apps


manifest = {
	"site": site,
	"timestamp_local": ts,
	"generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
	"bench_dir": bench_dir,
	"files": files,
	"versions": app_versions(),
}
with open(os.path.join(archive, "manifest.json"), "w") as fh:
	json.dump(manifest, fh, indent=2, sort_keys=True)
	fh.write("\n")
PY
	echo "$PROG: manifest generation failed." >&2
	exit 1
fi

db_file="$(find "$ARCHIVE" -maxdepth 1 -type f -name '*-database*.sql*' -print -quit 2>/dev/null || true)"

echo
echo "BACKUP: OK"
echo "  archive: $ARCHIVE"
[[ -n "$db_file" ]] && echo "  db dump: $(basename "$db_file")"
echo "  restore with: scripts/dev/restore.sh \"$ARCHIVE\" --site <target> --confirm"
exit 0
