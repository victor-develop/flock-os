#!/usr/bin/env bash
# Phase-1 exit-gate demo runner (FLO-52). One-command, reproducible.
#
# Drives the real flock_os spine (TreeTraversalService + the permissions
# scoping API) over an in-memory multi-branch world — no Frappe bench needed.
# Exits non-zero if any Phase-1 guarantee fails, so this is a demo AND a gate.
#
# Usage: ./scripts/demo-phase1.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"

if [[ -x "$VENV_PY" ]]; then
	PY="$VENV_PY"
else
	PY="$(command -v python3 || command -v python)"
fi

exec "$PY" "$HERE/demo_phase1.py" "$@"
