#!/usr/bin/env bash
#
# Regression test for the launch go/no-go gate's automation (FLO-354).
#
# Mirrors the contract of scripts/test_qa_gate_hygiene.sh (FLO-95): it drives
# the REAL scripts/launch-gate.sh against a temporary replica of the artifact
# tree, so it never renames, deletes, or edits a real file in the shared
# worktree. Every invariant that could regress the gate is asserted end-to-end.
#
# Invariants locked here:
#   [A] Green tree — all artifacts present + non-stub, coverage floor 80, debt
#       documented, tier tooling present -> gate PASSes (exit 0).
#   [B] Missing artifact — one required doc removed -> gate FAILs.
#   [C] Stub doc — a launch doc replaced with a PLACEHOLDER body -> gate FAILs.
#   [D] Coverage floor lowered — pyproject fail_under < 80 -> gate FAILs.
#   [E] Migration debt undocumented — runbook without --skip-failing -> gate FAILs.
#   [F] Tier tooling missing — socketio scale script removed -> gate FAILs.
#
# The qa sub-gate is skipped (FLOCK_LAUNCH_GATE_SKIP_QA=1) so this test is fast
# and scoped to the launch-gate's own logic; qa-gate.sh has its own hygiene test.
#
# Usage: scripts/test_launch_gate_hygiene.sh
# Exits 0 only if every invariant holds.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

GATE="$ROOT/scripts/launch-gate.sh"
[[ -f "$GATE" ]] || { echo "FAIL: $GATE not found"; exit 2; }

TMP="$(mktemp -d -t flock_launch_gate.XXXXXX)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# Build a faithful replica of the launch artifact tree under $TMP/root.
build_tree() {
	local root="$1"
	mkdir -p "$root/docs/operations" "$root/docs/security" "$root/docs/development"
	mkdir -p "$root/scripts/dev" "$root/scripts/deploy"
	# Real, non-stub docs.
	for d in docs/operations/launch-go-no-go.md docs/operations/migration-runbook.md \
		docs/operations/backup-restore.md docs/security/permission-audit.md \
		docs/development/staging-preflight-checklist.md docs/development/deploy-runbook.md; do
		{
			echo "# $(basename "$d")"
			echo
			echo "Launch-critical runbook content."
			echo
			echo "Line three of substantive body."
			echo "Line four of substantive body."
			echo "Line five of substantive body."
			echo "Line six of substantive body."
			echo "Line seven of substantive body."
			echo "Line eight of substantive body."
			echo "Line nine of substantive body."
			echo "Line ten of substantive body."
		} > "$root/$d"
	done
	# The migration runbook must mention --skip-failing (debt handling).
	grep -qi -- '--skip-failing' "$root/docs/operations/migration-runbook.md" \
		|| printf '\n## --skip-failing\n\nDebt is recorded with a follow-up issue.\n' \
			>> "$root/docs/operations/migration-runbook.md"
	# Real, non-empty scripts.
	for s in scripts/qa-gate.sh scripts/dev/restore-drill.sh scripts/dev/scale-socketio.sh \
		scripts/dev/backup.sh scripts/dev/restore.sh scripts/dev/wire-socketio-redis-adapter.sh \
		scripts/deploy/smoke-staging.sh scripts/deploy/rollback.sh scripts/deploy/render-config.sh \
		scripts/deploy/render-secrets.sh; do
		echo "#!/usr/bin/env bash" > "$root/$s"
	done
	# Coverage floor at the launch bar.
	printf '[tool.coverage.report]\nfail_under = 80\n' > "$root/pyproject.toml"
}

results_ok=1
fail() { echo "FAIL: $*" >&2; results_ok=0; }

# Return only the gate's exit code for a given root (output is asserted
# separately via the out_* captures, so suppress stdout/stderr here).
run_gate() {
	local root="$1"
	FLOCK_LAUNCH_GATE_ROOT="$root" FLOCK_LAUNCH_GATE_SKIP_QA=1 bash "$GATE" >/dev/null 2>&1
	printf '%s' "$?"
}

