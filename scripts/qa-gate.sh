#!/usr/bin/env bash
# Phase-1 QA merge gate (FLO-21). Mirrors .github/workflows/ci.yml so that
# "green here == green in CI". Exits non-zero if ANY check fails, so it is the
# local pre-merge gate a developer/agent runs before declaring code shippable.
#
# Checks:
#   [1/3] ruff check .            (--no-cache: a stale .ruff_cache must never
#                                  produce a false green — proven hazard)
#   [2/3] ruff format --check .   (formatting gate; CI enforces the same)
#   [3/3] pytest flock_os/tests/  (project-level, SQL-light, no Frappe site)
#
# Frappe-level integration tests (doctype test_*.py that import frappe) are out
# of scope here — they run via `bench run-tests` against a real site, matching
# the CI strategy documented in .github/workflows/ci.yml.
#
# Usage: scripts/qa-gate.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV_BIN="$ROOT/.venv/bin"
# Prefer the project venv if it has the tooling; otherwise fall back to PATH.
if [[ -x "$VENV_BIN/ruff" && -x "$VENV_BIN/pytest" ]]; then
	PATH="$VENV_BIN:$PATH"
fi

command -v ruff >/dev/null 2>&1 || { echo "FAIL: ruff not found (pip install ruff)"; exit 2; }
command -v pytest >/dev/null 2>&1 || { echo "FAIL: pytest not found (pip install pytest)"; exit 2; }

cd "$ROOT"
fail=0
echo "Phase-1 QA gate (FLO-21)"
echo "------------------------"

echo "==> [1/3] ruff check (--no-cache)"
if ruff check . --no-cache; then
	echo "[1/3] OK"
else
	echo "[1/3] FAIL"; fail=1
fi

echo "==> [2/3] ruff format --check"
if ruff format --check .; then
	echo "[2/3] OK"
else
	echo "[2/3] FAIL"; fail=1
fi

echo "==> [3/3] pytest flock_os/tests/ (no cache, deterministic)"
if pytest flock_os/tests/ -p no:cacheprovider -q; then
	echo "[3/3] OK"
else
	echo "[3/3] FAIL"; fail=1
fi

echo "------------------------"
if [[ $fail -eq 0 ]]; then
	echo "GATE: PASS"
	exit 0
else
	echo "GATE: FAIL — resolve the above before merge"
	exit 1
fi
