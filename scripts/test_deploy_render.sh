#!/usr/bin/env bash
#
# Minimal behavioral test for the flock-os-bench config/secret rendering path
# (FLO-672 Phase 6.1 no-board).
#
# The unit-of-deploy is the `flock-os-bench` container image (deploy/Dockerfile);
# it contains ZERO secrets. At container start, deploy/entrypoint.sh runs
# scripts/deploy/render-config.sh which renders site_config.json +
# common_site_config.json from environment, and scripts/deploy/render-secrets.sh
# decrypts the SOPS+age bundle into that environment. This test proves that
# rendering path works WITHOUT the board-gated cloud VM (the "(no-board)" slice):
#
#   Section 1 — render-config.sh (always runs; hermetic synthetic env):
#     [A] missing-secret fail-fast (--check names the missing var, exit != 0)
#     [B] all-present --check passes (exit 0)
#     [C] render -> valid JSON, _comment stripped, 0600 perms, secrets
#         substituted, db_port is a JSON number
#     [D] portability: both the envsubst path AND the pure-bash fallback render
#         byte-correct JSON (FLOCK_RENDER_FORCE_BASH_SUBST=1 exercises the
#         fallback on hosts that DO have envsubst, e.g. CI)
#     [E] --print-env redacts (never emits a raw secret value)
#
#   Section 2 — render-secrets.sh (SKIP if no age key / bundles; CI has neither,
#   the local Mac + the deploy runner do):
#     [F] --check passes for staging (real bundle decrypts, required keys present)
#     [G] --print-env redacts (never emits a raw secret value)
#     [H] composition: render-secrets --eval -> render-config renders valid config
#
#   Section 3 — image + template artifacts (always runs):
#     [I] deploy/Dockerfile + docker/Dockerfile + templates + entrypoints exist
#         and are non-stub; the `flock-os-bench` image tag is referenced
#         consistently across deploy.yml + rollback.sh + the runbook.
#
# The test drives the REAL scripts (no re-implementation) so a regression in the
# render logic itself is caught. It is hermetic: temp dirs + synthetic env +
# cleanup traps; it never writes a secret to a tracked path.
#
# Usage: scripts/test_deploy_render.sh
# Exits 0 only if every invariant holds (SOPS section SKIPs do not fail the run).

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
cd "$ROOT"

RENDER_CONFIG="$ROOT/scripts/deploy/render-config.sh"
RENDER_SECRETS="$ROOT/scripts/deploy/render-secrets.sh"
[[ -f "$RENDER_CONFIG" ]] || { echo "FAIL: $RENDER_CONFIG not found" >&2; exit 2; }
[[ -f "$RENDER_SECRETS" ]] || { echo "FAIL: $RENDER_SECRETS not found" >&2; exit 2; }

command -v jq >/dev/null 2>&1 || { echo "FAIL: jq is required (render-config.sh dep)" >&2; exit 2; }

TMP="$(mktemp -d -t flock_deploy_render.XXXXXX)"
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT

# Synthetic, non-secret env that satisfies render-config.sh's REQUIRED_VARS.
# These are PUBLIC test fixtures (not real secrets) — safe to assert on.
full_env() {
	cat <<ENV
DB_HOST=db.staging.example
DB_PORT=3306
DB_NAME=flock_os
DB_USER=flock
DB_PASSWORD=test-fixture-password-not-real
REDIS_CACHE_URI=redis://cache:6379
REDIS_QUEUE_URI=redis://queue:6379
REDIS_SOCKETIO_URI=redis://socketio:6379
FLOCK_SIO_ADAPTER_REDIS=redis://adapter:6379
SECRET_KEY=test-fixture-secret-key-not-real
SITE_URL=https://staging.flock.os
FLOCK_ENV=staging
FLOCK_SIO_PROCESSES=6
MUTE_EMAILS=0
ENV
}

# Run render-config.sh in a subshell with the synthetic env EXPORTED. \$1 is a
# snippet evaluated under `set -a` before the script (use it to drop a var or
# force the fallback renderer); remaining args are render-config flags.
run_render_config() {
	local pre="$1"; shift
	(
		set -a
		eval "$(full_env)"
		eval "$pre"
		set +a
		bash "$RENDER_CONFIG" "$@" 2>&1
	)
}

results_ok=1
fails=()
pass() { echo "  [$1] OK"; }
fail() { echo "  [$1] FAIL: $2" >&2; fails+=("[$1] $2"); results_ok=0; }

echo "FLO-672 — flock-os-bench config/secret rendering (minimal test)"
echo

# --------------------------------------------------------------------------- #
# Section 1 — render-config.sh (hermetic, always runs)
# --------------------------------------------------------------------------- #
echo "Section 1 — render-config.sh (config rendering from env):"