# --------------------------------------------------------------------------- #
# [A] Green tree -> PASS
# --------------------------------------------------------------------------- #
echo "[A] green tree — building replica and expecting PASS…"
build_tree "$TMP/root_a"
out_a="$(FLOCK_LAUNCH_GATE_ROOT="$TMP/root_a" FLOCK_LAUNCH_GATE_SKIP_QA=1 bash "$GATE" 2>&1)" || true
rc_a="$(run_gate "$TMP/root_a")"
grep -q '^\[1/5\] SKIP$' <<<"$out_a" || fail "[A] qa step not skipped in test mode"
grep -q '^\[2/5\] OK$' <<<"$out_a" || fail "[A] artifact step should pass on a full tree"
grep -q '^\[3/5\] OK$' <<<"$out_a" || fail "[A] coverage-floor step should pass at 80"
grep -q '^\[4/5\] OK$' <<<"$out_a" || fail "[A] migration-debt step should pass when documented"
grep -q '^\[5/5\] OK$' <<<"$out_a" || fail "[A] realtime-tier step should pass with tooling present"
grep -q 'LAUNCH GATE: PASS' <<<"$out_a" || fail "[A] gate reported non-PASS on a green tree"
[[ "$rc_a" == "0" ]] || fail "[A] exit code was $rc_a, expected 0"

# --------------------------------------------------------------------------- #
# [B] Missing artifact -> FAIL
# --------------------------------------------------------------------------- #
echo "[B] missing doc — expecting FAIL…"
rm -f "$TMP/root_a/docs/security/permission-audit.md"
out_b="$(FLOCK_LAUNCH_GATE_ROOT="$TMP/root_a" FLOCK_LAUNCH_GATE_SKIP_QA=1 bash "$GATE" 2>&1)" || true
rc_b="$(run_gate "$TMP/root_a")"
grep -qE 'doc \(MISSING\): docs/security/permission-audit.md' <<<"$out_b" \
	|| fail "[B] missing doc not reported"
grep -q '^\[2/5\] FAIL' <<<"$out_b" || fail "[B] artifact step should fail on a missing doc"
grep -q 'LAUNCH GATE: FAIL' <<<"$out_b" || fail "[B] gate reported non-FAIL on a missing doc"
[[ "$rc_b" != "0" ]] || fail "[B] exit code was 0, expected non-zero"
# Restore for subsequent checks.
build_tree "$TMP/root_a"

# --------------------------------------------------------------------------- #
# [C] Stub doc -> FAIL
# --------------------------------------------------------------------------- #
echo "[C] stub doc (PLACEHOLDER body) — expecting FAIL…"
printf 'PLACEHOLDER\n' > "$TMP/root_a/docs/operations/backup-restore.md"
out_c="$(FLOCK_LAUNCH_GATE_ROOT="$TMP/root_a" FLOCK_LAUNCH_GATE_SKIP_QA=1 bash "$GATE" 2>&1)" || true
rc_c="$(run_gate "$TMP/root_a")"
grep -qE 'doc \(STUB\): docs/operations/backup-restore.md' <<<"$out_c" \
	|| fail "[C] stub doc not reported"
grep -q '^\[2/5\] FAIL' <<<"$out_c" || fail "[C] artifact step should fail on a stub doc"
[[ "$rc_c" != "0" ]] || fail "[C] exit code was 0, expected non-zero"
build_tree "$TMP/root_a"

# --------------------------------------------------------------------------- #
# [D] Coverage floor < 80 -> FAIL
# --------------------------------------------------------------------------- #
echo "[D] coverage floor 70 — expecting FAIL…"
printf '[tool.coverage.report]\nfail_under = 70\n' > "$TMP/root_a/pyproject.toml"
out_d="$(FLOCK_LAUNCH_GATE_ROOT="$TMP/root_a" FLOCK_LAUNCH_GATE_SKIP_QA=1 bash "$GATE" 2>&1)" || true
rc_d="$(run_gate "$TMP/root_a")"
grep -q '^\[3/5\] FAIL' <<<"$out_d" || fail "[D] coverage-floor step should fail below 80"
grep -q 'fail_under = 70' <<<"$out_d" || fail "[D] lowered floor value not echoed"
[[ "$rc_d" != "0" ]] || fail "[D] exit code was 0, expected non-zero"
printf '[tool.coverage.report]\nfail_under = 80\n' > "$TMP/root_a/pyproject.toml"

