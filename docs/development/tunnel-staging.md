# Tunnel Staging — $0 Cloudflare Tunnel staging URL (FLO-889)

Interim staging validation path: expose the **local prod-equivalent docker WS
tier** as a public HTTPS URL for **$0** via a `cloudflared` quick tunnel, while
[FLO-872](/FLO/issues/FLO-872) (paid Frappe Cloud) waits on the board.

This is **not** production hosting. It is a fast, account-less way to get a
reachable, auto-TLS staging URL so the [FLO-249](/FLO/issues/FLO-249) acceptance
gate (`scripts/deploy/smoke-staging.sh`) can run against a real external URL,
unblocking the downstream chain ([FLO-250](/FLO/issues/FLO-250) promotion gate).

> **Status:** proven. `smoke-staging.sh` PASS over the tunnel (web 200,
> `/api/method/ping` → `pong`, WS handshake completed, engagement assets 200).
> Evidence: `load/telemetry/<ts>-tunnel/`.

---

## TL;DR

```bash
# 1. The prod-equivalent docker WS tier must be up (15k-proven topology).
scripts/dev/docker-ws-tier.sh up

# 2. Bring up the tunnel edge + cloudflared quick tunnel.
scripts/deploy/tunnel-staging.sh up      # prints https://<random>.trycloudflare.com

# 3. Run the staging acceptance gate against the public URL.
scripts/deploy/tunnel-staging.sh smoke   # -> SMOKE: PASS

# 4. Tear down when done.
scripts/deploy/tunnel-staging.sh down
```

---

## Architecture

A quick tunnel exposes **one** local port, and `smoke-staging.sh` expects
socketio at the **same origin** (`/socket.io/`). So `tunnel-staging.sh` runs a
tiny "edge" nginx container that merges both surfaces onto one origin, then
points **one** cloudflared quick tunnel at it. cloudflared itself ALSO runs in a
container on the same docker network, reaching the edge over docker DNS
(`http://flock-tunnel-edge:80`):

```
browser --https/wss--> Cloudflare edge --(QUIC)--> cloudflared container
  -> edge nginx (:80)
       /socket.io/  -> ws-lb:9000   (FLO-121 N-worker sticky-L7 WS tier)
       /assets/     -> sites/assets (disk — prod nginx-in-front-of-gunicorn pattern)
       /            -> web:8000     (gunicorn / Frappe web)
```

### Why the edge rewrites Host + Origin on `/socket.io/`

Frappe's realtime auth middleware
(`apps/frappe/realtime/middlewares/authenticate.js`) checks
`get_hostname(Host) == get_hostname(Origin)` and derives the site from the
request headers. Through the tunnel both arrive as `<random>.trycloudflare.com`,
which (a) does not match the provisioned Frappe site, and (b) makes
`get_url()` build the `get_user_info` callback from the public Origin, forcing a
hairpin through the internet.

The edge therefore rewrites, **on the `/socket.io/` path only**:

- `Host: flock_os.localhost` — so the multi-site WSGI dispatcher selects the
  provisioned site, and Frappe's `host == origin` check passes.
- `Origin: http://flock_os.localhost:8000` — so `get_url()` builds the callback
  as `http://flock_os.localhost:8000/api/method/...`, which resolves to the
  `web` container via its `flock_os.localhost` network alias. The callback stays
  on the docker network — **no public-edge hairpin** — exactly like the proven
  in-container k6 path.

The `/` (web) path also forces `Host: flock_os.localhost` for site dispatch.
This is the same host-rewriting the docker tier's own `:8100` block already
does (`docker-ws-tier.sh` → `render_nginx`).

---

## CRITICAL GOTCHA — the `~/.cloudflared/config.yml` collision

This Mac runs a launchd service `com.cloudflare.cloudflared` that fronts the
**Paperclip control plane** (`ppclip.otterrun.work → http://localhost:3100`)
via named tunnel `84ce11f0`, configured in `~/.cloudflared/config.yml`.

If you run a **host** `cloudflared tunnel --url http://localhost:8090` **without
precautions**, cloudflared auto-loads `~/.cloudflared/config.yml`, reuses named
tunnel `84ce11f0`, and its **catch-all `http_status:404` ingress shadows
`--url`** — every request to the trycloudflare URL returns `404` from
`server: cloudflare` and **never reaches the edge**. (Symptom: edge nginx access
log has zero entries from cloudflared; public URL 404s.) A backgrounded host
cloudflared is also SIGTERM'd by the agent-harness shell when its spawning
command exits, so the tunnel dies between heartbeats.

`tunnel-staging.sh` avoids **both** problems by running cloudflared in its own
**container** on the docker network. The container has no `~/.cloudflared/
config.yml`, so `--url` owns the quick tunnel + ingress with no collision, and a
detached container survives shell reaping.

> **NEVER** kill the launchd `com.cloudflare.cloudflared` service or delete
> `~/.cloudflared/config.yml` — it serves Paperclip itself (the agent harness,
> the API). Containerizing the staging cloudflared sidesteps it non-destructively.

---

## Commands

`scripts/deploy/tunnel-staging.sh`

| Command  | Action                                                                  |
| -------- | ----------------------------------------------------------------------- |
| `up`     | Render edge conf, start edge + cloudflared containers, print URL + reachability-check it. |
| `status` | Show edge + cloudflared containers, the URL, and a ping reachability probe. |
| `smoke`  | Run `scripts/deploy/smoke-staging.sh` against the current tunnel URL.   |
| `logs`   | Tail cloudflared's container log (edge: `docker logs -f flock-tunnel-edge`).      |
| `down`   | Remove the cloudflared + edge containers. The docker WS tier keeps running. |