# [A] Missing-secret fail-fast. Drop DB_PASSWORD; --check must exit non-zero and
# name it. This is the zero-secrets-in-repo gate: a half-rendered config must
# never reach disk.
echo "[A] missing-secret fail-fast on --check…"
rc_a=0; out_a="$(run_render_config 'unset DB_PASSWORD' --check)" || rc_a=$?
[[ "$rc_a" -ne 0 ]] && grep -q 'DB_PASSWORD' <<<"$out_a" \
	&& pass A || fail A "expected non-zero exit naming DB_PASSWORD (rc=$rc_a)"

# [B] All-present --check passes.
echo "[B] all-present --check passes…"
rc_b=0; out_b="$(run_render_config '' --check)" || rc_b=$?
[[ "$rc_b" -eq 0 ]] && grep -q -- '--check OK' <<<"$out_b" \
	&& pass B || fail B "expected exit 0 + '--check OK' (rc=$rc_b, out=$out_b)"

# [C] Render -> valid JSON, _comment stripped, 0600 perms, secrets substituted,
# db_port is a JSON number (not a string). This is the core render correctness
# assertion.
echo "[C] render produces valid 0600 JSON with secrets substituted…"
SITES_C="$TMP/sites-c"; rc_c=0; out_c="$(run_render_config '' --sites-dir "$SITES_C" --site flock_os)" || rc_c=$?
common_c="$SITES_C/common_site_config.json"
site_c="$SITES_C/flock_os/site_config.json"
if [[ "$rc_c" -eq 0 && -f "$common_c" && -f "$site_c" ]]; then
	jq -e . "$common_c" >/dev/null 2>&1 && jq -e . "$site_c" >/dev/null 2>&1 \
		|| { fail C "rendered config is not valid JSON"; }
	# _comment stripped from both.
	grep -q _comment "$common_c" "$site_c" 2>/dev/null \
		&& fail C "_comment field was not stripped" || true
	# 0600 perms (owner-read/write only). stat -c on Linux, -f on macOS.
	perm_of() { stat -f '%Lp' "$1" 2>/dev/null || stat -c '%a' "$1" 2>/dev/null; }
	[[ "$(perm_of "$common_c")" == "600" ]] || fail C "common_site_config perms=$(perm_of "$common_c") expected 600"
	[[ "$(perm_of "$site_c")" == "600" ]] || fail C "site_config perms=$(perm_of "$site_c") expected 600"
	# db_port rendered as a JSON NUMBER (unquoted), not a string.
	[[ "$(jq -r '.db_port | type' "$common_c")" == "number" ]] \
		|| fail C "common db_port is not a number: $(jq '.db_port' "$common_c")"
	[[ "$(jq -r '.db_port | type' "$site_c")" == "number" ]] \
		|| fail C "site db_port is not a number: $(jq '.db_port' "$site_c")"
	# Secret substitution: db_password == the env value; host_name == SITE_URL.
	[[ "$(jq -r '.db_password' "$site_c")" == "test-fixture-password-not-real" ]] \
		|| fail C "db_password not substituted: $(jq '.db_password' "$site_c")"
	[[ "$(jq -r '.host_name' "$site_c")" == "https://staging.flock.os" ]] \
		|| fail C "host_name not substituted"
	# Default expansion: FLOCK_SIO_PROCESSES=6 flowed into the scaled-socketio block.
	[[ "$(jq -r '.flock_os.scaled_socketio.processes' "$common_c")" == "6" ]] \
		|| fail C "FLOCK_SIO_PROCESSES default not rendered"
	# If we got here with no fail, the section is OK (avoid double-reporting).
	[[ "$results_ok" -eq 1 ]] && pass C || true
else
	fail C "render did not produce both configs (rc=$rc_c, out=$out_c)"
fi

# [D] Portability: the pure-bash fallback must render byte-correct JSON too. The
# fallback runs when envsubst is absent (this Mac) OR when
# FLOCK_RENDER_FORCE_BASH_SUBST=1 forces it (CI, where gettext is installed). We
# render via BOTH paths and assert they produce identical, valid JSON.
echo "[D] envsubst + bash fallback produce identical valid JSON…"
SITES_D1="$TMP/sites-d1"; SITES_D2="$TMP/sites-d2"
run_render_config 'export FLOCK_RENDER_FORCE_BASH_SUBST=1' \
	--sites-dir "$SITES_D1" --site flock_os >/dev/null 2>&1 \
	|| fail D "bash-fallback render failed"
run_render_config '' --sites-dir "$SITES_D2" --site flock_os >/dev/null 2>&1 \
	|| fail D "default-path render failed"
