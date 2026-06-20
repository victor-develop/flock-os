# Realtime engagement tier — authorization edge-case audit (FLO-428)

> Audit + regression coverage for the four authorization edge cases the
> ~15,000-attendee burst exposes on the realtime engagement tier. This is a
> **no-board** MVP-hardening slice — safe, reversible, no cloud or budget
> dependency. Source issue: [FLO-428](/FLO/issues/FLO-428).
>
> Scope peer: [FLO-121](/FLO/issues/FLO-121) shipped the scaled socketio tier
> (connection-setup wall cleared); this audit probes the **authorization**
> surface that burst traffic + complex group membership stress hardest.

## TL;DR

**No genuine bugs found.** Every edge case below is safe by design; this audit
exists to *freeze* that safe behavior so a future refactor cannot silently
regress it. Each section names the code path, the observed behavior, the test
that pins it, and (where relevant) the documented tradeoff noted as a candidate
future hardening item — none blocking, none a correctness gap.

| # | Edge case | Verdict | Regression test |
| --- | --- | --- | --- |
| 1 | Expired / revoked ticket on connect/join | **Safe** — connect cache ≠ join scope gate | `TestRevokedTicketOnJoin` + `FLO-428 #1 …` (Node) |
| 2 | Room capacity mid-broadcast | **Safe** — no cap exists; sharding IS the strategy | `TestRoomCapacityMidBroadcast` |
| 3 | Conflicting multi-branch group permissions | **Safe** — per-gathering decision, materialized-set union | `TestMultiBranchPermissionDeterminism` |
| 4 | Reconnect storm idempotency | **Safe** — Socket.IO room is a set; projector is stateless | `FLO-428 #4 …` (Node) + `TestReconnectStormProjectorStability` |

## Code surface audited

| Layer | Module | Responsibility |
| --- | --- | --- |
| Python pure decision | [`flock_os/realtime.py`](../../flock_os/realtime.py) | Channel naming, shard assignment, the projector (domain event → sharded fan-out), `event_room_join_allowed` scope gate |
| Python HTTP surface | [`flock_os/realtime_views.py`](../../flock_os/realtime_views.py) | `@frappe.whitelist()` adapter the node tier reaches over HTTP |
| Node connect middleware | [`realtime/middlewares/flock_auth_cache.js`](../../realtime/middlewares/flock_auth_cache.js) | sid-keyed auth cache (clears the §8 auth-callback wall, FLO-116) |
| Node join handler | [`realtime/handlers/flock_room_handlers.js`](../../realtime/handlers/flock_room_handlers.js) | The `socket.on("join", ...)` ACL + scope gate (FLO-107 / FLO-106) |
| Node multi-process adapter | [`realtime/adapters/flock_redis_adapter.js`](../../realtime/adapters/flock_redis_adapter.js) | `@socket.io/redis-adapter` wrapper (FLO-121) |

Two independent gates defend every room subscription (defense in depth,
documented in `flock_room_handlers.js`):

1. **Prefix ACL** — only `flock_os:event:<id>:(broadcast|shard:<k>)` rooms
   route through the handler. Frappe's internal rooms (`doc:*`, `user:*`,
   `task:*`, …) never reach the scope check.
2. **Branch scope (FLO-106)** — before joining, the handler POSTs to
   `/api/method/flock_os.realtime_views.can_join_event_room`, which delegates
   to `event_room_join_allowed` → `can_access_branch`. Fails closed: a denied
   / errored check keeps the socket out of the room.

## Edge case 1 — Expired / revoked ticket on connect/join

> A device holds a signed ticket that was valid at issue but revoked before
> the burst. Does the scope gate reject cleanly, or does the cached session
> linger?

### Two distinct paths, two distinct answers

**Connect path (Socket.IO middleware).** The auth cache
(`flock_auth_cache.js`) caches the resolved identity by `sid` for `TTL=60s`
(default). On a cache HIT the wrapper replays the cached identity and skips
the redundant `get_user_info` HTTP. **This means a revoked session CAN keep
opening new Socket.IO connections for up to `TTL` seconds after the revoke
lands in Frappe.** This is a *documented, deliberate* tradeoff (see the
header comment in `flock_auth_cache.js` lines 22–30): the TTL is bounded, the
burst is ~120 s, and a 60 s TTL yields 1 call + 14,999 cache hits per window —
the trade that clears the §8 auth wall.

