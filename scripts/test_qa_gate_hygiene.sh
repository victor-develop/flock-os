#!/usr/bin/env bash
#
# Regression test for the QA gate's churn-immunity invariants (FLO-95).
#
# The local gate must mirror CI's actions/checkout behaviour: it may only ever
# inspect git-tracked files that still exist on disk. Two false-red-while-CI-green
# hazards are locked here:
#
#   [A] Untracked WIP immunity — an in-flight slice's untracked scratch module
#       (lint + format violations) and untracked forward-looking test (imports a
#       module that does not exist) must NOT trip the ruff steps, must NOT be
#       collected by the pytest step, and must NOT show up in the coverage report
#       or pull the ratchet under its floor. (FLO-21/FLO-89 scoped the gate to
#       tracked files; this test proves it end-to-end.)
#
#   [B] Deleted-tracked-file immunity — a tracked .py removed from the worktree
#       mid-refactor must be SKIPPED, not fed to ruff as a missing path (E902),
#       because CI's checkout at the same commit still has the file. This is the
#       [ -f ] guard tracked_tests() has always had; FLO-95 added it to
#       tracked_py() for the ruff steps.
#
# The test drives the REAL scripts/qa-gate.sh (no re-implementation of its
# commands) so a regression in the gate's own scoping is caught. It runs the full
# gate (~tens of seconds) and asserts on per-step output plus the coverage report
# body — it does not depend on any other slice's state.
#
# Usage: scripts/test_qa_gate_hygiene.sh
# Exits 0 only if every invariant holds.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

VENV_BIN="$ROOT/.venv/bin"
if [[ -x "$VENV_BIN/ruff" ]]; then PATH="$VENV_BIN:$PATH"; fi
command -v ruff >/dev/null 2>&1 || { echo "FAIL: ruff not found"; exit 2; }

GATE="$ROOT/scripts/qa-gate.sh"
[[ -f "$GATE" ]] || { echo "FAIL: $GATE not found"; exit 2; }

# Scratch fixtures live under flock_os/ so the coverage source-enumeration and
# the omit pathspec ('flock_os/*.py') are both exercised realistically.
WIP_SRC="$ROOT/flock_os/_flo95_wip_src.py"
WIP_TST="$ROOT/flock_os/tests/test_flo95_wip_slice.py"
PROBE="$ROOT/flock_os/_flo95_deletion_probe.py"
planted=()

plant() { planted+=("$1"); }
cleanup() {
	for f in "${planted[@]:-}"; do
		rm -f "$f"
	done
	# Drop the staged probe from the index if test [B] left it tracked.
	git rm --quiet --cached --ignore-unmatch "$PROBE" >/dev/null 2>&1 || true
	rm -f "$PROBE"
	# NOTE: never `git checkout` the gate here — running the gate does not modify
	# it, and restoring it would clobber a developer's in-progress edits to
	# qa-gate.sh itself.
}
trap cleanup EXIT

results_ok=1
fail() { echo "FAIL: $*" >&2; results_ok=0; }

# --------------------------------------------------------------------------- #
# [A] Untracked WIP immunity
# --------------------------------------------------------------------------- #
# A source module ruff would flag (unused import, bad spacing) and that is NOT
# formatted to the project's tab style.
cat > "$WIP_SRC" <<'PY'
import os
def  badly_formatted( x,y ):
		return x+y
PY
plant "$WIP_SRC"

# A forward-looking test that imports a module which does not exist yet — left
# untracked, it must never abort the foundation suite's collection.
cat > "$WIP_TST" <<'PY'
def test_flo95_wip_slice():
	import flock_os.__flo95_does_not_exist_yet__  # noqa: F401
PY
plant "$WIP_TST"

# Precondition: the fixtures really ARE the false-red triggers we claim.
git ls-files --error-unmatch "$WIP_SRC" >/dev/null 2>&1 \
	&& fail "$WIP_SRC is tracked; untracked-WIP fixture invalid"
if ruff check "$WIP_SRC" --no-cache >/dev/null 2>&1; then
	fail "WIP source fixture did not trigger ruff check; test is non-functional"
fi
if ruff format --check "$WIP_SRC" >/dev/null 2>&1; then
	fail "WIP source fixture passed format check; test is non-functional"
fi

echo "[A] untracked WIP present — running full gate…"
out_a="$(bash "$GATE" 2>&1)" || true

grep -q '^\[1/4\] OK$' <<<"$out_a" \
	|| fail "[1/4] ruff check tripped on untracked WIP (should be tracked-only)"
grep -q '^\[2/4\] OK$' <<<"$out_a" \
	|| fail "[2/4] ruff format tripped on untracked WIP (should be tracked-only)"

# The untracked source must NOT appear in the coverage report (omit works), and
# the untracked test must NOT be collected by pytest.
grep -qE 'flock_os/_flo95_wip_src\.py' <<<"$out_a" \
	&& fail "untracked WIP source leaked into the coverage report"
grep -qE 'test_flo95_wip_slice|flo95_does_not_exist_yet' <<<"$out_a" \
	&& fail "untracked WIP test was collected by the pytest step"

# Remove the [A] fixtures before [B] so they cannot interfere.
rm -f "$WIP_SRC" "$WIP_TST"

# --------------------------------------------------------------------------- #
# [B] Deleted-tracked-file immunity
# --------------------------------------------------------------------------- #
# Stage (track) a throwaway module, then delete it from the worktree. git still
# lists it as tracked, so the gate must skip it via the [ -f ] guard instead of
# handing ruff a missing path (E902). CI's checkout at the same commit would
# still have the file, so a local deletion must not red the gate.
cat > "$PROBE" <<'PY'
def _flo95_probe():
	return True
PY
git add "$PROBE" >/dev/null 2>&1 || fail "could not stage deletion probe"
rm -f "$PROBE"

# Precondition: the probe is tracked but absent on disk — exactly the hazard.
git ls-files --error-unmatch "$PROBE" >/dev/null 2>&1 \
	|| fail "deletion probe is not tracked; [B] fixture invalid"
[[ -f "$PROBE" ]] && fail "deletion probe still on disk; [B] fixture invalid"
# And the unguarded form really WOULD trip ruff (E902) — so a passing guarded
# run is meaningful.
if git ls-files -z '*.py' | xargs -0 ruff check --no-cache >/dev/null 2>&1; then
	fail "unguarded tracked-file list did not error on the deleted probe; [B] is non-functional"
fi

echo "[B] tracked-but-deleted file present — running full gate…"
out_b="$(bash "$GATE" 2>&1)" || true

grep -q '^\[1/4\] OK$' <<<"$out_b" \
	|| fail "[1/4] ruff check errored on a deleted tracked file (E902 false red)"
grep -q '^\[2/4\] OK$' <<<"$out_b" \
	|| fail "[2/4] ruff format errored on a deleted tracked file (E902 false red)"
grep -qiE 'E902|No such file' <<<"$out_b" \
	&& fail "gate emitted E902/'No such file' for a deleted tracked file"

# --------------------------------------------------------------------------- #
echo
if [[ $results_ok -eq 1 ]]; then
	echo "PASS: QA gate is immune to untracked WIP and deleted-tracked files (FLO-95)."
	exit 0
else
	echo "FAIL: a gate-hygiene invariant regressed (see above)."
	exit 1
fi
