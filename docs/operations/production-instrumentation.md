# Production instrumentation runbook — Phase 6.2 (FLO-922 / FLO-586 §6)

> **Owner:** [FLO-922](/FLO/issues/FLO-922). **Parent:** [FLO-533](/FLO/issues/FLO-533).
> **Design:** [`metrics-alerting-design.md`](metrics-alerting-design.md) ([FLO-586](/FLO/issues/FLO-586)).
> **Alerting rules companion:** [`alerting.md`](alerting.md) ([FLO-897](/FLO/issues/FLO-897)).
>
> This is the **operational runbook** for the Phase 6.2 production
> instrumentation — the lowest-friction realization of the FLO-586 §5 default
> (Grafana + Prometheus) wired against the docker-compose staging tier that
> [FLO-889](/FLO/issues/FLO-889)'s cloudflared tunnel makes reachable. It is
> the home of the seven instrumentation gaps (G1–G7) the design flagged and
> the procedure for arming / exercising / restoring each.

## TL;DR — what shipped

| Gap | Component | Wired by | Status |
| --- | --- | --- | --- |
| G1 | per-worker socketio `/metrics` via `prom-client` | `realtime/metrics/flock_prometheus.js` + `scripts/dev/wire-socketio-metrics.sh` + the `after_migrate` hook + `Dockerfile` | ✅ wired |
| G2 | `statsd_host` + gunicorn `--statsd-host` + statsd→Prometheus bridge | `docker/entrypoint.sh` + `docker-compose.yml` web command + `deploy/statsd-exporter/mapping.yml` | ✅ wired |
| G3 | adapter-Redis `PUBSUB NUMSUB` exporter | `scripts/ops/redis-pubsub-exporter.py` | ✅ wired |
| G4 | scheduled low-VU k6 synthetic SLO probe | `scripts/ops/synthetic-slo-probe.sh` + `synth-probe` container | ✅ wired |
| G5 | nginx `stub_status` + sticky-upstream log format | `deploy/nginx/prod.conf` + `scripts/dev/nginx-socketio.conf.template` | ✅ wired |
| G6 | Cloudflare Logpush + GraphQL Analytics puller | tracks [FLO-249](/FLO/issues/FLO-249) | ⏳ follow-up |
| G7 | `mysqld_exporter` self-hosted piece + managed-DB monitor | self-hosted piece wired (mysqld-exporter container); managed-DB piece tracks [FLO-249](/FLO/issues/FLO-249) | ⏳ partial |

The Grafana + Prometheus + Alertmanager stack is a docker compose overlay
(`docker/observability.yml`) — bring it up alongside the live tier:

```bash
scripts/dev/docker-ws-tier.sh up                       # the flock-ws tier
docker compose -f docker/docker-compose.yml \
               -f docker/.runtime/ws-workers.yml \
               -f docker/observability.yml \
               --env-file docker/.env.docker up -d     # + monitoring stack
```

Endpoints:

- Prometheus: `http://localhost:9090`
- Alertmanager: `http://localhost:9093`
- Grafana: `http://localhost:3000` (admin/admin by default; override via
  `GRAFANA_ADMIN_PASSWORD` in `docker/.env.docker`).
- Per-worker socketio `/metrics`: `http://socketio-N:9100/metrics` (docker DNS;
  one URL per worker).
- Adapter Redis pubsub probe: `http://redis-pubsub-exporter:9300/metrics`.

## The five tiers — scrape targets

Prometheus reaches every tier the docker-compose stack exposes (one job per
tier — see `deploy/prometheus/prometheus.yml`):