**Join path (per-room scope gate).** The `join` handler does NOT consult the
auth cache. Every `join` event triggers `defaultAuthorize` → HTTP POST to
`can_join_event_room`, which runs through Frappe's full request cycle
(`@frappe.whitelist()` + `frappe.session`). If the sid is revoked, Frappe's
session middleware rejects the request → `defaultAuthorize` resolves `false`
→ the socket stays out of the room.

### Verdict

**Safe by design.** The auth cache is a *connect-time* optimization only; it
does not bypass the per-join scope gate. A revoked session therefore:

- **cannot join new rooms** after the revoke (the per-join HTTP check fails
  immediately, no TTL), and
- **can keep its existing Socket.IO connection open for up to `TTL=60s`**
  (the documented staleness window) — but every publish the socket receives
  in that window is one Frappe already authorized at emit time, so no
  unscoped data leaks.

### Regression coverage

| Test | Pins |
| --- | --- |
| `TestRevokedTicketOnJoin.test_signature_has_no_ticket_or_session_parameter` | The Python gate's signature structurally cannot stale-trust a ticket — no such parameter exists. |
| `TestRevokedTicketOnJoin.test_revoked_allowed_set_denies_the_next_join_immediately` | Clearing the user's allowed-set between two calls flips allow → deny on the next call (no TTL). |
| `TestRevokedTicketOnJoin.test_revoked_role_demotion_denies_the_next_join_immediately` | Demoting the user's role to a non-bypass role denies the next join live. |
| `TestRevokedTicketOnJoin.test_re_granting_scope_re_allows_the_next_join_immediately` | No negative cache either — re-granting scope re-allows on the next call. |
| `FLO-428 #1 cached identity contains NO scope/branch/permission field` (Node, `flock_auth_cache.test.mjs`) | Structural: the cache value is identity-only, so a future buggy join handler cannot consult it to bypass the live scope endpoint. |
| `FLO-428 #1 revoked ticket on join: a denied scope check keeps the socket out even after a prior allow` (Node, `flock_room_handlers.test.mjs`) | A denied scope decision keeps the socket out of NEW rooms even when other rooms are already joined. |
| `FLO-428 #1 revoked ticket on join: scope endpoint error (500/ECONNREFUSED) fails closed` (Node) | A transport error during the burst still denies — never opens an unscoped subscription. |
| `FLO-428 #1 revoked ticket on join: scope check fires on EVERY join event (no per-room memoization)` (Node) | The join handler does not memoize scope decisions per room — every `join` re-checks live, which is what makes mid-burst revocation effective. |

### Documented tradeoff (candidate future hardening — not a bug)

The auth cache's TTL window means a revoked session's *Socket.IO connection*
lingers up to 60 s. The runbook in
[`docs/development/ws-broadcast-delivery.md`](../development/ws-broadcast-delivery.md)
→ "auth cache" already names an optional Redis-push logout-invalidation
follow-up that would close this window proactively (push-evict on
`frappe.sessions.clear`). That is a performance/UX hardening, not a
correctness gap — every per-event decision already re-validates — so it is
out of scope for this audit and is **not** filed as a bug.

## Edge case 2 — Room capacity mid-broadcast

> A room at/near capacity when a broadcast fan-out fires. Is the overflow
> handled (drop + log) or does it cascade?

### Observed behavior

**There is no capacity cap in the realtime tier — by design.** A grep of
`flock_os/realtime.py` for `max_*`, `capacity`, `cap`, `overflow`, `limit`
returns no routing-relevant hits. The projector's fan-out shape is a pure
function of the shard count + the event name, never of the room population.

