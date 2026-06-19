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
   bench realtime server by `scripts/dev/wire-socketio-handler.sh`.

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

## Wire the handler (one-time, idempotent)

```bash
scripts/dev/wire-socketio-handler.sh          # insert the guarded require
bench restart                                  # reload the realtime node server
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
reversible. **Re-run it after a `bench update` / Frappe reinstall** — Frappe
rewrites `index.js` and drops the line.

### Why a patch (and not a framework hook)

Frappe v15 has no extension point for custom socket events: there is no
`socketio_handler` app hook, and `index.js` loads only `frappe_handlers`. The
alternatives (fork vendored Frappe, a Node `--require` preload that can't see
the live `io` instance, an upstream PR) are heavier or out of our control. The
guarded require is the least-invasive flock_os-owned option. Finer
per-event/per-branch join ACL (membership-aware scoping) is a follow-up for the
§5.1 ownership projector.

## Room-join event shape (§5.1 client→server contract)

```
client → server:  42/<site>,["join",{"room":"flock_os:event:<id>:shard:N"}]
                 42/<site>,["join",{"room":"flock_os:event:<id>:broadcast"}]
server:           socket.join(room)   (only flock_os:event:* rooms — ACL)
```

Both the object form (`{room}`) and the legacy bare-string form are accepted, so
the desk client and raw clients share one path. This is the shape flagged for
Architect §5.1 sign-off; changing it is a one-line client + server edit.

## Verify end-to-end

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
`44/...` frame means the namespace/auth step failed (check the `sid` + Origin).

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
