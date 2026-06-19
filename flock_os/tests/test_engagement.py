"""
Project-level tests for the Fun Attendance engagement runtime (FLO-11).

These run under plain ``pytest`` (no Frappe site / bench / Redis required). They
pin the FLO-9 design contract ([rev 4](/FLO/issues/FLO-9#document-design)):

* **Server-authoritative validators** — window/scope/throttle/state-machine.
* **Signed session tickets** — HMAC sign/verify + expiry + attendee binding.
* **Idempotency** — ``(session, attendee_key, nonce)`` dedupe on participate +
  bulk_attendance; the ``(engagement_session, attendee_key)`` backstop on close.
* **Close path** — projects the in-window participation set to
  :class:`flock_os.reporting.AttendanceItem` rows and routes them through the
  canonical :class:`BulkAttendanceService` (FLO-15), emitting exactly one
  ``flock.engagement.closed`` + the bulk service's single
  ``flock.attendance.bulk_recorded``.
* **Anti-abuse** — non-blocking suspect-pattern heuristics; out-of-window rows
  recorded but excluded from headcount; facilitator override flag.
* **Schema** — DocType catalog, scoping contract, indexes patch.

The pure service layer is exercised against :class:`InMemoryEngagementGateway`;
the production :class:`FrappeEngagementGateway` is bench-only (omitted from the
project gate) and exercised by Frappe-level integration tests.
"""

from __future__ import annotations

import json
import time

import pytest

from flock_os import engagement as eng
from flock_os.engagement import (
	DEFAULT_GRACE_SECONDS,
	DEFAULT_TICKET_TTL_SECONDS,
	STATUS_ARCHIVED,
	STATUS_CLOSED,
	STATUS_CLOSING,
	STATUS_DRAFT,
	STATUS_OPEN,
	STATUS_SCHEDULED,
	EngagementService,
	EngagementSession,
	FlockEngagementError,
	InMemoryEngagementGateway,
	ParticipateRequest,
	SessionTicket,
	attendee_key,
	detect_suspect_pattern,
	issue_session_ticket,
	normalize_score,
	sign_ticket,
	status_flags_for,
	validate_kind,
	validate_session_window,
	validate_state_transition,
	verify_ticket,
)
from flock_os.events import (
	ENGAGEMENT_SESSION_CLOSED,
	ENGAGEMENT_SESSION_OPENED,
	EventBus,
	RecordingEventSink,
)
from flock_os.reporting import (
	BulkAttendanceService,
	InMemoryBulkAttendanceGateway,
)

# ---------------------------------------------------------------------------- #
# Fixtures + helpers
# ---------------------------------------------------------------------------- #
ORG = "org-1"
BRANCH = "branch-a"
GROUP = "group-1"
GATHERING = "GATH-001"
SECRET = "test-secret"
SESSION_ID = "ENG-00001"


def _session(
	*,
	session_id: str = SESSION_ID,
	status: str = STATUS_DRAFT,
	kind: str = "tap_burst",
	open_at: float | None = None,
	close_at: float | None = None,
	group: str | None = GROUP,
) -> EngagementSession:
	return EngagementSession(
		session_id=session_id,
		gathering=GATHERING,
		branch=BRANCH,
		organization=ORG,
		group=group,
		kind=kind,
		status=status,
		open_at=open_at,
		close_at=close_at,
	)


def _service(
	*,
	grace_seconds: int = DEFAULT_GRACE_SECONDS,
	throttle_per_second: int = 5,
	ticket_secret: str = SECRET,
	bulk_gateway: InMemoryBulkAttendanceGateway | None = None,
	bus: EventBus | None = None,
) -> tuple[EngagementService, InMemoryEngagementGateway]:
	"""Build a service wired with an in-memory gateway + in-memory bulk path.

	The bulk service is wired through a factory so the close path routes
	through the canonical :class:`BulkAttendanceService` (FLO-15) against a
	recording gateway — same path as production, no Frappe.
	"""
	if bus is not None:
		# Swap the module-level bus the service emits on so the test captures
		# every emitted event through ``bus._sink.published``.
		from flock_os import events as events_mod

		events_mod._bus = bus  # noqa: SLF001
	gw = InMemoryEngagementGateway(ticket_secret=ticket_secret)
	bulk = bulk_gateway or InMemoryBulkAttendanceGateway()

	class _Factory:
		def __call__(self, organization: str) -> BulkAttendanceService:  # noqa: ARG002
			return BulkAttendanceService(bulk)

	service = EngagementService(
		gw,
		bulk_service_factory=_Factory(),
		grace_seconds=grace_seconds,
		throttle_per_second=throttle_per_second,
	)
	return service, gw


def _open_service(
	*,
	now: float = 1_000_000.0,
	kind: str = "tap_burst",
	grace_seconds: int = DEFAULT_GRACE_SECONDS,
	throttle_per_second: int = 5,
	bulk_gateway: InMemoryBulkAttendanceGateway | None = None,
	bus: EventBus | None = None,
) -> tuple[EngagementService, InMemoryEngagementGateway, EngagementSession]:
	service, gw = _service(
		grace_seconds=grace_seconds,
		throttle_per_second=throttle_per_second,
		bulk_gateway=bulk_gateway,
		bus=bus,
	)
	session = _session(status=STATUS_DRAFT, kind=kind)
	service.create_session(session)
	service.open_session(session.session_id, now=now)
	# Mimic Frappe stamping open_at via set_session_status.
	gw.set_session_status(session.session_id, STATUS_OPEN, now=now)
	opened = gw.get_session(session.session_id)
	assert opened is not None
	assert opened.status == STATUS_OPEN
	return service, gw, opened


