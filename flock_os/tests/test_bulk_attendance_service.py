"""
Unit tests for the bulk attendance service layer (FLO-15 DoD).

SQLite-fast: run under plain ``pytest`` (no Frappe / MariaDB / Redis). They pin
the domain rules the production :class:`BulkAttendanceService` must honour, as
specified in the FLO-10 scale ADR §3 and ADR-0001 ([FLO-4](/FLO/issues/FLO-4)):

* 15k-row fixture fully accepted; count read from the maintained aggregate
  (never a live ``COUNT(*)``) — FLO-10 §4.
* **Per-item idempotency** on ``(event, attendee_ref, client_req_id)`` with the
  ``(event, attendee_ref)`` unique-index backstop — FLO-15 / FLO-10 §3.2.
* **Wholesale scope rejection** — one out-of-scope row rejects the whole batch,
  no partial writes — FLO-10 §3.2 (D7).
* Aggregate math, branch isolation, and domain-event emission on every
  transition.

The QA harness ([FLO-16](/FLO/issues/FLO-16)) co-owns the 15k-fixture path; to
prove the production service drops into the same fixture + assertions, the
final tests drive it through :class:`QABulkAttendanceAdapter`, which conforms to
the :class:`BulkAttendanceService` Protocol defined in the QA contract layer
(``test_scale_attendance``).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager

import pytest

from flock_os.reporting import (
	BULK_BATCH_SIZE,
	EVENT_BATCH_REJECTED,
	EVENT_BULK_RECORDED,
	AttendanceItem,
	AttendanceScope,
	BatchSizeExceeded,
	BulkAttendanceService,
	DomainEvent,
	InMemoryBulkAttendanceGateway,
	enforce_batch_size,
	with_exponential_backoff,
)

# Reuse the QA contract layer's fixture + Protocol (DRY — co-owned 15k path).
from flock_os.tests.test_scale_attendance import (  # noqa: E402
	ATTENDEE_SCALE,
	AttendanceRecord,
	BulkBatchResult,
	ReportingScope,
	make_attendance_fixture,
)
from flock_os.tests.test_scale_attendance import (
	BulkAttendanceService as QABulkAttendanceService,
)
from flock_os.tests.test_scale_attendance import (
	DomainEvent as QADomainEvent,
)


# ---------------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------------- #
def _service() -> tuple[BulkAttendanceService, InMemoryBulkAttendanceGateway]:
	gateway = InMemoryBulkAttendanceGateway()
	return BulkAttendanceService(gateway), gateway


def _items(
	n: int,
	*,
	event: str = "gathering-1",
	branch: str = "branch-a",
	client_req_prefix: str = "batch-1",
	start: int = 0,
) -> list[AttendanceItem]:
	return [
		AttendanceItem(
			event=event,
			attendee_ref=f"member-{start + i}",
			branch=branch,
			client_req_id=f"{client_req_prefix}:member-{start + i}",
		)
		for i in range(n)
	]


SCOPE_A = AttendanceScope(branch="branch-a")
SCOPE_B = AttendanceScope(branch="branch-b")


# ---------------------------------------------------------------------------- #
# 15k fixture + aggregate (FLO-10 §4)
# ---------------------------------------------------------------------------- #
def test_15k_fixture_inserts_all_rows_counted_via_aggregate() -> None:
	"""A 15k-row batch is fully accepted; count read from the aggregate, not a scan."""
	service, gateway = _service()
	fixture = _items(ATTENDEE_SCALE)

	outcome = service.submit(fixture, SCOPE_A, batch_id="batch-1")

	assert outcome.accepted is True
	assert outcome.inserted == ATTENDEE_SCALE
	assert outcome.deduplicated == 0
	assert outcome.rejected_count == 0
	assert service.aggregate(SCOPE_A) == ATTENDEE_SCALE
	assert service.aggregate(SCOPE_A, event="gathering-1") == ATTENDEE_SCALE
	# Exactly one bulk_recorded event per event present in the new rows.
	assert [e.name for e in outcome.events] == [EVENT_BULK_RECORDED]
	assert outcome.events[0].payload["count"] == ATTENDEE_SCALE
	assert outcome.events[0].payload["branch"] == "branch-a"
	# Aggregate-only invariant: the gateway published the same event.
	assert gateway.published_events[-1].name == EVENT_BULK_RECORDED


# ---------------------------------------------------------------------------- #
# Idempotency (FLO-15 / FLO-10 §3.2)
# ---------------------------------------------------------------------------- #
def test_idempotent_replay_inserts_zero() -> None:
	"""Replaying the same batch (same per-item client_req_id) inserts 0 new rows."""
	service, _gateway = _service()
	fixture = _items(ATTENDEE_SCALE)

	first = service.submit(fixture, SCOPE_A, batch_id="batch-1")
	replay = service.submit(fixture, SCOPE_A, batch_id="batch-1-replay")

	assert first.inserted == ATTENDEE_SCALE
	assert replay.inserted == 0
	assert replay.accepted is True
	assert replay.deduplicated == ATTENDEE_SCALE
	assert replay.events == []
	assert service.aggregate(SCOPE_A) == ATTENDEE_SCALE


def test_partial_overlap_dedupes_only_new_rows() -> None:
	"""Per-item idempotency: a batch overlapping a prior one writes only the new rows.

	The ``(event, attendee_ref)`` backstop means an attendee is recorded once per
	event, so overlap is exercised with overlapping attendee *ranges*: members
	500-999 are already recorded (deduped), members 1000-1499 are new (inserted).
	"""
	service, _gateway = _service()
	first = _items(1000, client_req_prefix="b1")  # members 0-999
	overlap = _items(1000, client_req_prefix="b2", start=500)  # members 500-1499

	service.submit(first, SCOPE_A, batch_id="batch-1")
	outcome = service.submit(overlap, SCOPE_A, batch_id="batch-2")

	assert outcome.inserted == 500  # members 1000-1499
	assert outcome.deduplicated == 500  # members 500-999
	assert service.aggregate(SCOPE_A) == 1500


def test_unique_index_backstop_dedupes_across_distinct_client_req_ids() -> None:
	"""The (event, attendee_ref) unique index backstops a lost client_req_id."""
	service, _gateway = _service()
	service.submit(_items(1), SCOPE_A, batch_id="batch-1")  # member-0 recorded

	# Same attendee, different client_req_id — backstop must reject the double-count.
	dup = [
		AttendanceItem(
			event="gathering-1",
			attendee_ref="member-0",
			branch="branch-a",
			client_req_id="other-batch:member-0",
		)
	]
	outcome = service.submit(dup, SCOPE_A, batch_id="batch-2")

	assert outcome.inserted == 0
	assert outcome.deduplicated == 1
	assert service.aggregate(SCOPE_A) == 1


# ---------------------------------------------------------------------------- #
# Wholesale scope rejection (FLO-10 §3.2, D7)
# ---------------------------------------------------------------------------- #
def test_out_of_scope_batch_rejected_wholesale() -> None:
	"""One out-of-scope row rejects the whole batch — no partial writes."""
	service, _gateway = _service()
	service.submit(_items(ATTENDEE_SCALE), SCOPE_A, batch_id="batch-1")

	mixed = _items(100, client_req_prefix="batch-2")
	mixed[0] = AttendanceItem(
		event="gathering-1",
		attendee_ref="intruder",
		branch="branch-b",  # out of scope for SCOPE_A
		client_req_id="batch-2:intruder",
	)
	outcome = service.submit(mixed, SCOPE_A, batch_id="batch-2")

	assert outcome.accepted is False
	assert outcome.inserted == 0
	assert outcome.rejected_count == len(mixed)
	assert outcome.rejected[0].attendee_ref == "intruder"
	assert outcome.rejected[0].reason == "batch_scope_violation"
	# Aggregate untouched by a rejected batch.
	assert service.aggregate(SCOPE_A) == ATTENDEE_SCALE
	assert [e.name for e in outcome.events] == [EVENT_BATCH_REJECTED]
	assert outcome.events[0].payload["rejected"] == len(mixed)


# ---------------------------------------------------------------------------- #
# Aggregate math + branch isolation (FLO-10 §4.2)
# ---------------------------------------------------------------------------- #
def test_aggregate_math_multiple_events_and_branch_isolation() -> None:
	"""Per-event aggregates sum correctly; branch B is invisible to branch A."""
	service, _gateway = _service()
	g1 = _items(10_000, event="g-1", client_req_prefix="g1")
	g2 = _items(5_000, event="g-2", client_req_prefix="g2")
	# Branch-b attendees use distinct refs so the (event, attendee_ref) unique
	# backstop does not cross-dedupe them against branch-a's members.
	gb = [
		AttendanceItem(
			event="g-1",
			attendee_ref=f"member-b-{i}",
			branch="branch-b",
			client_req_id=f"gb1:member-b-{i}",
		)
		for i in range(3_000)
	]

	service.submit(g1, SCOPE_A, batch_id="g1")
	service.submit(g2, SCOPE_A, batch_id="g2")
	service.submit(gb, SCOPE_B, batch_id="gb1")

	assert service.aggregate(SCOPE_A, "g-1") == 10_000
	assert service.aggregate(SCOPE_A, "g-2") == 5_000
	assert service.aggregate(SCOPE_A) == ATTENDEE_SCALE
	assert service.aggregate(SCOPE_B) == 3_000


# ---------------------------------------------------------------------------- #
# Batch-size cap + backoff (FLO-10 §3.2 / §3.3)
# ---------------------------------------------------------------------------- #
def test_batch_size_cap_enforced() -> None:
	"""A batch over the cap is rejected before any write (BatchSizeExceeded)."""
	with pytest.raises(BatchSizeExceeded):
		enforce_batch_size(_items(BULK_BATCH_SIZE + 1))


def test_batch_at_cap_accepted() -> None:
	service, _gateway = _service()
	outcome = service.submit(_items(BULK_BATCH_SIZE), SCOPE_A, batch_id="cap")
	assert outcome.inserted == BULK_BATCH_SIZE


def test_backoff_is_monotonic_and_capped() -> None:
	"""Exponential backoff grows and never exceeds the cap (FLO-10 §3.3)."""
	delays = [with_exponential_backoff(a) for a in range(8)]
	assert delays == sorted(delays)
	assert delays[0] == 1
	assert all(d <= 300 for d in delays)


# ---------------------------------------------------------------------------- #
# Transaction boundary (FLO-100): data commits before events emit
# ---------------------------------------------------------------------------- #
class _OrderRecordingGateway:
	"""Wraps the in-memory gateway and records the relative order of the
	transaction boundary vs ``emit``.

	This pins the FLO-100 structural fix: the service must close its data
	transaction (commit, releasing the Event Attendance Summary hot-row X-lock)
	*before* it emits any event, so the best-effort Redis / outbox side effects
	never extend the lock hold under 200-wps concurrency.
	"""

	def __init__(self) -> None:
		self._inner = InMemoryBulkAttendanceGateway()
		self.order: list[str] = []

	@contextmanager
	def transaction(self) -> Iterator[None]:
		self.order.append("tx_enter")
		try:
			yield
		finally:
			self.order.append("tx_exit")

	def filter_unseen(self, keys: Iterable[tuple[str, str, str]]) -> set[tuple[str, str, str]]:
		return self._inner.filter_unseen(keys)

	def bulk_insert(self, items):  # type: ignore[no-untyped-def]
		return self._inner.bulk_insert(items)

	def increment_aggregate(self, scope, event, delta):  # type: ignore[no-untyped-def]
		return self._inner.increment_aggregate(scope, event, delta)

	def aggregate(self, scope, event=None):  # type: ignore[no-untyped-def]
		return self._inner.aggregate(scope, event)

	def emit(self, event: DomainEvent) -> None:
		self.order.append("emit")


def test_data_transaction_commits_before_events_emit() -> None:
	"""FLO-100: the persistence unit commits before any event is emitted.

	Under the live bench the Event Attendance Summary upsert takes an X-lock on
	the single shared ``(branch, event)`` row. If events (Redis + outbox writes)
	ran inside that transaction, every concurrent batch would serialize behind
	one lock held across the whole fan-out → lock-wait timeout → retry flood.
	Here every ``emit`` must follow the transaction exit.
	"""
	gateway = _OrderRecordingGateway()
	service = BulkAttendanceService(gateway)

	outcome = service.submit(_items(50), SCOPE_A, batch_id="b1")

	assert outcome.inserted == 50
	tx_exit_index = gateway.order.index("tx_exit")
	emit_indices = [i for i, marker in enumerate(gateway.order) if marker == "emit"]
	assert emit_indices, "expected at least one event emitted"
	assert all(i > tx_exit_index for i in emit_indices)


def test_scope_reject_emits_without_opening_data_transaction() -> None:
	"""FLO-100: a wholesale scope rejection performs no persistence, so it emits
	its reject event without opening (and holding) a data transaction/lock."""
	gateway = _OrderRecordingGateway()
	service = BulkAttendanceService(gateway)

	mixed = _items(5, client_req_prefix="b2")
	mixed[0] = AttendanceItem(
		event="gathering-1",
		attendee_ref="intruder",
		branch="branch-b",
		client_req_id="b2:intruder",
	)
	outcome = service.submit(mixed, SCOPE_A, batch_id="b2")

	assert outcome.accepted is False
	assert "tx_enter" not in gateway.order
	assert gateway.order.count("emit") == 1


def test_failed_data_write_rolls_back_and_does_not_emit() -> None:
	"""FLO-100: a persistence failure rolls the transaction back and emits no
	success event — the idempotent retry (handled by the queue job) re-runs it."""

	class _FailOnInsert(_OrderRecordingGateway):
		def bulk_insert(self, items):  # type: ignore[no-untyped-def]
			raise RuntimeError("simulated insert failure")

	gateway = _FailOnInsert()
	service = BulkAttendanceService(gateway)

	with pytest.raises(RuntimeError):
		service.submit(_items(5), SCOPE_A, batch_id="b-fail")

	# The transaction still closed (rolled back via the gateway), but no event
	# was emitted because the write never committed.
	assert "tx_exit" in gateway.order
	assert "emit" not in gateway.order


# ---------------------------------------------------------------------------- #
# QA-conformance adapter: production service satisfies the FLO-16 contract layer
# ---------------------------------------------------------------------------- #
class QABulkAttendanceAdapter:
	"""Adapts the canonical :class:`BulkAttendanceService` to the QA contract Protocol.

	The QA oracle (``test_scale_attendance``) models batch-level idempotency; this
	adapter derives a per-item ``client_req_id`` from the batch key so the
	canonical per-item dedupe satisfies the same observable invariants. This is
	the seam [FLO-16](/FLO/issues/FLO-16) tightens under ``bench run-tests``.
	"""

	def __init__(self) -> None:
		self._service = BulkAttendanceService(InMemoryBulkAttendanceGateway())

	def submit_batch(
		self,
		records: list[AttendanceRecord],
		scope: ReportingScope,
		idempotency_key: str,
	):  # type: ignore[no-untyped-def]
		items = [
			AttendanceItem(
				event=record.gathering_id,
				attendee_ref=record.member_id,
				branch=record.branch_id,
				client_req_id=f"{idempotency_key}:{record.member_id}",
			)
			for record in records
		]
		outcome = self._service.submit(items, AttendanceScope(scope.branch_id), idempotency_key)
		events = [
			QADomainEvent(
				name=e.name.split(".", 1)[1] if e.name.startswith("flock.") else e.name,
				payload=dict(e.payload),
			)
			for e in outcome.events
		]
		return BulkBatchResult(
			idempotency_key=idempotency_key,
			scope=scope,
			accepted=outcome.accepted,
			inserted=outcome.inserted,
			rejected=outcome.rejected_count,
			total=self.aggregate(scope),
			events=events,
		)

	def aggregate(self, scope: ReportingScope) -> int:
		return self._service.aggregate(AttendanceScope(scope.branch_id))


def test_production_service_conforms_to_qa_contract_protocol() -> None:
	"""The canonical service (via adapter) satisfies the QA BulkAttendanceService Protocol."""
	adapter = QABulkAttendanceAdapter()
	assert isinstance(adapter, QABulkAttendanceService)


def test_qa_15k_fixture_path_against_production_service() -> None:
	"""The exact FLO-16 15k-fixture assertions pass against the production service."""
	service = QABulkAttendanceAdapter()
	scope = ReportingScope(branch_id="branch-a")
	fixture = make_attendance_fixture(ATTENDEE_SCALE, branch_id="branch-a")

	result = service.submit_batch(fixture, scope, idempotency_key="batch-1")

	assert result.accepted is True
	assert result.inserted == ATTENDEE_SCALE
	assert result.rejected == 0
	assert service.aggregate(scope) == ATTENDEE_SCALE

	# Idempotent replay.
	replay = service.submit_batch(fixture, scope, idempotency_key="batch-1")
	assert replay.inserted == 0
	assert service.aggregate(scope) == ATTENDEE_SCALE

	# Wholesale scope rejection.
	mixed = make_attendance_fixture(100, branch_id="branch-a")
	mixed[0] = AttendanceRecord(member_id="intruder", branch_id="branch-b", gathering_id="gathering-1")
	rejected = service.submit_batch(mixed, scope, idempotency_key="batch-2")
	assert rejected.accepted is False
	assert rejected.inserted == 0
	assert rejected.rejected == len(mixed)
	assert service.aggregate(scope) == ATTENDEE_SCALE
