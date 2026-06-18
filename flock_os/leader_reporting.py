"""
Leader attendance-reporting workflow service (FLO-56, spec FLO-6 §4).

This is the **leader-facing reporting workflow** over a ``Flock Gathering``:
a group leader submits one attendance report (members **+ visitors / pre-members
as attendees**) which (a) records every attendee through the canonical bulk
write path, (b) drives the gathering ``Held → Reported`` lifecycle transition,
and (c) emits the canonical ``flock.attendance.reported`` domain event.

Architecture (separation of concerns — services out of transport code; ADR-0001
§2; mirrors :mod:`flock_os.reporting`)::

    REST  (flock_os.attendance.report_attendance)   @frappe.whitelist()
      → Service  (LeaderReportingService)           ← domain rules live HERE (pure)
      → Gateway port (LeaderReportingGateway)
          ├─ InMemoryLeaderReportingGateway          ← unit tests (reuses the
          │                                           in-memory bulk service)
          └─ FrappeLeaderReportingGateway            ← Frappe: BulkAttendanceService
                                                      (seeds the 15k queue path,
                                                      FLO-10 §3) + gathering
                                                      transition + canonical emit

Reuse rules honoured (DRY — no copied logic):

* **State machine** is delegated to :mod:`flock_os.gatherings`
  (:data:`gatherings.TRANSITIONS` / :func:`gatherings.validate_status_transition`).
  This service never re-declares the lifecycle table.
* **Bulk writes** are delegated to :class:`flock_os.reporting.BulkAttendanceService`
  via the gateway — so this workflow *is* the queue-backed 15k-scale path's
  client. Idempotency (``(event, attendee_ref, client_req_id)``), the
  ``(event, attendee_ref)`` unique backstop, atomic aggregates, and the
  ``flock.attendance.bulk_recorded`` fan-out all flow from that reuse
  (FLO-10 §3 / FLO-15). No parallel write path.
* **Event emission** is delegated to the gateway, which routes through the
  single sanctioned :func:`flock_os.events.emit` → Redis pub/sub + outbox
  (ADR-0001 §5.1, [FLO-14](/FLO/issues/FLO-14)). No dual emitters.

The :class:`LeaderReportingService` is pure Python and transport-agnostic: fully
unit-testable without HTTP, a queue, MariaDB, Redis, or even Frappe. It depends
only on the :class:`LeaderReportingGateway` port (hexagonal / ports & adapters).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from flock_os import gatherings
from flock_os.events import ATTENDANCE_REPORTED
from flock_os.reporting import (
	BULK_BATCH_SIZE,
	AttendanceItem,
	AttendanceScope,
	BulkAttendanceService,
	InMemoryBulkAttendanceGateway,
)

# ---------------------------------------------------------------------------- #
# Attendee status vocabulary (FLO-5 §3.2 / FLO-6 rev 2 addendum).
#
# ``Visitor`` is a **status on ``Flock Member``**, not a DocType — and so is
# ``Pre-Member``. Every attendee in a leader report is therefore a ``Flock
# Member`` ref; the member's ``status`` classifies the row for the
# member/visitor roll-up counters on the gathering. These are the canonical
# option strings on ``Flock Member.status``.
# ---------------------------------------------------------------------------- #
MEMBER_STATUS_MEMBER = "Member"
MEMBER_STATUS_PRE_MEMBER = "Pre-Member"
MEMBER_STATUS_VISITOR = "Visitor"

#: Member statuses that count as **visitors** in the gathering roll-up (FLO-6
#: rev 2 addendum: visitors/pre-members are non-joined attendees). Everything
#: else with a valid ``Flock Member`` ref counts as a member.
VISITOR_STATUSES: frozenset[str] = frozenset({MEMBER_STATUS_PRE_MEMBER, MEMBER_STATUS_VISITOR})

#: Every attendee must resolve to one of these (the ``Flock Member.status``
#: Select options). Anything else means the member master is inconsistent.
VALID_MEMBER_STATUSES: frozenset[str] = frozenset(
	{MEMBER_STATUS_MEMBER, MEMBER_STATUS_PRE_MEMBER, MEMBER_STATUS_VISITOR}
)

#: Reporting source stamped on every attendance row written by this workflow.
#: Distinguishes leader-reported rows from bulk/game/questionnaire sources
#: (``Flock Attendance Record.source``, FLO-10 §4.1).
LEADER_REPORT_SOURCE = "leader"

#: Default per-row attendance status for a reported-present attendee.
PRESENT_STATUS = "Present"

#: The lifecycle status this workflow advances the gathering *to* (FLO-6 §4).
REPORTED_STATUS = gatherings.STATUS_REPORTED

#: The lifecycle status the gathering must be in to be reported (FLO-6 §4).
REPORTABLE_STATUS = gatherings.STATUS_HELD


class LeaderReportingError(ValueError):
	"""Raised when a leader report submission violates a workflow invariant."""


# ---------------------------------------------------------------------------- #
# Report submission / outcome value objects.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AttendeeReport:
	"""One attendee on a leader's attendance report.

	``member`` is a ``Flock Member`` ref — members, visitors, and pre-members are
	all ``Flock Member`` rows (FLO-6 rev 2 addendum). ``present`` records
	attendance (only present attendees produce a ``Flock Attendance Record``
	row). ``first_time`` flags first-time attendees for the gathering's
	``first_time_count`` roll-up. ``client_req_id`` is the per-attendee
	idempotency key (falls back to the member ref when omitted, matching the
	bulk path's idempotency contract).
	"""

	member: str
	present: bool = True
	first_time: bool = False
	client_req_id: str | None = None


@dataclass(frozen=True)
class ReportSubmission:
	"""A single leader attendance-report submission for one gathering.

	A submission is **branch-scoped** (``branch`` is the row-level permission
	anchor, ADR-0001 §6.2) and **group-scoped** (``group`` is the leader's led
	group). ``client_batch_id`` is the caller-supplied idempotency key for the
	whole submission; per-chunk batch ids are derived from it so the queue path
	stays replay-safe.
	"""

	gathering: str
	branch: str
	group: str
	reported_by: str
	attendees: Sequence[AttendeeReport]
	client_batch_id: str


@dataclass
class ReportOutcome:
	"""Receipt for a submitted leader attendance report."""

	gathering: str
	status: str
	accepted: bool
	inserted: int
	deduplicated: int
	member_count: int
	visitor_count: int
	first_time_count: int
	batch_ids: list[str] = field(default_factory=list)
	reason: str | None = None


# ---------------------------------------------------------------------------- #
# Gateway port — the storage + emission seam (hexagonal).
# ---------------------------------------------------------------------------- #
@runtime_checkable
class LeaderReportingGateway(Protocol):
	"""Port: the read/write + emission surface :class:`LeaderReportingService` uses.

	Implementations:

	* :class:`InMemoryLeaderReportingGateway` — unit tests (reuses the bulk path).
	* :class:`FrappeLeaderReportingGateway` — production (MariaDB + Redis).

	The port keeps the service free of Frappe/DB concerns so the workflow is
	unit-testable in isolation (ADR-0001 §2). ``record_attendance`` returns
	``(inserted, deduplicated)`` per chunk — the bulk service owns idempotency,
	the unique backstop, and the atomic aggregate update.
	"""

	def gathering_status(self, gathering: str) -> str | None:
		"""The gathering's current lifecycle status, or ``None`` if it does not exist."""
		...

	def member_status(self, member: str) -> str | None:
		"""The member's ``status`` (Member/Pre-Member/Visitor), or ``None`` if unknown."""
		...

	def record_attendance(
		self, items: Sequence[AttendanceItem], scope: AttendanceScope, batch_id: str
	) -> tuple[int, int]:
		"""Persist one chunk of attendance rows (delegated to the bulk service).

		Returns ``(inserted, deduplicated)``. The bulk service owns per-key
		idempotency, the ``(event, attendee_ref)`` unique backstop, the atomic
		``Event Attendance Summary`` update, and the ``flock.attendance.bulk_recorded``
		fan-out (FLO-10 §3.3). Retries/replays are idempotent.
		"""
		...

	def advance_gathering(
		self,
		gathering: str,
		to_status: str,
		*,
		reported_by: str,
		member_count: int,
		visitor_count: int,
		first_time_count: int,
	) -> None:
		"""Apply the gathering lifecycle transition + roll-up counters in one tx."""
		...

	def emit(
		self,
		event: str,
		*,
		payload: dict[str, object],
		scope: dict[str, object],
	) -> None:
		"""Publish a domain event via the single sanctioned emitter (ADR-0001 §5.1)."""
		...


# ---------------------------------------------------------------------------- #
# In-memory adapter (unit-test surface).
# ---------------------------------------------------------------------------- #
class InMemoryLeaderReportingGateway:
	"""Reference in-memory adapter for unit tests (reuses the in-memory bulk path).

	Models the workflow semantics the production gateway must honour: gathering
	status read/transition, member-status classification, chunked bulk writes via
	:class:`BulkAttendanceService` + :class:`InMemoryBulkAttendanceGateway`, and
	captured canonical emits. No Frappe / DB / Redis — runs under plain pytest.
	"""

	def __init__(self) -> None:
		self._gathering_status: dict[str, str] = {}
		self._member_status: dict[str, str] = {}
		# One shared in-memory bulk gateway so idempotency/aggregate semantics
		# are exactly the production contract across chunks + replays.
		self._bulk_gateway = InMemoryBulkAttendanceGateway()
		self._bulk_service = BulkAttendanceService(self._bulk_gateway)
		self.published_events: list[tuple[str, dict[str, object], dict[str, object]]] = []
		self.advanced: list[dict[str, object]] = []

	# -- test setup helpers ------------------------------------------------- #
	def register_gathering(self, gathering: str, status: str) -> None:
		"""Seed a gathering + its current lifecycle status (test fixture)."""
		self._gathering_status[gathering] = status

	def register_member(self, member: str, status: str) -> None:
		"""Seed a member + their status (Member/Pre-Member/Visitor) (test fixture)."""
		self._member_status[member] = status

	# -- port impl ---------------------------------------------------------- #
	def gathering_status(self, gathering: str) -> str | None:
		return self._gathering_status.get(gathering)

	def member_status(self, member: str) -> str | None:
		return self._member_status.get(member)

	def record_attendance(
		self, items: Sequence[AttendanceItem], scope: AttendanceScope, batch_id: str
	) -> tuple[int, int]:
		outcome = self._bulk_service.submit(items, scope, batch_id)
		return outcome.inserted, outcome.deduplicated

	def advance_gathering(
		self,
		gathering: str,
		to_status: str,
		*,
		reported_by: str,
		member_count: int,
		visitor_count: int,
		first_time_count: int,
	) -> None:
		self._gathering_status[gathering] = to_status
		self.advanced.append(
			{
				"gathering": gathering,
				"to_status": to_status,
				"reported_by": reported_by,
				"member_count": member_count,
				"visitor_count": visitor_count,
				"first_time_count": first_time_count,
			}
		)

	def emit(
		self,
		event: str,
		*,
		payload: dict[str, object],
		scope: dict[str, object],
	) -> None:
		self.published_events.append((event, dict(payload), dict(scope)))

	# -- test introspection ------------------------------------------------- #
	def aggregate(self, branch: str, gathering: str) -> int:
		"""Read the maintained bulk rollup for ``(branch, gathering)`` (test asserts)."""
		return self._bulk_gateway.aggregate(AttendanceScope(branch=branch), gathering)


# ---------------------------------------------------------------------------- #
# The workflow service — pure coordinator (no Frappe).
# ---------------------------------------------------------------------------- #
class LeaderReportingService:
	"""The canonical leader attendance-reporting workflow (FLO-6 §4 / FLO-56).

	Owns the *coordination* invariants only — state-machine legality is delegated
	to :mod:`flock_os.gatherings`, bulk writes + idempotency + aggregates to the
	:class:`flock_os.reporting.BulkAttendanceService` via the gateway, and event
	emission to the gateway's canonical emitter. This keeps the service pure and
	free of copied logic (DRY).

	A submission:

	1. Validates the gathering is :data:`REPORTABLE_STATUS` (``Held``), not terminal.
	2. Validates every attendee is a known ``Flock Member`` (visitors/pre-members ok).
	3. Chunks present attendees into :data:`BULK_BATCH_SIZE` batches (15k path).
	4. Advances the gathering ``Held → Reported`` + stamps roll-up counters.
	5. Emits ``flock.attendance.reported`` via the canonical emitter (row-scoped).
	"""

	def __init__(self, gateway: LeaderReportingGateway) -> None:
		self.gateway = gateway

	def submit_report(self, submission: ReportSubmission) -> ReportOutcome:
		self._validate_submission_shape(submission)
		self._validate_gathering(submission)

		# Classify + build attendance items for present attendees. Visitors /
		# pre-members are recorded exactly like members (FLO-6 rev 2 addendum) —
		# the member.status only steers the roll-up classification.
		member_count = 0
		visitor_count = 0
		first_time_count = 0
		items: list[AttendanceItem] = []
		for attendee in submission.attendees:
			status = self.gateway.member_status(attendee.member)
			if status is None:
				raise LeaderReportingError(f"Attendee {attendee.member!r} is not a known Flock Member.")
			if status not in VALID_MEMBER_STATUSES:
				raise LeaderReportingError(
					f"Attendee {attendee.member!r} has unknown member status {status!r}."
				)
			if attendee.first_time:
				first_time_count += 1
			if not attendee.present:
				continue  # absent — no attendance row written (presence is the row).
			if status in VISITOR_STATUSES:
				visitor_count += 1
			else:
				member_count += 1
			items.append(self._to_attendance_item(submission, attendee))

		scope = AttendanceScope(branch=submission.branch)
		inserted, deduplicated, batch_ids = self._record_in_chunks(submission, items, scope)

		# Lifecycle legality is re-checked via the canonical state machine — the
		# service never re-declares the transition table (DRY).
		from_status = self.gateway.gathering_status(submission.gathering)
		gatherings.validate_status_transition(from_status=from_status, to_status=REPORTED_STATUS)
		self.gateway.advance_gathering(
			submission.gathering,
			REPORTED_STATUS,
			reported_by=submission.reported_by,
			member_count=member_count,
			visitor_count=visitor_count,
			first_time_count=first_time_count,
		)

		event_scope: dict[str, object] = {"branch": submission.branch, "group": submission.group}
		self.gateway.emit(
			ATTENDANCE_REPORTED,
			payload={
				"gathering": submission.gathering,
				"reported_by": submission.reported_by,
				"member_count": member_count,
				"visitor_count": visitor_count,
				"total_count": member_count + visitor_count,
				"first_time_count": first_time_count,
				"inserted": inserted,
				"deduplicated": deduplicated,
				"client_batch_id": submission.client_batch_id,
			},
			scope=event_scope,
		)

		return ReportOutcome(
			gathering=submission.gathering,
			status=REPORTED_STATUS,
			accepted=True,
			inserted=inserted,
			deduplicated=deduplicated,
			member_count=member_count,
			visitor_count=visitor_count,
			first_time_count=first_time_count,
			batch_ids=batch_ids,
		)

	# ------------------------------------------------------------------ #
	# Internals
	# ------------------------------------------------------------------ #
	def _validate_submission_shape(self, submission: ReportSubmission) -> None:
		if not submission.gathering:
			raise LeaderReportingError("Report submission requires a gathering.")
		if not submission.branch:
			raise LeaderReportingError("Report submission requires a branch (row-level scope).")
		if not submission.group:
			raise LeaderReportingError("Report submission requires a group (leader scope).")
		if not submission.reported_by:
			raise LeaderReportingError("Report submission requires a reported_by member ref.")
		if not submission.client_batch_id:
			raise LeaderReportingError("Report submission requires a client_batch_id (idempotency).")
		# Repeated attendee refs in a single submission collapse to one row via
		# the bulk unique backstop; we accept that rather than reject (replays +
		# accidental dups must never double-count). No shape error here.

	def _validate_gathering(self, submission: ReportSubmission) -> None:
		current = self.gateway.gathering_status(submission.gathering)
		if current is None:
			raise LeaderReportingError(f"Gathering {submission.gathering!r} does not exist.")
		if gatherings.is_terminal_status(current):
			raise LeaderReportingError(
				f"Gathering {submission.gathering!r} is terminal ({current!r}); cannot be reported."
			)
		if current != REPORTABLE_STATUS:
			raise LeaderReportingError(
				f"Gathering {submission.gathering!r} is {current!r}; a report requires "
				f"{REPORTABLE_STATUS!r} (FLO-6 §4: Held → Reported)."
			)

	def _to_attendance_item(self, submission: ReportSubmission, attendee: AttendeeReport) -> AttendanceItem:
		# ``client_req_id`` falls back to the member ref when the caller omits it,
		# matching the bulk path idempotency contract (AttendanceItem.idempotency_key)
		# and keeping leader-reported rows self-describing (never a NULL key).
		return AttendanceItem(
			event=submission.gathering,
			attendee_ref=attendee.member,
			branch=submission.branch,
			status=PRESENT_STATUS,
			source=LEADER_REPORT_SOURCE,
			client_req_id=attendee.client_req_id or attendee.member,
		)

	def _record_in_chunks(
		self,
		submission: ReportSubmission,
		items: Sequence[AttendanceItem],
		scope: AttendanceScope,
	) -> tuple[int, int, list[str]]:
		"""Chunk present attendees into :data:`BULK_BATCH_SIZE` batches (FLO-10 §3.2).

		Each chunk is one queue-ready batch with a deterministic id derived from
		the submission's ``client_batch_id`` so the 15k path is replay-safe. The
		gateway (→ bulk service) owns per-row idempotency + the atomic aggregate.
		"""
		inserted = 0
		deduplicated = 0
		batch_ids: list[str] = []
		item_list = list(items)
		for index, start in enumerate(range(0, len(item_list), BULK_BATCH_SIZE)):
			chunk = item_list[start : start + BULK_BATCH_SIZE]
			batch_id = f"{submission.client_batch_id}#{index}"
			chunk_inserted, chunk_dedup = self.gateway.record_attendance(chunk, scope, batch_id)
			inserted += chunk_inserted
			deduplicated += chunk_dedup
			batch_ids.append(batch_id)
		return inserted, deduplicated, batch_ids


# ---------------------------------------------------------------------------- #
# Production adapter — Frappe-coupled (lazy import; import-clean under pytest).
# ---------------------------------------------------------------------------- #
class FrappeLeaderReportingGateway:
	"""Production adapter wiring the workflow to Frappe + the canonical emitter.

	Lazily imports Frappe so this module stays import-clean in CI (no bench).
	Attendance writes delegate to :class:`BulkAttendanceService` over
	:class:`FrappeBulkAttendanceGateway` — i.e. the leader workflow *is* a client
	of the 15k-scale queue-backed write path (FLO-10 §3). Event emission routes
	through :func:`flock_os.events.emit` (single sanctioned publisher, FLO-14).
	"""

	def __init__(self) -> None:
		self._bulk_service: BulkAttendanceService | None = None

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	@property
	def bulk_service(self) -> BulkAttendanceService:
		"""Lazy single :class:`BulkAttendanceService` over the Frappe bulk gateway."""
		if self._bulk_service is None:
			from flock_os.reporting import FrappeBulkAttendanceGateway

			self._bulk_service = BulkAttendanceService(FrappeBulkAttendanceGateway())
		return self._bulk_service

	def gathering_status(self, gathering: str) -> str | None:
		frappe = self._frappe
		if not frappe.db.exists("Flock Gathering", gathering):
			return None
		return frappe.db.get_value("Flock Gathering", gathering, "status")

	def member_status(self, member: str) -> str | None:
		frappe = self._frappe
		if not frappe.db.exists("Flock Member", member):
			return None
		return frappe.db.get_value("Flock Member", member, "status")

	def record_attendance(
		self, items: Sequence[AttendanceItem], scope: AttendanceScope, batch_id: str
	) -> tuple[int, int]:
		outcome = self.bulk_service.submit(items, scope, batch_id)
		return outcome.inserted, outcome.deduplicated

	def advance_gathering(
		self,
		gathering: str,
		to_status: str,
		*,
		reported_by: str,
		member_count: int,
		visitor_count: int,
		first_time_count: int,
	) -> None:
		import frappe

		doc = frappe.get_doc("Flock Gathering", gathering)
		doc.status = to_status
		doc.reported_by = reported_by
		doc.reported_at = frappe.utils.now()
		# Roll-up counters are system-maintained (permlevel 2); set them in the
		# same tx as the lifecycle transition. The bulk path's maintained
		# ``Event Attendance Summary`` stays the source of truth for hot-path
		# counts (FLO-10 §4.2); these denormalized counters serve the gathering
		# view + roll-up (ADR §9).
		doc.member_attendance_count = member_count
		doc.visitor_attendance_count = visitor_count
		doc.total_attendance_count = member_count + visitor_count
		doc.first_time_count = first_time_count
		doc.save(ignore_permissions=True)

	def emit(
		self,
		event: str,
		*,
		payload: dict[str, object],
		scope: dict[str, object],
	) -> None:
		from flock_os.events import emit

		emit(event, payload=dict(payload), scope=dict(scope))


__all__ = (
	"FrappeLeaderReportingGateway",
	"InMemoryLeaderReportingGateway",
	"LEADER_REPORT_SOURCE",
	"LeaderReportingError",
	"LeaderReportingGateway",
	"LeaderReportingService",
	"MEMBER_STATUS_MEMBER",
	"MEMBER_STATUS_PRE_MEMBER",
	"MEMBER_STATUS_VISITOR",
	"PRESENT_STATUS",
	"REPORTABLE_STATUS",
	"REPORTED_STATUS",
	"ReportOutcome",
	"ReportSubmission",
	"AttendeeReport",
	"VALID_MEMBER_STATUSES",
	"VISITOR_STATUSES",
)