def _participate_request(
	*,
	ticket: SessionTicket,
	attendee_key_value: str | None = None,
	nonce: str = "n-1",
	member_id: str | None = None,
	device_fingerprint: str = "device-a",
	submitted_at: float | None = None,
	reaction_ms: float | None = None,
	ip_address: str | None = None,
	score: float | None = None,
	offline_replay: bool = False,
) -> ParticipateRequest:
	return ParticipateRequest(
		session_id=ticket.session_id,
		ticket=ticket,
		attendee_key=attendee_key_value or ticket.attendee_key,
		member_id=member_id,
		attendee_display_name="Alice",
		device_fingerprint=device_fingerprint,
		nonce=nonce,
		submitted_at=submitted_at,
		reaction_ms=reaction_ms,
		ip_address=ip_address,
		score=score,
		offline_replay=offline_replay,
	)


# ---------------------------------------------------------------------------- #
# attendee_key + score normalization (FLO-9 §5)
# ---------------------------------------------------------------------------- #
class TestAttendeeKey:
	def test_member_and_visitor_keys_are_stable(self):
		member_key = attendee_key(session_id="s1", member_id="mem-1", device_fingerprint="dev")
		visitor_key = attendee_key(session_id="s1", member_id=None, device_fingerprint="dev")
		# Same identity input → same key; different identity axis → different key.
		assert member_key == attendee_key(session_id="s1", member_id="mem-1", device_fingerprint="other-dev")
		assert visitor_key == attendee_key(session_id="s1", member_id=None, device_fingerprint="dev")
		assert member_key != visitor_key

	def test_session_id_is_bound_into_the_key(self):
		a = attendee_key(session_id="s1", member_id="mem-1", device_fingerprint="d")
		b = attendee_key(session_id="s2", member_id="mem-1", device_fingerprint="d")
		assert a != b

	def test_requires_some_identity(self):
		with pytest.raises(FlockEngagementError):
			attendee_key(session_id="s1", member_id=None, device_fingerprint="")
		with pytest.raises(FlockEngagementError):
			attendee_key(session_id="", member_id="m", device_fingerprint="d")


class TestNormalizeScore:
	@pytest.mark.parametrize("kind", list(eng.QUESTIONNAIRE_KINDS))
	def test_questionnaire_kinds_have_no_score(self, kind):
		assert normalize_score(kind, 99.0) is None
		assert normalize_score(kind, None) is None

	@pytest.mark.parametrize("kind", list(eng.GAME_KINDS))
	def test_game_kinds_clamp_to_0_100(self, kind):
		assert normalize_score(kind, 150.0) == 100.0
		assert normalize_score(kind, -5.0) == 0.0
		assert normalize_score(kind, 42.0) == 42.0
		assert normalize_score(kind, None) is None


# ---------------------------------------------------------------------------- #
# Session ticket signing (FLO-9 §6.4)
# ---------------------------------------------------------------------------- #
class TestSessionTicket:
	def test_sign_and_verify_roundtrip(self):
		# Issue in the future so the ticket is not expired at verify time.
		now = time.time()
		issued = now
		expires = now + DEFAULT_TICKET_TTL_SECONDS
		sig = sign_ticket(
			session_id="s1",
			attendee_key="ak-1",
			issued_at=issued,
			expires_at=expires,
			secret=SECRET,
		)
		ticket = SessionTicket(
			session_id="s1",
			attendee_key="ak-1",
			member_id=None,
			device_fingerprint="dev",
			issued_at=issued,
			expires_at=expires,
			signature=sig,
		)
		assert verify_ticket(ticket, secret=SECRET) is True

	def test_bad_secret_rejected(self):
		now = time.time()
		issued = now
		expires = now + DEFAULT_TICKET_TTL_SECONDS
		sig = sign_ticket(
			session_id="s1",
			attendee_key="ak-1",
			issued_at=issued,
			expires_at=expires,
			secret=SECRET,
		)
		ticket = SessionTicket(
			session_id="s1",
			attendee_key="ak-1",
			member_id=None,
			device_fingerprint="dev",
			issued_at=issued,
			expires_at=expires,
			signature=sig,
		)
		assert verify_ticket(ticket, secret="wrong-secret") is False

	def test_expired_ticket_rejected(self):
		now = time.time()
		issued = now - 2 * DEFAULT_TICKET_TTL_SECONDS
		expires = now - DEFAULT_TICKET_TTL_SECONDS  # already expired
		sig = sign_ticket(
			session_id="s1",
			attendee_key="ak-1",
			issued_at=issued,
			expires_at=expires,
			secret=SECRET,
		)
		ticket = SessionTicket(
			session_id="s1",
			attendee_key="ak-1",
			member_id=None,
			device_fingerprint="dev",
			issued_at=issued,
			expires_at=expires,
			signature=sig,
		)
		assert verify_ticket(ticket, secret=SECRET) is False

	def test_sign_requires_non_empty_secret(self):
		with pytest.raises(FlockEngagementError):
			sign_ticket(
				session_id="s1",
				attendee_key="ak-1",
				issued_at=1.0,
				expires_at=2.0,
				secret="",
			)


# ---------------------------------------------------------------------------- #
# State machine (FLO-9 §4)
# ---------------------------------------------------------------------------- #
class TestStateMachine:
	@pytest.mark.parametrize(
		"from_status,to_status,ok",
		[
			(STATUS_DRAFT, STATUS_SCHEDULED, True),
			(STATUS_DRAFT, STATUS_OPEN, True),
			(STATUS_SCHEDULED, STATUS_OPEN, True),
			(STATUS_SCHEDULED, STATUS_DRAFT, True),
			(STATUS_OPEN, STATUS_CLOSING, True),
			(STATUS_CLOSING, STATUS_CLOSED, True),
			(STATUS_CLOSED, STATUS_ARCHIVED, True),
			(STATUS_DRAFT, STATUS_CLOSED, False),
			(STATUS_OPEN, STATUS_ARCHIVED, False),
			(STATUS_CLOSED, STATUS_OPEN, False),
			(STATUS_ARCHIVED, STATUS_OPEN, False),
		],
	)
	def test_transitions(self, from_status, to_status, ok):
		if ok:
			validate_state_transition(from_status=from_status, to_status=to_status)
		else:
			with pytest.raises(FlockEngagementError):
				validate_state_transition(from_status=from_status, to_status=to_status)

	def test_kind_validation(self):
		validate_kind("tap_burst")
		validate_kind("poll")
		with pytest.raises(FlockEngagementError):
			validate_kind("unknown")