# --------------------------------------------------------------------------- #
# [E] Migration debt undocumented -> FAIL
# --------------------------------------------------------------------------- #
echo "[E] migration debt undocumented — expecting FAIL…"
build_tree "$TMP/root_e"
	# Strip the --skip-failing mention to simulate an undocumented escape hatch.
grep -vi -- '--skip-failing' "$TMP/root_e/docs/operations/migration-runbook.md" \
	> "$TMP/root_e/docs/operations/migration-runbook.md.tmp" || true
mv "$TMP/root_e/docs/operations/migration-runbook.md.tmp" \
	"$TMP/root_e/docs/operations/migration-runbook.md"
out_e="$(FLOCK_LAUNCH_GATE_ROOT="$TMP/root_e" FLOCK_LAUNCH_GATE_SKIP_QA=1 bash "$GATE" 2>&1)" || true
rc_e="$(run_gate "$TMP/root_e")"
grep -q '^\[4/5\] FAIL' <<<"$out_e" || fail "[E] migration-debt step should fail when undocumented"
[[ "$rc_e" != "0" ]] || fail "[E] exit code was 0, expected non-zero"

# --------------------------------------------------------------------------- #
# [F] Realtime tier tooling missing -> FAIL
# --------------------------------------------------------------------------- #
echo "[F] socketio scale tooling missing — expecting FAIL…"
rm -f "$TMP/root_a/scripts/dev/scale-socketio.sh"
out_f="$(FLOCK_LAUNCH_GATE_ROOT="$TMP/root_a" FLOCK_LAUNCH_GATE_SKIP_QA=1 bash "$GATE" 2>&1)" || true
rc_f="$(run_gate "$TMP/root_a")"
grep -q '^\[5/5\] FAIL' <<<"$out_f" || fail "[F] realtime-tier step should fail with tooling missing"
[[ "$rc_f" != "0" ]] || fail "[F] exit code was 0, expected non-zero"

# --------------------------------------------------------------------------- #
# [G] Untracked-but-present artifact -> FAIL (release-tree truth)
# --------------------------------------------------------------------------- #
# An artifact on disk but not git-tracked will be ABSENT at a promotion/tag
# checkout. The gate must surface it as UNTRACKED, not silently accept it.
echo "[G] untracked artifact (git tree) — expecting FAIL…"
git init -q "$TMP/root_g" 2>/dev/null
build_tree "$TMP/root_g"
git -C "$TMP/root_g" -c user.email=t@t -c user.name=t add -A >/dev/null 2>&1
git -C "$TMP/root_g" -c user.email=t@t -c user.name=t commit -q -m init >/dev/null 2>&1
# Remove one doc from tracking but leave it on disk -> untracked.
git -C "$TMP/root_g" rm -q --cached docs/security/permission-audit.md >/dev/null 2>&1
[[ -f "$TMP/root_g/docs/security/permission-audit.md" ]] \
	|| fail "[G] precondition: doc should still be on disk after rm --cached"
out_g="$(FLOCK_LAUNCH_GATE_ROOT="$TMP/root_g" FLOCK_LAUNCH_GATE_SKIP_QA=1 bash "$GATE" 2>&1)" || true
rc_g="$(run_gate "$TMP/root_g")"
grep -qE 'doc \(UNTRACKED\): docs/security/permission-audit.md' <<<"$out_g" \
	|| fail "[G] untracked doc not classified UNTRACKED"
grep -q '^\[2/5\] FAIL' <<<"$out_g" || fail "[G] artifact step should fail on an untracked doc"
[[ "$rc_g" != "0" ]] || fail "[G] exit code was 0, expected non-zero"

# --------------------------------------------------------------------------- #
echo
if [[ $results_ok -eq 1 ]]; then
	echo "PASS: launch gate automation invariants hold (FLO-354)."
	exit 0
else
	echo "FAIL: a launch-gate invariant regressed (see above)."
	exit 1
fi
