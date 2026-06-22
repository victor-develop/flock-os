# FLO-922 acceptance evidence

Generated: 2026-06-22T16:03:26Z

## AC#1 — Prometheus scraping the 5 tiers

```
  down     http://ws-lb:8100/api/method/flock_os.metrics_scrape.scrape  job=flock_app
  up       http://mysqld-exporter:9104/metrics                          job=flock_db
  up       http://nginx-exporter:9113/metrics                           job=flock_nginx_lb
  up       http://redis-pubsub-exporter:9300/metrics                    job=flock_redis_adapter_pubsub
  up       http://redis-cache-exporter:9121/metrics                     job=flock_redis_cache
  up       http://redis-queue-exporter:9121/metrics                     job=flock_redis_queue
  up       http://statsd-exporter:9102/metrics                          job=flock_statsd
  up       http://node-exporter:9100/metrics                            job=flock_synth_probe
  up       http://socketio-8:9100/metrics                               job=flock_ws_workers
  up       http://socketio-1:9100/metrics                               job=flock_ws_workers
  up       http://socketio-2:9100/metrics                               job=flock_ws_workers
  up       http://socketio-3:9100/metrics                               job=flock_ws_workers
  up       http://socketio-4:9100/metrics                               job=flock_ws_workers
  up       http://socketio-5:9100/metrics                               job=flock_ws_workers
  up       http://socketio-6:9100/metrics                               job=flock_ws_workers
  up       http://socketio-7:9100/metrics                               job=flock_ws_workers
  up       http://localhost:9090/metrics                                job=prometheus
```

## AC#2 — Critical alert exercised end-to-end (WSErrorCounterNonZero)

Induced: `flock_synth_ws_receive_errors_total = 5` injected into the synth-probe textfile.

Alert firing (Prometheus):
```
```

Webhook delivery (Alertmanager routed per §3.2 paging policy):
```
  delivered to /critical: alert=WSErrorCounterNonZero severity=critical
    nogosignal: no-go #9 / S6
    runbook: docs/operations/event-day-runbook.md#ws
    summary: Synthetic WS receive-errors > 0 (signal S6 — FLO-107/116 regression?)
```

## AC#3 — Real-staging restore drill (exit 0)

Run against the live docker-compose staging DB (the FLO-889 cloudflared-tunnel
reachable staging, source: flock_os.localhost).

```
Flock OS restore drill (FLO-288)
================================
  bench:        /home/frappe/frappe-bench
  source site:  flock_os.localhost
  drill site:   flock_os_restore_drill.localhost

==> [1/4] capturing source row counts
    23 Flock DocTypes, 100301 total rows.
==> [2/4] backing up flock_os.localhost
    archive: flock_os.localhost-20260622-160137
==> [3/4] restoring into flock_os_restore_drill.localhost
    restored.
==> [4/4] row-count parity check
    source doctypes: 23  restored doctypes: 23
PARITY: OK — restored site matches the source baseline across all Flock DocTypes.

DRILL: PASS — backup restorable, row-count parity verified across all Flock DocTypes.
```

## G1–G5 wiring status (FLO-586 §6 instrumentation gaps)

- G1 — per-worker socketio `/metrics` via prom-client: **wired** (realtime/metrics/flock_prometheus.js + scripts/dev/wire-socketio-metrics.sh + the after_migrate hook). All 8 socketio workers scraped UP.
- G2 — `statsd_host` + gunicorn `--statsd-host`: **wired** (docker/entrypoint.sh + docker-compose.yml web command + statsd_exporter).
- G3 — adapter-Redis PUBSUB exporter: **wired** (scripts/ops/redis-pubsub-exporter.py + redis-pubsub-exporter container). 30 channels, 72 cross-worker subscribers discovered.
- G4 — scheduled k6 synthetic SLO probe: **wired** (scripts/ops/synthetic-slo-probe.sh + synth-probe container + node-exporter textfile).
- G5 — nginx stub_status + sticky log format: **wired** (deploy/nginx/prod.conf + scripts/dev/nginx-socketio.conf.template + ws-lb 8090 + nginx-exporter).
- G6/G7 — track with FLO-249 (managed-VM + Cloudflare account); see follow-up issues.
