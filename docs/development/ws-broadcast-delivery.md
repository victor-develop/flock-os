# WebSocket broadcast-delivery — runbook (FLO-107 / FLO-116 / FLO-121)

> How a published realtime broadcast actually reaches a `ws_event_room.js`
> client on this bench, the one-time wiring that makes it work on Frappe v15,
> the per-connection auth cache that clears the §8 15k WS **auth-callback** wall,
> and the scaled socketio tier that clears the §8 15k WS **connection-setup**
> wall. (Two distinct walls; each is cleared by a different layer — see
> [FLO-53](/FLO/issues/FLO-53) §8 verdict.)

## The gap this closed

`flock_ws_broadcast_latency` (the §8 WS signal on [FLO-53](/FLO/issues/FLO-53))
was empty end-to-end: the Python projector published ticks via
`frappe.publish_realtime`, but **zero** reached the k6 clients. Two independent
root causes, both fixed by FLO-107:

1. **Wrong namespace.** Frappe v15 realtime (`apps/frappe/realtime/index.js`)
   namespaces per site: `io.of(/^\/.*$/)` and `frappe.publish_realtime` emits to
   `/<site>` (e.g. `/flock_os.localhost`). The smoke CONNECTed the **default**
   namespace `/` — a different room universe. **Fix:** the smoke now CONNECTs
   `/<site>` (every non-default Socket.IO packet carries the namespace + `,`
   per `socket.io-parser`).

2. **No room-join handler.** `frappe_handlers.js` auto-joins only `user:<u>`,
   `website`, `all` — there is no generic `join` event, and Frappe v15 ships no
   per-app socket handler loader. So `42["join",{room}]` was a no-op and the
   §5.1 shard/broadcast rooms had no subscribers. **Fix:** flock_os owns a
   `join` handler (`realtime/handlers/flock_room_handlers.js`) wired into the
   bench realtime server by `scripts/dev/wire-socketio-handler.sh`. **FLO-106**
   added a second gate on top: before joining, the handler asks
   `flock_os.realtime_views.can_join_event_room` whether the socket's user has branch
   scope over the gathering (ADR §6.2) — the same decision that scopes
   `tabFlock Attendance Record` rows — so a client can only subscribe to rooms
   for gatherings in its branch subtree. (The whitelisted surface lives in the
   bench-only `flock_os.realtime_views` module — `realtime.py` is import-clean
   by design, so the `@frappe.whitelist()` decorator cannot live there; FLO-112.)

A third wrinkle: the site namespace runs `realtime/middlewares/authenticate.js`,
which requires a `sid` cookie or `Authorization` header (validated via
`frappe.realtime.get_user_info`) and a matching Host/Origin hostname. The smoke
now logs in once (`setup()`) and presents the `sid` as a cookie on the WS
handshake. **Gotcha:** the middleware builds its `get_user_info` callback URL as
`origin + path` (`realtime/utils.js`), so the `Origin` header must include the
web port (`http://flock_os.localhost:8000`); a port-less origin makes the node
server call back to `:80` and reject the namespace with
`Unauthorized: AggregateError`. `config.ws.origin` defaults to the web base for
this reason.

## Wire the handler (auto-wired on migrate, idempotent)

```bash
scripts/dev/wire-socketio-handler.sh          # insert the guarded require
bench restart                                  # reload the realtime node server
scripts/dev/wire-socketio-handler.sh --check   # assert wired: exit 0 ok / 1 absent
# undo:
scripts/dev/wire-socketio-handler.sh --revert
```

The script inserts ONE guarded line into the bench's vendored
`apps/frappe/realtime/index.js`, right after `frappe_handlers(realtime, socket);`:

```js
try { require("../../flock_os/realtime/handlers/flock_room_handlers")(socket); }
catch (err) { console.error("flock_os realtime handler:", (err && err.message) || err); }
```

The handler **logic** lives in flock_os (version-controlled); only this single
require lands in vendored Frappe. It is idempotent (marker-guarded) and
reversible.

