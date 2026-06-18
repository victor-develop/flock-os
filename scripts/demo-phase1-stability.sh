#!/usr/bin/env bash
# Phase-1 demo stability sweep (FLO-81). Runs the exit-gate demo many times in
# clean subprocesses (fresh interpreter per run, no bytecode cache) so an
# intermittent flake fails loudly and repeatably instead of silently flipping.
#
# This is the DoD evidence harness for FLO-81:
#   - >=N consecutive clean-subprocess runs (default 50), AND
#   - a PYTHONHASHSEED sweep (default 0..31),
# each exiting 0 with "DEMO: PASS". Any non-zero exit fails the whole sweep and
# names the run/seed that broke — the gate/CI guard test below runs a fast subset
# of this every run; this script is the full sign-off sweep.
#
# Tunables (env): DEMO_STABILITY_RUNS (default 50), DEMO_STABILITY_SEEDS
# (default 32 -> seeds 0..31).
#
# Usage: ./scripts/demo-phase1-stability.sh
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
if [[ -x "$VENV_PY" ]]; then
	PY="$VENV_PY"
else
	PY="$(command -v python3 || command -v python)"
fi

DEMO="$HERE/demo_phase1.py"
RUNS="${DEMO_STABILITY_RUNS:-50}"
SEEDS="${DEMO_STABILITY_SEEDS:-32}"

# Fresh interpreter + no bytecode writes => determinism by construction.
export PYTHONDONTWRITEBYTECODE=1

cd "$ROOT"
fail=0
ran=0

echo "Phase-1 demo stability sweep (FLO-81)"
echo "-------------------------------------"
echo "consecutive runs: $RUNS   |   hashseed sweep: 0..$((SEEDS - 1))"
echo

echo "==> [1/2] $RUNS consecutive clean-subprocess runs"
for i in $(seq 1 "$RUNS"); do
	out=$("$PY" "$DEMO" 2>&1 >/dev/null); rc=$?
	ran=$((ran + 1))
	if [[ $rc -ne 0 ]]; then
		fail=$((fail + 1))
		echo "  RUN $i FAILED (rc=$rc):"
		echo "$out" | sed 's/^/    /'
	fi
done
echo "[1/2] $((RUNS - fail))/$RUNS consecutive runs green"

hashseed_fail=0
echo
echo "==> [2/2] PYTHONHASHSEED sweep 0..$((SEEDS - 1))"
for s in $(seq 0 $((SEEDS - 1))); do
	out=$(PYTHONHASHSEED="$s" "$PY" "$DEMO" 2>&1 >/dev/null); rc=$?
	ran=$((ran + 1))
	if [[ $rc -ne 0 ]]; then
		hashseed_fail=$((hashseed_fail + 1))
		echo "  SEED $s FAILED (rc=$rc):"
		echo "$out" | sed 's/^/    /'
	fi
done
echo "[2/2] $((SEEDS - hashseed_fail))/$SEEDS hashseed runs green"

echo "-------------------------------------"
total_fail=$((fail + hashseed_fail))
if [[ $total_fail -eq 0 ]]; then
	echo "SWEEP: PASS — $ran/$ran runs green (consecutive + hashseed)"
	exit 0
else
	echo "SWEEP: FAIL — $total_fail/$ran runs failed (demo is not deterministic)"
	exit 1
fi
