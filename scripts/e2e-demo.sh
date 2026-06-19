#!/usr/bin/env bash
# Phase-5.1b end-to-end MVP demo runner (FLO-224 / FLO-221 deliverable #1).
# One-command, reproducible — the FLO-1 close-trigger evidence.
#
# Drives the real flock_os domain services (traversal, permissions, approvals,
# registrations, scheduling, reporting, engagement, notifications, events) over
# an in-memory multi-branch world — no Frappe bench needed. Exits non-zero if
# any North-Star step fails, so this is a demo AND a gate. The project-level
# gate test (flock_os/tests/test_e2e_demo.py) loads the same script in-process;
# this runner is the standalone sign-off evidence.
#
# Usage: ./scripts/e2e-demo.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"

if [[ -x "$VENV_PY" ]]; then
	PY="$VENV_PY"
else
	PY="$(command -v python3 || command -v python)"
fi

# Determinism guard (mirrors demo-phase1.sh / FLO-81): never leave stale bytecode
# behind for the next process to load. Each `exec` is a fresh interpreter.
export PYTHONDONTWRITEBYTECODE=1

exec "$PY" "$HERE/e2e_demo.py" "$@"
