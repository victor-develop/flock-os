"""
Runtime telemetry for the FLO-10 scale revisit triggers (FLO-49 / ADR §8 D3/D5).

Owns the **DevOps observability** half of the FLO-16 scale harness
([FLO-16](/FLO/issues/FLO-16) shipped the QA test half; this module + the
``load/`` k6 smoke ship the runtime/telemetry half under
[FLO-49](/FLO/issues/FLO-49)). It surfaces exactly the signals the FLO-10 ADR
§8 names as the D3 (Redis cluster escape hatch) and D5 (MariaDB scale limits)
**revisit triggers**, plus the bulk-endpoint latency histogram the acceptance
bar (p95 < 500ms) is measured against.

Three telemetry surfaces (ADR §8 D3/D5):

* **Redis** — pub/sub ``msg/s`` + ``connected_clients`` + RQ depth. When
  pubsub throughput or RQ backlog saturates a single node, D3 (shard to a
  Redis cluster) is triggered.
* **MariaDB** — slow-query count + active connections + buffer-pool hit ratio.
  When the hit ratio drops or slow queries climb, D5 (index/tune/scale MariaDB)
  is triggered.
* **Bulk endpoint latency** — a bucketed histogram of
  ``POST /api/method/flock_os.attendance.bulk_submit`` receipt latency. The
  p95 of this histogram IS the FLO-10 §8 ``p95 < 500ms`` gate.

Architecture (ports & adapters — same layering as ``flock_os.reporting`` /
``flock_os.events``)::

    bulk_submit (flock_os.attendance)
      -> measure_bulk_latency()           <- THIS module, latency histogram
    TelemetryCollector
      -> RedisMetricsSource  (port)       <- FrappeRedisMetricsSource (prod)
      -> MariaDBMetricsSource (port)      <- FrappeMariaDBMetricsSource (prod)
      -> LatencyHistogram                 <- pure, in-process
      -> snapshot()                       <- the dashboard / scrape surface

Transport-agnostic + import-clean without a Frappe site: the production source
adapters lazy-import Frappe so this module imports under plain ``pytest`` (no
bench). The unit suite injects :class:`StaticRedisMetricsSource` /
:class:`StaticMariaDBMetricsSource` and asserts the snapshot + histogram math;
the ``load/`` k6 smoke drives the production adapters against a live runtime.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------- #
# Bulk-endpoint latency histogram (FLO-10 §8 p95 < 500ms gate).
# ---------------------------------------------------------------------------- #
# Bucket upper bounds in SECONDS. Resolution is finest under the 500ms budget
# (5/10/25/50/100/150/250/400/500ms) with headroom above it for the D5 revisit
# trigger (anything landing past 1s is a slow-query smoke signal). Mirrors a
# Prometheus-style cumulative histogram so the k6 scrape / dashboard reads the
# same shape a real metrics backend would expose.
DEFAULT_LATENCY_BUCKETS: tuple[float, ...] = (
	0.005,
	0.01,
	0.025,
	0.05,
	0.1,
	0.15,
	0.25,
	0.4,
	0.5,  # <- the §8 p95 budget
	0.75,
	1.0,
	2.5,
	5.0,
	10.0,
	float("inf"),
)
"""Default latency buckets (seconds) for the bulk-attendance endpoint (FLO-10 §8)."""

BULK_LATENCY_P95_BUDGET_SECONDS = 0.5
"""The §8 acceptance budget the bulk-latency p95 is gated against (500ms)."""


@dataclass(frozen=True)
class LatencyHistogramSnapshot:
	"""An immutable read of a :class:`LatencyHistogram` at one instant.

	``bucket_counts`` is **cumulative**: each entry is the count of observations
	``<=`` that bucket's upper bound (Prometheus shape), so the last bucket is
	the total. ``buckets`` are the upper bounds aligned 1:1 with ``bucket_counts``.
	"""

	name: str
	count: int
	sum_seconds: float
	buckets: tuple[float, ...]
	bucket_counts: tuple[int, ...]
	p50_seconds: float | None
	p95_seconds: float | None
	p99_seconds: float | None

	@property
	def mean_seconds(self) -> float | None:
		"""Arithmetic mean latency (``None`` when there are no observations)."""
		return self.sum_seconds / self.count if self.count else None


def _quantile_from_cumulative(
	buckets: Sequence[float],
	cumulative: Sequence[int],
	quantile: float,
) -> float | None:
	"""Approximate ``quantile`` from a cumulative histogram (Prometheus rule).

	Linear interpolation within the bucket that first reaches the target rank.
	Returns ``None`` for an empty histogram.
	"""
	total = cumulative[-1] if cumulative else 0
	if total <= 0:
		return None
	target = quantile * total
	prev_count = 0
	prev_bound = 0.0
	for upper, count in zip(buckets, cumulative, strict=False):
		if count >= target:
			within = count - prev_count
			if within <= 0 or upper == float("inf"):
				# All mass in this bucket is at/under prev_bound's region; return
				# the upper bound (inf bucket → prev_bound, the last finite edge).
				return upper if upper != float("inf") else prev_bound
			frac = (target - prev_count) / within
			span = upper - prev_bound
			return prev_bound + frac * span
		prev_count = count
		prev_bound = upper if upper != float("inf") else prev_bound
	return buckets[-1] if buckets and buckets[-1] != float("inf") else None


@dataclass
class LatencyHistogram:
	"""A cumulative bucket histogram for one named latency signal (FLO-10 §8).

	The bulk-attendance endpoint is wrapped in :func:`measure_bulk_latency` so
	every receipt is observed here; the p95 of this histogram is the §8 gate.
	"""

	name: str
	buckets: tuple[float, ...] = DEFAULT_LATENCY_BUCKETS
	_count: int = 0
	_sum: float = 0.0
	# Per-bucket cumulative counts, lazily aligned to ``buckets``.
	_cumulative: list[int] = field(default_factory=list, repr=False)

	def __post_init__(self) -> None:
		if not self.buckets:
			raise ValueError("LatencyHistogram requires at least one bucket")
		if any(self.buckets[i] > self.buckets[i + 1] for i in range(len(self.buckets) - 1)):
			raise ValueError("LatencyHistogram buckets must be non-decreasing")
		if self.buckets[-1] != float("inf"):
			# Always keep an +Inf catch-all so every observation lands somewhere.
			self.buckets = (*self.buckets, float("inf"))
		if not self._cumulative:
			self._cumulative = [0] * len(self.buckets)

	def observe(self, seconds: float) -> None:
		"""Record one latency observation (seconds). Negative values ignored."""
		if seconds < 0:
			return
		self._count += 1
		self._sum += seconds
		for index, upper in enumerate(self.buckets):
			if seconds <= upper:
				# Increment every cumulative bucket at/above the landing bucket.
				for j in range(index, len(self.buckets)):
					self._cumulative[j] += 1
				break

	def snapshot(self) -> LatencyHistogramSnapshot:
		"""Freeze the current state (percentiles computed from the buckets)."""
		cumulative = tuple(self._cumulative)
		buckets = tuple(self.buckets)
		return LatencyHistogramSnapshot(
			name=self.name,
			count=self._count,
			sum_seconds=self._sum,
			buckets=buckets,
			bucket_counts=cumulative,
			p50_seconds=_quantile_from_cumulative(buckets, cumulative, 0.5),
			p95_seconds=_quantile_from_cumulative(buckets, cumulative, 0.95),
			p99_seconds=_quantile_from_cumulative(buckets, cumulative, 0.99),
		)

	def reset(self) -> None:
		"""Clear all observations (used between k6 smoke stages)."""
		self._count = 0
		self._sum = 0.0
		self._cumulative = [0] * len(self.buckets)


# ---------------------------------------------------------------------------- #
# D3/D5 revisit-trigger metric records + source ports.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RedisMetrics:
	"""The D3 (Redis cluster) revisit-trigger signals (FLO-10 §8).

	* ``pubsub_messages_per_sec`` — Redis pub/sub throughput; sustained high
		rates push the single-node ceiling that triggers the D3 cluster shard.
	* ``connected_clients`` — subscribers + workers; the fan-out fan-in load.
	* ``rq_depth`` — backlog across the bulk-attendance RQ queue (``long``, per
		FLO-76) + the general ``default`` queue; a non-draining backlog is the
		queue-budget trigger.
	"""

	pubsub_messages_per_sec: float = 0.0
	connected_clients: int = 0
	rq_depth: int = 0


@dataclass(frozen=True)
class MariaDBMetrics:
	"""The D5 (MariaDB scale) revisit-trigger signals (FLO-10 §8).

	* ``slow_query_count`` — cumulative ``Slow_queries`` since server start; the
		rate between snapshots is the slow-query smoke signal (§8: 0 slow queries).
	* ``connections`` — open ``Threads_connected``.
	* ``buffer_pool_hit_ratio`` — ``Innodb_buffer_pool_reads`` hit ratio in
		``[0.0, 1.0]``; a falling ratio is the index/working-set revisit trigger.
	"""

	slow_query_count: int = 0
	connections: int = 0
	buffer_pool_hit_ratio: float = 1.0


@runtime_checkable
class RedisMetricsSource(Protocol):
	"""Port: snapshot the D3 Redis revisit-trigger :class:`RedisMetrics`."""

	def snapshot(self) -> RedisMetrics: ...


@runtime_checkable
class MariaDBMetricsSource(Protocol):
	"""Port: snapshot the D5 MariaDB revisit-trigger :class:`MariaDBMetrics`."""

	def snapshot(self) -> MariaDBMetrics: ...


class StaticRedisMetricsSource:
	"""In-memory :class:`RedisMetricsSource` for unit tests + local smoke."""

	def __init__(self, metrics: RedisMetrics | None = None) -> None:
		self.metrics = metrics or RedisMetrics()

	def snapshot(self) -> RedisMetrics:
		return self.metrics


class StaticMariaDBMetricsSource:
	"""In-memory :class:`MariaDBMetricsSource` for unit tests + local smoke."""

	def __init__(self, metrics: MariaDBMetrics | None = None) -> None:
		self.metrics = metrics or MariaDBMetrics()

	def snapshot(self) -> MariaDBMetrics:
		return self.metrics


class FrappeRedisMetricsSource:
	"""Production D3 source: Redis INFO (via Frappe's cache client) + RQ depth.

	Lazily touches Frappe so this module stays import-clean in CI (no bench).
	Uses the Frappe cache's underlying Redis client for INFO (the only sanctioned
	Redis-touching surface besides ``frappe.publish_realtime``, FLO-10 §6) and
	Frappe's RQ registry for queue depth. Defensive: any failure yields a
	zeroed :class:`RedisMetrics` so a scrape never breaks the runtime.
	"""

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def snapshot(self) -> RedisMetrics:
		try:
			frappe = self._frappe
			client = frappe.cache().get_client()
			info = client.info() if hasattr(client, "info") else {}
			# connected_clients + pubsub throughput from INFO.
			connected = int(info.get("connected_clients", 0) or 0)
			pubsub_commands = float(info.get("pubsub_commands", 0) or 0)
			uptime = float(info.get("uptime_in_seconds", 1) or 1) or 1.0
			msg_per_sec = pubsub_commands / uptime if uptime else 0.0
			rq_depth = _frappe_rq_depth(frappe)
			return RedisMetrics(
				pubsub_messages_per_sec=round(msg_per_sec, 3),
				connected_clients=connected,
				rq_depth=rq_depth,
			)
		except Exception:  # noqa: BLE001 - a scrape must never break the runtime
			return RedisMetrics()


class FrappeMariaDBMetricsSource:
	"""Production D5 source: MariaDB ``SHOW GLOBAL STATUS`` via Frappe's DB.

	Slow_queries (cumulative), Threads_connected, and the InnoDB buffer-pool hit
	ratio computed from ``Innodb_buffer_pool_read_requests`` / ``..._reads``.
	Defensive like the Redis source.
	"""

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def snapshot(self) -> MariaDBMetrics:
		try:
			frappe = self._frappe

			def status(name: str) -> str | None:
				row = frappe.db.sql("SHOW GLOBAL STATUS WHERE Variable_name = %s", name, as_dict=True)
				return row[0].get("Value") if row else None

			slow = int(status("Slow_queries") or 0)
			connections = int(status("Threads_connected") or 0)
			read_requests = float(status("Innodb_buffer_pool_read_requests") or 0)
			reads = float(status("Innodb_buffer_pool_reads") or 0)
			# hit ratio = 1 - (physical reads / logical reads); 1.0 when idle.
			hit_ratio = 1.0
			if read_requests > 0:
				hit_ratio = max(0.0, 1.0 - (reads / read_requests))
			return MariaDBMetrics(
				slow_query_count=slow,
				connections=connections,
				buffer_pool_hit_ratio=round(hit_ratio, 4),
			)
		except Exception:  # noqa: BLE001 - a scrape must never break the runtime
			return MariaDBMetrics()


def _frappe_rq_depth(frappe: Any) -> int:  # type: ignore[no-untyped-def]
	"""Sum started + queued jobs across the bulk-attendance RQ queues (FLO-10 §3.3).

	The bulk path rides the stock ``long`` queue (FLO-76), so the depth signal
	covers ``long`` (bulk attendance) + ``default`` (general Frappe jobs).
	"""
	from frappe.utils.background_jobs import get_redis_conn  # type: ignore

	from flock_os.reporting import BULK_ATTENDANCE_JOB_QUEUE

	queue_names = (BULK_ATTENDANCE_JOB_QUEUE, "default")
	conn = get_redis_conn() if callable(get_redis_conn) else None
	if conn is None:
		return 0
	depth = 0
	for name in queue_names:
		depth += int(conn.llen(f"rq:queue:{name}") or 0)
	return depth


# ---------------------------------------------------------------------------- #
# The collector: composes the sources + histogram into a scrape snapshot.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TelemetrySnapshot:
	"""One point-in-time read of every D3/D5 + latency signal (the dashboard)."""

	redis: RedisMetrics
	mariadb: MariaDBMetrics
	bulk_latency: LatencyHistogramSnapshot
	taken_at: float

	def as_dict(self) -> dict[str, Any]:
		"""Flatten the snapshot for a Prometheus/JSON scrape or dashboard."""
		return {
			"taken_at": self.taken_at,
			"redis": {
				"pubsub_messages_per_sec": self.redis.pubsub_messages_per_sec,
				"connected_clients": self.redis.connected_clients,
				"rq_depth": self.redis.rq_depth,
			},
			"mariadb": {
				"slow_query_count": self.mariadb.slow_query_count,
				"connections": self.mariadb.connections,
				"buffer_pool_hit_ratio": self.mariadb.buffer_pool_hit_ratio,
			},
			"bulk_latency": {
				"name": self.bulk_latency.name,
				"count": self.bulk_latency.count,
				"sum_seconds": self.bulk_latency.sum_seconds,
				"p50_seconds": self.bulk_latency.p50_seconds,
				"p95_seconds": self.bulk_latency.p95_seconds,
				"p99_seconds": self.bulk_latency.p99_seconds,
				"buckets": list(self.bulk_latency.buckets),
				"bucket_counts": list(self.bulk_latency.bucket_counts),
			},
		}


@dataclass
class TelemetryCollector:
	"""The single telemetry surface the runtime + dashboards scrape (FLO-49).

	Holds the process-wide :class:`LatencyHistogram` for the bulk endpoint and
	delegates the D3/D5 source reads to injected ports. Production wiring
	(``hooks.py`` / the bench) installs the Frappe adapters; the unit suite
	installs static sources. ``snapshot()`` is the dashboard/scrape entry point.
	"""

	bulk_latency: LatencyHistogram = field(
		default_factory=lambda: LatencyHistogram(name="flock_bulk_attendance_latency")
	)
	redis_source: RedisMetricsSource = field(default_factory=StaticRedisMetricsSource)
	mariadb_source: MariaDBMetricsSource = field(default_factory=StaticMariaDBMetricsSource)

	def install_redis_source(self, source: RedisMetricsSource) -> None:
		"""Swap the D3 Redis source (production wiring / tests)."""
		self.redis_source = source

	def install_mariadb_source(self, source: MariaDBMetricsSource) -> None:
		"""Swap the D5 MariaDB source (production wiring / tests)."""
		self.mariadb_source = source

	def observe_bulk_latency(self, seconds: float) -> None:
		"""Record one bulk-endpoint receipt latency (called by the transport)."""
		self.bulk_latency.observe(seconds)

	def snapshot(self) -> TelemetrySnapshot:
		"""Read every signal at one instant — the dashboard/scrape surface."""
		return TelemetrySnapshot(
			redis=self.redis_source.snapshot(),
			mariadb=self.mariadb_source.snapshot(),
			bulk_latency=self.bulk_latency.snapshot(),
			taken_at=time.time(),
		)


# ---------------------------------------------------------------------------- #
# Module-level collector + the transport-facing latency API.
# ---------------------------------------------------------------------------- #
_collector = TelemetryCollector()


def collector() -> TelemetryCollector:
	"""The process-wide :class:`TelemetryCollector` (single telemetry surface)."""
	return _collector


def install_redis_source(source: RedisMetricsSource) -> None:
	"""Install the D3 Redis source on the module-level collector."""
	_collector.install_redis_source(source)


def install_mariadb_source(source: MariaDBMetricsSource) -> None:
	"""Install the D5 MariaDB source on the module-level collector."""
	_collector.install_mariadb_source(source)


def observe_bulk_latency(seconds: float) -> None:
	"""Record one bulk-endpoint receipt latency on the module-level collector."""
	_collector.observe_bulk_latency(seconds)


@contextmanager
def measure_bulk_latency() -> Iterator[None]:
	"""Context manager timing the wrapped bulk-endpoint block (FLO-10 §8 gate).

	Usage in ``flock_os.attendance.bulk_submit``::

		with measure_bulk_latency():
			...parse, resolve scope, enqueue, build receipt...
	"""
	start = time.perf_counter()
	try:
		yield
	finally:
		observe_bulk_latency(time.perf_counter() - start)


def snapshot() -> TelemetrySnapshot:
	"""Scrape the module-level collector (the dashboard entry point)."""
	return _collector.snapshot()


def install_frappe_sources() -> TelemetryCollector:
	"""Wire the production Frappe D3/D5 sources (called from ``hooks.py``).

	Guarded so a scrape never breaks the runtime: the adapters themselves are
	defensive, and this installer is a no-op outside a bench (no Frappe).
	"""
	try:
		install_redis_source(FrappeRedisMetricsSource())
		install_mariadb_source(FrappeMariaDBMetricsSource())
	except Exception:  # noqa: BLE001 - telemetry is best-effort
		pass
	return _collector
