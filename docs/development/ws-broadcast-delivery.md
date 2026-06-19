# WebSocket broadcast-delivery — runbook (FLO-107)

> How a published realtime broadcast actually reaches a `ws_event_room.js`
> client on this bench, and the one-time wiring that makes it work on Frappe v15.

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
   `flock_os.realtime.can_join_event_room` whether the socket's user has branch
   scope over the gathering (ADR §6.2) — the same decision that scopes
   `tabFlock Attendance Record` rows — so a client can only subscribe to rooms
   for gatherings in its branch subtree.

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
   `flock_os.realtime.can_join_event_room` over the socket's session (same shape
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
> smoke sees no broadcasts (looks like an FLO-107 regression). Seed it once
> (after the `Flock Gathering` doctype is migrated into the site):
>
> ```python
> # bench --site flock_os.localhost console
> import frappe
> if not frappe.db.exists("Flock Gathering", "gathering-smoke"):
>     frappe.get_doc({
>         "doctype": "Flock Gathering", "title": "Smoke Gathering",
>         "organization": frappe.db.get_single_value("Flock Settings", "default_organization") or frappe.db.get_value("Flock Organization", {}, "name"),
>         "branch": "branch-smoke", "gathering_type": "Weekly Service",
>         "starts_on": "2026-01-01 10:00", "status": "Scheduled",
>     }).insert(ignore_permissions=True)
>     frappe.db.commit()
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