| Tier | Job | Source |
| --- | --- | --- |
| App (Frappe `bench`) | `flock_app` | `ws-lb:8100/api/method/flock_os.metrics_scrape.scrape` (FLO-897) — auth via Flock Auditor service account. |
| App statsd | `flock_statsd` | `statsd-exporter:9102` — gunicorn worker stats + Frappe statsd. |
| DB (MariaDB) | `flock_db` | `mysqld-exporter:9104` (self-hosted G7 piece). |
| Redis adapter (pubsub) | `flock_redis_adapter_pubsub` | `redis-pubsub-exporter:9300` (G3 custom probe). |
| Redis cache / queue | `flock_redis_cache`, `flock_redis_queue` | `redis-cache-exporter:9121`, `redis-queue-exporter:9121`. |
| nginx LB | `flock_nginx_lb` | `nginx-exporter:9113` (reads `ws-lb:8090/stub_status`, G5). |
| WebSocket workers | `flock_ws_workers` | `socketio-N:9100/metrics` × N (G1 prom-client). |
| Synthetic probe | `flock_synth_probe` | `node-exporter:9100` (textfile collector surface for G4). |

## Arming alerts

The rules live in `deploy/prometheus/alerts.yml`. The FLO-586 §3.4 critical
rows are armed by default:

- `WSConnectSLOBreach`, `WSBroadcastSLOBreach`, `WSErrorCounterNonZero`,
  `WSessionsDropped` — the four §8 WS SLO signals (S4–S7). Arming basis: G1 +
  G4.
- `AdapterSubscribersLost` — cross-worker adapter subscriber loss. Arming
  basis: G3.
- `WSConnectionsNearCap`, `WSMassDisconnect` — cluster-shape critical.
- `App5xxSpike`, `RegistrationLatencyBudget`, `RQLongQueueBacklog` — App tier
  (FLO-897).

To exercise an alert end-to-end (the [FLO-533](/FLO/issues/FLO-533) AC#1
"≥1 critical alert fired + routed" proof), see
[`flo-922-evidence/acceptance.md`](flo-922-evidence/acceptance.md) for the
recorded drill that fired `WSErrorCounterNonZero` and routed the page to the
`critical-page` webhook.

## Restoring / unwiring

Each G1–G5 component is independently reversible:

- G1: `scripts/dev/wire-socketio-metrics.sh --revert --bench <bench>` (also
  reverts automatically on a `bench update` if the `after_migrate` hook is
  removed — see `flock_os/utils/realtime_setup.py`).
- G2: unset `STATSD_HOST` on the web service; the entrypoint writes
  `common_site_config.json` without `statsd_host` + gunicorn omits
  `--statsd-host`.
- G3/G4/G5: `docker compose ... down` for the monitoring stack; the tier
  continues unaffected (the monitoring stack is a non-invasive overlay).
- G5 (nginx): remove the `stub_status` server block + the `flock_sticky`
  log_format from `deploy/nginx/prod.conf` / the dev template.

## Acceptance — what FLO-922 closed

- [FLO-533](/FLO/issues/FLO-533) **AC#1** (metrics + dashboard + alert fired
  on staging) — Prometheus scrapes all 5 tiers, Alertmanager routes per §3.2,
  `WSErrorCounterNonZero` fired end-to-end.
- [FLO-533](/FLO/issues/FLO-533) **AC#2** (real-staging restore drill) —
  `scripts/dev/restore-drill.sh` exited 0 against the live staging DB
  (`flock_os.localhost`, 23 Flock DocTypes, 100,301 rows, full parity). See
  [`flo-922-evidence/acceptance.md`](flo-922-evidence/acceptance.md).
- G6/G7 (Cloudflare Logpush + managed-DB monitor) left as tracked follow-ups
  tied to [FLO-249](/FLO/issues/FLO-249) — not required for this issue's
  sign-off per the FLO-922 scope §4.

## Related

- Design: [`metrics-alerting-design.md`](metrics-alerting-design.md) ([FLO-586](/FLO/issues/FLO-586)).
- Alerting rules: [`alerting.md`](alerting.md) ([FLO-897](/FLO/issues/FLO-897)).
- Dashboards: [`dashboards/`](../../dashboards/) ([FLO-694](/FLO/issues/FLO-694)).
- Event-day response: [`event-day-runbook.md`](event-day-runbook.md) ([FLO-581](/FLO/issues/FLO-581)).
- Acceptance evidence: [`flo-922-evidence/acceptance.md`](flo-922-evidence/acceptance.md).
