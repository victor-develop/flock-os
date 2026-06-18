#!/usr/bin/env bash
# Phase-1 QA merge gate (FLO-21). Mirrors .github/workflows/ci.yml so that
# "green here == green in CI". Exits non-zero if ANY check fails, so it is the
# local pre-merge gate a developer/agent runs before declaring code shippable.
#
# Checks:
#   [1/4] ruff check .            (--no-cache: a stale .ruff_cache must never
#                                  produce a false green — proven hazard)
#   [2/4] ruff format --check .   (formatting gate; CI enforces the same)
#   [3/4] pytest flock_os/tests/  (project-level, SQL-light, no Frappe site)
#   [4/4] coverage gate           (branch-coverage ratchet over the pure domain
#                                  logic; bench-integration surface omitted —
#                                  fail_under configured in pyproject.toml). A
#                                  drop below the floor blocks the merge.
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

echo "==> [1/4] ruff check (--no-cache)"
if ruff check . --no-cache; then
	echo "[1/4] OK"
else
	echo "[1/4] FAIL"; fail=1
fi

echo "==> [2/4] ruff format --check"
if ruff format --check .; then
	echo "[2/4] OK"
else
	echo "[2/4] FAIL"; fail=1
fi

echo "==> [3/4] pytest flock_os/tests/ (no cache, deterministic)"
if pytest flock_os/tests/ -p no:cacheprovider -q; then
	echo "[3/4] OK"
else
	echo "[3/4] FAIL"; fail=1
fi

echo "==> [4/4] coverage gate (branch-coverage ratchet, pure domain logic)"
# pytest-cov reads [tool.coverage.*] from pyproject.toml. The gate fails when
# scoped branch coverage drops below `fail_under`. The bench-integration
# surface is omitted via config so the floor measures only the project-testable
# logic this gate is the authority on.
if pytest flock_os/tests/ -p no:cacheprovider -q \
	--cov=flock_os --cov-branch --cov-report=term-missing; then
	echo "[4/4] OK"
else
	echo "[4/4] FAIL"; fail=1
fi

echo "------------------------"
if [[ $fail -eq 0 ]]; then
	echo "GATE: PASS"
	exit 0
else
	echo "GATE: FAIL — resolve the above before merge"
	exit 1
fi