### Surviving a `bench update` (FLO-109)

A `bench update` / Frappe reinstall rewrites `apps/frappe/realtime/index.js`
and **drops the guarded require** — joins then no-op and broadcasts reach zero
clients, with no startup error (the runtime `try/catch` swallows the missing
module). That is exactly the FLO-107 symptom recurring silently. The wiring is
now **self-healing** through two independent guards:

1. **Auto-wire hook.** flock_os registers `after_migrate` + `after_install`
   hooks (`flock_os/utils/realtime_setup.py`) that re-run
   `wire-socketio-handler.sh` against the bench. `bench update` performs a
   `bench migrate`, so the handler is re-inserted automatically — no manual
   runbook step. The hook is best-effort: it logs a warning on failure but
   never breaks `migrate`/`install`.
2. **`--check` assert.** `wire-socketio-handler.sh --check` is a non-mutating
   gate that exits `1` when the marker is absent. Run it from CI / a deploy
   runbook after an update to turn a dropped handler into a loud failure
   instead of a silent regression.

If you ever suspect the line went missing (e.g. a k6 run delivered 0
broadcasts), the one-line confirm is `wire-socketio-handler.sh --check`; if it
fails, a plain `wire-socketio-handler.sh` + `bench restart` restores delivery.

## Cache per-connection auth to clear the §8 15k WS **auth-callback** wall (FLO-116)

