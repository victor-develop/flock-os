# Metrics + alerting design — Flock OS production observability (FLO-586 / Phase 6.2)

> **Definition owner:** [FLO-586](/FLO/issues/FLO-586) (Phase 6.2 metrics + alerting
> design slice). **Parent:** [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 — production
> observability, security & ops) acceptance criterion #1. **Strategy:** Phase 6
> ([FLO-231](/FLO/issues/FLO-231) §3, Phase 6.2).
>
> **This is a design artifact, not a deployment.** No instrumentation is wired, no
> dashboards are pushed, no budget is committed, and no prod VM is presupposed by
> this document. It defines the **full observability surface** for the production
> target topology ([FLO-245](/FLO/issues/FLO-245) ADR; mirrored in
> [hosting-quote](/FLO/issues/FLO-231#document-hosting-quote)) so that Phase 6.1
> provisioning ([FLO-249](/FLO/issues/FLO-249)) can wire the collectors and
> Phase 6.2 ops ([FLO-533](/FLO/issues/FLO-533)) can arm the alerts the moment
> the board endorses Phase 6 ([609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc)).
> The metric set is identical regardless of which cloud the board picks — it
> follows the same endorsement-independent precedent as
> [FLO-581](/FLO/issues/FLO-581) (event-day runbook) and
> [FLO-544](/FLO/issues/FLO-544) (staging-VM runbook).

## TL;DR

- **Five tiers, one stack.** App (Frappe `bench` / gunicorn + RQ), DB (managed
  MariaDB), Redis (dedicated socketio adapter — the WS-scale bottleneck per
  [FLO-127](/FLO/issues/FLO-127)), WebSocket (N node socketio workers behind an
  nginx sticky L7 LB), and Edge (Cloudflare).
- **Targets are already empirically grounded.** Thresholds below are derived
  from the [FLO-365](/FLO/issues/FLO-365) 15k data-tier stress
  ([findings](scale-15k-findings.md) + [profile](scale-15k-profile-report.json))
  and the [FLO-347](/FLO/issues/FLO-347) §8 WS targets (signals S4–S8 on the
  [launch go/no-go gate](launch-go-no-go.md)). They are not estimates.
- **The WS tier is the dominant axis.** 15k concurrent connections is the load
  that broke two independent walls in
  [FLO-53](/FLO/issues/FLO-53) §8 — the auth-callback wall
  ([FLO-116](/FLO/issues/FLO-116)) and the connection-setup wall
  ([FLO-121](/FLO/issues/FLO-121)). Redis adapter health and per-worker socket
  count are the metrics that will tell us first if the tier is degrading.
- **Three dashboards.** (a) event-day real-time ops board (the on-call screen),
  (b) day-to-day platform health board, (c) incident-triage view.
- **Paging policy is two-tier.** **Critical** pages the event-day on-call
  immediately and links the [FLO-581](/FLO/issues/FLO-581) runbook;
  **warning** posts to the ops channel and opens a tracking issue, no page.
- **Phase 6.1 must close seven instrumentation gaps** (§6) — none of the
  collectors below ship enabled by default on a fresh `bench`.

## 0. Topology under observation

Canonical target from [FLO-245](/FLO/issues/FLO-245) /
[hosting-quote](/FLO/issues/FLO-231#document-hosting-quote):

```
                    Cloudflare edge (CDN + WAF + rate-limit FLO-294)
                              │ TLS / WS upgrade
                    nginx sticky L7 LB (per-site upstream hash on sid)
                              │
            ┌─────────────────┴─────────────────┐
       gunicorn (bench web)              N × node socketio workers
            │                                    │
            │   ┌────────── Frappe RQ workers ───┘
            ▼   ▼
      managed MariaDB ◄──── dedicated adapter Redis (@socket.io/redis-adapter)
                          + cache Redis (separate — FLO-127 §2)
```

What this means for observability:

- **Sticky LB ⇒ affinity is a first-class signal.** A sticky-session hash miss
  is not fatal (the adapter fans broadcasts through Redis pub/sub), but a
  sustained miss rate means the LB hash is wrong and the auth-cache hit rate
  ([FLO-116](/FLO/issues/FLO-116)) collapses back to per-connection HTTP callbacks
  — the §8 auth-callback wall.
- **Two Redis instances, two monitor stanzas.** The adapter Redis (the WS-scale
  bottleneck) and the cache Redis are separate; the adapter Redis is the one
  whose `connected_clients`, `used_memory_rss`, and `pubsub` counts gate the
  15k-event SLO. The cache Redis is commodity.
- **N socketio workers ⇒ per-worker metrics aggregate to cluster totals.** The
  on-call board shows cluster totals + a per-worker sparkline so a single
  degrading worker is visible against its peers (the [FLO-121](/FLO/issues/FLO-121)
  round-robin distribution signature).

## 1. Metric families

Each tier below lists: **metric → source → collection method → target window**
(the 15k-event load profile). Targets marked **baseline** mean "establish the
launch value as the new floor; alert on regression" rather than a fixed number.

### 1.1 App tier — Frappe `bench` (gunicorn + RQ)

| Metric | Source | Collection | Warning | Critical |
| --- | --- | --- | --- | --- |
| `http.requests` (req/s, by route class: `register`, `attendance`, `realtime-connect`, `other`) | gunicorn access log + Frappe `statsd` | `statsd` client in gunicorn workers → `statsd_host` in `site_config.json` | > 2× 24h baseline for the route class, sustained 5 min | > 5× baseline OR registration-burst rate exceeds throttle cap × N workers ([FLO-319](/FLO/issues/FLO-319)) |
| `http.request.duration_ms` p50 / p95 / p99 (per route class) | gunicorn `statsd` timer (`frappe.request_time`) | same `statsd` pipeline | registration p95 > **300 ms**; attendance-write p95 > **500 ms**; list-view p95 > **150 ms** (the [FLO-517](/FLO/issues/FLO-517) fix holds this at ~16 ms — alert long before) | any route-class p95 > **1 s** (the §8 WS SLO ceiling; HTTP should be well under) OR any route-class p99 > **2 s** |
| `http.5xx` rate (incl. unhandled exceptions) | gunicorn access log + `Sentry`/error log | log-derived counter (Logpush / filebeat tail) | > 0.5% of requests for 2 min | > 2% of requests for 1 min, OR any single unhandled traceback |
| `rq.queue_depth` (per queue: `default`, `long`, `short`) | RQ `rq.jobs` / `rq.stats` | `rq info` poll (10s) → exporter | `long` queue > **1,000** jobs; any queue growing monotonically for 5 min | `long` queue > **10,000** OR any queue depth growing for 15 min (the bulk-attendance / bulk-reporting backlog — [FLO-15](/FLO/issues/FLO-15)) |
| `rq.job_failure_rate` | RQ failed-job registry | exporter | > 1% failed/5 min | > 5% failed/5 min (silent bulk-path failure) |
| `gunicorn.workers.idle` / `busy` ratio | gunicorn statsd / `guv` worker stats | statsd gauge | busy ratio > 80% for 5 min | busy ratio == 100% for 2 min (worker saturation; new requests queue) |
| `frappe.db.queries_per_request` | Frappe dev flag `developer_mode`-only; in prod, sample via `statsd` if `log-level` raised | periodic audit (not continuous) | — | not alertable continuously; flag in the triage view from access-log spans |

> The 15k profile anchors: bulk attendance writes ~5,000 rows/s (~97 ms per
> 500-row batch — [FLO-365](/FLO/issues/FLO-365)); `registration.dashboard` ~2.5 ms;
> `realtime.room_join_scope` ~1.2 ms; `attendance.aggregate` rollup sub-ms (the
> 4.06 ms `COUNT(*)` anti-pattern is a regression signal, not a target).

### 1.2 DB tier — managed MariaDB

| Metric | Source | Collection | Warning | Critical |
| --- | --- | --- | --- | --- |
| `db.connections.active` vs `max_connections` | `SHOW STATUS LIKE 'Threads_connected'` / managed-DB monitor | exporter (`mariadb_performance_schema` / `mysqld_exporter`) / Frappe Cloud DB panel | active > 70% of pool | active > 90% of pool (connection exhaustion blocks new requests) |
| `db.slow_queries` rate (over `long_query_time`) | MariaDB `slow_query_log` | log-derived counter | > 10/min | > 50/min OR any single query > 5 s (re-run [`scripts/dev/profile-15k-scale.sh`](../../scripts/dev/profile-15k-scale.sh) against the prod-shaped data — see the [PERF backlog](scale-15k-findings.md#prioritized-performance-backlog)) |
| `db.buffer_pool_hit_ratio` | `SHOW ENGINE INNODB STATUS` / `innodb_buffer_pool_reads` vs `reads` | exporter | < 99% | < 95% (working set no longer fits in RAM — index regression or seed leak) |
| `db.lock_waits` rate | `performance_schema.events_waits_summary_global_by_event_name` (`wait/synch/mutex/...`) | exporter | > 5/s for 2 min | > 20/s for 2 min (the [PERF-BULK-FORUPDATE](scale-15k-findings.md#high) class — per-member `SELECT … FOR UPDATE` serializing a bulk batch) |
| `db.replication_lag_seconds` | managed-DB monitor (if read-replica in use) | provider API / exporter | > 5 s | > 30 s (scoped reads from a replica serve stale room-scope decisions — [FLO-127](/FLO/issues/FLO-127) §6.2 scope resolution) |
| `db.deadlocks` rate | `SHOW ENGINE INNODB STATUS` | exporter / log tail | > 0/min | > 0/s (any deadlock in the bulk-write path is a correctness signal, not just perf) |

### 1.3 Redis tier — dedicated socketio adapter (the WS-scale bottleneck)

> Per [FLO-127](/FLO/issues/FLO-127) the shared dev `redis_socketio == redis_cache`
> stalls under 8× adapter pub/sub at the 15k burst. The production topology
> separates them; **this section monitors the adapter Redis only** (the cache
> Redis is commodity and gets the standard `used_memory` / `evicted_keys` pair,
> not duplicated here).

| Metric | Source | Collection | Warning | Critical |
| --- | --- | --- | --- | --- |
| `redis.connected_clients` (adapter) | `INFO clients` | `redis_exporter` | > 70% of `maxclients` | > 90% of `maxclients` (one client per socketio worker × N + Frappe publisher; a sudden jump = connection leak in a worker) |
| `redis.used_memory_rss` vs `maxmemory` | `INFO memory` | `redis_exporter` | > 70% of `maxmemory` | > 90% of `maxmemory` (the adapter's room-tracking keys are the working set — eviction here means a broadcast misses a subscriber) |
| `redis.evicted_keys` rate | `INFO stats` (`evicted_keys`) | `redis_exporter` | > 0/s | > 10/s (adapter keys must not evict; `maxmemory-policy=noeviction` is the production setting — eviction = config drift) |
| `redis.pubsub_channels` (count of active channels incl. the per-site socketio namespace) | `PUBSUB CHANNELS` | exporter custom probe | baseline | sudden drop > 50% (a worker lost its subscriber = broadcast gap) |
| `redis.pubsub_numsub_socketio` (subscriber count on the socketio events channel) | `PUBSUB NUMSUB <events-channel>` | exporter custom probe | < N (expected one subscriber per worker) | < N−1 (a worker's adapter subscriber is down → cross-worker broadcast gap — see [FLO-121](/FLO/issues/FLO-121) §"Cross-worker fan-out") |
| `redis.cmd_latency_p95` (`PUBLISH`, `SUBSCRIBE`) | `redis-cli --latency` / `INFO commandstats` | exporter | p95 > 5 ms | p95 > 50 ms (the adapter fan-out path is the WS broadcast latency floor — see [FLO-53](/FLO/issues/FLO-53) §8) |

### 1.4 WebSocket tier — N × node socketio workers

| Metric | Source | Collection | Warning | Critical |
| --- | --- | --- | --- | --- |
| `ws.connections.active` (cluster total) | `io.engine.clientsCount` summed across workers | custom `/metrics` endpoint per worker → scraped | > 14,000 approaching 15k cap | > 15,000 (we have only load-proven 15k — beyond is unknown territory) |
| `ws.connections.per_worker` (round-robin distribution) | per-worker `clientsCount` | custom `/metrics` | any worker > 1.5× the mean (sticky-hash skew) | any worker > 2× the mean OR any worker at 0 (the [FLO-121](/FLO/issues/FLO-121) round-robin signature broke — recheck LB hash) |
| `ws.connect_rate` / `ws.disconnect_rate` (per second) | engine.io handshake events | custom `/metrics` | connect rate > 100/s sustained (event-opening burst) | disconnect rate > 500/s (mass disconnect = LB restart, adapter Redis blip, or auth-cache invalidation storm) |
| `ws.rooms.count` (active event rooms) | `io.sockets.adapter.rooms.size` | custom `/metrics` | baseline | sudden drop > 50% in 1 min (rooms collapsing without a closing event = mass silent disconnect) |
| `ws.broadcast.fanout` (msgs/s, per channel class: `attendance`, `notification`, `room-update`) | publisher-side counter on `frappe.publish_realtime` | Frappe statsd | baseline | any fan-out queue backing up (the [FLO-107](/FLO/issues/FLO-107) "broadcasts reached zero clients" symptom) |
| `ws.lb.affinity_miss_rate` (sticky-hash miss %) | nginx upstream-hash log / LB stats | nginx `stub_status` + log-derived | > 5% for 5 min | > 20% (a miss storm cascades into the §8 auth-callback wall — the [FLO-116](/FLO/issues/FLO-116) cache hit rate collapses) |
| **Synthetic SLO** `flock_ws_connect_duration` p95 | k6 metric, `load/ws_event_room.js` | continuous SLO monitor (not just launch gate) — see §6 gap G4 | p95 > **500 ms** (the §8 target is < 1 s; warn at half) | p95 > **1 s** (signal S4 — no-go #9 on the [launch gate](launch-go-no-go.md#no-go-conditions)) |
| **Synthetic SLO** `flock_ws_broadcast_latency` p95 | k6 metric, same run | continuous SLO monitor | p95 > **500 ms** | p95 > **1 s** (signal S5) |
| **Synthetic SLO** `flock_ws_receive_errors` count | k6 Counter, same run | continuous SLO monitor | > 0/min | > 0/s (signal S6 — any error is the [FLO-107](/FLO/issues/FLO-107)/[FLO-116](/FLO/issues/FLO-116) symptom recurring) |
| **Synthetic SLO** `ws.sessions_established_pct` | k6 VU connect-success | continuous SLO monitor | < 99.5% | < 99% (signal S7) |

### 1.5 Edge tier — Cloudflare

| Metric | Source | Collection | Warning | Critical |
| --- | --- | --- | --- | --- |
| `edge.requests` (req/s) | Cloudflare Analytics | GraphQL Analytics API / Logpush | > 3× baseline for the route | > 10× baseline (viral registration spike — the [hosting-quote](/FLO/issues/FLO-231#document-hosting-quote) risk #3 metered-CPU guard) |
| `edge.cache_hit_ratio` (static assets) | Cloudflare Analytics | GraphQL API | < 80% | < 50% (origin pulling statics = Gunicorn-billed CPU wasted) |
| `edge.ws.connections` | Cloudflare Spectrum / WS analytics | GraphQL API | baseline | sudden drop > 50% (edge-side WS termination issue, distinct from origin drop) |
| `edge.rate_limit_tripped` count (registration + realtime-connect rules, [FLO-294](/FLO/issues/FLO-294)) | Cloudflare WAF / Rate Limiting logs | Logpush → counter | > 100/min | > 1,000/min AND `edge.requests` rising (active abuse, not a flash crowd — the [FLO-319](/FLO/issues/FLO-319) app-limiter is the inner ring; this is the outer) |
| `edge.origin_5xx` rate | Cloudflare Analytics | GraphQL API | > 1% for 2 min | > 5% for 1 min (Cloudflare is observing our 5xx — cross-correlate with §1.1 `http.5xx`) |
| `edge.tls_errors` rate | Cloudflare SSL/TLS analytics | GraphQL API | > 0/min | > 0/s (cert or SNI misconfig) |

## 2. Dashboards

Three boards. Each is a *panel list*, not a finished Grafana JSON — Phase 6.1
implements against this spec. Panels marked **LIVE** update ≤ 5 s (event-day);
**HIST** panels are 1 h rolling with a 24 h comparison.

### 2.1 Event-day real-time ops board (the on-call screen)

The screen on the wall during the [FLO-581](/FLO/issues/FLO-581) T-0 window.
Every panel **LIVE**, time window **last 15 min** unless noted.

1. **Headline SLO strip** (top, large numbers, traffic-light):
   - `ws.connections.active` / 15,000 (the cap we load-proved)
   - `flock_ws_connect_duration` p95 last 5 min (green < 500 ms, amber < 1 s, red ≥ 1 s)
   - `flock_ws_broadcast_latency` p95 last 5 min (same thresholds)
   - `http.5xx` rate (green = 0)
   - `flock_ws_receive_errors` last 5 min (green = 0)
2. **WS cluster shape** — `ws.connections.per_worker` sparkline (N series); the [FLO-121](/FLO/issues/FLO-121) round-robin distribution at a glance.
3. **Sticky-LB affinity** — `ws.lb.affinity_miss_rate` line + `redis.pubsub_numsub_socketio` gauge (the §8 auth-callback wall's early indicators).
4. **Adapter Redis panel** — `redis.used_memory_rss` vs `maxmemory`, `redis.connected_clients`, `redis.evicted_keys` rate, `redis.cmd_latency_p95` (`PUBLISH`/`SUBSCRIBE`). This is the single most strategically valuable panel — Redis adapter health is the WS-scale bottleneck.
5. **App throughput** — `http.requests` stacked by route class + `rq.queue_depth` per queue (bulk-attendance / bulk-reporting backlog visibility).
6. **App latency** — `http.request.duration_ms` p95 by route class (registration, attendance-write, list-view).
7. **DB pressure** — `db.connections.active` / pool, `db.slow_queries` rate, `db.lock_waits` rate.
8. **Edge** — `edge.requests`, `edge.cache_hit_ratio`, `edge.rate_limit_tripped` count.
9. **Synthetic SLO** — the four continuous-probe k6 metrics (§1.4 synthetic rows) as a row of single-stat panels. These are the §8 / launch-gate signals S4–S7 reproduced continuously, not just at the launch gate.
10. **On-call context strip** — pinned links to [event-day-runbook](event-day-runbook.md) (the [FLO-581](/FLO/issues/FLO-581) T-0 section), [launch-go-no-go](launch-go-no-go.md#no-go-conditions) no-go list, and the [scale findings](scale-15k-findings.md).

### 2.2 Day-to-day platform health board

The board the DevOps engineer glances at daily outside event windows. **HIST**,
window **last 24 h** with **7-day** comparison.

1. **Uptime strip** — gunicorn, socketio cluster, MariaDB, adapter Redis, cache Redis, edge (each green/red over 24 h).
2. **Traffic trend** — `http.requests` (total + by route class), `edge.requests`, `ws.connections.active` peak.
3. **Error budget** — `http.5xx` % over 7 days; `rq.job_failure_rate`; `db.deadlocks`.
4. **DB capacity** — `db.buffer_pool_hit_ratio` 7-day, `db.connections.active` peak vs pool, slow-query trend, index regression detector (`attendance.aggregate` p95 drifting toward the `COUNT(*)` anti-pattern baseline 4.06 ms).
5. **Redis adapter trend** — `redis.used_memory_rss`, `redis.connected_clients`, eviction count (should always be 0 under `noeviction`).
6. **Backup + restore drill status** — last successful [`scripts/dev/restore-drill.sh`](../../scripts/dev/restore-drill.sh) run timestamp + result (cross-link [backup-restore.md](backup-restore.md)); alert if > 7 days stale.
7. **Coverage + CI** — branch coverage at HEAD vs the 91.29% launch bar (signal S1), last CI run status.
8. **Cost meter** — Frappe Cloud CPU-hours/day against the [hosting-quote](/FLO/issues/FLO-231#document-hosting-quote) budget envelope ($50–$100 staging, $250–$500 prod).

### 2.3 Incident-triage view

The board the on-call opens when an alert fires. Optimized for **correlation**,
not overview. **LIVE**, window **last 1 h** with drill-down.

1. **Fired-alert header** — the alert name, severity, firing-since, runbook link (deep-link into [event-day-runbook](event-day-runbook.md) incident section).
2. **Correlated tier view** — three stacked panels aligned on the same time axis: WS tier (connections + per-worker), adapter Redis (`used_memory`, `connected_clients`, `cmd_latency_p95`), DB (`connections`, `slow_queries`, `lock_waits`). The pattern of which tier moves first localizes the incident.
3. **Recent-deploy marker** — vertical line at the last `master`→staging / staging→prod promotion (cross-link [deploy-runbook](../development/deploy-runbook.md)); most incidents are deploy-correlated.
4. **5xx trace list** — last 20 unhandled tracebacks with request-id + deep-link to the log stream.
5. **Rate-limit / throttle activity** — `edge.rate_limit_tripped` + the app-level [FLO-319](/FLO/issues/FLO-319) limiter events (`flock_os.rate_limit` rejections); distinguishes abuse from capacity.
6. **Top-K slow queries** — from the slow-query log, with the EXPLAIN plan link (re-run [`scripts/dev/profile-15k-scale.sh`](../../scripts/dev/profile-15k-scale.sh) for the comparison baseline).

## 3. Alerting — thresholds, paging, escalation

### 3.1 Severity rubric

- **Critical (page):** a §8 WS SLO is violated, the adapter Redis is at
  exhaustion, the DB is at connection exhaustion, or `flock_ws_receive_errors`
  is non-zero. The 15k-event SLO will not hold without intervention.
- **Warning (no page):** a metric is trending toward a critical threshold or a
  non-SLO regression is observed. Posts to `#ops`, opens a tracking issue, and
  sits on the day-to-day board. Does not interrupt the event.
- **Info (log only):** baseline drift that has no SLO impact yet. Recorded for
  retrospective; no notification.

### 3.2 Paging policy

| Severity | Routes to | Channel | SLA |
| --- | --- | --- | --- |
| Critical during event window ([FLO-581](/FLO/issues/FLO-581) T-0 to T+24h) | On-call engineer (rotated, named in the [event-day runbook](event-day-runbook.md) roster) | PagerDuty / Phone + SMS + push | ack < 1 min, respond < 5 min |
| Critical outside event window | On-call engineer | PagerDuty / Push only | ack < 5 min, respond < 30 min |
| Warning (always) | `#ops` channel + tracking issue | Slack/Teams only | triage < 4 business hours |
| Info | Metrics log only | none | none |

> **Single-channel principle.** All alerts — page or no page — also post to
> `#ops` so the warning→critical promotion is visible in thread context. Pages
> do not bypass the channel; they layer on top of it.

### 3.3 Escalation to the event-day runbook

Every **critical** alert's notification body includes:

1. The metric, the observed value, the threshold, and the firing-since time.
2. A deep link to the corresponding panel on the **incident-triage view** (§2.3).
3. A deep link to the matching section of
   [`docs/operations/event-day-runbook.md`](event-day-runbook.md) ([FLO-581](/FLO/issues/FLO-581))
   — the runbook owns the human procedure; this doc owns the *detection*. The
   two are deliberately separate artifacts so the alerting layer can be retuned
   without rewriting the procedure, and vice versa.
4. The relevant no-go condition from the
   [launch gate](launch-go-no-go.md#no-go-conditions) if the alert maps to one
   (e.g. a `flock_ws_connect_duration` critical is no-go #9).

### 3.4 Alert-routing table (critical rows only — see §1 for the full threshold set)

| Alert | Tier | Threshold | No-go link | Runbook section |
| --- | --- | --- | --- | --- |
| `WSConnectSLOBreach` | WS (synthetic) | `flock_ws_connect_duration` p95 > 1 s for 1 min | [no-go #9](launch-go-no-go.md#no-go-conditions) / signal S4 | [FLO-581](/FLO/issues/FLO-581) T-0 § WS |
| `WSBroadcastSLOBreach` | WS (synthetic) | `flock_ws_broadcast_latency` p95 > 1 s for 1 min | no-go #9 / S5 | [FLO-581](/FLO/issues/FLO-581) T-0 § WS |
| `WSErrorCounterNonZero` | WS (synthetic) | `flock_ws_receive_errors` > 0/s | no-go #9 / S6 | [FLO-581](/FLO/issues/FLO-581) T-0 § WS — suspect [FLO-107](/FLO/issues/FLO-107)/[FLO-116](/FLO/issues/FLO-116) regression |
| `WSessionsDropped` | WS (synthetic) | `ws.sessions_established_pct` < 99% | no-go #9 / S7 | [FLO-581](/FLO/issues/FLO-581) T-0 § WS |
| `AdapterRedisNearMaxMemory` | Redis | `used_memory_rss` > 90% `maxmemory` for 2 min | no-go #6 (dedicated adapter Redis degraded) | [FLO-581](/FLO/issues/FLO-581) T-0 § Redis |
| `AdapterRedisEvicting` | Redis | `evicted_keys` > 0/s (policy drift under `noeviction`) | no-go #6 | [FLO-581](/FLO/issues/FLO-581) T-0 § Redis |
| `AdapterSubscribersLost` | Redis | `pubsub_numsub_socketio` < N−1 for 1 min | no-go #6 / [FLO-121](/FLO/issues/FLO-121) adapter | [FLO-581](/FLO/issues/FLO-581) T-0 § Redis — worker adapter subscriber down |
| `DBConnectionExhaustion` | DB | active connections > 90% pool | — | [FLO-581](/FLO/issues/FLO-581) T-0 § DB |
| `App5xxSpike` | App | `http.5xx` > 2% for 1 min OR unhandled traceback | — | [FLO-581](/FLO/issues/FLO-581) T-0 § App |
| `GunicornSaturation` | App | workers busy == 100% for 2 min | — | [FLO-581](/FLO/issues/FLO-581) T-0 § App |
| `EdgeRateLimitStorm` | Edge | `edge.rate_limit_tripped` > 1,000/min AND `edge.requests` rising | no-go #5 ([FLO-294](/FLO/issues/FLO-294)) | [FLO-581](/FLO/issues/FLO-581) T-0 § Edge |
| `RealtimeTierCollapsed` | WS | socketio worker count < N post-deploy (signal S8) | [no-go #3](launch-go-no-go.md#no-go-conditions) | [`migration-runbook.md` §6](migration-runbook.md#6-the-realtime-tier-across-migrations) |

## 4. Source mapping

Where each metric physically comes from in the chosen topology. The right-hand
column is the **collection mechanism Phase 6.1 must provision**; today none of
these are wired on a fresh `bench`.

| Tier | Metric family | Physical source | Collection mechanism |
| --- | --- | --- | --- |
| App | request rate, latency, 5xx | gunicorn workers + Frappe `statsd` integration | set `statsd_host` in `site_config.json`; Frappe's built-in statsd client emits `frappe.request_time` etc. — wire gunicorn `statsd-host` too |
| App | RQ queue depth, failure rate | RQ registry on the adapter/cache Redis | `rq info` poll → Prometheus exporter; RQ dashboard is human-only |
| App | gunicorn worker saturation | gunicorn worker stats | gunicorn `--statsd-host` (or `guv`) → same statsd pipeline |
| DB | connections, slow queries, buffer pool, lock waits, deadlocks | MariaDB `information_schema` / `performance_schema` / `slow_query_log` | `mysqld_exporter` / `mariadb_performance_schema` Prometheus exporter (self-hosted) OR Frappe Cloud managed-DB monitor (managed) |
| DB | replication lag | managed-DB provider API (if a read-replica is provisioned) | provider exporter (only if a replica exists — not in the base topology) |
| Redis (adapter) | clients, memory, evictions, cmd latency | `INFO` commands on the adapter Redis instance | `redis_exporter` against the adapter Redis URL (separate from the cache Redis exporter) |
| Redis (adapter) | pubsub channel/numsub | `PUBSUB CHANNELS` / `PUBSUB NUMSUB` | custom exporter probe — `redis_exporter` does not emit pubsub counts natively (gap G3) |
| WS | connections, per-worker, rooms | `io.engine.clientsCount`, `io.sockets.adapter.rooms` inside each node socketio worker | custom `/metrics` endpoint per worker (Prometheus scrape) — flock_os owns this (gap G1) |
| WS | connect/disconnect rate, broadcast fanout | engine.io events + `frappe.publish_realtime` publisher | same custom `/metrics` endpoint (gap G1) |
| WS | sticky-LB affinity miss rate | nginx upstream-hash decisions | nginx `stub_status` + log-derived counter; requires `stub_status on;` on a localhost location (gap G5) |
| WS (synthetic SLO) | connect p95, broadcast p95, receive errors, sessions % | k6 metrics from `load/ws_event_room.js` | continuous SLO probe (not just launch gate) — a scheduled k6 run at reduced load (gap G4) |
| Edge | requests, cache hit, WS, rate-limit trips, origin 5xx, TLS errors | Cloudflare Analytics + WAF/Rate Limiting logs | Cloudflare GraphQL Analytics API (panel data) + Cloudflare Logpush to object storage (rate-limit / WAF event stream); both wired in the Cloudflare dashboard (gap G6) |

## 5. Dashboards / alerting stack — implementation choice

This section is **recommendation, not commitment** (no budget). It exists so
Phase 6.1 has a default rather than re-discovering the tradeoffs.

- **Time-series + dashboards:** **Grafana + Prometheus** as the default. The
  collector pattern in §4 (Prometheus exporters, `/metrics` endpoints) is
  Prometheus-native, and Grafana's panel-list spec maps 1:1 to §2. Frappe
  Cloud's built-in dashboards cover app + DB at the platform level; the
  Grafana layer is where the cross-tier correlation panels (§2.1 #4, §2.3 #2)
  live because they mix Cloudflare + bench + DB + Redis in one view.
- **Alternative:** if the board picks Frappe Cloud Server plan and we want to
  minimize ops surface, the **Frappe Cloud built-in dashboards + Cloudflare
  Analytics** pair covers §1.1, §1.2, §1.5 without standing up Grafana. The
  gap is §1.3 (adapter Redis pubsub) and §1.4 (WS worker metrics) — those
  still need the custom exporters. The doc does not pick; Phase 6.1 picks
  based on whether the launch partner event needs the cross-tier view.
- **Paging:** **PagerDuty** (or equivalent — Opsgenie) for the critical path;
  Slack/Teams for warnings. The `#ops` channel is the common substrate.
- **Synthetic SLO probe:** a scheduled k6 run (e.g. every 15 min at 1,000 VUs
  — not the 15k launch gate) feeding the four §1.4 synthetic metrics. The
  full 15k run stays a launch-gate / pre-event exercise, not continuous.

## 6. Instrumentation gaps Phase 6.1 must close

Each gap is the delta between this design and a fresh `bench` after
[FLO-249](/FLO/issues/FLO-249) provisions the VM. They are the implementation
work Phase 6.1 owes Phase 6.2.

| # | Gap | What Phase 6.1 lands | Cited metric |
| --- | --- | --- | --- |
| G1 | No `/metrics` endpoint on the node socketio workers | a `prom-client`-based `/metrics` route per worker emitting `clientsCount`, per-room counts, connect/disconnect counters, broadcast-fanout counters — wired alongside the [FLO-107](/FLO/issues/FLO-107) handler / [FLO-116](/FLO/issues/FLO-116) cache / [FLO-121](/FLO/issues/FLO-121) adapter | §1.4 (all rows except synthetic + LB) |
| G2 | No `statsd_host` in `site_config.json`; gunicorn not emitting statsd | set `statsd_host` + gunicorn `--statsd-host`; point at a `statsd` → Prometheus bridge | §1.1 |
| G3 | `redis_exporter` does not emit pubsub channel/numsub counts | small custom exporter (cron or long-running probe) issuing `PUBSUB NUMSUB <events-channel>` against the adapter Redis | §1.3 `redis.pubsub_channels`, `redis.pubsub_numsub_socketio` |
| G4 | The §8 k6 metrics run only as a launch gate, not continuously | a scheduled low-VU k6 probe feeding the four synthetic metrics; the full 15k run stays a launch-gate exercise | §1.4 synthetic SLO rows; signals S4–S7 |
| G5 | nginx `stub_status` not exposed; sticky-hash decisions not logged | `stub_status on;` on a localhost location + an upstream-hash log format that surfaces the chosen backend per request | §1.4 `ws.lb.affinity_miss_rate` |
| G6 | Cloudflare Logpush not configured; GraphQL Analytics not pulled | a Logpush job to object storage for WAF/Rate-Limit events + a periodic GraphQL puller for the analytics panels | §1.5 (all rows) |
| G7 | `mysqld_exporter` / Frappe Cloud managed-DB monitor not wired | depending on the board's cloud choice: self-hosted → `mysqld_exporter`; Frappe Cloud managed → confirm the managed-DB monitor exposes these as an export (some metrics in §1.2 may need the slow-query log tailed if the managed monitor is panel-only) | §1.2 (all rows) |

G1 and G4 are the two gaps that block arming the **critical** alerts on the WS
SLO — they are the highest-priority close for Phase 6.1. The rest arm warning
and day-to-day panels.

## 7. Acceptance criteria traceability

Maps this doc back to the [FLO-586](/FLO/issues/FLO-586) issue and the
[FLO-533](/FLO/issues/FLO-533) acceptance criterion #1.

- [FLO-586](/FLO/issues/FLO-586) "All four tiers (app/DB/Redis/WS) + edge covered with named metrics + source" → §1.1–§1.5 + §4.
- [FLO-586](/FLO/issues/FLO-586) "At least one dashboard proposed with a concrete panel list" → §2 (three boards, full panel lists).
- [FLO-586](/FLO/issues/FLO-586) "Warning + critical thresholds defined for the metrics that gate the 15k-event SLO, with a paging policy" → §1 (per-metric) + §3 (paging + escalation).
- [FLO-586](/FLO/issues/FLO-586) "Instrumentation gaps flagged for Phase 6.1" → §6 (G1–G7).
- [FLO-586](/FLO/issues/FLO-586) "No budget committed, no VM required, no presupposition of the board endorsement" → this doc commits no spend and names the [609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc) endorsement as a prerequisite for *implementation*, not for *design*.

## 8. Out of scope

- **Implementing the instrumentation / deploying the dashboards.** Lands with
  Phase 6.1 provisioning ([FLO-249](/FLO/issues/FLO-249)); the gaps in §6 are
  the implementation backlog.
- **The SEC-RL edge rate-limit policy itself** ([FLO-294](/FLO/issues/FLO-294),
  VM-dependent). This doc names the *telemetry* for it; the policy is owned by
  that issue.
- **Backup/restore drill** (already drafted per [FLO-533](/FLO/issues/FLO-533);
  runbook at [`backup-restore.md`](backup-restore.md)). This doc only names the
  "last successful drill > 7 days stale" warning on the day-to-day board.
- **The event-day procedure.** Owned by [FLO-581](/FLO/issues/FLO-581)
  ([`event-day-runbook.md`](event-day-runbook.md)). This doc owns *detection*;
  that doc owns *response*. The two are linked in §3.3.

## 9. Related

- Parent strategy: [FLO-231](/FLO/issues/FLO-231) §3 Phase 6.2.
- Parent epic: [FLO-533](/FLO/issues/FLO-533) (Phase 6.2 — observability, security & ops).
- Production target topology: [FLO-245](/FLO/issues/FLO-245) ADR; [hosting-quote](/FLO/issues/FLO-231#document-hosting-quote).
- WS scale tier (the dominant axis): [FLO-121](/FLO/issues/FLO-121) (scaled socketio), [FLO-127](/FLO/issues/FLO-127) (dedicated adapter Redis), [FLO-116](/FLO/issues/FLO-116) (auth cache), [FLO-107](/FLO/issues/FLO-107) (broadcast delivery); runbook [`ws-broadcast-delivery.md`](../development/ws-broadcast-delivery.md).
- 15k load profile (the threshold source): [FLO-365](/FLO/issues/FLO-365) [findings](scale-15k-findings.md) + [profile JSON](scale-15k-profile-report.json); §8 WS gate [FLO-347](/FLO/issues/FLO-347).
- Launch gate (signals S1–S15): [`launch-go-no-go.md`](launch-go-no-go.md) ([FLO-357](/FLO/issues/FLO-357)).
- Event-day procedure (the response layer this alerts into): [FLO-581](/FLO/issues/FLO-581) — [`event-day-runbook.md`](event-day-runbook.md).
- Staging VM provisioning (lands the §6 collectors): [FLO-544](/FLO/issues/FLO-544) / [FLO-249](/FLO/issues/FLO-249).
- Backup/restore drill + runbook: [`backup-restore.md`](backup-restore.md) ([FLO-288](/FLO/issues/FLO-288)).
- Migration runbook (realtime-tier no-go #3): [`migration-runbook.md` §6](migration-runbook.md#6-the-realtime-tier-across-migrations).
- Board endorsement (prerequisite for implementation, not for this design): [609ccd5d](/FLO/approvals/609ccd5d-13ca-4db2-8cbc-99069e0224cc).
