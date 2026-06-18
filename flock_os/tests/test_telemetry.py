"""
Runtime telemetry lock for the FLO-10 D3/D5 revisit triggers (FLO-49 / ADR §8).

Pins the DevOps-owned observability half of the scale harness:

* The bulk-endpoint latency histogram math (bucket placement, cumulative counts,
  Prometheus-style quantile interpolation, the §8 p95 < 500ms gate surface).
* The :class:`TelemetryCollector` composition — injected D3/D5 sources + the
  histogram roll up into one :class:`TelemetrySnapshot` (the dashboard/scrape
  surface the ``load/`` k6 smoke + future Grafana read).
* The :func:`measure_bulk_latency` transport hook records exactly once per call
  and is import-clean without a bench (no Frappe).

Runs under plain ``pytest`` (no bench); the production Frappe source adapters
are lazy and asserted to stay import-clean (they never touch Frappe at import).
"""

from __future__ import annotations

import time

import flock_os.telemetry as telemetry
from flock_os.telemetry import (
	BULK_LATENCY_P95_BUDGET_SECONDS,
	LatencyHistogram,
	MariaDBMetrics,
	RedisMetrics,
	StaticMariaDBMetricsSource,
	StaticRedisMetricsSource,
	TelemetryCollector,
	measure_bulk_latency,
)

BUDGET = BULK_LATENCY_P95_BUDGET_SECONDS


# --------------------------------------------------------------------------- #
# Latency histogram — bucket placement + cumulative counts.
# --------------------------------------------------------------------------- #
def test_histogram_starts_empty_and_ignores_negative():
	h = LatencyHistogram(name="t")
	snap = h.snapshot()
	assert snap.count == 0
	assert snap.p95_seconds is None
	assert snap.mean_seconds is None
	h.observe(-1.0)
	assert h.snapshot().count == 0


def test_histogram_counts_land_in_the_right_cumulative_bucket():
	h = LatencyHistogram(name="t", buckets=(0.1, 0.5, 1.0))
	# 20ms → only the 0.1 bucket; 400ms → 0.1 and 0.5; 800ms → all three.
	h.observe(0.02)
	h.observe(0.4)
	h.observe(0.8)
	snap = h.snapshot()
	assert snap.count == 3
	# Cumulative: <=0.1 →1, <=0.5 →2, <=1.0(+inf) →3.
	assert snap.bucket_counts == (1, 2, 3, 3)


def test_histogram_auto_appends_infinity_catch_all_bucket():
	h = LatencyHistogram(name="t", buckets=(0.1, 0.5))
	# 10s lands in the auto-appended +Inf bucket; count still recorded.
	h.observe(10.0)
	snap = h.snapshot()
	assert snap.count == 1
	assert snap.buckets[-1] == float("inf")
	assert snap.bucket_counts[-1] == 1


def test_histogram_rejects_non_decreasing_buckets():
	import pytest

	with pytest.raises(ValueError):
		LatencyHistogram(name="t", buckets=(0.5, 0.1))


# --------------------------------------------------------------------------- #
# Latency histogram — quantile interpolation (the §8 p95 surface).
# --------------------------------------------------------------------------- #
def test_histogram_p95_under_budget_when_most_observes_are_fast():
	h = LatencyHistogram(name="bulk")
	# 100 fast (10ms) + 5 over budget (800ms) → p95 still in the fast mass.
	for _ in range(100):
		h.observe(0.01)
	for _ in range(5):
		h.observe(0.8)
	p95 = h.snapshot().p95_seconds
	assert p95 is not None
	assert p95 < BUDGET, f"p95 {p95}s must stay under the {BUDGET}s §8 budget"


def test_histogram_p95_breaches_when_enough_slow_observations():
	h = LatencyHistogram(name="bulk")
	for _ in range(50):
		h.observe(0.01)
	# 6% over budget → p95 lands in the slow mass, breaching the gate.
	for _ in range(4):
		h.observe(0.9)
	p95 = h.snapshot().p95_seconds
	assert p95 is not None
	assert p95 >= BUDGET


