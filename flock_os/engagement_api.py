"""
Transport layer for the Fun Attendance engagement runtime (FLO-11).

Exposes the FLO-9 engagement runtime as Frappe whitelisted REST endpoints +
the matching Frappe adapter (Redis hot counters + MariaDB participation log +
:eventive emission). **No domain rules live here** — this module only parses
input, resolves the caller's row-level scope via the central
:mod:`flock_os.permissions` guards, and delegates persistence to
:class:`flock_os.engagement.EngagementService`.

REST contract (FLO-9 §11):

    POST /api/method/flock_os.engagement_api.create_session
    POST /api/method/flock_os.engagement_api.open_session
    POST /api/method/flock_os.engagement_api.close_session
    POST /api/method/flock_os.engagement_api.join_session
    POST /api/method/flock_os.engagement_api.participate
    GET  /api/method/flock_os.engagement_api.session_state
    POST /api/method/flock_os.engagement_api.bulk_attendance

All routes are ``@frappe.whitelist()`` with role + row-level perms anchored on
the two-axis scoping contract (``branch`` native UP + ``group`` nested-set hook
+ ``organization`` tenant floor, ADR-0001 §6) via the central
:mod:`flock_os.permissions` guards. The realtime fan-out is owned by the
:class:`flock_os.realtime` projector (FLO-14) which subscribes to the emitted
``flock.engagement.opened`` / ``flock.engagement.closed`` events — the transport
never publishes realtime directly (single-publisher rule, ADR-0001 §5.1).
"""

from __future__ import annotations

from typing import Any

import frappe

from flock_os import engagement as engagement_mod
from flock_os.engagement import (
	DEFAULT_GRACE_SECONDS,
	STATUS_CLOSED,
	STATUS_CLOSING,
	STATUS_DRAFT,
	STATUS_OPEN,
	STATUS_SCHEDULED,
	EngagementService,
	EngagementSession,
	FlockEngagementError,
	ParticipateRequest,
	SessionTicket,
)
from flock_os.engagement_frappe import FrappeEngagementGateway
from flock_os.permissions import (
	FlockPermissionError,
	assert_branch_scope,
)
from flock_os.permissions import (
	get_gateway as get_permission_gateway,
)

# ---------------------------------------------------------------------------- #
# Service accessor — process-wide, with a Frappe-backed gateway + the canonical
# bulk-reporting service factory (FLO-15) wired in for the close path.
# ---------------------------------------------------------------------------- #
_service: EngagementService | None = None


def get_service() -> EngagementService:
	"""The process-wide engagement service, wired with the Frappe gateway."""
	global _service
	if _service is None:
		_service = EngagementService(FrappeEngagementGateway())
	return _service


def install_service(service: EngagementService) -> EngagementService:
	"""Install a custom service (tests) and return it."""
	global _service
	_service = service
	return _service


# ---------------------------------------------------------------------------- #
# REST endpoints — facilitator + player workflow.
# ---------------------------------------------------------------------------- #
@frappe.whitelist()
def create_session(
	*,
	gathering: str,
	title: str,
	engagement_type: str,
	kind: str,
	branch: str | None = None,
	group: str | None = None,
	organization: str | None = None,
	facilitator: str | None = None,
	config: dict[str, Any] | None = None,
	geofence: dict[str, Any] | None = None,
	grace_seconds: int = DEFAULT_GRACE_SECONDS,
	scheduled_at: str | None = None,
) -> dict[str, Any]:
	"""Create a new engagement session in ``draft``/``scheduled`` (FLO-9 §2/§4).

	The caller must carry row-level scope over the resolved ``(branch, group,
	organization)`` triple (ADR-0001 §6). The gathering supplies the default
	branch/group/organization when the caller omits them.
	"""
	gathering_doc = frappe.get_doc("Flock Gathering", gathering)
	resolved_branch = branch or gathering_doc.branch
	resolved_group = group or gathering_doc.group
	resolved_org = organization or gathering_doc.organization
	if not resolved_branch or not resolved_org:
		frappe.throw("branch and organization are required (pass explicitly or via gathering)")

	# Row-level scope assertion (ADR §6.5) — facilitator must own this gathering's scope.
	_gw = get_permission_gateway()
	assert_branch_scope(doc_branch=resolved_branch, user=frappe.session.user, gateway=_gw)

	status = STATUS_SCHEDULED if scheduled_at else STATUS_DRAFT
	session_id = _next_session_id()
	session = EngagementSession(
		session_id=session_id,
		gathering=gathering,
		branch=resolved_branch,
		organization=resolved_org,
		group=resolved_group,
		kind=kind,
		status=status,
		facilitator=facilitator,
		config=dict(config or {}),
		geofence=geofence,
	)
	try:
		get_service().create_session(session)
	except FlockEngagementError as exc:
		frappe.throw(str(exc), title="Invalid engagement session")
	# Persist a row on the Flock Engagement Session DocType so list views / perms
	# / the realtime room can resolve it. Best-effort metadata only — the runtime
	# state lives in the engagement service.
	_persist_session_doc(
		session,
		title=title,
		engagement_type=engagement_type,
		grace_seconds=grace_seconds,
		scheduled_at=scheduled_at,
	)
	return {"session_id": session_id, "status": status}


