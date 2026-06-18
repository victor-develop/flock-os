"""
Scale load harness for bulk attendance reporting (FLO-16, per FLO-10 ADR §8).

This is the **SQL-light, project-level** contract layer of the 15k scale bar:
fast, deterministic, and runnable under plain ``pytest`` in CI without a
Frappe site, MariaDB, or Redis. It pins the *observable contract* that the
queue-based bulk attendance service (FLO-15) must satisfy:

  * count via a maintained summary aggregate, never a full-table scan
  * idempotency: replaying the same batch writes 0 new rows
  * scope rejection: an out-of-scope batch is rejected **wholesale** (atomic)
  * aggregate counters match the raw fixture breakdown
  * expected domain events are emitted on each state transition

Every scale assertion runs **parametrized** over two backends:

  * :class:`InMemoryBulkAttendanceService` -- the transparent reference oracle
    (the spec, independent of any implementation).
  * :class:`ProductionBulkAttendanceAdapter` -- the **real** canonical
    :class:`flock_os.reporting.BulkAttendanceService` (FLO-15) backed by its
    :class:`InMemoryBulkAttendanceGateway`, driven through the QA contract
    Protocol. This is the FLO-16 DoD: the 15k fixture is inserted *via the bulk
    service*, gated in CI. The Frappe-backed adapter under ``bench run-tests``
    reuses the same fixture + assertions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import pytest

from flock_os.reporting import (
	AttendanceItem,
	AttendanceScope,
	InMemoryBulkAttendanceGateway,
)
from flock_os.reporting import (
	BulkAttendanceService as _ProductionBulkAttendanceService,
)

ATTENDEE_SCALE = 15_000


@dataclass(frozen=True)
class AttendanceRecord:
	"""A single attendance row to be bulk-reported."""

	member_id: str
	branch_id: str
	gathering_id: str
	attended: bool = True


@dataclass(frozen=True)
class ReportingScope:
	"""The org-tree node (branch) a batch is reported against."""

	branch_id: str

	@property
	def key(self) -> str:
		return self.branch_id


@dataclass(frozen=True)
class DomainEvent:
	"""A domain event emitted by a state change (Frappe hook + Redis pub/sub)."""

	name: str
	payload: dict[str, object] = field(default_factory=dict)


@dataclass
class BulkBatchResult:
	"""Result of submitting one attendance batch."""

	idempotency_key: str
	scope: ReportingScope
	accepted: bool
	inserted: int
	rejected: int
	total: int
	events: list[DomainEvent] = field(default_factory=list)


@runtime_checkable
class BulkAttendanceService(Protocol):
	"""Contract the production bulk attendance service (FLO-15) conforms to."""

	def submit_batch(
		self,
		records: list[AttendanceRecord],
		scope: ReportingScope,
		idempotency_key: str,
	) -> BulkBatchResult: ...

	def aggregate(self, scope: ReportingScope) -> int: ...


class InMemoryBulkAttendanceService:
	"""Reference oracle implementing :class:`BulkAttendanceService`.

	Models exactly the bulk-write semantics the production service must honour:
	batch-level idempotency keys, atomic wholesale scope rejection, a maintained
	summary aggregate (the source of truth for counts -- never scanned), and
	domain-event emission on every transition.
	"""

	def __init__(self) -> None:
		self._seen_keys: set[str] = set()
		self._counters: dict[str, int] = {}

	def submit_batch(
		self,
		records: list[AttendanceRecord],
		scope: ReportingScope,
		idempotency_key: str,
	) -> BulkBatchResult:
		if idempotency_key in self._seen_keys:
			return BulkBatchResult(
				idempotency_key=idempotency_key,
				scope=scope,
				accepted=True,
				inserted=0,
				rejected=0,
				total=self.aggregate(scope),
				events=[],
			)

		if any(record.branch_id != scope.branch_id for record in records):
			return BulkBatchResult(
				idempotency_key=idempotency_key,
				scope=scope,
				accepted=False,
				inserted=0,
				rejected=len(records),
				total=self.aggregate(scope),
				events=[
					DomainEvent(
						name="attendance.batch_rejected",
						payload={"scope": scope.key, "rejected": len(records)},
					)
				],
			)

		self._seen_keys.add(idempotency_key)
		self._counters[scope.key] = self.aggregate(scope) + len(records)
		return BulkBatchResult(
			idempotency_key=idempotency_key,
			scope=scope,
			accepted=True,
			inserted=len(records),
			rejected=0,
			total=self.aggregate(scope),
			events=[
				DomainEvent(
					name="attendance.bulk_recorded",
					payload={"scope": scope.key, "inserted": len(records)},
				)
			],
		)

	def aggregate(self, scope: ReportingScope) -> int:
		return self._counters.get(scope.key, 0)


class ProductionBulkAttendanceAdapter:
	"""QA conformance seam: drives the **real** canonical ``BulkAttendanceService``
	([FLO-15](/FLO/issues/FLO-15), ``flock_os.reporting``) through the QA contract
	:class:`BulkAttendanceService` Protocol, so the 15k scale assertions gate the
	production domain service -- not just the reference oracle.

	The production service dedupes per-item on ``(event, attendee_ref,
	client_req_id)``; this adapter derives a per-item ``client_req_id`` from the
	batch key so the observable FLO-16 invariants (replay = 0 new rows, wholesale
	scope rejection) hold against the real dedupe path. Event payloads are
	projected to the canonical contract shape (``scope`` / ``inserted``). This
	mirrors the adapter in ``test_bulk_attendance_service`` but lives in the
	QA-owned gate module to avoid a test-file import cycle.
	"""

	def __init__(self) -> None:
		self._service = _ProductionBulkAttendanceService(InMemoryBulkAttendanceGateway())

	def submit_batch(
		self,
		records: list[AttendanceRecord],
		scope: ReportingScope,
		idempotency_key: str,
	) -> BulkBatchResult:
		items = [
			AttendanceItem(
				event=record.gathering_id,
				attendee_ref=record.member_id,
				branch=record.branch_id,
				client_req_id=f"{idempotency_key}:{record.member_id}",
			)
			for record in records
		]
		outcome = self._service.submit(items, AttendanceScope(branch=scope.branch_id), idempotency_key)
		return BulkBatchResult(
			idempotency_key=idempotency_key,
			scope=scope,
			accepted=outcome.accepted,
			inserted=outcome.inserted,
			rejected=outcome.rejected_count,
			total=self.aggregate(scope),
			events=[_canonical_event(e) for e in outcome.events],
		)

	def aggregate(self, scope: ReportingScope) -> int:
		return self._service.aggregate(AttendanceScope(branch=scope.branch_id))


def _canonical_event(event) -> DomainEvent:  # type: ignore[no-untyped-def]
	"""Project a production domain event onto the QA contract shape."""
	name = event.name.split("flock.", 1)[1] if event.name.startswith("flock.") else event.name
	payload = dict(event.payload)
	canonical: dict[str, object] = {}
	if "count" in payload:
		canonical["inserted"] = payload["count"]
	elif "inserted" in payload:
		canonical["inserted"] = payload["inserted"]
	if "branch" in payload:
		canonical["scope"] = payload["branch"]
	elif "scope" in payload:
		canonical["scope"] = payload["scope"]
	if "rejected" in payload:
		canonical["rejected"] = payload["rejected"]
	return DomainEvent(name=name, payload=canonical)


ServiceFactory = Callable[[], BulkAttendanceService]
_BACKENDS = [
	pytest.param(lambda: InMemoryBulkAttendanceService(), id="oracle"),
	pytest.param(lambda: ProductionBulkAttendanceAdapter(), id="production"),
]


def make_attendance_fixture(
	n: int = ATTENDEE_SCALE,
	*,
	branch_id: str = "branch-a",
	gathering_id: str = "gathering-1",
	member_prefix: str = "member",
) -> list[AttendanceRecord]:
	"""Build an ``n``-row attendance fixture for a single branch/gathering.

	``member_prefix`` keeps attendee refs globally distinct across branches so
	the production ``(event, attendee_ref)`` unique-index backstop (FLO-15) does
	not cross-dedupe one branch's members against another's.
	"""
	return [
		AttendanceRecord(
			member_id=f"{member_prefix}-{i}",
			branch_id=branch_id,
			gathering_id=gathering_id,
		)
		for i in range(n)
	]


def test_reference_service_implements_contract() -> None:
	"""The oracle satisfies the :class:`BulkAttendanceService` contract."""
	assert isinstance(InMemoryBulkAttendanceService(), BulkAttendanceService)


def test_production_service_conforms_to_qa_contract() -> None:
	"""The real bulk service (via adapter) satisfies the QA contract Protocol."""
	assert isinstance(ProductionBulkAttendanceAdapter(), BulkAttendanceService)


@pytest.mark.parametrize("service_factory", _BACKENDS)
def test_15k_fixture_inserts_all_rows_counted_via_aggregate(
	service_factory: ServiceFactory,
) -> None:
	"""A 15k-row batch is fully accepted; count read from the aggregate, not a scan."""
	service = service_factory()
	scope = ReportingScope(branch_id="branch-a")
	fixture = make_attendance_fixture(ATTENDEE_SCALE, branch_id="branch-a")

	result = service.submit_batch(fixture, scope, idempotency_key="batch-1")

	assert result.accepted is True
	assert result.inserted == ATTENDEE_SCALE
	assert result.rejected == 0
	# Count is read from the maintained summary aggregate -- the production
	# service must never answer this with a full-table scan at 15k scale.
	assert service.aggregate(scope) == ATTENDEE_SCALE


@pytest.mark.parametrize("service_factory", _BACKENDS)
def test_idempotent_replay_writes_zero_new_rows(service_factory: ServiceFactory) -> None:
	"""Replaying the same batch (same idempotency key) inserts 0 new rows."""
	service = service_factory()
	scope = ReportingScope(branch_id="branch-a")
	fixture = make_attendance_fixture(ATTENDEE_SCALE, branch_id="branch-a")

	service.submit_batch(fixture, scope, idempotency_key="batch-1")
	replay = service.submit_batch(fixture, scope, idempotency_key="batch-1")

	assert replay.inserted == 0
	assert replay.accepted is True
	assert service.aggregate(scope) == ATTENDEE_SCALE


@pytest.mark.parametrize("service_factory", _BACKENDS)
def test_out_of_scope_batch_rejected_wholesale(service_factory: ServiceFactory) -> None:
	"""One out-of-scope record rejects the whole batch -- no partial writes."""
	service = service_factory()
	scope = ReportingScope(branch_id="branch-a")
	fixture = make_attendance_fixture(ATTENDEE_SCALE, branch_id="branch-a")
	service.submit_batch(fixture, scope, idempotency_key="batch-1")

	mixed = make_attendance_fixture(100, branch_id="branch-a")
	mixed[0] = AttendanceRecord(member_id="intruder", branch_id="branch-b", gathering_id="gathering-1")
	result = service.submit_batch(mixed, scope, idempotency_key="batch-2")

	assert result.accepted is False
	assert result.inserted == 0
	assert result.rejected == len(mixed)
	# Aggregate is untouched by a rejected batch.
	assert service.aggregate(scope) == ATTENDEE_SCALE


@pytest.mark.parametrize("service_factory", _BACKENDS)
def test_aggregate_counters_match_raw_fixture_breakdown(service_factory: ServiceFactory) -> None:
	"""Aggregate counters equal the fixture's per-gathering breakdown."""
	service = service_factory()
	scope = ReportingScope(branch_id="branch-a")
	one = make_attendance_fixture(10_000, branch_id="branch-a", gathering_id="g-1")
	two = make_attendance_fixture(5_000, branch_id="branch-a", gathering_id="g-2")

	service.submit_batch(one, scope, idempotency_key="batch-g1")
	service.submit_batch(two, scope, idempotency_key="batch-g2")

	assert service.aggregate(scope) == len(one) + len(two) == ATTENDEE_SCALE


