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
from collections.abc import Iterable, Sequence
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

BULK_ATTENDANCE_JOB_QUEUE = "flock_attendance"
"""Dedicated RQ queue for bulk attendance jobs (isolated from default queue)."""

BULK_ATTENDANCE_MAX_RETRY = 5
"""Max retries with exponential backoff before dead-lettering a batch."""

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

	``event`` is the gathering/event id (``Flock Gathering``). ``attendee_ref``
	is the member or visitor reference. ``branch`` is the org-tree node the row
	belongs to and the row-level permission anchor. ``client_req_id`` is the
	per-item idempotency key supplied by the client.
	"""

	event: str
	attendee_ref: str
	branch: str
	status: str = "Present"
	source: str = "bulk"
	client_req_id: str | None = None

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

	def increment_aggregate(self, scope: AttendanceScope, event: str, delta: int) -> None:
		"""Atomically bump the ``Event Attendance Summary`` counter (FLO-10 §3.3).

		Production uses ``UPDATE … SET count = count + n`` under ``FOR UPDATE`` or
		``INSERT … ON DUPLICATE KEY UPDATE`` — never a scan-then-write.
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
		return len(items)

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

		# 2. Per-item idempotency dedupe (backstopped by the unique index).
		keys = [item.idempotency_key for item in items]
		unseen_keys = self.gateway.filter_unseen(keys)
		new_items = [item for item in items if item.idempotency_key in unseen_keys]
		deduplicated = len(items) - len(new_items)

		# 3. Batched write + atomic aggregate + event emission.
		events: list[DomainEvent] = []
		inserted = 0
		if new_items:
			inserted = self.gateway.bulk_insert(new_items)
			by_event = Counter(item.event for item in new_items)
			for event, count in by_event.items():
				self.gateway.increment_aggregate(scope, event, count)
				events.append(
					DomainEvent(
						EVENT_BULK_RECORDED,
						{
							"gathering": event,
							"count": count,
							"batch_id": batch_id,
							"branch": scope.key,
						},
					)
				)
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
	status, source, client_req_id``, the ``UNIQUE (event, attendee_ref)`` index
	and the ``UNIQUE (event, attendee_ref, client_req_id)`` idempotency index
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
		fields = [
			"event",
			"attendee_ref",
			"branch",
			"status",
			"source",
			"client_req_id",
		]
		values = [
			[
				item.event,
				item.attendee_ref,
				item.branch,
				item.status,
				item.source,
				item.client_req_id or item.attendee_ref,
			]
			for item in items
		]
		# Raw bulk insert: scope already proven at admission (FLO-10 §3.3).
		frappe.db.bulk_insert(self.ATTENDANCE_DOCTYPE, fields=fields, values=values)
		return len(values)

	def increment_aggregate(self, scope: AttendanceScope, event: str, delta: int) -> None:
		frappe = self._frappe
		# Atomic upsert — never scan-then-write. ON DUPLICATE KEY UPDATE keeps the
		# counter correct under concurrent batches (FLO-10 §3.3/§4.2).
		frappe.db.sql(
			f"""
			INSERT INTO `tab{self.SUMMARY_DOCTYPE}` (branch, event, total)
			VALUES (%s, %s, %s)
			ON DUPLICATE KEY UPDATE total = total + VALUES(total)
			""",
			values=(scope.branch, event, delta),
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
