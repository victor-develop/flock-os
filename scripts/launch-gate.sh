#!/usr/bin/env bash
# Launch go/no-go QA gate — repo-local automation (FLO-354, Phase 6.2).
#
# This is the launch-readiness analog of scripts/qa-gate.sh. Where qa-gate.sh is
# the pre-MERGE code gate (ruff/pytest/coverage), this is the pre-LAUNCH gate:
# it machine-checks every go/no-go criterion that has repo-local evidence, and
# prints the human/cloud/board-gated items as explicit MANUAL reminders so the
# script's output is the complete decision picture.
#
# The human sign-off checklist itself lives in
# docs/operations/launch-go-no-go.md (FLO-332). This script does NOT duplicate
# that checklist — it automates the subset a script can prove, and references
# the doc for everything that needs a signature. The two artifacts are paired:
# green here + the four signatures in the doc == GO.
#
# Design rules (inherited from qa-gate.sh, FLO-21/FLO-89/FLO-95):
#   - DRY: reuses scripts/qa-gate.sh as a sub-gate rather than re-implementing it.
#   - Release-tree truth: artifact presence is checked against the COMMITTED
#     tree (git-tracked), because a promotion/tag checkout only ever has tracked
#     files. An artifact that exists only as untracked WIP in one worktree will
#     be ABSENT at deploy time, so it is surfaced as UNTRACKED (a real launch
#     risk), not silently accepted. (CI-parity with qa-gate.sh's tracked model.)
#
# Checks (each is a [N/M] step; ANY automated failure -> exit 1):
#   [1/5] qa merge gate    — scripts/qa-gate.sh green (lint + tests + coverage).
#   [2/5] artifact presence — launch-critical docs + scripts committed & non-stub.
#   [3/5] coverage floor    — pyproject fail_under >= 80 (launch coverage bar).
#   [4/5] migration debt    — migration runbook documents --skip-failing debt
#                              handling (no-go condition #4: debt must be recorded).
#   [5/5] realtime tier     — socketio scale tooling present (no-go #3: a collapsed
#                              single-process tier will not carry 15k WS).
#
# After the automated steps, MANUAL-ONLY items (signatures, launch partner,
# budget, cloud smokes, edge rate-limit, dedicated Redis, real restore drill)
# are printed. These are NOT failures — they are the human/cloud/board surface
# that no repo-local script can prove. They map 1:1 to the no-go conditions in
# docs/operations/launch-go-no-go.md.
#
# Exit codes:
#   0 — every automated check passed (manual sign-off in the doc still required).
#   1 — at least one automated check failed; launch is NO-GO until resolved.
#   2 — environment error (qa-gate.sh missing, pyproject unreadable).
#
# Usage: scripts/launch-gate.sh
#        FLOCK_LAUNCH_GATE_SKIP_QA=1 scripts/launch-gate.sh  # skip qa sub-gate
#        FLOCK_LAUNCH_GATE_ROOT=/tmp/tree scripts/launch-gate.sh  # hygiene test

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
# Test hook: hygiene test points this at a temp replica of the artifact tree so
# it never renames/disturbs real files in the shared worktree (FLO-95 pattern).
GATE_ROOT="${FLOCK_LAUNCH_GATE_ROOT:-$ROOT}"

cd "$ROOT"
fail=0
echo "Launch go/no-go QA gate (FLO-354)"
echo "---------------------------------"

# Is GATE_ROOT inside a git working tree? When yes, artifact presence is checked
# against the tracked tree (release truth). Temp test roots that are not git
# repos skip the tracking check and validate existence + non-stub only.
in_git_tree() {
	[[ "$GATE_ROOT" == "$ROOT" ]] && return 0
	git -C "$GATE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1
}

