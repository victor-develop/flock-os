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

# Foundation gate scope = tracked test files that still exist on disk. Shared
# working tree is churny: untracked WIP tests are excluded (CI parity), and a
# tracked test deleted from the worktree mid-refactor is skipped rather than
# aborting the whole suite's collection. (Architect directive, FLO-21.)
tracked_tests() {
	git ls-files -z 'flock_os/tests/test_*.py' | while IFS= read -r -d '' f; do
		[ -f "$f" ] && printf '%s\0' "$f"
	done
}

# All tracked Python sources — CI parity. actions/checkout only ever has
# committed files, so CI's `ruff` never lints an untracked in-flight scratch
# file. Running `ruff ... .` locally instead lints every untracked WIP file in
# the shared tree and intermittently reds the gate on code CI will never see
# (a transient doctype scratch file already produced a false format-red that
# mis-diagnosed fixtures.py — FLO-89). Same parity gap FLO-21 closed for the
# pytest/coverage steps below. (FLO-89)
tracked_py() {
	git ls-files -z '*.py'
}

echo "==> [1/4] ruff check (--no-cache, TRACKED files only — CI parity)"
if tracked_py | xargs -0 ruff check --no-cache; then
	echo "[1/4] OK"
else
	echo "[1/4] FAIL"; fail=1
fi

echo "==> [2/4] ruff format --check (TRACKED files only — CI parity)"
if tracked_py | xargs -0 ruff format --check; then
	echo "[2/4] OK"
else
	echo "[2/4] FAIL"; fail=1
fi

echo "==> [3/4] pytest — foundation unit tests (TRACKED files only, no cache)"
# Parity with CI: actions/checkout only ever sees committed files, so CI's
# pytest can never trip on an untracked WIP test. The local gate must match, or
# a single in-flight slice's untracked test (which often imports a module that
# does not exist yet) aborts the WHOLE suite's collection and blocks every
# other slice. The foundation gate therefore runs only git-tracked test files;
# a slice's tests enter the gate when their owner commits them green
# (Architect directive: no forward-looking tests in the shared gate until their
# implementation imports and passes). Stale-cache-proofed (-p no:cacheprovider).
if tracked_tests | xargs -0 pytest -p no:cacheprovider -q; then
	echo "[3/4] OK"
else
	echo "[3/4] FAIL"; fail=1
fi

echo "==> [4/4] coverage gate (branch-coverage ratchet, TRACKED source only)"
# CI parity: actions/checkout only ever has tracked files, so CI's coverage
# never measures an untracked in-flight module (e.g. notifications.py) at 0%.
# Locally the working tree carries other slices' untracked WIP source; left in,
# it trips the ratchet with no foundation regression. So this run omits every
# untracked .py under flock_os/ in addition to the bench-only surface. The
# bench-only entries below mirror [tool.coverage.run] omit in pyproject.toml
# (single source of truth there) — keep in sync if that list changes.
COVRC="$(mktemp -t flock_covrc.XXXXXX)"
{
	echo "[run]"
	echo "source = flock_os"
	echo "branch = True"
	echo "omit ="
	printf '\t%s\n' \
		"flock_os/flock_os/doctype/*" \
		"flock_os/hooks.py" \
		"flock_os/fixtures.py" \
		"flock_os/patches/*" \
		"flock_os/_smoke_runtime.py" \
		"flock_os/utils/*"
	# Dynamically omit untracked WIP source so the ratchet measures the
	# committed foundation only (matches CI's checkout).
	git ls-files --others --exclude-standard -z 'flock_os/*.py' | while IFS= read -r -d '' f; do
		printf '\t%s\n' "$f"
	done
	echo "[report]"
	echo "show_missing = True"
	echo "fail_under = 80"
} > "$COVRC"
if tracked_tests | xargs -0 pytest -p no:cacheprovider -q \
	--cov=flock_os --cov-config="$COVRC" --cov-branch --cov-report=term-missing; then
	echo "[4/4] OK"
else
	echo "[4/4] FAIL"; fail=1
fi
rm -f "$COVRC"

echo "------------------------"
if [[ $fail -eq 0 ]]; then
	echo "GATE: PASS"
	exit 0
else
	echo "GATE: FAIL — resolve the above before merge"
	exit 1
fi