# ---------------------------------------------------------------------------- #
# Session window + anti-abuse (FLO-9 §6)
# ---------------------------------------------------------------------------- #
class TestSessionWindow:
	def test_in_window(self):
		session = _session(status=STATUS_OPEN, open_at=100.0, close_at=200.0)
		ok, reason = validate_session_window(session=session, submitted_at=150.0, grace_seconds=30)
		assert ok is True and reason is None

	def test_during_grace_window(self):
		session = _session(status=STATUS_CLOSED, open_at=100.0, close_at=200.0)
		ok, reason = validate_session_window(session=session, submitted_at=220.0, grace_seconds=30)
		assert ok is True and reason is None

	def test_after_grace_marks_out_of_scope(self):
		session = _session(status=STATUS_CLOSED, open_at=100.0, close_at=200.0)
		ok, reason = validate_session_window(session=session, submitted_at=300.0, grace_seconds=30)
		assert ok is False and reason == "after_grace"

	def test_before_open_marks_out_of_scope(self):
		session = _session(status=STATUS_OPEN, open_at=100.0, close_at=200.0)
		ok, reason = validate_session_window(session=session, submitted_at=50.0, grace_seconds=30)
		assert ok is False and reason == "before_open"


class TestSuspectHeuristic:
	def test_fast_reaction_flagged(self):
		# detect_suspect_pattern is a pure function of reaction_ms + same-ip count
		# (FLO-195 nit: the unused `participation` param was dropped).
		assert detect_suspect_pattern(reaction_ms=42) is True
		assert detect_suspect_pattern(reaction_ms=250) is False

	def test_many_attendees_from_one_ip_flagged(self):
		assert detect_suspect_pattern(same_ip_attendee_count=eng.SUSPECT_SAME_IP_ATTENDEES) is True
		assert detect_suspect_pattern(same_ip_attendee_count=2) is False


class TestStatusFlags:
	def test_in_window_clean(self):
		flags = status_flags_for(in_window=True)
		assert flags["out_of_scope"] is False
		assert flags["suspect_pattern"] is False
		assert flags["offline_replay"] is False
		assert flags["facilitator_override"] is False

	def test_out_of_window_carries_reason(self):
		flags = status_flags_for(in_window=False, out_of_scope_reason="after_grace")
		assert flags["out_of_scope"] is True
		assert flags["out_of_scope_reason"] == "after_grace"


# ---------------------------------------------------------------------------- #
# EngagementService — lifecycle + participate (FLO-9 §4 / §6)
# ---------------------------------------------------------------------------- #
class TestServiceLifecycle:
	def test_create_session_persists(self):
		service, gw = _service()
		session = _session()
		service.create_session(session)
		assert gw.get_session(SESSION_ID) is not None

	def test_create_rejects_unknown_kind(self):
		service, _ = _service()
		with pytest.raises(FlockEngagementError):
			service.create_session(
				EngagementSession(
					session_id=SESSION_ID,
					gathering=GATHERING,
					branch=BRANCH,
					organization=ORG,
					group=GROUP,
					kind="bogus",
				)
			)

	def test_open_requires_valid_transition(self):
		service, _ = _service()
		service.create_session(_session(status=STATUS_DRAFT))
		# Force the gateway into a closed state directly; open should refuse.
		service.gateway.set_session_status(SESSION_ID, STATUS_CLOSED, now=1.0)
		with pytest.raises(FlockEngagementError):
			service.open_session(SESSION_ID)

	def test_open_emits_engagement_opened(self):
		bus = EventBus(sink=RecordingEventSink())
		service, _, _ = _open_service(bus=bus)
		opened = [e for e in bus._sink.published if e[0].name == ENGAGEMENT_SESSION_OPENED]  # noqa: SLF001
		assert len(opened) == 1
		event = opened[0][0]
		assert event.payload["session"] == SESSION_ID
		assert event.payload["gathering"] == GATHERING
		assert event.payload["branch"] == BRANCH
		assert event.payload["organization"] == ORG
		assert event.payload["group"] == GROUP
		assert event.scope["branch"] == BRANCH