# A doc is a stub if missing, empty, has < 10 substantive lines, or every
# non-blank line is a placeholder/todo marker. The marker rule is exact: it only
# fires when the WHOLE body is markers, so a 139-line runbook that mentions
# "placeholder" once in prose is NOT a stub (false-positive proven in review).
is_stub_doc() {
	local f="$1"
	[[ -f "$f" ]] || return 0
	[[ -s "$f" ]] || return 0
	local n markers
	n=$(grep -cE '^[[:space:]]*[^[:space:]]' "$f" || true)
	[[ "${n:-0}" -lt 10 ]] && return 0
	markers=$(grep -cEi '^[[:space:]]*(TEST PLACEHOLDER|PLACEHOLDER|TODO|TBD|FIXME|FILL[[:space:]])' "$f" || true)
	if [[ "${n:-0}" -gt 0 && "${markers:-0}" -eq "${n:-0}" ]]; then
		return 0
	fi
	return 1
}

# A script is a stub only if missing or empty (an empty script can't run).
is_stub_script() {
	local f="$1"
	[[ -f "$f" ]] && [[ -s "$f" ]] && return 1
	return 0
}

# Classify an artifact as OK / MISSING / STUB / UNTRACKED on stdout.
# UNTRACKED means: present on disk but not git-tracked — it will be absent at a
# promotion/tag checkout, so it is a launch risk that must be committed first.
classify() {
	local rel="$1"
	local abs="$GATE_ROOT/$rel"
	if [[ ! -e "$abs" ]]; then
		printf 'MISSING'
		return
	fi
	if [[ "$rel" == *.md ]]; then
		if is_stub_doc "$abs"; then printf 'STUB'; return; fi
	else
		if is_stub_script "$abs"; then printf 'STUB'; return; fi
	fi
	if in_git_tree && ! git -C "$GATE_ROOT" ls-files --error-unmatch "$rel" >/dev/null 2>&1; then
		printf 'UNTRACKED'
		return
	fi
	printf 'OK'
}

# [1/5] qa merge gate (reused, not re-implemented — DRY).
echo "==> [1/5] qa merge gate (scripts/qa-gate.sh — lint + tests + coverage ratchet)"
if [[ "${FLOCK_LAUNCH_GATE_SKIP_QA:-0}" == "1" ]]; then
	echo "    (skipped: FLOCK_LAUNCH_GATE_SKIP_QA=1 — hygiene test mode)"
	echo "[1/5] SKIP"
else
	QA="$ROOT/scripts/qa-gate.sh"
	if [[ ! -f "$QA" ]]; then
		echo "FAIL: $QA not found" >&2
		exit 2
	fi
	if bash "$QA"; then
		echo "[1/5] OK"
	else
		echo "[1/5] FAIL — qa merge gate is red; launch cannot promote a red tree"
		fail=1
	fi
fi

# [2/5] launch-critical artifacts committed & non-stub.
echo "==> [2/5] launch-critical artifacts committed & non-stub"

# Docs that must exist with substantive content (FLO-332 checklist prerequisites).
LAUNCH_DOCS=(
	"docs/operations/launch-go-no-go.md"
	"docs/operations/migration-runbook.md"
	"docs/operations/backup-restore.md"
	"docs/security/permission-audit.md"
	"docs/development/staging-preflight-checklist.md"
	"docs/development/deploy-runbook.md"
)
# Scripts that the launch depends on (restore drill, realtime tier, deploy).
LAUNCH_SCRIPTS=(
	"scripts/qa-gate.sh"
	"scripts/dev/restore-drill.sh"
	"scripts/dev/scale-socketio.sh"
	"scripts/dev/backup.sh"
	"scripts/dev/restore.sh"
	"scripts/deploy/smoke-staging.sh"
	"scripts/deploy/rollback.sh"
	"scripts/deploy/render-config.sh"
	"scripts/deploy/render-secrets.sh"
)

artifacts_fail=0
for rel in "${LAUNCH_DOCS[@]}"; do
	verdict="$(classify "$rel")"
	if [[ "$verdict" != "OK" ]]; then
		echo "    doc ($verdict): $rel"
		artifacts_fail=1
	fi
done
for rel in "${LAUNCH_SCRIPTS[@]}"; do
	verdict="$(classify "$rel")"
	if [[ "$verdict" != "OK" ]]; then
		echo "    script ($verdict): $rel"
		artifacts_fail=1
	fi
