"""
Queue-based bulk attendance reporting service (FLO-15).

Implements the **write path** defined in the FLO-10 scale ADR §3 ([design](
/FLO/issues/FLO-10#document-design)) against the canonical data model + event
catalog from ADR-0001 ([FLO-4](/FLO/issues/FLO-4)).

Architecture (separation of concerns — services out of transport code):

    REST  (flock_os.attendance.bulk_submit)   @frappe.whitelist()
      → Service  (BulkAttendanceService)      ← domain rules live HERE
      → Queue job  (process_bulk_batch)        ← durable buffer (Frappe RQ)
      → Gateway  (BulkAttendanceGateway)       ← storage + event emission port
          ├─ InMemoryBulkAttendanceGateway     ← unit tests (SQLite-fast)
          └─ FrappeBulkAttendanceGateway       ← db.bulk_insert + Event
                                                 Attendance Summary + emit

The :class:`BulkAttendanceService` is pure Python and transport-agnostic: it is
fully unit-testable without HTTP, a queue, MariaDB, Redis, or even Frappe. It
depends only on the :class:`BulkAttendanceGateway` port (hexagonal / ports &
adapters). The in-memory adapter is the test surface; the Frappe adapter wires
the real persistence + aggregates + event emitter once the backing DocTypes
(``Flock Attendance Record`` → [FLO-17](/FLO/issues/FLO-17)) and the single
event emitter (``flock_os.events.emit`` → [FLO-14](/FLO/issues/FLO-14)) land.

Scale properties locked here (FLO-10 §3 / §4):

* **Set-based scope check once per batch** (D7) — O(1) permission cost; a batch
  with a single out-of-scope row is rejected wholesale (no partial writes).
* **Idempotency** on ``(event, attendee_ref, client_req_id)``, backstopped by the
  ``(event, attendee_ref)`` unique index — retries / replays never double-count.
* **Maintained aggregate** (``Event Attendance Summary``) updated atomically in
  the same job — counts are read from the rollup, never via a live ``COUNT(*)``.
* **Domain events** emitted on every transition via the gateway → Redis pub/sub.
* **Dead-letter** to a visible ``attendance_import_error`` queue w/ exp backoff.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from flock_os.events import (
	ATTENDANCE_BATCH_REJECTED,
	ATTENDANCE_BULK_RECORDED,
	ATTENDANCE_IMPORT_FAILED,
)

# ---------------------------------------------------------------------------- #
# Tunables (config-over-constants; ADR-0001 §7). Mirrored by Flock Organization
# settings when those land; the values here are the service-layer defaults.
# ---------------------------------------------------------------------------- #
BULK_BATCH_SIZE = 500
"""Max attendance items accepted per bulk request (FLO-10 §3.2). 15k ≈ 30 batches."""

ATTENDANCE_IMPORT_ERROR_QUEUE = "attendance_import_error"
"""Visible dead-letter queue for failed attendance batches (FLO-10 §3.3)."""

BULK_ATTENDANCE_JOB_QUEUE = "long"
"""Standard RQ queue for bulk attendance jobs (FLO-76).