The sharded design (ADR [FLO-10](/FLO/issues/FLO-10#document-design) §5.1)
IS the capacity strategy:

- `DEFAULT_SHARD_COUNT = 10` → a 15k-attendee event partitions into 10
  presence shards (~1.5k subscribers each) + 1 shared broadcast room.
- A fan-out from the projector issues `N+1` publishes (10 shard emits + 1
  broadcast emit), each fanned out by Redis pub/sub to per-process
  subscribers via `@socket.io/redis-adapter` (FLO-121).
- Per-process, `io.to(room).emit(...)` walks a Set of socket ids — there is
  no per-room array to overflow, no count to trip.

The closest thing to an "overflow" is a Redis publish failure, and §5.3
declares realtime best-effort: the projector's `project()` swallows any
single publish exception per-target and continues — never cascading,
never propagating back to the emitter.

> Note: the §5.3 swallow is currently silent (no log/metric on the dropped
> publish). The audit treats this as a candidate observability hardening,
> not a correctness gap — see "Documented tradeoff" below.

### Verdict

**Safe by design.** There is no capacity guard to trip; the shard design
keeps every individual publish cheap; §5.3 contains any per-target failure.

### Regression coverage

| Test | Pins |
| --- | --- |
| `TestRoomCapacityMidBroadcast.test_bulk_attendance_emit_count_is_shard_count_plus_one_regardless_of_size` | Emit count is `N+1` for `count ∈ {10, 1_500, 15_000}` — capacity plays no role in routing. |
| `TestRoomCapacityMidBroadcast.test_game_close_fans_all_shards_and_broadcast_without_attendee_list` | A game close carries no attendee list and still fans every shard — no early-exit on audience size. |
| `TestRoomCapacityMidBroadcast.test_single_attendance_always_emits_exactly_two_targets` | 2k+ single-attendance events in the SAME shard each still emit exactly 2 publishes — no shard-population cap. |
| `TestRoomCapacityMidBroadcast.test_publish_failure_on_one_target_does_not_cascade` | One Redis publish failure (the "overflow" surrogate) is swallowed; the remaining `N` publishes still fire; routing returns the full plan. |

### Documented tradeoff (candidate future hardening — not a bug)

The §5.3 swallow (`except Exception: pass` in `EventRoomProjector.project`)
is drop-without-log. Adding a `flock_realtime_publish_dropped` metric (or a
debug log) would give ops visibility into a Redis-pub-sub brownout during a
15k burst. Filed as a non-blocking enhancement — see
[FLO-428](/FLO/issues/FLO-428) comment for the suggested follow-up.

## Edge case 3 — Conflicting multi-branch group permissions

> A member in two branch groups with different event-room scopes. Which scope
> wins, and is it deterministic + tested?

### Observed behavior

**There is no "conflict" because the decision is per-gathering, and a
gathering has exactly one branch.** Per ADR
[FLO-10](/FLO/issues/FLO-10#document-design) §4.2, a `Flock Gathering` is
branch-bound (`validate_gathering_branch_binding` requires
`Flock Gathering.branch`). The scope decision therefore reduces to:

```
can_access_branch(branch=gathering.branch,
                  allowed_branches=user.materialized_allowed_set,
                  roles=user.roles)
```

The user's `allowed_branches` is the **materialized union** of their group
subtrees (computed and stored as Frappe User Permissions, not walked at
decision time). For any given gathering the decision is binary
(`branch ∈ allowed_set` for a Branch Admin / Group Leader, or `True` for a
global role) and **order-independent** — set membership is commutative, so
the order in which the user's group memberships were created cannot change
the outcome.

### Verdict

**Safe by design.** Conflicting multi-branch membership cannot produce
non-determinism because:

1. Each gathering has exactly one branch (no multi-branch gathering to
   conflict over).
2. The allowed-set is a union, computed once and materialized — decision
   time does not traverse any tree.
3. `can_access_branch` is a pure set-membership check (or a role bypass) —
   no precedence rules, no shadow-deny, no "last write wins".

### Regression coverage

| Test | Pins |
| --- | --- |
| `TestMultiBranchPermissionDeterminism.test_user_in_two_branch_groups_joins_both_gatherings` | `allowed=[north, south]` joins gatherings in BOTH branches. |
| `TestMultiBranchPermissionDeterminism.test_allowed_set_order_does_not_change_the_decision` | Forward vs reverse set iteration → same decision. |
| `TestMultiBranchPermissionDeterminism.test_dual_membership_does_not_grant_the_third_branch` | `allowed=[north, south]` does NOT leak east — no transitive grant. |
| `TestMultiBranchPermissionDeterminism.test_repeated_calls_are_deterministic` | 100 identical calls → 100 identical results (no per-call state). |
| `TestMultiBranchPermissionDeterminism.test_subtree_union_via_materialized_set` | A regional admin's subtree (parent + child) joins both gatherings via the single materialized set. |

(Sibling coverage of the underlying branch-set semantics lives in
[`test_realtime_room_scope.py`](../../flock_os/tests/test_realtime_room_scope.py)
— `test_subtree_scope_uses_materialized_allowed_set`,
`test_shard_room_uses_same_branch_scope_as_broadcast`.)

## Edge case 4 — Reconnect storm idempotency

> Mass reconnect after a socketio worker restart (the 15k reconnect spike).
> Are re-joins idempotent, or do they leak duplicate room subscriptions?

### Observed behavior

**Joins are idempotent at two layers:**

**Socket.IO layer.** A Socket.IO room is a Set of socket ids.
`socket.join(room)` is idempotent: calling it 100 times for the same room
leaves the socket in that room exactly once. The flock_os listener
(`makeJoinListener` in `flock_room_handlers.js`) calls `socket.join(room)`
straight through on every qualifying `join` event — it does not track its
own per-room join state, so it cannot defeat Socket.IO's set semantics and
cannot leak duplicates.

**Projector layer.** The Python `EventRoomProjector` owns no per-room or
per-event mutable state. Projecting the same domain event N times produces
the SAME emission plan every time (a pure function of `(event, shard_count)`).
A reconnect-storm's worth of bus redelivery therefore cannot accumulate
"ghost" rooms or drift the projector's behavior.

### Verdict

**Safe by design.** Reconnect storms re-run the same idempotent operations
on stateless or set-based structures. No leak, no drift.

### Regression coverage

| Test | Pins |
| --- | --- |
| `FLO-428 #4 reconnect storm: re-joining the same room N times leaves the socket in it exactly once` (Node, `flock_room_handlers.test.mjs`) | 1_000 re-joins of the same shard → `socket.rooms.size === 1`. |
| `FLO-428 #4 reconnect storm: shard + broadcast re-joins land in a 2-room set` (Node) | 500 re-joins of shard + broadcast → `socket.rooms.size === 2`. |
| `FLO-428 #4 reconnect storm: leave between re-joins cannot leak stale rooms` (Node) | `leave` + re-`join` cycle produces the post-join set, no stale rooms. |
| `TestReconnectStormProjectorStability.test_repeated_attendance_event_emits_identical_plan_every_time` | 1_000 projections of the same event → 1_000 identical emission plans. |
| `TestReconnectStormProjectorStability.test_repeated_bulk_event_touched_shards_do_not_grow` | The touched-shard set is the same on every re-projection (no drift). |
| `TestReconnectStormProjectorStability.test_subscribed_projector_handles_repeated_bus_emits_without_drift` | End-to-end via the event bus: 100 reconnect-storm replays reach exactly the touched shard set (no duplicates, no growing room list). |

### Documented tradeoff (candidate future hardening — not a bug)

The per-join scope check is **not cached**. At 15k simultaneous reconnects,
each client typically emits 2 joins (its shard + the broadcast room), so the
burst produces ~30k HTTP scope-check calls against Frappe in the reconnect
window. Each call is cheap (one frappe session + one User Permissions read),
and the auth cache (FLO-116) already cleared the much heavier per-connection
`get_user_info` wall — but a per-sid scope-decision cache with a short TTL
(e.g. 5 s) would smooth the reconnect spike further. This is a performance
hardening, not a correctness gap — every join already authorizes correctly —
so it is out of scope for this audit and is **not** filed as a bug. The
runbook in
[`docs/development/ws-broadcast-delivery.md`](../development/ws-broadcast-delivery.md)
names it as the natural follow-up to FLO-116's connect-time cache.

## How to run the regression suite

The audit lives in two test surfaces, matching the existing one-test-file-per
-module convention:

```bash
# Python pure-decision regressions (no bench, no Redis)
pytest flock_os/tests/test_realtime_edge_cases.py -v

# Node realtime-tier regressions (no bench, no socket.io, no npm install)
node --test realtime/handlers/flock_room_handlers.test.mjs \
            realtime/middlewares/flock_auth_cache.test.mjs
```

The full pre-merge gate
([`scripts/qa-gate.sh`](../../scripts/qa-gate.sh)) runs the Python side; the
Node tests run via `node --test` locally and in CI for the realtime tier.

## Follow-ups (candidate hardening — not blocking, not bugs)

None of these is a correctness gap. They are listed here so a future
hardening pass has the audit's findings in one place. Filing them as
enhancement issues is optional and was deliberately NOT done under FLO-428
to keep the audit scoped to "audit + regression tests" per the issue's
acceptance criteria.

1. **Push-evict the auth cache on logout.** Closes the 60 s TTL window
   proactively. Runbook pointer:
   [`docs/development/ws-broadcast-delivery.md`](../development/ws-broadcast-delivery.md)
   → "auth cache".
2. **Emit a metric on a swallowed realtime publish.** Gives ops visibility
   into a Redis-pub-sub brownout during a 15k burst (today the §5.3 swallow
   is silent).
3. **Short-TTL scope-decision cache for `can_join_event_room`.** Smooths
   the reconnect-storm spike (today every `join` re-HTTPs the scope
   endpoint). Natural follow-up to FLO-116.