@pytest.mark.parametrize("service_factory", _BACKENDS)
def test_expected_domain_events_emitted_on_each_transition(service_factory: ServiceFactory) -> None:
	"""Accept emits bulk_recorded; scope violation emits batch_rejected; replay emits nothing."""
	service = service_factory()
	scope = ReportingScope(branch_id="branch-a")
	fixture = make_attendance_fixture(ATTENDEE_SCALE, branch_id="branch-a")

	accepted = service.submit_batch(fixture, scope, idempotency_key="batch-1")
	assert [e.name for e in accepted.events] == ["attendance.bulk_recorded"]
	assert accepted.events[0].payload["inserted"] == ATTENDEE_SCALE
	assert accepted.events[0].payload["scope"] == scope.key

	replay = service.submit_batch(fixture, scope, idempotency_key="batch-1")
	assert replay.events == []

	out_of_scope = [AttendanceRecord(member_id="x", branch_id="branch-b", gathering_id="g")]
	rejected = service.submit_batch(out_of_scope, scope, idempotency_key="batch-2")
	assert [e.name for e in rejected.events] == ["attendance.batch_rejected"]
	assert rejected.events[0].payload["rejected"] == len(out_of_scope)


@pytest.mark.parametrize("service_factory", _BACKENDS)
def test_branch_isolation_branch_b_invisible_to_branch_a(service_factory: ServiceFactory) -> None:
	"""Permission/scope matrix: branch B's attendance never counts under branch A."""
	service = service_factory()
	branch_a = ReportingScope(branch_id="branch-a")
	branch_b = ReportingScope(branch_id="branch-b")

	service.submit_batch(
		make_attendance_fixture(ATTENDEE_SCALE, branch_id="branch-a", member_prefix="member-a"),
		branch_a,
		idempotency_key="a-1",
	)
	service.submit_batch(
		make_attendance_fixture(3_000, branch_id="branch-b", member_prefix="member-b"),
		branch_b,
		idempotency_key="b-1",
	)

	assert service.aggregate(branch_a) == ATTENDEE_SCALE
	assert service.aggregate(branch_b) == 3_000