This is deliberately a **stock Frappe queue** (``short`` / ``default`` / ``long``)
rather than a custom queue name. Frappe validates the requested queue against
``get_queues_timeout()``, which is ``@lru_cache``-d and reads the ``workers`` key
from ``common_site_config.json`` once per process. A long-running *web* process
that first resolved the queue list before a custom queue was registered (or whose
cache predates a config edit) permanently rejects the custom name with
``ValidationError: Queue should be one of short, default, long`` -- even though a
fresh console/worker process accepts it. That runtime-context fragility cannot be
reliably fixed from app code (it lives in Frappe's internal cache), so the bulk
path rides the stock ``long`` queue (1500s timeout -- ample for a 500-row batch),
and queue isolation is achieved by **RQ worker binding** instead of a custom name
(i.e. run a dedicated worker that drains ``long``), exactly as FLO-10 S3.3
allows. See [FLO-76](/FLO/issues/FLO-76).
"""

BULK_ATTENDANCE_MAX_RETRY = 5
"""Max retries with exponential backoff before dead-lettering a batch."""

BULK_ATTENDANCE_IN_PLACE_ATTEMPTS = 10
"""In-place retries for transient concurrency errors before the slow backoff path.

MariaDB raises error 1020 ("record has changed since last read") / deadlock on
concurrent ``UPDATE``s of the single shared ``Event Attendance Summary`` row
even under READ COMMITTED (FLO-100). Those errors are transient and the batch
is idempotent (``filter_unseen`` dedupes), so the queue job retries the whole
``service.submit`` in-place with a tiny jittered backoff. That keeps the
§8 drain fast (batches succeed once the contended X-lock frees) instead of
flooding the slow exponential-backoff re-enqueue path, which backs the queue
up past the 60s budget. Only after these in-place attempts are exhausted does
the batch fall through to ``_deadletter_or_retry``.
"""

# ---------------------------------------------------------------------------- #
# Domain event names (ADR-0001 §5.3 / §5.4). The canonical catalog
# (``flock_os.events``) is the single source of truth for event names; these
# aliases re-export the catalog constants under the reporting-layer vocabulary
# the service emits via the gateway port (the Frappe adapter routes them through
# ``flock_os.events.emit`` → Redis pub/sub, FLO-14). Importing the catalog here
# keeps the reporting module DRY — no duplicated string literals to drift apart.
# ---------------------------------------------------------------------------- #
EVENT_BULK_RECORDED = ATTENDANCE_BULK_RECORDED
EVENT_BATCH_REJECTED = ATTENDANCE_BATCH_REJECTED
EVENT_IMPORT_FAILED = ATTENDANCE_IMPORT_FAILED


class BatchSizeExceeded(Exception):
	"""Raised when a bulk batch exceeds :data:`BULK_BATCH_SIZE` items."""


@dataclass(frozen=True)
class AttendanceItem:
	"""A single attendance row to be bulk-reported.

	``event`` is the gathering/event id (``Flock Gathering``) — the legacy rev-1
	grouping axis the rollup/``bulk_recorded`` payload keys on. ``attendee_ref``
	is the member or visitor reference. ``branch`` is the org-tree node the row
	belongs to and the row-level permission anchor. ``client_req_id`` is the
	per-item idempotency key supplied by the client.

	The optional **provenance** fields (``gathering``, ``member``,
	``engagement_session``, ``attendee_key``) stamp the ADR §9 cross-source dedup
	columns when the row originates from the engagement runtime (FLO-11). They
	are additive + backward-compatible: the manual-roster bulk path leaves them
	``None`` and ``FrappeBulkAttendanceGateway.bulk_insert`` writes them only when
	present. The engagement close path (FLO-11 §5) populates them so the
	``UNIQUE (branch, gathering, member)`` index (ADR §9) collapses a
	manual-roster credit + an engagement credit for the same member at the same
	gathering into one attendance row, and ``UNIQUE (engagement_session,
	attendee_key)`` is the per-session backstop.
	"""

	event: str
	attendee_ref: str
	branch: str
	status: str = "Present"
	source: str = "bulk"
	client_req_id: str | None = None
	# Engagement provenance (FLO-11 §5 / ADR §9). None for manual-roster rows.
	gathering: str | None = None
	member: str | None = None
	engagement_session: str | None = None
	attendee_key: str | None = None

	@property
	def idempotency_key(self) -> tuple[str, str, str]:
		"""Canonical dedupe key ``(event, attendee_ref, client_req_id)`` (FLO-15).

		Falls back to ``attendee_ref`` when the client omits ``client_req_id`` so
		non-bulk callers still dedupe correctly against the unique backstop.
		"""
		return (self.event, self.attendee_ref, self.client_req_id or self.attendee_ref)

	@property
	def unique_key(self) -> tuple[str, str]:
		"""The ``(event, attendee_ref)`` unique-index backstop (FLO-10 §4.1)."""
		return (self.event, self.attendee_ref)


@dataclass(frozen=True)
class AttendanceScope:
	"""The resolved org-tree node (branch) a batch is reported against (D7).

	The branch axis is the primary row-level permission surface (ADR-0001 §6.2).
	"""

	branch: str

	@property
	def key(self) -> str:
		return self.branch


@dataclass(frozen=True)
class DomainEvent:
	"""A domain event emitted by a state change (Frappe hook + Redis pub/sub)."""

	name: str
	payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class RejectedItem:
	"""A single rejected item within a batch (index + reason for the receipt)."""

	index: int
	attendee_ref: str
	reason: str


@dataclass
class BulkBatchOutcome:
	"""Receipt for one submitted batch (returned immediately to the reporter)."""

	batch_id: str
	scope: AttendanceScope
	accepted: bool
	inserted: int
	deduplicated: int
	rejected: list[RejectedItem] = field(default_factory=list)
	events: list[DomainEvent] = field(default_factory=list)

	@property
	def rejected_count(self) -> int:
		return len(self.rejected)


@runtime_checkable
class BulkAttendanceGateway(Protocol):
	"""Storage + emission port the :class:`BulkAttendanceService` depends on.

	Implementations:

	* :class:`InMemoryBulkAttendanceGateway` — unit tests (SQLite-fast).
	* :class:`FrappeBulkAttendanceGateway` — production (MariaDB + Redis).

	The port keeps the service free of Frappe/DB concerns so domain rules are
	unit-testable in isolation (ADR-0001 §2, FLO-10 §3.1).
	"""

	def transaction(self) -> Iterator[None]:
		"""Context manager bounding one atomic persistence unit (FLO-100).

		The service keeps its data writes (``filter_unseen`` → ``bulk_insert`` →
		``increment_aggregate``) inside this boundary and emits events *after* it
		closes. The Frappe adapter commits on normal exit (releasing all row
		locks immediately) and rolls back on exception; the in-memory adapter is
		a no-op. This is the structural fix for the 200-wps concurrency failure:
		the ``Event Attendance Summary`` hot-row X-lock (held by the
		``ON DUPLICATE KEY UPDATE``) must release **before** the best-effort
		Redis / outbox side effects, not at job-end — otherwise concurrent batches
		serialize behind one lock held across the whole event fan-out and blow
		the ``innodb_lock_wait_timeout`` (ADR §5.3: correctness in the queue).
		"""
		...

	def filter_unseen(self, keys: Iterable[tuple[str, str, str]]) -> set[tuple[str, str, str]]:
		"""Return the subset of ``keys`` not already persisted.

		Dedupe is decided here so the service never inserts duplicates. A key is
		``(event, attendee_ref, client_req_id)``; the ``(event, attendee_ref)``
		unique index acts as a backstop (FLO-10 §4.1).
		"""
		...

	def bulk_insert(self, items: Sequence[AttendanceItem]) -> int:
		"""Batched raw insert (``db.bulk_insert`` in production) in one tx.

		Returns the number of rows actually written. Scope was already proven at
		admission, so this bypasses per-row validation/perm re-checks (FLO-10 §3.3).
		"""
		...

	def seed_aggregate(self, scope: AttendanceScope, events: Iterable[str]) -> None:
		"""Ensure summary rows exist for ``events`` (FLO-100).

		The seed runs in its **own committed transaction**, separate from the
		write transaction, so the increment is the *only* summary operation in
		the write transaction. MariaDB raises error 1020 ("record has changed
		since last read") whenever a transaction modifies a summary row it
		already read in the same transaction and a concurrent batch committed in
		between; keeping the seed (an ``INSERT IGNORE`` that reads the unique
		index) out of the same transaction as the ``UPDATE`` removes the
		read-then-modify pattern that triggers 1020 under 200-wps concurrency.
		Idempotent — safe to call every batch.
		"""
		...

	def increment_aggregate(self, scope: AttendanceScope, event: str, delta: int) -> None:
		"""Atomically bump the ``Event Attendance Summary`` counter (FLO-10 §3.3).

		Production uses a single ``UPDATE … SET total = total + n`` on a row the
		caller already seeded via :meth:`seed_aggregate`. The increment must be
		the *only* summary operation in its transaction (no prior read of the
		row) — that is what avoids MariaDB 1020 under concurrency (FLO-100).
		"""
		...

	def aggregate(self, scope: AttendanceScope, event: str | None = None) -> int:
		"""Read the maintained rollup. ``event=None`` sums across events in scope.

		This is the *only* sanctioned count path on hot paths (FLO-10 §4.2).
		"""
		...

	def emit(self, event: DomainEvent) -> None:
		"""Publish a domain event (→ ``flock_os.events.emit`` → Redis pub/sub)."""
		...


class InMemoryBulkAttendanceGateway:
	"""Reference in-memory adapter for unit tests (SQLite-fast, no Frappe).

	Models exactly the persistence semantics the production gateway must honour:
	per-key idempotency, the ``(event, attendee_ref)`` unique backstop, an
	atomic-style aggregate counter, and event capture.
	"""

	def __init__(self) -> None:
		self._seen: set[tuple[str, str, str]] = set()
		self._unique: set[tuple[str, str]] = set()
		self._counters: dict[tuple[str, str], int] = {}
		self.published_events: list[DomainEvent] = []
		# Full items captured in insertion order so callers/tests can assert the
		# provenance fields (gathering/member/engagement_session/attendee_key)
		# actually reached the write path (FLO-11 §5 dedup contract).
		self.inserted_items: list[AttendanceItem] = []

	@contextmanager
	def transaction(self) -> Iterator[None]:
		# In-memory adapter: no locks to release, so the boundary is a no-op.
		# The service still relies on the same exit contract (data committed
		# before events emit) so the Frappe adapter is a drop-in.
		yield

	def filter_unseen(self, keys: Iterable[tuple[str, str, str]]) -> set[tuple[str, str, str]]:
		unseen: set[tuple[str, str, str]] = set()
		for key in keys:
			if key in self._seen:
				continue
			event, attendee_ref, _client_req_id = key
			if (event, attendee_ref) in self._unique:
				# Unique-index backstop: same attendee already recorded under a
				# different client_req_id — must not double-count.
				continue
			unseen.add(key)
		return unseen

	def bulk_insert(self, items: Sequence[AttendanceItem]) -> int:
		for item in items:
			self._seen.add(item.idempotency_key)
			self._unique.add(item.unique_key)
			self.inserted_items.append(item)
		return len(items)

	def seed_aggregate(self, scope: AttendanceScope, events: Iterable[str]) -> None:
		# In-memory: counters are created on first increment, so seeding is a
		# no-op. Implemented for protocol conformance with the Frappe adapter.
		for event in events:
			self._counters.setdefault((scope.branch, event), 0)

	def increment_aggregate(self, scope: AttendanceScope, event: str, delta: int) -> None:
		self._counters[(scope.branch, event)] = self._counters.get((scope.branch, event), 0) + delta

	def aggregate(self, scope: AttendanceScope, event: str | None = None) -> int:
		if event is not None:
			return self._counters.get((scope.branch, event), 0)
		return sum(count for (branch, _event), count in self._counters.items() if branch == scope.branch)

	def emit(self, event: DomainEvent) -> None:
		self.published_events.append(event)


class BulkAttendanceService:
	"""The canonical queue-backed bulk attendance service (FLO-10 §3).

	Domain rules live here and only here. It performs a wholesale (set-based)
	scope check once per batch (D7), per-item idempotency on the tuple
	``(event, attendee_ref, client_req_id)`` backstopped by the unique
	``(event, attendee_ref)`` index, a batched write plus an atomic aggregate
	update in one tx, and emits one ``flock.attendance.bulk_recorded`` event per
	event present in the new rows (or ``flock.attendance.batch_rejected`` on a
	scope violation). Replaying an identical batch yields ``inserted=0``.
	"""

	def __init__(self, gateway: BulkAttendanceGateway) -> None:
		self.gateway = gateway

	def submit(
		self,
		items: Sequence[AttendanceItem],
		scope: AttendanceScope,
		batch_id: str,
	) -> BulkBatchOutcome:
		# The 500-item cap is enforced at the transport boundary (ADR §3.2 "500
		# items/request"); the service processes whatever batch the queue hands it.
		# 1. Set-based scope check, once per batch (D7). If any item is out of
		#    scope the *whole* batch is rejected atomically (no partial writes).
		#    This path performs no persistence, so its reject event emits outside
		#    any transaction boundary.
		out_of_scope = sum(1 for item in items if item.branch != scope.branch)
		if out_of_scope:
			rejected = [
				RejectedItem(index=index, attendee_ref=item.attendee_ref, reason="batch_scope_violation")
				for index, item in enumerate(items)
			]
			event = DomainEvent(
				EVENT_BATCH_REJECTED,
				{
					"scope": scope.key,
					"batch_id": batch_id,
					"rejected": len(items),
					"out_of_scope": out_of_scope,
					"reason": "out_of_scope",
				},
			)
			self.gateway.emit(event)
			return BulkBatchOutcome(
				batch_id=batch_id,
				scope=scope,
				accepted=False,
				inserted=0,
				deduplicated=0,
				rejected=rejected,
				events=[event],
			)

		keys = [item.idempotency_key for item in items]

		# Seed the summary rows for every event in the batch BEFORE the write
		# transaction (FLO-100). The seed (INSERT IGNORE) runs in its own
		# committed transaction, so inside the write transaction below the
		# increment is the *only* summary operation — a single UPDATE with no
		# prior read of the row. That breaks the read-then-modify pattern that
		# makes MariaDB raise error 1020 ("record has changed since last read")
		# on the shared (branch, event) row under 200-wps concurrency. Seeding is
		# idempotent and over-broad (covers events that dedupe away), which is
		# harmless.
		self.gateway.seed_aggregate(scope, {item.event for item in items})

		# 2 + 3. Per-item idempotency dedupe, batched write, and atomic aggregate
		# update — all inside ONE tight committed transaction (FLO-100). The
		# gateway commits this block on exit, so the Event Attendance Summary
		# hot-row X-lock (and every row/gap lock taken by the insert) releases
		# *before* any event/outbox side effect below. Events are best-effort
		# (ADR §5.3 — correctness lives in the queue): emitting them after the
		# data commit keeps them from extending the lock hold under 200-wps
		# concurrency, which is what flooded retries and stalled the queue.
		events: list[DomainEvent] = []
		inserted = 0
		deduplicated = 0
		with self.gateway.transaction():
			unseen_keys = self.gateway.filter_unseen(keys)
			new_items = [item for item in items if item.idempotency_key in unseen_keys]
			deduplicated = len(items) - len(new_items)
			if new_items:
				inserted = self.gateway.bulk_insert(new_items)
				by_event = Counter(item.event for item in new_items)
				for event_name, count in by_event.items():
					self.gateway.increment_aggregate(scope, event_name, count)
					events.append(
						DomainEvent(
							EVENT_BULK_RECORDED,
							{
								"gathering": event_name,
								"count": count,
								"batch_id": batch_id,
								"branch": scope.key,
							},
						)
					)
		# Events emit only after the data transaction has committed — never
		# inside it — so best-effort Redis/outbox writes hold no data locks.
		for event in events:
			self.gateway.emit(event)

		return BulkBatchOutcome(
			batch_id=batch_id,
			scope=scope,
			accepted=True,
			inserted=inserted,
			deduplicated=deduplicated,
			rejected=[],
			events=events,
		)

	def aggregate(self, scope: AttendanceScope, event: str | None = None) -> int:
		"""Delegate count read to the maintained gateway rollup (FLO-10 §4.2)."""
		return self.gateway.aggregate(scope, event)


def enforce_batch_size(items: Sequence[AttendanceItem], *, limit: int = BULK_BATCH_SIZE) -> None:
	"""Assert the batch is within the per-request cap (FLO-10 §3.2).

	Raises :class:`BatchSizeExceeded` so the transport layer can map it to a 422.
	"""
	if len(items) > limit:
		raise BatchSizeExceeded(f"batch of {len(items)} items exceeds the {limit}-item cap")


def with_exponential_backoff(attempt: int, base_seconds: float = 1.0, cap_seconds: float = 300.0) -> float:
	"""Compute an exponential-backoff delay (seconds) for RQ retries (FLO-10 §3.3).

	``attempt`` is 0-indexed (first retry → ``base``). Capped at ``cap_seconds``.
	"""
	return min(cap_seconds, base_seconds * (2**attempt))


class FrappeBulkAttendanceGateway:
	"""Production adapter wiring the service to MariaDB + Redis (FLO-10 §3.3/§4).

	Lazily imports Frappe so this module stays import-clean in CI (no bench).

	Expected backing schema (provided by [FLO-17](/FLO/issues/FLO-17)):
	``tabFlock Attendance Record`` with columns ``event, attendee_ref, branch,
	status, source, client_req_id`` plus the engagement-provenance columns
	``gathering, member, engagement_session, attendee_key`` (written when the
	``AttendanceItem`` carries them — FLO-11 §5), the ``UNIQUE (event, attendee_ref)``
	index and the ``UNIQUE (event, attendee_ref, client_req_id)`` idempotency index
	(FLO-10 §4.1); and ``tabEvent Attendance Summary`` with one row per
	``(branch, event)`` and a ``total`` counter maintained atomically by
	:meth:`increment_aggregate`. Event emission routes through
	``flock_os.events.emit`` (single sanctioned publisher, [FLO-14]) → Redis
	pub/sub + Frappe realtime.
	"""

	ATTENDANCE_DOCTYPE = "Flock Attendance Record"
	SUMMARY_DOCTYPE = "Event Attendance Summary"

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	@contextmanager
	def transaction(self) -> Iterator[None]:
		"""Commit the persistence unit immediately, under READ COMMITTED (FLO-100).

		Fixes two concurrency hazards on the 200-wps bulk path. (1) Lock hold:
		Frappe runs a whole background job in one transaction that commits only at
		job end (execute_job); without an explicit boundary the summary upsert's
		X-lock on the single shared (branch, event) row is held across the Redis +
		outbox event writes until that job-end commit, so concurrent batches
		serialize behind one lock and blow innodb_lock_wait_timeout. Committing the
		moment the data writes finish releases the lock before any best-effort side
		effect. (2) MariaDB 1020 ("record has changed since last read"): under the
		default REPEATABLE READ, filter_unseen's SELECT establishes the transaction
		snapshot; a concurrent batch then commits the same summary row, so our
		atomic UPDATE total = total + n sees a newer version and InnoDB raises 1020
		(QueryDeadlockError) on ~30%+ of batches. READ COMMITTED gives each
		statement a fresh read view, so the contended summary UPDATE always sees the
		latest committed row and never 1020s, while the tight transaction still
		keeps insert + aggregate atomic. READ COMMITTED is the standard isolation
		for high-concurrency write/counter workloads.

		Any ambient transaction is committed first so the SESSION isolation change
		applies to the fresh transaction, and the default (REPEATABLE READ) is
		restored afterward so the worker's other jobs are unaffected. Roll back on
		error so a failed batch leaves no half-written state for its retry.
		"""
		frappe = self._frappe
		frappe.db.commit()  # close any ambient job transaction so SET SESSION applies
		frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
		try:
			yield
		except Exception:
			frappe.db.rollback()
			raise
		finally:
			frappe.db.commit()
			frappe.db.sql("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")

	def filter_unseen(self, keys: Iterable[tuple[str, str, str]]) -> set[tuple[str, str, str]]:
		frappe = self._frappe
		key_list = list(keys)
		if not key_list:
			return set()
		# (event, attendee_ref, client_req_id) idempotency index — already-seen
		# keys are filtered out; the UNIQUE(event, attendee_ref) backstop is
		# enforced by the DB on insert and surfaced via IntegrityError handling
		# in the queue job (retries are idempotent).
		groups = ", ".join(["(%s, %s, %s)"] * len(key_list))
		flat_values = [value for key in key_list for value in key]
		rows = frappe.db.sql(
			f"""
			SELECT event, attendee_ref, client_req_id
			FROM `tab{self.ATTENDANCE_DOCTYPE}`
			WHERE (event, attendee_ref, client_req_id) IN ({groups})
			""",
			values=flat_values,
			as_dict=True,
		)
		seen = {(r.event, r.attendee_ref, r.client_req_id) for r in rows}
		return {key for key in key_list if key not in seen}

	def bulk_insert(self, items: Sequence[AttendanceItem]) -> int:
		frappe = self._frappe
		if not items:
			return 0
		# The engagement provenance columns (gathering, member, engagement_session,
		# attendee_key) are written only when the item carries them (FLO-11 §5 /
		# ADR §9). They are None for manual-roster rows, so the cross-source dedup
		# ``UNIQUE (branch, gathering, member)`` index only fires when BOTH paths
		# stamp the same member at the same gathering — which is exactly the
		# headline feature. ``event`` stays the legacy grouping axis (rollup +
		# ``bulk_recorded`` payload key on ``gathering``); it equals ``gathering``
		# for engagement-sourced rows.
		fields = [
			"event",
			"attendee_ref",
			"branch",
			"status",
			"source",
			"client_req_id",
			"gathering",
			"member",
			"engagement_session",
			"attendee_key",
		]
		values = [
			[
				item.event,
				item.attendee_ref,
				item.branch,
				item.status,
				item.source,
				item.client_req_id or item.attendee_ref,
				item.gathering,
				item.member,
				item.engagement_session,
				item.attendee_key,
			]
			for item in items
		]
		# Raw bulk insert: scope already proven at admission (FLO-10 §3.3).
		frappe.db.bulk_insert(self.ATTENDANCE_DOCTYPE, fields=fields, values=values)
		return len(values)

	def seed_aggregate(self, scope: AttendanceScope, events: Iterable[str]) -> None:
		frappe = self._frappe
		events = [e for e in events]
		if not events:
			return
		# Idempotent seed via the UNIQUE(branch, event) index. Committed in its
		# OWN transaction (separate from the write transaction) so the increment's
		# single UPDATE is the only summary operation in the write transaction --
		# no read-then-modify of the shared row within one transaction, which is
		# what triggers MariaDB 1020 ("record has changed since last read") under
		# 200-wps concurrency (FLO-100). Multi-row INSERT IGNORE seeds every event
		# in the batch in one statement.
		rows = ", ".join(["(%s, %s, 0)"] * len(events))
		frappe.db.sql(
			f"""
			INSERT IGNORE INTO `tab{self.SUMMARY_DOCTYPE}` (branch, event, total)
			VALUES {rows}
			""",
			values=[v for event in events for v in (scope.branch, event)],
		)
		frappe.db.commit()

	def increment_aggregate(self, scope: AttendanceScope, event: str, delta: int) -> None:
		frappe = self._frappe
		# Single atomic increment on a row the caller already seeded. This is the
		# ONLY summary operation in the write transaction (the seed ran in a
		# separate committed transaction), so there is no prior read of this row
		# in the transaction for MariaDB to flag as "changed since last read" --
		# that read-then-modify pattern is what raised error 1020 under 200-wps
		# concurrency (FLO-100). A plain UPDATE takes a brief X-lock and
		# serializes concurrent batches cleanly without the consistency-check
		# failure (FLO-10 §3.3/§4.2).
		frappe.db.sql(
			f"""
			UPDATE `tab{self.SUMMARY_DOCTYPE}`
			SET total = total + %s
			WHERE branch = %s AND event = %s
			""",
			values=(delta, scope.branch, event),
		)

	def aggregate(self, scope: AttendanceScope, event: str | None = None) -> int:
		frappe = self._frappe
		if event is not None:
			total = frappe.db.get_value(
				self.SUMMARY_DOCTYPE, {"branch": scope.branch, "event": event}, "total"
			)
			return int(total or 0)
		rows = frappe.db.get_all(
			self.SUMMARY_DOCTYPE,
			filters={"branch": scope.branch},
			fields=["total"],
		)
		return sum(int(r.total or 0) for r in rows)

	def emit(self, event: DomainEvent) -> None:
		from flock_os.events import emit

		emit(event.name, payload=dict(event.payload))