class TestServiceParticipate:
	def test_join_then_participate_records_credit(self):
		service, gw, session = _open_service(now=1_000_000.0)
		ticket = service.join(session_id=SESSION_ID, member_id="mem-1", device_fingerprint="dev-1")
		req = _participate_request(
			ticket=ticket, attendee_key_value=ticket.attendee_key, submitted_at=1_000_010.0
		)
		receipt = service.participate(req)
		assert receipt.accepted is True
		assert receipt.status_flags["out_of_scope"] is False
		assert service.gateway.has_attendee(SESSION_ID, ticket.attendee_key) is True

	def test_participate_rejects_bad_ticket(self):
		service, gw, session = _open_service()
		# Issue a ticket under a different secret → bad signature for our service.
		ticket = issue_session_ticket(
			session=session,
			member_id="mem-2",
			device_fingerprint="dev",
			secret="wrong",
		)
		req = _participate_request(ticket=ticket, attendee_key_value=ticket.attendee_key)
		receipt = service.participate(req)
		assert receipt.accepted is False
		assert receipt.reason == "invalid_ticket"

	def test_participate_rejects_attendee_key_mismatch(self):
		service, gw, session = _open_service()
		ticket = service.join(session_id=SESSION_ID, member_id="mem-3", device_fingerprint="dev")
		# Submit under a different attendee_key than the ticket binds.
		req = _participate_request(ticket=ticket, attendee_key_value="someone-else")
		receipt = service.participate(req)
		assert receipt.accepted is False
		assert receipt.reason == "attendee_key_mismatch"

	def test_participate_idempotent_on_nonce(self):
		service, gw, session = _open_service()
		ticket = service.join(session_id=SESSION_ID, member_id="mem-4", device_fingerprint="dev")
		req = _participate_request(ticket=ticket, attendee_key_value=ticket.attendee_key, nonce="dup-1")
		first = service.participate(req)
		second = service.participate(req)
		assert first.accepted is True
		assert second.accepted is False
		assert second.reason == "duplicate_nonce"

	def test_participate_throttles_burst(self):
		service, gw, session = _open_service(throttle_per_second=2)
		ticket = service.join(session_id=SESSION_ID, member_id="mem-5", device_fingerprint="dev")
		results = []
		for i in range(5):
			req = _participate_request(
				ticket=ticket,
				attendee_key_value=ticket.attendee_key,
				nonce=f"n-{i}",
				submitted_at=1_000_000.0,  # same instant → all in one 1s window
			)
			results.append(service.participate(req))
		accepted = [r for r in results if r.accepted]
		throttled = [r for r in results if r.reason == "throttled"]
		# First 2 within the per-second cap accepted; the rest throttled.
		assert len(accepted) == 2
		assert len(throttled) == 3

	def test_out_of_window_participation_recorded_but_not_credited(self):
		# Open at t=1000; close at t=2000; grace=30; submit at t=5000.
		service, gw, session = _open_service(now=1_000.0, grace_seconds=30)
		service.gateway.set_session_status(SESSION_ID, STATUS_CLOSED, now=2_000.0)
		# Re-open for the participate path (the validator only needs the ticket
		# to be live at issue time; the closed status is what marks out_of_scope).
		# Simulate the closed-window participate flow by re-issuing via open then
		# closing before participate.
		service.gateway.set_session_status(SESSION_ID, STATUS_OPEN, now=1_000.0)
		ticket = service.join(session_id=SESSION_ID, member_id="mem-6", device_fingerprint="dev")
		service.gateway.set_session_status(SESSION_ID, STATUS_CLOSED, now=2_000.0)
		# Participate against the now-closed session → session_not_live.
		req = _participate_request(
			ticket=ticket, attendee_key_value=ticket.attendee_key, submitted_at=5_000.0
		)
		receipt = service.participate(req)
		assert receipt.accepted is False
		assert receipt.reason == "session_not_live"

	def test_suspect_reaction_time_flagged_but_credited(self):
		service, gw, session = _open_service()
		ticket = service.join(session_id=SESSION_ID, member_id="mem-7", device_fingerprint="dev")
		req = _participate_request(
			ticket=ticket,
			attendee_key_value=ticket.attendee_key,
			reaction_ms=42.0,  # sub-100ms → suspect
		)
		receipt = service.participate(req)
		# Non-blocking: still credited.
		assert receipt.accepted is True
		assert receipt.status_flags["suspect_pattern"] is True


class TestServiceBulkParticipate:
	def test_bulk_participate_is_idempotent_per_nonce(self):
		service, gw, session = _open_service()
		ticket = service.join(session_id=SESSION_ID, member_id="mem-b", device_fingerprint="dev")
		base_req = _participate_request(ticket=ticket, attendee_key_value=ticket.attendee_key, nonce="bulk-1")
		# First batch — accepted.
		receipts_a = service.bulk_participate([base_req])
		assert receipts_a[0].accepted is True
		# Replay same batch — duplicate_nonce.
		receipts_b = service.bulk_participate([base_req])
		assert receipts_b[0].accepted is False
		assert receipts_b[0].reason == "duplicate_nonce"

	def test_bulk_participate_processes_each_independently(self):
		service, gw, session = _open_service(throttle_per_second=100)
		ticket = service.join(session_id=SESSION_ID, member_id="mem-c", device_fingerprint="dev")
		reqs = [
			_participate_request(ticket=ticket, attendee_key_value=ticket.attendee_key, nonce=f"b-{i}")
			for i in range(3)
		]
		# Add one with a bad attendee_key to confirm it does not abort the batch.
		reqs.append(_participate_request(ticket=ticket, attendee_key_value="other"))
		receipts = service.bulk_participate(reqs)
		assert sum(1 for r in receipts if r.accepted) == 3
		assert any(r.reason == "attendee_key_mismatch" for r in receipts)