> **Two walls, two layers.** The §8 15k WS bar hit two independent walls in
> sequence (see [FLO-53](/FLO/issues/FLO-53) §8 verdict):
> 1. the **auth-callback** wall — ~15k redundant `get_user_info` HTTPs in a burst
>    (connect p95 2.26 s, `flock_ws_receive_errors` 8255), cleared by this cache
>    ([FLO-116](/FLO/issues/FLO-116)); and
> 2. the **connection-setup** wall — a single node event loop serializing ~15k
>    concurrent handshakes (connect p95 ~27 s, <1 % established), cleared by
>    scaling the socketio tier horizontally ([FLO-121](/FLO/issues/FLO-121), see
>    [Scaling the socketio tier](#scale-the-socketio-tier-flo-121) below).
>
> The cache cleared wall #1; it does **not** by itself clear wall #2.

Frappe's site-namespace auth middleware
(`apps/frappe/realtime/middlewares/authenticate.js`) fires ONE synchronous HTTP
callback per WS connection — `GET /api/method/frappe.realtime.get_user_info` —
to resolve `socket.user`. The smoke logs in once (`setup()`) and every VU
presents the **same** `sid`, so at the full 15k bar the node realtime server
makes ~15k identical `get_user_info` round-trips through gunicorn in a burst.
The superagent calls then hit `ETIMEDOUT` (errno -60), connections cycle/fail
(connect p95 **2.26 s**), in-flight packets drop (`flock_ws_receive_errors`
**8255**), and broadcasts back up (p95 **16.41 s**). Redis is **not** the wall;
the per-connection auth HTTP is, not the broadcast fan-out.

**Fix:** flock_os owns a sid-keyed cache (`realtime/middlewares/flock_auth_cache.js`)
that wraps frappe's `authenticate` middleware, so `get_user_info` fires **once
per session, not once per connection** — at 15k clients sharing one sid that is
1 call + 14,999 in-memory hits. The cache is TTL'd (default 60 s; a revoked
session is re-checked within the window) and bounded LRU (default 50k entries);
on a HIT it replays the cached `{user,user_type}` and skips the redundant HTTP.

socket.io's `namespace.use()` has no removal API and *appends*, so registering
the cached middleware alongside the original would still let the original run
first (firing the HTTP). The wiring therefore **replaces** the single
`realtime.use(authenticate);` line in `apps/frappe/realtime/index.js` with a
guarded `realtime.use(require("...flock_auth_cache").wrap(authenticate))`. It is
marker-guarded, idempotent, and reversible — the same shape as the join-handler
wiring above, and independent of it (different anchor line), so the two compose.

```bash
scripts/dev/wire-socketio-auth-cache.sh          # replace the anchor with the cached swap
bench restart                                     # reload the realtime node server
scripts/dev/wire-socketio-auth-cache.sh --check   # assert wired: exit 0 ok / 1 absent
# undo:
scripts/dev/wire-socketio-auth-cache.sh --revert  # restore realtime.use(authenticate);
```

It survives a `bench update` by the **same self-healing guards** as the join
handler: an `after_migrate` + `after_install` hook
(`flock_os.utils.realtime_setup.rewire_socketio_auth_cache`, sharing the
handler's drive+verify core) re-runs the script on every migrate, then
**verifies** the marker landed and raises `RealtimeWiringError` if it is missing
— a dropped cache can never silently bring back the wall.

> **Security tradeoff (deliberate, bounded):** on a cache HIT the wrapper skips
> the redundant `get_user_info` HTTP. The `sid` already passed frappe's FULL
> validation (namespace + origin + cookie + `get_user_info`) when it was first
> cached, and entries expire on a short TTL, so a revoked/changed session is
> re-checked within the TTL window. TTL + LRU bound staleness and memory.
> Redis-push logout invalidation is an optional future hardening (not needed for
> the §8 bar).

The cache + middleware branching is unit-tested under plain `node --test`
(`realtime/middlewares/flock_auth_cache.test.mjs`); the wiring harness under
`pytest` (`flock_os/tests/test_realtime_auth_setup.py`) — neither needs a bench.

### Why a patch (and not a framework hook)

Frappe v15 has no extension point for custom socket events: there is no
`socketio_handler` app hook, and `index.js` loads only `frappe_handlers`. The
alternatives (fork vendored Frappe, a Node `--require` preload that can't see
the live `io` instance, an upstream PR) are heavier or out of our control. The
guarded require is the least-invasive flock_os-owned option.

### Branch-scope gate (FLO-106)

The handler applies **two** gates, defense in depth:

1. **Prefix/shape ACL** — only well-formed `flock_os:event:<id>:(broadcast|shard:<k>)`
   rooms route through it (an authenticated socket cannot eavesdrop on Frappe's
   `doc:*` / `task:*` / `user:*` rooms).
2. **Branch scope** — before `socket.join(room)`, the handler calls
   `flock_os.realtime_views.can_join_event_room` over the socket's session (same shape
   as Frappe's `can_subscribe_doc`). That endpoint resolves the gathering's
   `Flock Gathering.branch` and reuses the single sanctioned decision
   `flock_os.permissions.can_access_branch`: global-branch roles (Org Admin /
   Auditor) pass; a Branch Admin / Group Leader passes iff the branch is in their
   materialized `Flock Branch` User Permissions (their subtree); an unknown
   gathering fails closed. Fails best-effort: a denied or errored check keeps the
   socket out of the room without killing the connection (FLO-10 §5.3).

Pure room validation + the authorize allow/deny branching are unit-tested under
plain `node --test` (`realtime/handlers/flock_room_handlers.test.mjs`); the
Python scope decision under `pytest`
(`flock_os/tests/test_realtime_room_scope.py`) — neither needs a bench.

> **Throughput note:** the scope check is one HTTP call per `join`. A client
> joins two rooms (its shard + the broadcast) for one event, so that is two
> calls per client during ramp-up — fine through the 15k ramp (≈500/s over 60s).
> A per-`(socket, event)` memo (the scope is per-gathering, not per-room) is a
> straightforward future optimization if a gate run ever shows join throughput
> as the bottleneck; it is not needed for the §8 bar.

## Scale the socketio tier (FLO-121)

The auth cache cleared the §8 15k WS **auth-callback** wall. With that gone the
bar shifted to the **connection-setup** wall: one node event loop serializes
~15k concurrent handshakes (TCP accept + engine.io OPEN + SIO CONNECT + per-room
JOIN), so connect p95 balloons (~27 s) and <1 % of sessions ever establish. This
is *not* a flock_os code gap — the realtime path is correct end-to-end — it is a
single-process node socketio throughput wall. The remedy is horizontal scaling:
run **N** node socketio processes behind a WS-aware load balancer so the
handshakes distribute across event loops, and wire `@socket.io/redis-adapter` so
the cluster behaves as one logical io instance.

### Cross-worker fan-out: `@socket.io/redis-adapter` (auto-wired, idempotent)

Frappe's realtime server already fans a `frappe.publish_realtime` out via a
Redis "events" pub/sub channel that **every** node process subscribes to — so the
publish→room path already crosses processes without help. What does **not**
cross processes by default is Socket.IO's own room machinery
(`io.to(room).emit` / `socket.broadcast` issued from inside a connection
handler, plus room-membership coordination), which is per-process.
`@socket.io/redis-adapter` routes those through Redis pub/sub so a broadcast
originating on process A reaches sockets that joined the room on process B — the
standard socket.io multi-process story and the defense-in-depth that keeps the
tier correct (not just fast) as it scales.

The adapter attaches to the per-site namespace (`realtime`). Since that live
instance only exists inside vendored `apps/frappe/realtime/index.js`, it is wired
the same way as the join handler + auth cache — a marker-guarded INSERT before
`realtime.on("connection", on_connection);`, independent of the other two
anchors so all three compose:

```bash
scripts/dev/wire-socketio-redis-adapter.sh          # insert the guarded adapter-attach block
bench restart                                         # reload the realtime node server(s)
scripts/dev/wire-socketio-redis-adapter.sh --check   # assert wired: exit 0 ok / 1 absent
# undo:
scripts/dev/wire-socketio-redis-adapter.sh --revert
```

The wiring creates two node-redis clients from the bench's `redis_socketio` URL
(via frappe's `get_redis_subscriber`), connects them, and attaches the adapter.
The adapter **logic + opts** live in flock_os
(`realtime/adapters/flock_redis_adapter.js`); only the guarded block lands in
vendored Frappe. **Prereq:** `@socket.io/redis-adapter` is declared in the
repo-root `package.json`; run `npm install` there once (or let
`scale-socketio.sh start` do it). Without the package the wiring is still
inserted (armed) but the runtime `try/catch` logs the missing-package error and
the tier falls back to the events-pub/sub path only (defensive, not the full
multi-process story).

It survives a `bench update` by the **same self-healing guards** as the other
two wirings: an `after_migrate` + `after_install` hook
(`flock_os.utils.realtime_setup.rewire_socketio_redis_adapter`, sharing the
handler/cache drive+verify core) re-runs the script on every migrate, then
**verifies** the marker landed and raises `RealtimeWiringError` if it is missing.
The cache + opts logic is unit-tested under `node --test`
(`realtime/adapters/flock_redis_adapter.test.mjs`); the wiring harness under
`pytest` (`flock_os/tests/test_realtime_redis_adapter_setup.py`) — neither needs
a bench.

### Running the scaled tier

`scripts/dev/scale-socketio.sh` brings the scaled tier up in one command:
N socketio backends (each `node apps/frappe/socketio.js` with
`FRAPPE_SOCKETIO_PORT` set) behind `scripts/dev/socketio-lb.js`, a dependency-free
pure-node TCP round-robin proxy. The LB listens on the smoke's default WS port
(9000) so k6 needs no `WS_BASE_URL` override.

```bash
scripts/dev/scale-socketio.sh start        # bring up the scaled tier (N = nproc, capped 8)
scripts/dev/scale-socketio.sh status       # pids + per-backend connection distribution
# run the §8 gate against the scaled tier:
k6 run -e WS_VUS=15000 -e WS_DURATION_SEC=120 load/ws_event_room.js
scripts/dev/scale-socketio.sh stop         # tear down (single-process restored by `bench restart`)
```

> **Why a TCP round-robin LB (not sticky L7).** The smoke uses
> `transport=websocket` only — no engine.io polling upgrade — so each client is
> exactly ONE independent TCP connection whose full lifecycle stays on whichever
> backend it first lands on. There is no separate polling handshake that must
> revisit the same backend, so plain per-connection round-robin distributes
> cleanly and no sticky session is needed. A mixed/polling production tier would
> instead need sticky L7 (nginx `ip_hash` / cookie) so the WS upgrade revisits
> the backend that owns the polling session. The LB logs per-backend
> `accepted/active/failed` counters so a run can verify connections actually
> spread across the tier.

## Room-join event shape (§5.1 client→server contract)

```
client → server:  42/<site>,["join",{"room":"flock_os:event:<id>:shard:N"}]
                  42/<site>,["join",{"room":"flock_os:event:<id>:broadcast"}]
server:           socket.join(room)   (flock_os:event:* only, then branch-scope checked)
```

Both the object form (`{room}`) and the legacy bare-string form are accepted, so
the desk client and raw clients share one path. This is the shape flagged for
Architect §5.1 sign-off; changing it is a one-line client + server edit.

## Verify end-to-end

> **Runtime fixture (required since FLO-106).** The branch-scope gate resolves
> the gathering's branch, so the smoke's `EVENT_ID` (`gathering-smoke`) MUST
> exist as a `Flock Gathering` in a branch the smoke user (`leader@flock.os`,
> scoped to `branch-smoke`) can access — otherwise joins fail closed and the
> smoke sees no broadcasts (looks like an FLO-107 regression). Seed it once with
> the idempotent seeder (after the `Flock Gathering` doctype is migrated into
> the site — these are runtime smoke rows, not migrate-seeded catalog fixtures):
>
> ```bash
> # org-smoke -> branch-smoke -> group-smoke -> gathering-smoke + scoped leader.
> scripts/dev/seed-smoke-fixtures.sh          # idempotent; safe to re-run
> # or: bench --site flock_os.localhost execute flock_os.utils.smoke_fixtures.execute
> ```

```bash
# 1. clients up (CONNECT /<site>, auth via sid, join shard + broadcast rooms)
k6 run -e WS_VUS=200 -e WS_DURATION_SEC=30 -e WS_BASE_URL=ws://flock_os.localhost:9000 \
      load/ws_event_room.js

# 2. in another shell, drive the producer while clients are connected:
bench --site flock_os.localhost execute \
  'frappe.publish_realtime("flock_os:attendance:count", message={"delta":1,"ts":int(__import__("time").time()*1000)}, room="flock_os:event:gathering-smoke:broadcast")'
```

Pass: `flock_ws_broadcast_latency` reports samples (p95 < 1s), and
`flock_ws_receive_errors == 0`. A non-zero `flock_ws_receive_errors` with a
`44/...` frame means the namespace/auth step failed (check the `sid` + Origin);
samples staying at `0` while the producer runs means joins are being denied —
confirm `gathering-smoke` is seeded in `branch-smoke` and the smoke user has
that branch in scope.

## Wire-format reference (EIO=4 / SIO=5, namespaced)

Per `socket.io-parser` `encodeAsString`: when `nsp != "/"`, the namespace is
appended followed by `,` before the (optional) id and JSON data.

| Packet        | Default ns `/`         | Site ns `/flock_os.localhost`                       |
| ------------- | ---------------------- | --------------------------------------------------- |
| CONNECT       | `40`                   | `40/flock_os.localhost,`                            |
| CONNECT ack   | `40{"sid":...}`        | `40/flock_os.localhost,{"sid":...}`                 |
| EVENT (join)  | `42["join",{...}]`     | `42/flock_os.localhost,["join",{...}]`              |
| EVENT (recv)  | `42["evt",{...}]`      | `42/flock_os.localhost,["evt",{...}]`               |
| ERROR (auth)  | `44{"message":...}`    | `44/flock_os.localhost,{"message":...}`             |

engine.io keepalive: server sends ping `2`, client must answer pong `3` (the
smoke does) or the server drops the socket after its ping timeout.