@frappe.whitelist()
def open_session(*, session_id: str) -> dict[str, Any]:
	"""Transition a session to ``open`` (FLO-9 §4). Facilitator-only."""
	_assert_facilitator(session_id)
	try:
		get_service().open_session(session_id)
	except FlockEngagementError as exc:
		frappe.throw(str(exc), title="Cannot open session")
	_update_session_doc(session_id, {"status": STATUS_OPEN})
	return {"session_id": session_id, "status": STATUS_OPEN}


@frappe.whitelist()
def close_session(*, session_id: str) -> dict[str, Any]:
	"""Transition a session to ``closing`` + schedule deferred finalize (FLO-198).

	Stamps ``close_at`` and schedules ``finalize_close`` (RQ) after the
	session's ``grace_seconds`` so a reconnecting device that flushes its
	offline queue inside ``[close_at, close_at + grace]`` is credited. Returns a
	**preview** receipt: ``status=closing`` + the in-window attendee count at
	close time; ``inserted``/``deduplicated`` are zeroed (the bulk projection
	runs at finalize). The final counts + the single ``flock.engagement.closed``
	emission land when :func:`finalize_close` fires.

	The facilitator UI should drive a state refresh on ``finalize_close`` (RQ)
	rather than treating this response as final — see FLO-198.
	"""
	_assert_facilitator(session_id)
	try:
		outcome = get_service().close_session(session_id)
	except FlockEngagementError as exc:
		frappe.throw(str(exc), title="Cannot close session")
	_update_session_doc(
		session_id,
		{
			"status": STATUS_CLOSING,
			"attendee_count": outcome.attendee_count,
			"batch_id": f"engagement:{session_id}",
		},
	)
	return {
		"session_id": session_id,
		"status": STATUS_CLOSING,
		"attendee_count": outcome.attendee_count,
		"inserted": outcome.inserted,
		"deduplicated": outcome.deduplicated,
		"finalized": outcome.finalized,
		"batch_id": f"engagement:{session_id}",
	}


@frappe.whitelist()
def finalize_close(*, session_id: str) -> dict[str, Any]:
	"""RQ target: deferred finalize ``closing`` → ``closed`` (FLO-198).

	Projects the post-grace participation log to ``AttendanceItem`` rows via the
	canonical bulk service (FLO-15), emits ``flock.engagement.closed`` once with
	the final count, and returns the final receipt (``inserted``/
	``deduplicated``). Idempotent: an RQ retry on an already-``closed`` session
	returns a preview receipt without re-projecting or re-emitting.

	Scheduled by :func:`close_session` after ``grace_seconds`` via the gateway's
	``schedule_finalize_close`` (``frappe.enqueue`` on the ``short`` queue).
	"""
	try:
		outcome = get_service().finalize_close(session_id)
	except FlockEngagementError as exc:
		frappe.log_error(f"flock_os.engagement_api finalize_close failed: {session_id}: {exc}")
		return {"session_id": session_id, "status": STATUS_CLOSING, "error": str(exc)}
	if outcome.finalized:
		_update_session_doc(
			session_id,
			{
				"status": STATUS_CLOSED,
				"attendee_count": outcome.attendee_count,
				"batch_id": f"engagement:{session_id}",
			},
		)
	return {
		"session_id": session_id,
		"status": STATUS_CLOSED if outcome.finalized else STATUS_CLOSING,
		"attendee_count": outcome.attendee_count,
		"inserted": outcome.inserted,
		"deduplicated": outcome.deduplicated,
		"finalized": outcome.finalized,
		"batch_id": f"engagement:{session_id}",
	}


@frappe.whitelist()
def join_session(
	*,
	session_id: str,
	member_id: str | None = None,
	device_fingerprint: str | None = None,
) -> dict[str, Any]:
	"""Issue a signed session ticket (FLO-9 §6.4). Player entry point.

	Returns the ticket + the current live state so the client can render the
	stage without a second round-trip.
	"""
	if not device_fingerprint and not member_id:
		frappe.throw("member_id or device_fingerprint is required to join")
	try:
		ticket = get_service().join(
			session_id=session_id,
			member_id=member_id,
			device_fingerprint=device_fingerprint or "",
		)
	except FlockEngagementError as exc:
		frappe.throw(str(exc), title="Cannot join session")
	state = session_state(session_id=session_id)
	return {
		"ticket": _ticket_to_dict(ticket),
		"attendee_key": ticket.attendee_key,
		"state": state,
	}


