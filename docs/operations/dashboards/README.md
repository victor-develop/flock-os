# Grafana dashboard templates — Flock OS (FLO-694 Phase 6.2)

> **Definition owner:** [FLO-694](/FLO/issues/FLO-694). **Metric source of
> truth:** [`metrics-alerting-design.md`](../metrics-alerting-design.md) ([FLO-586](/FLO/issues/FLO-586)).
> **No-spend artifact:** these are importable templates, not a deployed stack.
> Phase 6.1 ([FLO-249](/FLO/issues/FLO-249)) stands up the Prometheus
> datasource + exporters the templates query against.

## What is here

Three Grafana dashboard JSON templates, one per board in
[metrics-alerting-design.md §2](../metrics-alerting-design.md#2-dashboards):

| File | Board | Live? | Window | Panels |
|------|-------|-------|--------|--------|
| `event-day-ops.json` | [§2.1](../metrics-alerting-design.md#21-event-day-real-time-ops-board-the-on-call-screen) event-day real-time ops (the on-call screen) | LIVE | last 15 min | headline SLO strip, WS cluster shape, sticky-LB affinity, adapter Redis, app throughput + DB pressure |
| `platform-health.json` | [§2.2](../metrics-alerting-design.md#22-day-to-day-platform-health-board) day-to-day platform health | HIST | last 24h, 7-day comparison | uptime strip, traffic + error budget, DB + Redis capacity trend, restore-drill staleness, coverage + CI |
| `incident-triage.json` | [§2.3](../metrics-alerting-design.md#23-incident-triage-view) incident-triage view | LIVE | last 1h | firing-alert header, correlated tier view (WS/Redis/DB aligned), 5xx + deploy markers, rate-limit activity, top-K slow queries, realtime-tier process count |

Every panel names its source metric from §1 of the metrics design and its
threshold from §1/§3, so the templates are a 1:1 rendering of the design — not a
re-derivation. Open the `description` field on each panel (or this directory's
parent design doc) for the rationale.

## Import

```bash
# Grafana UI: Dashboards → New → Import → upload the JSON, pick the Prometheus datasource.
# Or via provisioning (recommended for prod): symlink/copy these into the Grafana
# provisioning/dashboards directory — they import on startup, no UI action.
```

The templates use a `${DS_PROMETHEUS}` datasource variable, so they import into
any Grafana instance with a Prometheus datasource named (or aliased to)
`Prometheus`. Pick the datasource at the top of the board after import.

## Conventions

- **Prometheus-native.** Per the [metrics design §5 recommendation](../metrics-alerting-design.md#5-dashboards--alerting-stack--implementation-choice):
  Grafana + Prometheus is the default; the panel spec maps 1:1.
- **Metric names are the canonical §1 names**, prefixed `flock_` for the custom
  app/WS exporters (`flock_ws_*`, `flock_http_*`, `flock_rq_*`,
  `flock_rate_limit_*`) and the standard exporter names for the rest
  (`redis_*`, `mysql_*`, `up`, `ALERTS`). Phase 6.1's
  [instrumentation gaps G1–G7](../metrics-alerting-design.md#6-instrumentation-gaps-phase-61-must-close)
  are the collectors that produce these series.
- **Thresholds are the §1/§3 warning/critical values** encoded as panel
  threshold steps (green → yellow → red). The Grafana alerting layer (armed in
  Phase 6.1) is separate from the panel thresholds; the panel thresholds are the
  at-a-glance traffic-light, the alerting layer is the paging path.
- **Deploy markers** as annotations on the event-day + triage boards — most
  incidents are deploy-correlated, so the vertical line at each promotion is the
  fastest triage signal.
- **Stable `uid`** per board (`flock-event-day`, `flock-platform-health`,
  `flock-incident-triage`) so re-imports update in place rather than duplicating.

## Out of scope

- **Deploying Grafana / Prometheus** — Phase 6.1 ([FLO-249](/FLO/issues/FLO-249)
  / [FLO-246](/FLO/issues/FLO-246)) provisions the stack.
- **The alerting rules** — defined in [metrics design §3](../metrics-alerting-design.md#3-alerting--thresholds-paging-escalation);
  armed in Phase 6.1 as Grafana alert rules / Prometheus alerting rules. These
  templates carry the panel thresholds (the traffic-light), not the paging rules.
- **The incident procedures** — [`incident-runbooks.md`](../incident-runbooks.md).
- **Cloudflare / Frappe Cloud built-in panels** — covered by those platforms'
  native dashboards; these templates are the cross-tier correlation layer that
  mixes Cloudflare + bench + DB + Redis in one view.

## Related

- Metric + threshold source: [`../metrics-alerting-design.md`](../metrics-alerting-design.md) ([FLO-586](/FLO/issues/FLO-586)).
- Incident response: [`../incident-runbooks.md`](../incident-runbooks.md) ([FLO-694](/FLO/issues/FLO-694)).
- Event-day timeline: [`../event-day-runbook.md`](../event-day-runbook.md) ([FLO-581](/FLO/issues/FLO-581)).
- Launch go/no-go gate: [`../launch-go-no-go.md`](../launch-go-no-go.md) ([FLO-357](/FLO/issues/FLO-357)).