def test_histogram_quantiles_are_monotonic_and_within_range():
	h = LatencyHistogram(name="bulk")
	for i in range(200):
		h.observe((i % 20) * 0.05)  # spread 0–0.95s
	snap = h.snapshot()
	for lo, hi in (("p50_seconds", "p95_seconds"), ("p95_seconds", "p99_seconds")):
		assert getattr(snap, lo) <= getattr(snap, hi)
	assert snap.sum_seconds > 0
	assert snap.mean_seconds == snap.sum_seconds / snap.count


def test_histogram_reset_clears_observations():
	h = LatencyHistogram(name="bulk")
	h.observe(0.1)
	h.observe(0.2)
	h.reset()
	snap = h.snapshot()
	assert snap.count == 0
	assert all(c == 0 for c in snap.bucket_counts)


# --------------------------------------------------------------------------- #
# Collector composition — D3/D5 sources + histogram → snapshot (dashboard).
# --------------------------------------------------------------------------- #
def test_collector_snapshot_reads_injected_sources():
	redis = RedisMetrics(pubsub_messages_per_sec=1234.0, connected_clients=42, rq_depth=7)
	mariadb = MariaDBMetrics(slow_query_count=3, connections=9, buffer_pool_hit_ratio=0.97)
	collector = TelemetryCollector(
		redis_source=StaticRedisMetricsSource(redis),
		mariadb_source=StaticMariaDBMetricsSource(mariadb),
	)
	collector.observe_bulk_latency(0.02)
	snap = collector.snapshot()
	assert snap.redis == redis
	assert snap.mariadb == mariadb
	assert snap.bulk_latency.count == 1
	assert snap.taken_at <= time.time()


def test_collector_snapshot_as_dict_is_flat_scrape_shaped():
	collector = TelemetryCollector(
		redis_source=StaticRedisMetricsSource(RedisMetrics(rq_depth=1)),
		mariadb_source=StaticMariaDBMetricsSource(MariaDBMetrics(connections=2)),
	)
	flat = collector.snapshot().as_dict()
	# Every dashboard signal is reachable without attribute descent.
	for key in ("pubsub_messages_per_sec", "connected_clients", "rq_depth"):
		assert key in flat["redis"]
	for key in ("slow_query_count", "connections", "buffer_pool_hit_ratio"):
		assert key in flat["mariadb"]
	for key in ("count", "p95_seconds", "buckets", "bucket_counts"):
		assert key in flat["bulk_latency"]


# --------------------------------------------------------------------------- #
# Transport hook — measure_bulk_latency records exactly once per call.
# --------------------------------------------------------------------------- #
def test_measure_bulk_latency_records_exactly_one_observation():
	telemetry._collector.bulk_latency.reset()
	with measure_bulk_latency():
		pass
	assert telemetry.collector().bulk_latency.snapshot().count == 1


def test_measure_bulk_latency_records_even_when_block_raises():
	telemetry._collector.bulk_latency.reset()
	try:
		with measure_bulk_latency():
			raise RuntimeError("boom")
	except RuntimeError:
		pass
	assert telemetry.collector().bulk_latency.snapshot().count == 1


# --------------------------------------------------------------------------- #
# Import-clean invariant — production Frappe adapters never touch Frappe at import.
# --------------------------------------------------------------------------- #
def test_production_source_adapters_are_import_clean():
	# Importing the module must not require Frappe; the adapters lazy-import it
	# only inside snapshot(). Constructing them must also stay Frappe-free.
	from flock_os.telemetry import FrappeMariaDBMetricsSource, FrappeRedisMetricsSource

	FrappeRedisMetricsSource()
	FrappeMariaDBMetricsSource()


def test_install_frappe_sources_is_defensive_without_bench():
	# No Frappe in CI → installer must swallow + leave static defaults in place.
	telemetry.install_frappe_sources()
	snap = telemetry.snapshot()
	assert isinstance(snap.redis, RedisMetrics)
	assert isinstance(snap.mariadb, MariaDBMetrics)