# ---------------------------------------------------------------------------- #
# Close path — projection + bulk-recorded + engagement.closed (FLO-9 §4/§5)
# ---------------------------------------------------------------------------- #
class TestClosePath:
	def test_close_projects_in_window_participations_to_bulk(self):
		bus = EventBus(sink=RecordingEventSink())
		bulk_gw = InMemoryBulkAttendanceGateway()
		service, gw, session = _open_service(now=1_000.0, bus=bus, bulk_gateway=bulk_gw)
		# Three players join + participate.
		for i in range(3):
			t = service.join(session_id=SESSION_ID, member_id=f"mem-{i}", device_fingerprint=f"dev-{i}")
			service.participate(
				_participate_request(
					ticket=t,
					attendee_key_value=t.attendee_key,
					nonce=f"n-{i}",
					submitted_at=1_010.0,
				)
			)
		outcome = service.close_session(SESSION_ID, now=2_000.0)
		# close_session returns a *preview* (closing, no bulk projection yet).
		assert outcome.attendee_count == 3
		assert outcome.inserted == 0
		assert outcome.finalized is False
		# engagement.closed is deferred to finalize — not emitted at close.
		assert not [e for e in bus._sink.published if e[0].name == ENGAGEMENT_SESSION_CLOSED]
		# The deferred finalize was scheduled on the in-memory gateway.
		assert gw.pending_finalizes == [(SESSION_ID, service.grace_seconds)]
		# Drain the deferred finalize (RQ in prod; explicit in tests).
		outcome = service.finalize_close(SESSION_ID)
		assert outcome.attendee_count == 3
		# Inserted via the bulk service (FLO-15): one row per attendee.
		assert outcome.inserted == 3
		assert outcome.deduplicated == 0
		assert outcome.finalized is True
		# Engagement emitted exactly one flock.engagement.closed at finalize.
		closed = [e for e in bus._sink.published if e[0].name == ENGAGEMENT_SESSION_CLOSED]  # noqa: SLF001
		assert len(closed) == 1
		assert closed[0][0].payload["count"] == 3
		# The bulk service emitted exactly one flock.attendance.bulk_recorded
		# through the in-memory bulk gateway (FLO-15 single-emission contract).
		bulk_recorded = [e for e in bulk_gw.published_events if e.name == "flock.attendance.bulk_recorded"]
		assert len(bulk_recorded) == 1
		assert bulk_recorded[0].payload["count"] == 3
		assert bulk_recorded[0].payload["branch"] == BRANCH

	def test_close_excludes_out_of_scope_participations(self):
		bus = EventBus(sink=RecordingEventSink())
		bulk_gw = InMemoryBulkAttendanceGateway()
		# grace=30 — anything past close+30 is out_of_scope.
		service, gw, session = _open_service(now=1_000.0, grace_seconds=30, bus=bus, bulk_gateway=bulk_gw)
		# One in-window participant.
		t_in = service.join(session_id=SESSION_ID, member_id="mem-in", device_fingerprint="dev-in")
		# Close the session so the next participate is out_of_scope.
		# We use a direct participate while open, then verify close excludes
		# any out-of-scope rows by manually appending one through the gateway.
		service.participate(
			_participate_request(
				ticket=t_in, attendee_key_value=t_in.attendee_key, nonce="in-1", submitted_at=1_010.0
			)
		)
		# Manually inject an out_of_scope participation for a second attendee
		# through the gateway (the validator would refuse a closed session).
		from flock_os.engagement import Participation

		out_of_scope = Participation(
			session_id=SESSION_ID,
			attendee_key="ak-out",
			member_id="mem-out",
			attendee_display_name="Out",
			device_fingerprint="dev-out",
			role="member",
			engagement_type=eng.ENGAGEMENT_TYPE_GAME,
			engagement_kind="tap_burst",
			score=None,
			submitted_at=5_000.0,
			client_submitted_at=None,
			branch=BRANCH,
			organization=ORG,
			group=GROUP,
			geo_region=None,
			nonce="out-1",
			ip_address=None,
			status_flags=status_flags_for(in_window=False, out_of_scope_reason="after_grace"),
			feedback={},
		)
		gw.record_participation(out_of_scope)
		service.close_session(SESSION_ID, now=2_000.0)
		outcome = service.finalize_close(SESSION_ID)
		# Only the in-window attendee is projected.
		assert outcome.attendee_count == 1
		assert outcome.inserted == 1

	def test_close_dedupes_one_attendee_many_participations(self):
		bus = EventBus(sink=RecordingEventSink())
		bulk_gw = InMemoryBulkAttendanceGateway()
		service, gw, session = _open_service(now=1_000.0, bus=bus, bulk_gateway=bulk_gw)
		t = service.join(session_id=SESSION_ID, member_id="mem-d", device_fingerprint="dev")
		# Same attendee, three distinct nonces (different rounds).
		for i in range(3):
			service.participate(
				_participate_request(
					ticket=t,
					attendee_key_value=t.attendee_key,
					nonce=f"r-{i}",
					submitted_at=1_010.0 + i,
				)
			)
		service.close_session(SESSION_ID, now=2_000.0)
		outcome = service.finalize_close(SESSION_ID)
		# One attendance row per attendee_key, not per round.
		assert outcome.attendee_count == 1
		assert outcome.inserted == 1

	def test_close_replay_is_idempotent(self):
		"""Re-closing is a state-machine error (closing → closing is illegal).

		Idempotency on close itself is enforced by the closed-state guard +
		by the bulk service's ``(event, attendee_ref, client_req_id)`` index.
		The deferred finalize is separately idempotent (an RQ retry on an
		already-closed session is a preview no-op, no re-emit).
		"""
		bus = EventBus(sink=RecordingEventSink())
		service, gw, session = _open_service(bus=bus)
		t = service.join(session_id=SESSION_ID, member_id="mem-e", device_fingerprint="dev")
		service.participate(_participate_request(ticket=t, attendee_key_value=t.attendee_key, nonce="n"))
		service.close_session(SESSION_ID, now=2_000.0)
		# A second close attempt is rejected by the state machine (closing→closing).
		with pytest.raises(FlockEngagementError):
			service.close_session(SESSION_ID, now=3_000.0)
		# The deferred finalize projects + emits once.
		service.finalize_close(SESSION_ID)
		closed = [e for e in bus._sink.published if e[0].name == ENGAGEMENT_SESSION_CLOSED]  # noqa: SLF001
		assert len(closed) == 1
		# An RQ retry on the now-closed session is an idempotent no-op: no
		# re-projection, no re-emit (single-emission contract).
		replay = service.finalize_close(SESSION_ID)
		assert replay.finalized is True
		assert replay.inserted == 0  # preview — no re-projection
		assert len([e for e in bus._sink.published if e[0].name == ENGAGEMENT_SESSION_CLOSED]) == 1

	def test_close_routes_through_canonical_bulk_service(self):
		"""The close path's AttendanceItem projection rides the FLO-15 service.

		This pins DRY: engagement does NOT re-implement the bulk write or the
		per-attendee event fan-out. The single ``flock.attendance.bulk_recorded``
		comes from :class:`BulkAttendanceService.submit`.
		"""
		bus = EventBus(sink=RecordingEventSink())
		bulk_gw = InMemoryBulkAttendanceGateway()
		service, gw, session = _open_service(now=1_000.0, bus=bus, bulk_gateway=bulk_gw)
		t = service.join(session_id=SESSION_ID, member_id="mem-f", device_fingerprint="dev")
		service.participate(_participate_request(ticket=t, attendee_key_value=t.attendee_key, nonce="n"))
		service.close_session(SESSION_ID, now=2_000.0)
		service.finalize_close(SESSION_ID)
		# The bulk gateway captured the projected items.
		assert len(bulk_gw._seen) == 1  # noqa: SLF001
		# Source = engagement, client_req_id = attendee_key (the FLO-11 contract).
		keys = list(bulk_gw._seen)  # noqa: SLF001
		_event, _ref, client_req_id = keys[0]
		# The attendee_key is a sha256 hex digest; the bulk path stored it as
		# the per-item idempotency key.
		assert len(client_req_id) == 64

	def test_close_stamps_engagement_provenance_on_bulk_items(self):
		"""The close path stamps the ADR §9 / FLO-11 §5 provenance columns.

		Pins the P1 dedup contract (FLO-195): every engagement-sourced
		AttendanceItem reaching the bulk gateway carries ``gathering``,
		``member``, ``engagement_session`` and ``attendee_key`` so the
		``UNIQUE (branch, gathering, member)`` cross-source index and the
		``UNIQUE (engagement_session, attendee_key)`` per-session backstop
		actually fire. Without this the gathering headcount misses engagement
		entirely and manual+engagement credits double-count.
		"""
		bus = EventBus(sink=RecordingEventSink())
		bulk_gw = InMemoryBulkAttendanceGateway()
		service, gw, session = _open_service(now=1_000.0, bus=bus, bulk_gateway=bulk_gw)
		# One member + one visitor join + participate.
		t_mem = service.join(session_id=SESSION_ID, member_id="mem-prov", device_fingerprint="dev-m")
		service.participate(
			_participate_request(
				ticket=t_mem, attendee_key_value=t_mem.attendee_key, nonce="n-m", member_id="mem-prov"
			)
		)
		t_vis = service.join(session_id=SESSION_ID, member_id=None, device_fingerprint="dev-v")
		service.participate(
			_participate_request(ticket=t_vis, attendee_key_value=t_vis.attendee_key, nonce="n-v")
		)
		service.close_session(SESSION_ID, now=2_000.0)
		service.finalize_close(SESSION_ID)

		items = bulk_gw.inserted_items  # noqa: SLF001
		assert len(items) == 2
		by_ref = {it.attendee_ref: it for it in items}

		# Member row: gathering + member + engagement_session + attendee_key all stamped.
		mem_item = by_ref["mem-prov"]
		assert mem_item.event == GATHERING  # gathering is the grouping axis (not session_id)
		assert mem_item.gathering == GATHERING
		assert mem_item.member == "mem-prov"
		assert mem_item.engagement_session == SESSION_ID
		assert mem_item.attendee_key == t_mem.attendee_key
		assert mem_item.source == "engagement"
		assert mem_item.client_req_id == t_mem.attendee_key

		# Visitor row: member is None (rides the (engagement_session, attendee_key) index).
		vis_item = by_ref[t_vis.attendee_key]
		assert vis_item.gathering == GATHERING
		assert vis_item.member is None
		assert vis_item.engagement_session == SESSION_ID
		assert vis_item.attendee_key == t_vis.attendee_key

	def test_close_gathering_is_the_bulk_recorded_axis(self):
		"""``flock.attendance.bulk_recorded`` is keyed on the gathering, not the session.

		The ``Event Attendance Summary`` rollup therefore counts engagement-
		sourced rows against the gathering — playing a game counts as attending
		the gathering (the FLO-11 §5 purpose). Pre-P1 this was keyed on the
		opaque session id, so ``aggregate(branch, gathering)`` returned 0 from
		engagement.
		"""
		bus = EventBus(sink=RecordingEventSink())
		bulk_gw = InMemoryBulkAttendanceGateway()
		service, gw, session = _open_service(now=1_000.0, bus=bus, bulk_gateway=bulk_gw)
		t = service.join(session_id=SESSION_ID, member_id="mem-axis", device_fingerprint="dev")
		service.participate(_participate_request(ticket=t, attendee_key_value=t.attendee_key, nonce="n"))
		service.close_session(SESSION_ID, now=2_000.0)
		service.finalize_close(SESSION_ID)

		from flock_os.reporting import EVENT_BULK_RECORDED, AttendanceScope

		bulk_recorded = [e for e in bulk_gw.published_events if e.name == EVENT_BULK_RECORDED]
		assert len(bulk_recorded) == 1
		# The bulk_recorded payload's gathering key = the actual gathering, not the session.
		assert bulk_recorded[0].payload["gathering"] == GATHERING
		# The maintained aggregate reads the gathering, not the session.
		scope = AttendanceScope(branch=BRANCH)
		from flock_os.reporting import BulkAttendanceService

		BulkAttendanceService(bulk_gw).aggregate(scope, event=GATHERING)  # no raise
		assert bulk_gw.aggregate(scope, GATHERING) == 1
		# And NOT keyed on the session id.
		assert bulk_gw.aggregate(scope, SESSION_ID) == 0

	def test_offline_reconnect_flush_within_grace_is_credited(self):
		"""A device reconnecting after close + flushing within grace is credited.

		FLO-198 core acceptance: ``close_session`` stamps ``close_at`` and holds
		``closing`` for ``grace_seconds``; ``participate`` during ``closing``
		applies the window check normally (``submitted_at ≤ close_at + grace`` →
		in-window), so the offline flush is credited at finalize. A flush
		*after* grace is recorded (audit) but excluded from headcount.
		"""
		bus = EventBus(sink=RecordingEventSink())
		bulk_gw = InMemoryBulkAttendanceGateway()
		service, gw, session = _open_service(now=1_000.0, grace_seconds=30, bus=bus, bulk_gateway=bulk_gw)
		# One player participates while open.
		t_live = service.join(session_id=SESSION_ID, member_id="mem-live", device_fingerprint="dev-live")
		service.participate(
			_participate_request(
				ticket=t_live,
				attendee_key_value=t_live.attendee_key,
				nonce="n-live",
				submitted_at=1_010.0,
			)
		)
		# Facilitator closes → closing (stamp close_at=2000, grace=30).
		preview = service.close_session(SESSION_ID, now=2_000.0)
		assert preview.finalized is False
		assert gw.get_session(SESSION_ID).status == STATUS_CLOSING

		# A device reconnects + flushes its offline queue at t=2020 (in grace).
		t_off = service.join(session_id=SESSION_ID, member_id="mem-off", device_fingerprint="dev-off")
		receipt = service.participate(
			_participate_request(
				ticket=t_off,
				attendee_key_value=t_off.attendee_key,
				nonce="n-off",
				submitted_at=2_020.0,
				offline_replay=True,
			)
		)
		assert receipt.accepted is True
		assert receipt.status_flags["out_of_scope"] is False
		assert receipt.status_flags["offline_replay"] is True

		# A flush *after* grace (t=2100 > 2000+30) is recorded but out-of-scope.
		t_late = service.join(session_id=SESSION_ID, member_id="mem-late", device_fingerprint="dev-late")
		late = service.participate(
			_participate_request(
				ticket=t_late,
				attendee_key_value=t_late.attendee_key,
				nonce="n-late",
				submitted_at=2_100.0,
				offline_replay=True,
			)
		)
		assert late.accepted is True  # recorded for audit
		assert late.status_flags["out_of_scope"] is True
		assert late.status_flags["out_of_scope_reason"] == "after_grace"

		# Finalize credits live + in-grace offline attendee (2); late excluded.
		final = service.finalize_close(SESSION_ID)
		assert final.attendee_count == 2
		assert final.inserted == 2
		assert final.finalized is True

	def test_engagement_closed_emits_once_at_finalize_with_post_grace_count(self):
		"""``flock.engagement.closed`` emits once, at finalize, post-grace count.

		FLO-198 acceptance: the event is not emitted at close time (deferred to
		finalize) and carries the post-grace attendee count, not the close-time
		preview count.
		"""
		bus = EventBus(sink=RecordingEventSink())
		bulk_gw = InMemoryBulkAttendanceGateway()
		service, gw, session = _open_service(now=1_000.0, grace_seconds=30, bus=bus, bulk_gateway=bulk_gw)
		t1 = service.join(session_id=SESSION_ID, member_id="mem-a", device_fingerprint="dev-a")
		service.participate(
			_participate_request(
				ticket=t1, attendee_key_value=t1.attendee_key, nonce="n-a", submitted_at=1_010.0
			)
		)
		service.close_session(SESSION_ID, now=2_000.0)
		# No closed event at close time (deferred to finalize).
		assert not [e for e in bus._sink.published if e[0].name == ENGAGEMENT_SESSION_CLOSED]

		# A second attendee flushes within grace, *after* close.
		t2 = service.join(session_id=SESSION_ID, member_id="mem-b", device_fingerprint="dev-b")
		service.participate(
			_participate_request(
				ticket=t2, attendee_key_value=t2.attendee_key, nonce="n-b", submitted_at=2_005.0
			)
		)

		# Finalize emits once with the post-grace count (2).
		service.finalize_close(SESSION_ID)
		closed = [e for e in bus._sink.published if e[0].name == ENGAGEMENT_SESSION_CLOSED]  # noqa: SLF001
		assert len(closed) == 1
		assert closed[0][0].payload["count"] == 2


