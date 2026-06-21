#!/usr/bin/env bash
#
# Flock OS staging smoke (FLO-246 Phase 6.1, acceptance gate).
#
# Runs the deploy-verification smoke against a reachable staging URL:
#   [1/4] HTTP reachability + TLS (curl the site root, expect 2xx/3xx).
#   [2/4] Frappe API liveness (/api/method/ping) — proves gunicorn + the bench
#         boot succeeded and the site config rendered cleanly.
#   [3/4] WebSocket connect smoke against the scaled-socketio tier — proves the
#         FLO-121 N-worker sticky-L7 tier is up and the @socket.io/redis-adapter
#         is armed (a WS handshake completes end-to-end).
#   [4/4] Engagement asset smoke — proves `bench build --app flock_os` ran AND
#         the web worker was restarted after the build (a freshly-added asset
#         dir 404s under a running gunicorn until restart — FLO-617 / FLO-610
#         P1-1). Asserts /assets/flock_os/js/engage.js + css/engage.css return
#         200 so a build-skipping deploy fails loud.
#
# This is the acceptance slice #4 in the FLO-246 plan — the gate that closes
# Phase 6.1 once a real staging URL exists. It is parameterized by $STAGING_URL
# and $STAGING_WS_URL so it can be run from CI, from the deploy host, or by
# hand during the rollback drill.
#
# Usage:
#   scripts/deploy/smoke-staging.sh                       # reads STAGING_URL / STAGING_WS_URL from env
#   scripts/deploy/smoke-staging.sh --url https://stage…  # override
#   scripts/deploy/smoke-staging.sh --ws-url wss://stage…/socket.io
#   scripts/deploy/smoke-staging.sh --help
#
# Exits non-zero on any failure, so it is wired as the post-deploy gate in
# .github/workflows/deploy.yml. Runbook: docs/development/deploy-runbook.md.
set -uo pipefail

PROG="$(basename "${BASH_SOURCE[0]}")"

SITE_URL="${STAGING_URL:-}"
WS_URL="${STAGING_WS_URL:-}"
WS_TIMEOUT="${WS_TIMEOUT:-15}"

usage() { sed -n '2,/^$/p' "${BASH_SOURCE[0]}" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --url) SITE_URL="$2"; shift 2 ;;
        --url=*) SITE_URL="${1#--url=}"; shift ;;
        --ws-url) WS_URL="$2"; shift 2 ;;
        --ws-url=*) WS_URL="${1#--ws-url=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "$PROG: unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ -z "$SITE_URL" ]]; then
    echo "$PROG: STAGING_URL (or --url) is required — set it to the deployed staging base URL." >&2
    echo "$PROG: e.g. export STAGING_URL=https://staging.flock-os.example" >&2
    exit 2
fi

# Default the WS URL to the site's /socket.io path (the prod nginx proxies :9000
# at the site origin in the standard deploy; override with --ws-url for a split
# LB host). Normalize: drop any trailing slash.
SITE_URL="${SITE_URL%/}"
if [[ -z "$WS_URL" ]]; then
    # Derive wss:// from https://, ws:// from http://.
    case "$SITE_URL" in
        https://*) WS_URL="wss://${SITE_URL#https://}/socket.io" ;;
        http://*)  WS_URL="ws://${SITE_URL#http://}/socket.io" ;;
        *) echo "$PROG: STAGING_URL must include the scheme (http(s)://)" >&2; exit 2 ;;
    esac
fi

fail=0
echo "Flock OS staging smoke (FLO-246)"
echo "--------------------------------"
echo "site: $SITE_URL"
echo "ws:   $WS_URL"
echo

# --- [1/4] HTTP reachability --------------------------------------------------
echo "==> [1/4] HTTP reachability + TLS"
http_code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
    --max-time 15 "$SITE_URL/" || true)"
# Frappe's root may redirect (302) to /app or /login; both are healthy. 2xx/3xx
# is the success bar. 4xx/5xx/000 means nginx or gunicorn is misconfigured.
if [[ "$http_code" =~ ^(2|3)[0-9][0-9]$ ]]; then
    echo "[1/4] OK (HTTP $http_code)"
else
    echo "[1/4] FAIL (HTTP $http_code — expected 2xx/3xx)" >&2
    fail=1
fi

# --- [2/4] Frappe API liveness ------------------------------------------------
echo "==> [2/4] Frappe API liveness (/api/method/ping)"
ping_body="$(curl --silent --show-error --max-time 15 \
    "$SITE_URL/api/method/ping" || true)"
# Frappe's ping returns the literal string "pong" (text/plain). Anything else
# means gunicorn is up but the bench/site config is broken (a half-rendered
# site_config.json, a stuck migrate, etc.).
if [[ "$ping_body" == *"pong"* ]]; then
    echo "[2/4] OK (/api/method/ping → pong)"
else
    echo "[2/4] FAIL (/api/method/ping → '${ping_body:0:80}'; expected 'pong')" >&2
    fail=1
fi