@frappe.whitelist()
def participate(
	*,
	session_id: str,
	ticket: dict[str, Any],
	attendee_key: str,
	nonce: str,
	member_id: str | None = None,
	attendee_display_name: str = "",
	device_fingerprint: str = "",
	role: str | None = None,
	score: float | None = None,
	reaction_ms: float | None = None,
	submitted_at: float | None = None,
	client_submitted_at: float | None = None,
	geo_region: str | None = None,
	offline_replay: bool = False,
	feedback: dict[str, Any] | None = None,
) -> dict[str, Any]:
	"""Validate + record one player interaction (FLO-9 §6). Player entry point."""
	parsed_ticket = _ticket_from_dict(ticket)
	request = ParticipateRequest(
		session_id=session_id,
		ticket=parsed_ticket,
		attendee_key=attendee_key,
		member_id=member_id,
		attendee_display_name=attendee_display_name,
		device_fingerprint=device_fingerprint,
		nonce=nonce,
		role=role,
		score=score,
		reaction_ms=reaction_ms,
		submitted_at=submitted_at,
		client_submitted_at=client_submitted_at,
		ip_address=_client_ip(),
		geo_region=geo_region,
		offline_replay=bool(offline_replay),
		feedback=feedback or {},
	)
	receipt = get_service().participate(request)
	return {
		"accepted": receipt.accepted,
		"attendee_key": receipt.attendee_key,
		"reason": receipt.reason,
		"submitted_at": receipt.submitted_at,
		"status_flags": receipt.status_flags,
	}