# ---------------------------------------------------------------------------- #
# EngagementService.join — must be live
# ---------------------------------------------------------------------------- #
class TestServiceJoin:
	def test_join_requires_live_session(self):
		service, _ = _service()
		service.create_session(_session(status=STATUS_DRAFT))
		with pytest.raises(FlockEngagementError):
			service.join(session_id=SESSION_ID, member_id="mem", device_fingerprint="dev")

	def test_join_returns_signed_ticket(self):
		service, _, _ = _open_service()
		ticket = service.join(session_id=SESSION_ID, member_id="mem-g", device_fingerprint="dev")
		assert ticket.session_id == SESSION_ID
		assert verify_ticket(ticket, secret=SECRET) is True


# ---------------------------------------------------------------------------- #
# DocType schema contract — the engagement DocType set + scoping (FLO-9 §13).
# ---------------------------------------------------------------------------- #
DOCTYPE_DIR = __import__("pathlib").Path(__file__).resolve().parent.parent / "flock_os" / "doctype"

ENGAGEMENT_DOCTYPES = (
	"Flock Engagement Session",
	"Flock Engagement Round",
	"Flock Engagement Feedback",
	"Flock Engagement Game Template",
	"Flock Engagement Questionnaire Template",
)


def _load_doctype(name: str) -> dict:
	path = DOCTYPE_DIR / name.lower().replace(" ", "_") / f"{name.lower().replace(' ', '_')}.json"
	assert path.exists(), f"Missing engagement DocType JSON: {name}"
	with path.open() as f:
		return json.load(f)