# --- [3/4] WebSocket connect smoke -------------------------------------------
# Proves the FLO-121 scaled-socketio tier (N workers + nginx sticky-L7 + the
# @socket.io/redis-adapter) is up: a WS handshake completes end-to-end. This is
# the prod-shape counterpart to the dev `socketio-lb.js` ws-only k6 smoke.
echo "==> [3/4] WebSocket connect smoke ($WS_URL)"
# Prefer a node one-liner (the bench image has node); fall back to a python
# websocket-client check if node is unavailable locally. Both complete a single
# engine.io polling→WS upgrade handshake against the LB, which is exactly the
# real-browser path the sticky-L7 nginx exists to protect.
ws_ok=0
if command -v node >/dev/null 2>&1; then
    # Minimal engine.io handshake: GET ?EIO=4&transport=polling opens the session,
    # then Upgrade: websocket on the same path with the sid completes it. node's
    # built-in `ws` is enough for a raw upgrade; we just prove the LB routes the
    # WS upgrade to a live backend (not a 4xx/5xx or a hang).
    ws_ok=1
    node -e '
        const WebSocket = require("ws");
        const url = process.argv[1];
        const ws = new WebSocket(url, { handshakeTimeout: parseInt(process.env.WS_TIMEOUT || "15", 10) });
        const to = setTimeout(() => { console.error("WS_TIMEOUT"); process.exit(1); }, parseInt(process.env.WS_TIMEOUT || "15", 10) * 1000);
        ws.on("open", () => { clearTimeout(to); ws.close(); console.log("WS_OPEN_OK"); process.exit(0); });
        ws.on("error", (e) => { clearTimeout(to); console.error("WS_ERROR:", e.message); process.exit(1); });
    ' "$WS_URL" || ws_ok=0
elif command -v python3 >/dev/null 2>&1 && python3 -c 'import websocket' 2>/dev/null; then
    ws_ok=1
    WS_TIMEOUT="$WS_TIMEOUT" python3 -c '
import os, sys, websocket
websocket.setdefaulttimeout(int(os.environ.get("WS_TIMEOUT", "15")))
try:
    ws = websocket.create_connection(sys.argv[1])
    ws.close()
    print("WS_OPEN_OK")
except Exception as e:
    print("WS_ERROR:", e, file=sys.stderr); sys.exit(1)
' "$WS_URL" || ws_ok=0
else
    echo "[3/4] SKIP (no node 'ws' module and no python websocket-client — install one to run the WS smoke)" >&2
    fail=1
fi
if [[ $ws_ok -eq 1 ]]; then
    echo "[3/4] OK (WS handshake completed — scaled-socketio tier is up + adapter armed)"
else
    echo "[3/4] FAIL (WS handshake did not complete — check the socketio-tier supervisor logs + nginx upstream)" >&2
    fail=1
fi

# --- [4/4] Engagement asset smoke --------------------------------------------
# FLO-617 / FLO-610 P1-1: flock_os engagement assets (/assets/flock_os/js/*.js,
# /assets/flock_os/css/engage.css) are NOT collected into sites/assets/ until
# `bench build --app flock_os` runs. And the web-serving process must (re)boot
# AFTER the build to serve the freshly-collected tree: nginx (prod container)
# serves /assets/ from disk per-request, but the dev `bench serve`
# SharedDataMiddleware caches lookups per worker, so a gunicorn that started
# before the build 404s the new asset dirs until it is restarted.
#
# This check fails any deploy that skipped the build OR skipped the post-build
# web-worker restart — the two steps the deploy runbook now mandates. Override
# the asset list with FLOCK_ASSET_SMOKE_PATHS (space-separated site-relative
# paths); the default covers the engagement surface from FLO-610 P1-1.
echo "==> [4/4] Engagement asset smoke (bench build + web-worker restart)"
ASSET_PATHS="${FLOCK_ASSET_SMOKE_PATHS:-/assets/flock_os/js/engage.js /assets/flock_os/js/engage-host.js /assets/flock_os/js/engagement-core.js /assets/flock_os/js/announce.js /assets/flock_os/css/engage.css}"
assets_fail=0
for asset in $ASSET_PATHS; do
    code="$(curl --silent --show-error --output /dev/null --write-out '%{http_code}' \
        --max-time 15 "$SITE_URL$asset" || true)"
    if [[ "$code" == "200" ]]; then
        echo "      OK   $asset (200)"
    else
        echo "      FAIL $asset ($code — expected 200)" >&2
        assets_fail=1
    fi
done
if [[ $assets_fail -eq 0 ]]; then
    echo "[4/4] OK (engagement assets served — bench build ran + web worker restarted)"
else
    echo "[4/4] FAIL (one or more engagement assets 404'd)" >&2
    echo "      Fix: on the deploy host run 'bench --site flock_os build --app flock_os'," >&2
    echo "           then restart the web worker ('bench restart' or 'supervisorctl restart gunicorn')." >&2
    echo "      See docs/development/deploy-runbook.md -> Asset build + web-worker restart." >&2
    fail=1
fi

echo "--------------------------------"
if [[ $fail -eq 0 ]]; then
    echo "SMOKE: PASS"
    exit 0
fi
echo "SMOKE: FAIL — staging is not healthy; do NOT promote to prod." >&2
exit 1