done
if [[ $artifacts_fail -eq 0 ]]; then
	echo "    all ${#LAUNCH_DOCS[@]} docs + ${#LAUNCH_SCRIPTS[@]} scripts committed & non-stub"
	echo "[2/5] OK"
else
	echo "[2/5] FAIL — UNTRACKED = commit before launch; MISSING/STUB = author the artifact"
	fail=1
fi

# [3/5] configured coverage floor meets the launch bar (>= 80).
# qa-gate.sh enforces this at runtime; this step asserts the *configured* floor
# was not silently lowered beneath the launch bar in pyproject.toml.
echo "==> [3/5] coverage floor (pyproject fail_under >= 80 — launch coverage bar)"
PP="$GATE_ROOT/pyproject.toml"
if [[ ! -f "$PP" ]]; then
	echo "FAIL: $PP not found" >&2
	exit 2
fi
floor=$(grep -E '^[[:space:]]*fail_under[[:space:]]*=' "$PP" | head -1 | sed -E 's/.*=[[:space:]]*([0-9]+).*/\1/')
if [[ -z "${floor:-}" ]]; then
	echo "    no fail_under configured"
	echo "[3/5] FAIL — coverage ratchet floor is unset"
	fail=1
elif [[ "$floor" -lt 80 ]]; then
	echo "    fail_under = $floor (< 80)"
	echo "[3/5] FAIL — coverage floor lowered below the launch bar (80)"
	fail=1
else
	echo "    fail_under = $floor"
	echo "[3/5] OK"
fi

# [4/5] migration debt handling is documented (no-go #4: --skip-failing debt must
# be recorded with a follow-up owner). The migration runbook must address the
# escape hatch; an undocumented one is the silent-corruption no-go condition.
echo "==> [4/5] migration --skip-failing debt handling documented"
MR="$GATE_ROOT/docs/operations/migration-runbook.md"
if [[ -f "$MR" ]] && grep -qi -- '--skip-failing' "$MR"; then
	echo "[4/5] OK"
else
	echo "[4/5] FAIL — migration runbook does not document --skip-failing debt handling"
	fail=1
fi

# [5/5] realtime tier scale tooling present (no-go #3: a collapsed single-process
# socketio tier will not carry 15k WS). The scale script + redis-adapter wiring
# are the repo-local evidence the tier can be expanded to N processes.
echo "==> [5/5] realtime socketio tier scale tooling present"
SS="$(classify "scripts/dev/scale-socketio.sh")"
SA="$(classify "scripts/dev/wire-socketio-redis-adapter.sh")"
if [[ "$SS" == "OK" && "$SA" == "OK" ]]; then
	echo "[5/5] OK"
else
	echo "[5/5] FAIL — socketio scale ($SS) / redis-adapter ($SA) tooling not committed"
	fail=1
fi

echo "---------------------------------"
echo "MANUAL-ONLY items (not failures — require a human/cloud/board):"
echo "  - Sign-off block: all four signatures (DevOps/QA/CEO/Architect) — no-go #8"
echo "  - Launch partner named + onboarded — no-go #7 + docs/operations/launch-go-no-go.md §3"
echo "  - Hosting budget approved through launch — §3"
echo "  - Staging smoke SMOKE: PASS at the promoted tag — no-go #2 (cloud)"
echo "  - Restore drill green against a REAL backup — no-go #1 (real run)"
echo "  - Edge rate-limit active on register + realtime-connect — no-go #5 (Cloudflare)"
echo "  - Dedicated adapter Redis in prod — no-go #6 (FLO-245)"
echo "  Fill these in the sign-off block of docs/operations/launch-go-no-go.md."
echo "---------------------------------"

if [[ $fail -eq 0 ]]; then
	echo "LAUNCH GATE: PASS (automated) — manual sign-off still required for GO"
	exit 0
else
	echo "LAUNCH GATE: FAIL — resolve the automated failures above before re-walking the gate"
	exit 1
fi