if [[ -f "$SITES_D1/flock_os/site_config.json" && -f "$SITES_D2/flock_os/site_config.json" ]]; then
	# Both must be valid JSON.
	jq -e . "$SITES_D1/common_site_config.json" >/dev/null 2>&1 \
		&& jq -e . "$SITES_D1/flock_os/site_config.json" >/dev/null 2>&1 \
		|| fail D "bash-fallback produced invalid JSON"
	# Byte-identical after canonical jq (key order is preserved by the template).
	if diff -q \
		<(jq -S . "$SITES_D1/common_site_config.json") \
		<(jq -S . "$SITES_D2/common_site_config.json") >/dev/null 2>&1 \
		&& diff -q \
		<(jq -S . "$SITES_D1/flock_os/site_config.json") \
		<(jq -S . "$SITES_D2/flock_os/site_config.json") >/dev/null 2>&1; then
		pass D
	else
		fail D "envsubst and bash-fallback outputs differ"
	fi
else
	fail D "one or both renders missing a site_config"
fi

# [E] --print-env redacts: never emits the raw secret value, only <set,…>.
echo "[E] --print-env redacts raw secrets…"
rc_e=0; out_e="$(run_render_config '' --print-env)" || rc_e=$?
if [[ "$rc_e" -eq 0 ]]; then
	grep -q 'test-fixture-password-not-real' <<<"$out_e" \
		&& fail E "--print-env leaked DB_PASSWORD" || true
	grep -q 'test-fixture-secret-key-not-real' <<<"$out_e" \
		&& fail E "--print-env leaked SECRET_KEY" || true
	grep -q 'DB_PASSWORD=<set' <<<"$out_e" \
		|| fail E "--print-env did not report DB_PASSWORD redacted"
	[[ "$results_ok" -eq 1 ]] && pass E || true
else
	fail E "--print-env exited non-zero (rc=$rc_e)"
fi

echo

# --------------------------------------------------------------------------- #
# Section 2 — render-secrets.sh (SKIP if no age key / bundles)
# --------------------------------------------------------------------------- #
# CI does not carry the age private key (it is a deploy-runner secret), so the
# SOPS-decrypt path is exercised only where the key + ciphertext bundles exist.
AGE_KEY=""
if [[ -n "${SOPS_AGE_KEY:-}" ]]; then
	AGE_KEY="<SOPS_AGE_KEY env>"
elif [[ -f "$ROOT/secrets/.age-key" ]]; then
	AGE_KEY="$ROOT/secrets/.age-key"
fi
command -v sops >/dev/null 2>&1 || AGE_KEY=""

if [[ -z "$AGE_KEY" ]] || [[ ! -f "$ROOT/secrets/staging.enc.yaml" ]]; then
	echo "Section 2 — render-secrets.sh: SKIP"
	echo "  (no age key + staging bundle on this host; the SOPS-decrypt path"
	echo "   runs on the local Mac + the deploy runner, not in CI.)"
	echo
else
	echo "Section 2 — render-secrets.sh (SOPS+age decrypt, key=$AGE_KEY):"
	export SOPS_AGE_KEY_FILE="$ROOT/secrets/.age-key"
	[[ -n "${SOPS_AGE_KEY:-}" ]] && unset SOPS_AGE_KEY_FILE

	# [F] --check passes for staging.
	echo "[F] staging --check passes…"
	rc_f=0; out_f="$(bash "$RENDER_SECRETS" --env staging --check 2>&1)" || rc_f=$?
	[[ "$rc_f" -eq 0 ]] && grep -q -- '--check OK' <<<"$out_f" \
		&& pass F || fail F "staging --check failed (rc=$rc_f, out=$out_f)"

	# [G] --print-env redacts raw secrets.
	echo "[G] staging --print-env redacts raw secrets…"
	rc_g=0; out_g="$(bash "$RENDER_SECRETS" --env staging --print-env 2>&1)" || rc_g=$?
	if [[ "$rc_g" -eq 0 ]]; then
		# A leaked raw secret would be a bare KEY=<non-redacted> line. Redacted
		# lines look like KEY=<set,N chars>. Assert every required-var line is
		# redacted (no raw value after the =).
		leaked=0
		while IFS= read -r line; do
			case "$line" in
				DB_HOST=*|DB_NAME=*|DB_USER=*|DB_PASSWORD=*|REDIS_*URI=*|\
				FLOCK_SIO_ADAPTER_REDIS=*|SECRET_KEY=*|SITE_URL=*|FLOCK_ENV=*)
					[[ "$line" == *"<set,"* ]] || [[ "$line" == *"(unset)"* ]] \
						|| { leaked=1; echo "    leaked: $line" >&2; }
					;;
			esac
		done <<<"$out_g"
		[[ "$leaked" -eq 0 ]] && pass G || fail G "--print-env leaked a raw secret"
	else
		fail G "staging --print-env exited non-zero (rc=$rc_g)"
	fi

	# [H] Composition: render-secrets --eval | render-config renders valid config.
	# This proves the two scripts compose end-to-end (secrets bundle -> env ->
	# site_config.json), which is the whole "(no-board)" rendering deliverable.
	echo "[H] composition: render-secrets --eval -> render-config renders valid JSON…"
	SITES_H="$TMP/sites-h"
	# Eval the staging secrets into the current shell, then render config from
	# that environment. The render writes to a temp sites dir.
	comp_out=""
	rc_h=0
	comp_out="$(eval "$(bash "$RENDER_SECRETS" --env staging --eval 2>/dev/null)" \
		&& bash "$RENDER_CONFIG" --sites-dir "$SITES_H" --site flock_os 2>&1)" || rc_h=$?
	common_h="$SITES_H/common_site_config.json"; site_h="$SITES_H/flock_os/site_config.json"
	if [[ "$rc_h" -eq 0 && -f "$common_h" && -f "$site_h" ]] \
		&& jq -e . "$common_h" >/dev/null 2>&1 && jq -e . "$site_h" >/dev/null 2>&1; then
		# db_port must still be a number through the full pipeline.
		[[ "$(jq -r '.db_port | type' "$site_h")" == "number" ]] \
			&& pass H || fail H "composition produced non-number db_port"
	else
		fail H "composition render failed (rc=$rc_h, out=$comp_out)"
	fi
	echo