def _field(doc: dict, fieldname: str) -> dict:
	matches = [f for f in doc["fields"] if f["fieldname"] == fieldname]
	assert matches, f"field {fieldname!r} missing on {doc['name']}"
	return matches[0]


class TestEngagementDoctypeSchema:
	@pytest.mark.parametrize("name", ENGAGEMENT_DOCTYPES)
	def test_doctypes_use_flock_prefix_and_module(self, name):
		doc = _load_doctype(name)
		assert doc["doctype"] == "DocType"
		assert doc["name"] == name
		assert name.startswith("Flock ")
		assert doc["module"] == "flock_os"

	@pytest.mark.parametrize("name", ENGAGEMENT_DOCTYPES)
	def test_doctypes_define_role_permissions(self, name):
		doc = _load_doctype(name)
		roles = {p["role"] for p in doc["permissions"]}
		assert "System Manager" in roles
		assert "Flock Org Admin" in roles
		assert "Flock Branch Admin" in roles

	def test_session_carries_full_scoping_contract(self):
		doc = _load_doctype("Flock Engagement Session")
		for fld in ("branch", "group", "organization"):
			field = _field(doc, fld)
			assert field["options"] in (
				"Flock Branch",
				"Flock Group",
				"Flock Organization",
			)
		# branch is required + indexed (the primary row-level anchor).
		assert _field(doc, "branch").get("reqd") == 1
		assert _field(doc, "branch").get("search_index") == 1
		# organization is required + indexed (tenant floor).
		assert _field(doc, "organization").get("reqd") == 1
		assert _field(doc, "organization").get("search_index") == 1
		# status has the canonical lifecycle options.
		status = _field(doc, "status")
		assert status["fieldtype"] == "Select"
		assert "open" in status["options"].split("\n")
		assert "closing" in status["options"].split("\n")
		assert "closed" in status["options"].split("\n")

	def test_session_kind_options_match_catalog(self):
		doc = _load_doctype("Flock Engagement Session")
		kind = _field(doc, "kind")
		options = set(kind["options"].split("\n"))
		assert options == eng.ALL_KINDS

	def test_session_registered_in_scoped_doctypes(self):
		from flock_os.permissions import MEMBER_ANCHORED_DOCTYPES, SCOPED_DOCTYPES

		assert "Flock Engagement Session" in SCOPED_DOCTYPES
		assert "Flock Attendance Record" in SCOPED_DOCTYPES
		# Attendance Record carries member → self-membership applies.
		assert "Flock Attendance Record" in MEMBER_ANCHORED_DOCTYPES

	def test_attendance_record_extended_with_engagement_fields(self):
		doc = _load_doctype("Flock Attendance Record")
		for fld in (
			"engagement_session",
			"gathering",
			"member",
			"attendee_key",
			"engagement_type",
			"engagement_kind",
			"score",
			"submitted_at",
			"status_flags",
			"organization",
			"group",
		):
			_field(doc, fld)
		# The unique-idempotency axis fields are indexed.
		assert _field(doc, "engagement_session").get("search_index") == 1
		assert _field(doc, "attendee_key").get("search_index") == 1
		assert _field(doc, "member").get("search_index") == 1

	def test_feedback_child_links_attendance_and_round(self):
		doc = _load_doctype("Flock Engagement Feedback")
		assert _field(doc, "attendance_record")["options"] == "Flock Attendance Record"
		assert _field(doc, "round")["options"] == "Flock Engagement Round"
		# feedback_kind covers the questionnaire response shapes.
		kind = _field(doc, "feedback_kind")
		assert set(kind["options"].split("\n")) == {
			"poll_choice",
			"word_cloud_term",
			"qa_question",
			"qa_upvote",
			"slider_value",
		}

	def test_round_child_links_session(self):
		doc = _load_doctype("Flock Engagement Round")
		assert _field(doc, "session")["options"] == "Flock Engagement Session"
		assert _field(doc, "calm_equivalent")["fieldtype"] == "Check"