@frappe.whitelist()
def bulk_attendance(*, session_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
	"""Offline-queue flush (FLO-9 §8). Player entry point.

	Each item is one participation row (same shape as ``participate`` minus the
	server-resolved fields). Idempotent per ``(session, attendee_key, nonce)``.
	"""
	if not isinstance(items, list):
		frappe.throw("items must be a list")
	requests = [_participate_request_from_payload(session_id, raw, index) for index, raw in enumerate(items)]
	receipts = get_service().bulk_participate(requests)
	accepted = sum(1 for r in receipts if r.accepted)
	# Dedup counts only true duplicate-nonce replays (idempotent flushes), not
	# throttled / invalid-ticket / mismatch rejections — those aren't dedup
	# (FLO-195 nit). The per-row reason is in ``receipts`` for full detail.
	deduplicated = sum(1 for r in receipts if r.reason == "duplicate_nonce")
	return {
		"accepted": accepted,
		"deduplicated": deduplicated,
		"receipts": [
			{
				"index": index,
				"accepted": r.accepted,
				"reason": r.reason,
				"attendee_key": r.attendee_key,
			}
			for index, r in enumerate(receipts)
		],
	}


@frappe.whitelist()
def session_state(*, session_id: str) -> dict[str, Any]:
	"""Polling-fallback state snapshot (FLO-9 §8). Player + facilitator."""
	session = get_service()._require_session(session_id)  # noqa: SLF001
	service = get_service()
	attendees = service.gateway.attendees(session_id)
	return {
		"session_id": session_id,
		"status": session.status,
		"kind": session.kind,
		"engagement_type": engagement_mod.engagement_type_for(session.kind),
		"gathering": session.gathering,
		"branch": session.branch,
		"group": session.group,
		"organization": session.organization,
		"open_at": session.open_at,
		"close_at": session.close_at,
		"attendee_count": len(attendees),
	}


# ---------------------------------------------------------------------------- #
# Session-ticket (de)serialization — the wire shape between client + server.
# ---------------------------------------------------------------------------- #
def _ticket_to_dict(ticket: SessionTicket) -> dict[str, Any]:
	return {
		"session_id": ticket.session_id,
		"attendee_key": ticket.attendee_key,
		"member_id": ticket.member_id,
		"device_fingerprint": ticket.device_fingerprint,
		"issued_at": ticket.issued_at,
		"expires_at": ticket.expires_at,
		"signature": ticket.signature,
	}


def _ticket_from_dict(raw: dict[str, Any]) -> SessionTicket:
	try:
		return SessionTicket(
			session_id=raw["session_id"],
			attendee_key=raw["attendee_key"],
			member_id=raw.get("member_id"),
			device_fingerprint=raw["device_fingerprint"],
			issued_at=float(raw["issued_at"]),
			expires_at=float(raw["expires_at"]),
			signature=raw["signature"],
		)
	except KeyError as exc:
		frappe.throw(f"ticket missing required field: {exc.args[0]}")
	except (TypeError, ValueError) as exc:
		frappe.throw(f"ticket malformed: {exc}")


def _participate_request_from_payload(session_id: str, raw: dict[str, Any], index: int) -> ParticipateRequest:
	ticket = raw.get("ticket") or {}
	if "attendee_key" not in raw:
		frappe.throw(f"items[{index}].attendee_key is required")
	if "nonce" not in raw:
		frappe.throw(f"items[{index}].nonce is required (idempotency)")
	return ParticipateRequest(
		session_id=session_id,
		ticket=_ticket_from_dict(ticket),
		attendee_key=str(raw["attendee_key"]),
		member_id=raw.get("member_id"),
		attendee_display_name=raw.get("attendee_display_name") or "",
		device_fingerprint=raw.get("device_fingerprint") or "",
		nonce=str(raw["nonce"]),
		role=raw.get("role"),
		score=raw.get("score"),
		reaction_ms=raw.get("reaction_ms"),
		submitted_at=raw.get("submitted_at"),
		client_submitted_at=raw.get("client_submitted_at"),
		ip_address=_client_ip(),
		geo_region=raw.get("geo_region"),
		offline_replay=bool(raw.get("offline_replay")),
		feedback=raw.get("feedback") or {},
	)


# ---------------------------------------------------------------------------- #
# Frappe-facing helpers — DocType persistence, facilitator guard, id generation.
# ---------------------------------------------------------------------------- #
def _next_session_id() -> str:
	"""Generate the next ``ENG-`` name (Frappe naming series)."""
	return (
		frappe.model.naming.set_new_name(frappe.new_doc("Flock Engagement Session"))
		or f"ENG-{frappe.generate_hash(length=10)}"
	)


def _persist_session_doc(
	session: EngagementSession,
	*,
	title: str,
	engagement_type: str,
	grace_seconds: int,
	scheduled_at: str | None,
) -> None:
	"""Persist the Flock Engagement Session DocType row (list-view surface)."""
	doc = frappe.new_doc("Flock Engagement Session")
	doc.update(
		{
			"name": session.session_id,
			"title": title,
			"gathering": session.gathering,
			"facilitator": session.facilitator,
			"branch": session.branch,
			"group": session.group,
			"organization": session.organization,
			"engagement_type": engagement_type,
			"kind": session.kind,
			"status": session.status,
			"grace_seconds": grace_seconds,
			"scheduled_at": scheduled_at,
			"config": frappe.as_json(session.config) if session.config else None,
			"geofence": frappe.as_json(session.geofence) if session.geofence else None,
		}
	)
	try:
		doc.db_insert(ignore_permissions=True)
	except Exception:  # noqa: BLE001 — DocType persistence is best-effort metadata
		frappe.log_error(f"flock_os.engagement_api persist session failed: {session.session_id}")


def _update_session_doc(session_id: str, values: dict[str, Any]) -> None:
	"""Patch fields on the Flock Engagement Session DocType row."""
	try:
		frappe.db.set_value("Flock Engagement Session", session_id, values, update_modified=False)
	except Exception:  # noqa: BLE001 — DocType persistence is best-effort metadata
		frappe.log_error(f"flock_os.engagement_api update session failed: {session_id}")


def _assert_facilitator(session_id: str) -> None:
	"""Guard: only the facilitator (or a branch admin+) may drive lifecycle.

	Uses the central permission guards so the row-level scope assertion rides
	native branch User Permissions (ADR-0001 §6.2). A System Manager / Org Admin
	passes; a Branch Admin scoped to the session's branch passes; anyone else
	must be the session's recorded facilitator.
	"""
	session = get_service()._require_session(session_id)  # noqa: SLF001
	user = frappe.session.user
	_gw = get_permission_gateway()
	roles = _gw.get_user_roles(user)
	# Bypass-role check — admins can always drive lifecycle.
	from flock_os.permissions import BYPASS_ROLES

	if roles & BYPASS_ROLES:
		try:
			assert_branch_scope(doc_branch=session.branch, user=user, gateway=_gw)
		except FlockPermissionError:
			frappe.throw("facilitator scope violated for this session", frappe.PermissionError)
		return
	# Facilitator-of-record check.
	facilitator_user = (
		frappe.db.get_value("Flock Member", session.facilitator, "linked_user")
		if session.facilitator
		else None
	)
	if facilitator_user and facilitator_user == user:
		return
	# Last-resort: branch scope (lets a Branch Admin scoped to the branch close).
	try:
		assert_branch_scope(doc_branch=session.branch, user=user, gateway=_gw)
	except FlockPermissionError:
		frappe.throw("only the facilitator or a scoped admin may drive this session", frappe.PermissionError)


def _client_ip() -> str | None:
	"""Best-effort client IP for the suspect-IP heuristic (§6.7)."""
	try:
		return frappe.local.request_ip if getattr(frappe.local, "request_ip", None) else None
	except Exception:  # noqa: BLE001
		return None