### Environment overrides

| Var                     | Default                       | Purpose                                              |
| ----------------------- | ----------------------------- | ---------------------------------------------------- |
| `EDGE_HOST_PORT`        | `8090`                        | host port the edge nginx publishes (debug only, 127.0.0.1) |
| `SITE_NAME`             | `flock_os.localhost`          | Frappe site name / docker-local Host to rewrite to   |
| `FLOCK_NETWORK`         | `flock-ws_backend`            | docker network the WS tier runs on                   |
| `FLOCK_SITES_VOLUME`    | `flock-ws_sites`              | shared sites volume (read-only mount for /assets/)   |
| `FLOCK_CLOUDFLARED_IMAGE` | `cloudflare/cloudflared:latest` | cloudflared container image                        |
| `WEB_PORT`              | `8000`                        | internal gunicorn port                               |

---

## Known limitations

- **Ephemeral URL.** The `https://<random>.trycloudflare.com` hostname changes
  on every `up`. It is not stable. For a stable URL, provision a **named**
  Cloudflare Tunnel (free tier) with a fixed hostname — see
  [Stable URL](#stable-url-optional) below.
- **Mac must stay running.** cloudflared runs on this host; sleep/shutdown drops
  the tunnel. Sufficient for a staging smoke window, not for always-on prod.
- **Best-effort / no SLA.** Quick tunnels carry no uptime guarantee (per the
  cloudflared banner). DNS for a freshly-minted hostname occasionally fails to
  publish — if `up` reports the URL but `status`/`smoke` show `HTTP 000` /
  `Could not resolve host`, cycle it: `down && up`.
- **Rapid-create throttle.** Cloudflare may stop publishing DNS for new quick
  tunnels created in rapid succession from one host. Create **one** tunnel per
  staging session and keep it up; avoid repeated `down`/`up` churn. (The
  containerized cloudflared is the one proven to register + route immediately;
  host-process quick tunnels are flakier.)
- **Throughput.** The quick tunnel is fine for smoke + demo traffic. It is not
  sized for a 15k-concurrent WS gate — that stays on the in-container k6 path
  (`docker-ws-tier.sh gate`). Always-on production still needs the real
  Frappe Cloud VM ([FLO-872](/FLO/issues/FLO-872)).

### Stable URL (optional)

A quick tunnel is random. For a reusable staging hostname (still $0):

1. Create a free Cloudflare account and a named tunnel:
   `cloudflared tunnel login`, `cloudflared tunnel create flock-staging`.
2. Route a hostname (e.g. `staging.<your-domain>`) to the tunnel:
   `cloudflared tunnel route dns flock-staging staging.example.com`.
3. Point the tunnel's ingress at the edge container:
   `service: http://flock-tunnel-edge:80` (on `flock-ws_backend`).

This keeps the SAME edge nginx + docker tier; only the tunnel front-end becomes
stable. Out of scope for FLO-889 (account creation is a human/board step) —
documented for whoever picks up after [FLO-872](/FLO/issues/FLO-872).

---

## Troubleshooting

- **Public URL returns `404` from `server: cloudflare`, edge log empty.** You ran
  cloudflared as a HOST process and hit the `~/.cloudflared/config.yml` collision.
  Always bring the tunnel up via `tunnel-staging.sh up` (which containerizes
  cloudflared). Do NOT run a bare host `cloudflared tunnel --url` on this Mac.
- **`HTTP 000` / `Could not resolve host` right after `up`.** The fresh
  quick-tunnel hostname's DNS hasn't published (or was throttled). Wait ~30s and
  re-run `status`; if still `000`, cycle `down && up`.
- **`[3/4] WS handshake did not complete` in smoke.** Confirm the docker WS tier
  is up (`docker-ws-tier.sh status` — ws-lb reachable on :9000) and the edge is
  running (`tunnel-staging.sh status`). The smoke's WS probe uses node `ws` when
  available, else python `websocket-client` (`pip3 install --break-system-packages
  websocket-client` on a PEP-668 Mac).
- **`/assets/flock_os/...` 404.** The sites volume isn't mounted on the edge, or
  `bench build --app flock_os` hasn't run in the docker tier. Re-bring the tier
  up (`docker-ws-tier.sh down && up`) — its `init` one-shot runs the build.

---

## Verification record (FLO-889)

- `scripts/deploy/smoke-staging.sh` → **SMOKE: PASS** over the tunnel URL:
  HTTP reachability 200, `/api/method/ping` → `pong`, WS handshake completed
  (engine.io OPEN frame received), engagement assets 200. Verified end-to-end
  with cloudflared running in its own container (`flock-tunnel-cloudflared`).
- Evidence bundle: `load/telemetry/20260622T133500Z-tunnel/` (`smoke.log`,
  `ws-handshake.txt`, `url.txt`) — clean script-driven `down`→`up`→`smoke` cycle.

## Related

- [FLO-889](/FLO/issues/FLO-889) — this slice.
- [FLO-872](/FLO/issues/FLO-872) — paid Frappe Cloud (the board-blocked path
  this interim unblocks).
- [FLO-775](/FLO/issues/FLO-775) — the 15k-proven docker WS tier this fronts.
- [FLO-249](/FLO/issues/FLO-249) / [FLO-250](/FLO/issues/FLO-250) — staging
  acceptance + promotion gates.
- `scripts/dev/docker-ws-tier.sh` — the prod-equivalent docker tier.
- `docs/development/deploy-runbook.md` — the prod deploy runbook.