# ---------------------------------------------------------------------------- #
# Engagement-index patch contract — the composite UNIQUE / reporting indexes.
# ---------------------------------------------------------------------------- #
class TestEngagementIndexPatch:
	def test_index_contract_matches_design(self):
		from flock_os.patches.v0_1.add_engagement_indexes import INDEXES

		by_name = {name: (cols, unique) for _dt, name, cols, unique in INDEXES}
		# Per-session idempotency backstop.
		assert by_name["unique_engagement_session_attendee_key"] == (
			"(`engagement_session`, `attendee_key`)",
			True,
		)
		# ADR §9 cross-source dedup.
		assert by_name["unique_branch_gathering_member"] == (
			"(`branch`, `gathering`, `member`)",
			True,
		)
		# Branch-leading reporting index (ADR §8).
		assert by_name["idx_branch_org_gathering_submitted"] == (
			"(`branch`, `organization`, `gathering`, `submitted_at`)",
			False,
		)

	def test_all_indexes_target_attendance_record(self):
		from flock_os.patches.v0_1.add_engagement_indexes import INDEXES

		for doctype, _name, _cols, _unique in INDEXES:
			assert doctype == "Flock Attendance Record"


# ---------------------------------------------------------------------------- #
# Event catalog: the engagement events are 3-segment (design rev 4 / ADR §5.3).
# ---------------------------------------------------------------------------- #
class TestEngagementEventNames:
	def test_engagement_events_use_three_segment_form(self):
		assert ENGAGEMENT_SESSION_OPENED == "flock.engagement.opened"
		assert ENGAGEMENT_SESSION_CLOSED == "flock.engagement.closed"