fi

# --------------------------------------------------------------------------- #
# Section 3 — image + template artifacts (always runs)
# --------------------------------------------------------------------------- #
echo "Section 3 — flock-os-bench image + template artifacts:"

# [I] The container image definition, entrypoints, templates, and the
# `flock-os-bench` tag references must all be present and non-stub. A missing or
# stub artifact would silently break the deploy pipeline.
echo "[I] image + templates + tag references present and non-stub…"
artifacts=(
	deploy/Dockerfile deploy/entrypoint.sh deploy/Procfile deploy/supervisord.conf
	deploy/nginx/prod.conf
	deploy/templates/site_config.json.tmpl deploy/templates/common_site_config.json.tmpl
	docker/Dockerfile docker/entrypoint.sh docker/init-site.sh docker/docker-compose.yml
	scripts/deploy/render-config.sh scripts/deploy/render-secrets.sh
)
stub_hit=0
for a in "${artifacts[@]}"; do
	f="$ROOT/$a"
	if [[ ! -s "$f" ]]; then
		fail I "missing/empty artifact: $a"; stub_hit=1
	elif grep -qiE '^(PLACEHOLDER|TODO|STUB)$' "$f"; then
		fail I "stub artifact: $a"; stub_hit=1
	fi
done
# Each template MUST contain at least one ${VAR} expansion (a template with no
# placeholder is a sign the rendering contract was broken).
for t in deploy/templates/site_config.json.tmpl deploy/templates/common_site_config.json.tmpl; do
	grep -qE '\$\{[A-Za-z_]' "$ROOT/$t" \
		|| { fail I "template has no \${VAR} expansion: $t"; stub_hit=1; }
done
# The deploy/Dockerfile + docker/Dockerfile must each reference flock_os +
# entrypoint (they are real image definitions, not stubs).
grep -qi 'flock_os' "$ROOT/deploy/Dockerfile" \
	|| { fail I "deploy/Dockerfile does not reference flock_os"; stub_hit=1; }
grep -qi 'entrypoint' "$ROOT/deploy/Dockerfile" \
	|| { fail I "deploy/Dockerfile has no ENTRYPOINT/COPY entrypoint"; stub_hit=1; }
# The `flock-os-bench` image tag is referenced consistently across the pipeline.
grep -rq 'flock-os-bench' "$ROOT/.github/workflows/deploy.yml" \
	|| fail I "deploy.yml does not reference the flock-os-bench image tag"
grep -rq 'flock-os-bench' "$ROOT/scripts/deploy/rollback.sh" \
	|| fail I "rollback.sh does not reference the flock-os-bench image tag"
grep -rq 'flock-os-bench' "$ROOT/docs/development/deploy-runbook.md" \
	|| fail I "deploy-runbook.md does not reference the flock-os-bench image"
[[ "$stub_hit" -eq 0 ]] && [[ "$results_ok" -eq 1 ]] && pass I || true
echo

# --------------------------------------------------------------------------- #
echo "------------------------------------------------------------------------"
if [[ ${#fails[@]} -gt 0 ]]; then
	echo "FAIL: ${#fails[@]} invariant(s) regressed:"
	for f in "${fails[@]}"; do echo "  $f"; done
	exit 1
fi
echo "PASS: flock-os-bench config/secret rendering invariants hold (FLO-672)."
exit 0
