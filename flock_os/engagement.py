"""
Fun Attendance engagement runtime — pure service layer (FLO-11).

Implements the **live engagement** domain defined by the FLO-9 design
([rev 4](/FLO/issues/FLO-9#document-design)) against the canonical data model
+ event catalog in ADR-0001 ([FLO-4](/FLO/issues/FLO-4)).

This is the **domain core** of the Flock OS engagement runtime: server-
authoritative validators, signed session tickets, the participation log, the
anti-abuse heuristics, and the close path that materializes attendance. It is
transport-agnostic and import-clean without a Frappe site — exactly the same
hexagonal discipline as :mod:`flock_os.reporting` (FLO-15):

    REST  (flock_os.engagement_api.* @frappe.whitelist())   transport
      → Service  (EngagementService)                         ← domain rules HERE
      → Gateway  (EngagementGateway)                         ← I/O port
          ├─ InMemoryEngagementGateway                       ← unit tests (SQLite-fast)
          └─ FrappeEngagementGateway                         ← Redis hot counters +
                                                             MariaDB participation log +
                                                             flock_os.events.emit

Design rules honoured (FLO-9 §6 anti-abuse, §9 scale):

* **Server-authoritative time.** ``submitted_at`` is stamped by the server on
  every participation; the client ``client_submitted_at`` is audit-only.
* **Single credit per attendee.** The ``(engagement_session, attendee_key)``
  unique index is the idempotency backstop; the in-window dedupe set rejects a
  second credit for the same attendee in the same session.
* **Session window + grace.** Participation only counts if received within
  ``[open_at, close_at + grace]``; out-of-window rows are recorded with
  ``status_flags.out_of_scope = True`` and excluded from headcount.
* **Signed session ticket.** :func:`issue_session_ticket` returns a short-lived
  HMAC-signed token; every ``participate`` call must carry a valid ticket whose
  ``attendee_key`` matches the caller and whose window has not expired.
* **Anti-abuse is non-blocking.** Suspect-pattern heuristics flag a record for
  facilitator review (``status_flags.suspect_pattern = True``) but never
  exclude a player — false positives never silence a real attendee (§6.7).

The close path (§4) reuses the canonical bulk-attendance service from
:mod:`flock_os.reporting` (DRY) — the participation log is projected to
``AttendanceItem`` rows and routed through ``BulkAttendanceService.submit``,
which owns the sharded RQ write, the idempotency on
``(branch, gathering, member)``, and the single ``flock.attendance.bulk_recorded``
emission (ADR-0001 §5.4). Engagement emits ``flock.engagement.opened`` /
``flock.engagement.closed`` itself; the bulk-recorded event is emitted by the
reporting path it delegates to.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from flock_os.events import (
	ENGAGEMENT_SESSION_CLOSED,
	ENGAGEMENT_SESSION_OPENED,
)
from flock_os.events import (
	emit as emit_event,
)
from flock_os.reporting import (
	AttendanceItem,
	AttendanceScope,
	BulkAttendanceService,
)

# ---------------------------------------------------------------------------- #
# Tunables (config-over-constants; ADR-0001 §7). The Frappe adapter resolves
# org-level overrides from ``Flock Organization`` settings when those land; the
# values here are the service-layer defaults.
# ---------------------------------------------------------------------------- #
DEFAULT_GRACE_SECONDS = 30
"""Closing grace window (FLO-9 §4): absorbs clock skew + flaky reconnects."""

DEFAULT_TICKET_TTL_SECONDS = 60 * 60
"""Session ticket lifetime (FLO-9 §6.4): 1h covers a long live session."""

DEFAULT_PARTICIPATION_THROTTLE_PER_SEC = 5
"""Per-device raw participation throttle (FLO-9 §6.6): ≤5/s/device."""

SUSPECT_REACTION_MS = 100
"""Sub-100ms reaction times flag a suspect pattern (FLO-9 §6.7)."""

SUSPECT_SAME_IP_ATTENDEES = 8
"""One IP spawning ≥ this many attendees flags a suspect pattern (§6.7)."""

DEFAULT_ATTENDEE_ROLE = "member"
DEFAULT_ENGAGEMENT_SOURCE = "engagement"
DEFAULT_ATTENDANCE_STATUS = "Present"

# ---------------------------------------------------------------------------- #
# Engagement kinds (FLO-9 §3). Single source of truth for the catalog the
# DocType ``Select`` options + the score-normalization helpers consult.
# ---------------------------------------------------------------------------- #
GAME_KINDS: frozenset[str] = frozenset({"quiz_race", "tap_burst", "reaction", "bingo", "team_challenge"})
QUESTIONNAIRE_KINDS: frozenset[str] = frozenset({"poll", "word_cloud", "qa", "pulse"})
ALL_KINDS: frozenset[str] = GAME_KINDS | QUESTIONNAIRE_KINDS

ENGAGEMENT_TYPE_GAME = "game"
ENGAGEMENT_TYPE_QUESTIONNAIRE = "questionnaire"


def engagement_type_for(kind: str) -> str:
	"""Map a kind to its engagement_type (``game`` / ``questionnaire``)."""
	if kind in GAME_KINDS:
		return ENGAGEMENT_TYPE_GAME
	if kind in QUESTIONNAIRE_KINDS:
		return ENGAGEMENT_TYPE_QUESTIONNAIRE
	raise ValueError(f"unknown engagement kind: {kind!r}")


# ---------------------------------------------------------------------------- #
# Session lifecycle states (FLO-9 §4).
# ---------------------------------------------------------------------------- #
STATUS_DRAFT = "draft"
STATUS_SCHEDULED = "scheduled"
STATUS_OPEN = "open"
STATUS_CLOSING = "closing"
STATUS_CLOSED = "closed"
STATUS_ARCHIVED = "archived"

_LIVE_STATES: frozenset[str] = frozenset({STATUS_OPEN, STATUS_CLOSING})
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
	STATUS_DRAFT: frozenset({STATUS_SCHEDULED, STATUS_OPEN}),
	STATUS_SCHEDULED: frozenset({STATUS_DRAFT, STATUS_OPEN}),
	STATUS_OPEN: frozenset({STATUS_CLOSING}),
	STATUS_CLOSING: frozenset({STATUS_CLOSED}),
	STATUS_CLOSED: frozenset({STATUS_ARCHIVED}),
	STATUS_ARCHIVED: frozenset(),
}


class FlockEngagementError(Exception):
	"""Raised when an engagement invariant is violated (window/scope/state)."""


# ---------------------------------------------------------------------------- #
# Attendee key — the idempotency primitive (FLO-9 §5 / §6.2).
# ---------------------------------------------------------------------------- #
def attendee_key(*, session_id: str, member_id: str | None, device_fingerprint: str) -> str:
	"""Stable per-attendee idempotency key: ``sha256(session | member | device)``.

	Members derive identity from ``member_id``; visitors (no member) derive it
	from the device fingerprint. The session id binds the key to one session so
	a player cannot be counted twice in the same session, and so cross-session
	credit does not collide (each session has its own key space).
	"""
	if not session_id:
		raise FlockEngagementError("session_id is required to derive attendee_key")
	identity = member_id or device_fingerprint
	if not identity:
		raise FlockEngagementError(
			"either member_id or device_fingerprint is required to derive attendee_key"
		)
	digest = hashlib.sha256(f"{session_id}|{identity}".encode()).hexdigest()
	return digest


def normalize_score(kind: str, raw: float | None) -> float | None:
	"""Normalize a raw score to 0–100 per kind (FLO-9 §5 ``score``).

	Pure questionnaires have no score (``None``). Games clamp into ``[0, 100]``;
	a ``None`` raw is ``None``. The bounded persistence rule (§14.2) is enforced
	at the storage layer; this helper only guarantees the range invariant.
	"""
	if raw is None:
		return None
	if kind in QUESTIONNAIRE_KINDS:
		return None
	return max(0.0, min(100.0, float(raw)))


# ---------------------------------------------------------------------------- #
# Data shapes — plain data the service + gateway exchange.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EngagementSession:
	"""A live engagement session bound to a gathering (FLO-9 §2)."""

	session_id: str
	gathering: str
	branch: str
	organization: str
	group: str | None = None
	kind: str = "tap_burst"
	status: str = STATUS_DRAFT
	open_at: float | None = None
	close_at: float | None = None
	facilitator: str | None = None
	config: dict[str, Any] = field(default_factory=dict)
	geofence: dict[str, Any] | None = None

	@property
	def scope(self) -> dict[str, Any]:
		"""Row-level scope anchors forwarded into emitted events (event-modeling)."""
		s = {"branch": self.branch, "organization": self.organization}
		if self.group:
			s["group"] = self.group
		return s


@dataclass(frozen=True)
class SessionTicket:
	"""A signed, short-lived ticket issued on ``join`` (FLO-9 §6.4)."""

	session_id: str
	attendee_key: str
	member_id: str | None
	device_fingerprint: str
	issued_at: float
	expires_at: float
	signature: str

	@property
	def is_expired(self) -> bool:
		return time.time() >= self.expires_at


@dataclass(frozen=True)
class Participation:
	"""One validated player interaction recorded against the participation log."""

	session_id: str
	attendee_key: str
	member_id: str | None
	attendee_display_name: str
	device_fingerprint: str
	role: str
	engagement_type: str
	engagement_kind: str
	score: float | None
	submitted_at: float
	client_submitted_at: float | None
	branch: str
	organization: str
	group: str | None
	geo_region: str | None
	nonce: str
	ip_address: str | None = None
	# The gathering this session belongs to (FLO-11 §5). Copied from
	# ``session.gathering`` at participate time so the close-path projection can
	# stamp the ADR §9 provenance columns (gathering/member) without re-loading
	# the session. This is the dedup axis the cross-source index keys on.
	gathering: str = ""
	status_flags: dict[str, bool] = field(default_factory=dict)
	feedback: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ParticipationReceipt:
	"""Returned to the player from ``participate`` — confirms the credit landed."""

	accepted: bool
	attendee_key: str
	reason: str | None = None
	submitted_at: float | None = None
	status_flags: dict[str, bool] = field(default_factory=dict)


# ---------------------------------------------------------------------------- #
# Session ticket signing — HMAC-SHA256 over the canonical ticket payload.
#
# The signature binds (session, attendee, window) so a stolen ticket cannot be
# replayed against another session or attendee, and an expired ticket is rejected
# without re-querying the DB. The ``secret`` is supplied by the gateway (prod:
# ``Flock Organization`` settings → a per-org HMAC key); tests pass a fixed key.
# ---------------------------------------------------------------------------- #
def _canonical_ticket_payload(
	*,
	session_id: str,
	attendee_key: str,
	issued_at: float,
	expires_at: float,
) -> bytes:
	return f"{session_id}|{attendee_key}|{issued_at:.6f}|{expires_at:.6f}".encode()


def sign_ticket(
	*,
	session_id: str,
	attendee_key: str,
	issued_at: float,
	expires_at: float,
	secret: str,
) -> str:
	"""Compute the HMAC-SHA256 signature for a session ticket (FLO-9 §6.4)."""
	if not secret:
		raise FlockEngagementError("a non-empty ticket secret is required")
	payload = _canonical_ticket_payload(
		session_id=session_id,
		attendee_key=attendee_key,
		issued_at=issued_at,
		expires_at=expires_at,
	)
	return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_ticket(ticket: SessionTicket, *, secret: str) -> bool:
	"""Constant-time ticket signature check (FLO-9 §6.4).

	Returns ``False`` on a bad signature or an expired window; ``True`` only when
	the signature matches AND the ticket is still in-window.
	"""
	expected = sign_ticket(
		session_id=ticket.session_id,
		attendee_key=ticket.attendee_key,
		issued_at=ticket.issued_at,
		expires_at=ticket.expires_at,
		secret=secret,
	)
	if not hmac.compare_digest(expected, ticket.signature):
		return False
	return not ticket.is_expired


def issue_session_ticket(
	*,
	session: EngagementSession,
	member_id: str | None,
	device_fingerprint: str,
	secret: str,
	now: float | None = None,
	ttl_seconds: int = DEFAULT_TICKET_TTL_SECONDS,
) -> SessionTicket:
	"""Issue a signed ticket for ``(session, attendee)`` (FLO-9 §6.4)."""
	if session.status not in _LIVE_STATES:
		raise FlockEngagementError(f"cannot join a session in status {session.status!r} (must be open)")
	issued = float(now if now is not None else time.time())
	expires = issued + ttl_seconds
	key = attendee_key(
		session_id=session.session_id,
		member_id=member_id,
		device_fingerprint=device_fingerprint,
	)
	signature = sign_ticket(
		session_id=session.session_id,
		attendee_key=key,
		issued_at=issued,
		expires_at=expires,
		secret=secret,
	)
	return SessionTicket(
		session_id=session.session_id,
		attendee_key=key,
		member_id=member_id,
		device_fingerprint=device_fingerprint,
		issued_at=issued,
		expires_at=expires,
		signature=signature,
	)


# ---------------------------------------------------------------------------- #
# Anti-abuse validators (FLO-9 §6) — pure functions of (participation, context).
# ---------------------------------------------------------------------------- #
def validate_session_window(
	*,
	session: EngagementSession,
	submitted_at: float,
	grace_seconds: int = DEFAULT_GRACE_SECONDS,
) -> tuple[bool, str | None]:
	"""Check that ``submitted_at`` falls in ``[open_at, close_at + grace]`` (§6.3).

	Returns ``(in_window, reason)``. ``reason`` is ``None`` when in-window, else a
	machine-readable code (``before_open`` / ``after_grace``) the caller stuffs
	into ``status_flags.out_of_scope``.
	"""
	if session.open_at is not None and submitted_at < session.open_at:
		return False, "before_open"
	if session.close_at is not None and submitted_at > session.close_at + grace_seconds:
		return False, "after_grace"
	return True, None


def validate_state_transition(*, from_status: str | None, to_status: str) -> None:
	"""Raise :class:`FlockEngagementError` on an illegal lifecycle transition (§4)."""
	allowed = _VALID_TRANSITIONS.get(from_status, frozenset()) if from_status else frozenset()
	if to_status not in allowed:
		raise FlockEngagementError(f"illegal engagement session transition: {from_status!r} → {to_status!r}")


def detect_suspect_pattern(
	*,
	reaction_ms: float | None = None,
	same_ip_attendee_count: int = 0,
) -> bool:
	"""Non-blocking suspect-pattern heuristic (FLO-9 §6.7).

	Flags a record for facilitator review without excluding the attendee:
	sub-100ms reaction times, or one IP spawning many attendees. Attendance is
	still credited — false positives never silence a real attendee.
	"""
	if reaction_ms is not None and 0 < reaction_ms < SUSPECT_REACTION_MS:
		return True
	if same_ip_attendee_count >= SUSPECT_SAME_IP_ATTENDEES:
		return True
	return False


def status_flags_for(
	*,
	in_window: bool,
	suspect: bool = False,
	offline_replay: bool = False,
	facilitator_override: bool = False,
	out_of_scope_reason: str | None = None,
) -> dict[str, Any]:
	"""Build the canonical ``status_flags`` JSON shape (FLO-9 §5).

	The shape mixes boolean flags with an optional ``out_of_scope_reason`` code
	(``before_open`` / ``after_grace``), so the return type is ``dict[str, Any]``
	rather than ``dict[str, bool]`` (FLO-195 nit).
	"""
	return {
		"out_of_scope": (not in_window),
		"out_of_scope_reason": out_of_scope_reason,
		"suspect_pattern": bool(suspect),
		"offline_replay": bool(offline_replay),
		"facilitator_override": bool(facilitator_override),
	}


# ---------------------------------------------------------------------------- #
# Gateway port (hexagonal) — the only Frappe/Redis-touching surface.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class EngagementGateway(Protocol):
	"""Port: the I/O surface the engagement runtime depends on (FLO-9 §9).

	Production adapter: :class:`FrappeEngagementGateway` (Redis hot counters +
	MariaDB participation log + :func:`flock_os.events.emit`). Unit tests:
	:class:`InMemoryEngagementGateway`. The port keeps the service free of I/O so
	the validators + close path are unit-testable in isolation.
	"""

	def ticket_secret(self, organization: str) -> str:
		"""The HMAC secret used to sign/verify session tickets for ``organization``."""
		...

	def get_session(self, session_id: str) -> EngagementSession | None:
		"""Load a session by id, or ``None`` if absent."""
		...

	def upsert_session(self, session: EngagementSession) -> None:
		"""Persist ``session`` (create or update)."""
		...

	def set_session_status(self, session_id: str, status: str, *, now: float | None = None) -> None:
		"""Transition a session to ``status`` (open/close window stamp)."""
		...

	def record_participation(self, participation: Participation) -> bool:
		"""Append one participation row keyed on ``(session, attendee_key, nonce)``.

		Returns ``True`` if the row was newly recorded, ``False`` if the
		``(session, attendee_key, nonce)`` triple was already present (idempotent
		replay — FLO-9 §6.2 / §8).
		"""
		...

	def has_attendee(self, session_id: str, attendee_key: str) -> bool:
		"""Whether ``attendee_key`` already has ≥1 participation in ``session_id``."""
		...

	def attendees(self, session_id: str) -> tuple[str, ...]:
		"""The ordered set of attendee_keys with ≥1 in-window participation."""
		...

	def participations(self, session_id: str) -> tuple[Participation, ...]:
		"""The full participation log for ``session_id`` (close-path projection)."""
		...

	def throttle_allows(self, key: str, *, now: float, max_per_second: int) -> bool:
		"""Per-``key`` rate-limit check (§6.6). Returns ``True`` if the call is allowed."""
		...

	def same_ip_attendee_count(self, session_id: str, ip_address: str) -> int:
		"""Distinct attendees already seen from ``ip_address`` in ``session_id``."""
		...

	def schedule_finalize_close(self, session_id: str, grace_seconds: int) -> None:
		"""Schedule the deferred ``closing → closed`` finalize (FLO-198).

		Production adapter enqueues ``finalize_close`` as an RQ job that fires
		after ``grace_seconds`` so offline-reconnect flushes landing inside the
		``[close_at, close_at + grace]`` window are credited before the
		participation → attendance projection runs. The in-memory adapter records
		the pending finalize for unit-test control (tests drive finalize
		explicitly rather than waiting on a timer).
		"""
		...


class InMemoryEngagementGateway:
	"""Reference in-memory adapter for unit tests (SQLite-fast, no Frappe).

	Models exactly the persistence + counter semantics the production gateway
	must honour: per-``(session, attendee_key, nonce)`` idempotency, the
	per-session attendee set, a token-bucket-ish throttle, and the IP heuristic.
	"""

	def __init__(self, *, ticket_secret: str = "test-secret") -> None:
		self._secret = ticket_secret
		self._sessions: dict[str, EngagementSession] = {}
		self._participations: dict[str, list[Participation]] = {}
		# (session, attendee_key, nonce) idempotency index.
		self._nonce_seen: set[tuple[str, str, str]] = set()
		# Per-session attendee set (in-window only).
		self._attendees: dict[str, dict[str, None]] = {}
		# Throttle buckets: key -> list of recent timestamps.
		self._throttle: dict[str, list[float]] = {}
		# Per-(session, ip) attendee set for the suspect-IP heuristic.
		self._ip_attendees: dict[tuple[str, str], set[str]] = {}
		# Pending deferred finalizes recorded for unit-test control (FLO-198).
		# Each entry is ``(session_id, grace_seconds)``; tests drain these by
		# calling ``EngagementService.finalize_close`` explicitly.
		self.pending_finalizes: list[tuple[str, int]] = []

	def ticket_secret(self, organization: str) -> str:  # noqa: ARG002
		return self._secret

	def get_session(self, session_id: str) -> EngagementSession | None:
		return self._sessions.get(session_id)

	def upsert_session(self, session: EngagementSession) -> None:
		self._sessions[session.session_id] = session

	def set_session_status(self, session_id: str, status: str, *, now: float | None = None) -> None:
		session = self._sessions.get(session_id)
		if session is None:
			raise FlockEngagementError(f"unknown session: {session_id!r}")
		ts = float(now if now is not None else time.time())
		open_at = session.open_at
		close_at = session.close_at
		if status == STATUS_OPEN and open_at is None:
			open_at = ts
		# ``close_at`` is stamped when the session enters ``closing`` (FLO-198):
		# the grace window is ``[close_at, close_at + grace]`` and must be live
		# during the closing dwell-state, so the timestamp is set on the
		# closing transition, not the closed one.
		if status in (STATUS_CLOSING, STATUS_CLOSED) and close_at is None:
			close_at = ts
		self._sessions[session_id] = EngagementSession(
			session_id=session.session_id,
			gathering=session.gathering,
			branch=session.branch,
			organization=session.organization,
			group=session.group,
			kind=session.kind,
			status=status,
			open_at=open_at,
			close_at=close_at,
			facilitator=session.facilitator,
			config=session.config,
			geofence=session.geofence,
		)

	def record_participation(self, participation: Participation) -> bool:
		nonce_key = (participation.session_id, participation.attendee_key, participation.nonce)
		if nonce_key in self._nonce_seen:
			return False
		self._nonce_seen.add(nonce_key)
		self._participations.setdefault(participation.session_id, []).append(participation)
		if not participation.status_flags.get("out_of_scope"):
			self._attendees.setdefault(participation.session_id, {})[participation.attendee_key] = None
		if participation.ip_address:
			ip_key = (participation.session_id, participation.ip_address)
			self._ip_attendees.setdefault(ip_key, set()).add(participation.attendee_key)
		return True

	def has_attendee(self, session_id: str, attendee_key: str) -> bool:
		return attendee_key in self._attendees.get(session_id, {})

	def attendees(self, session_id: str) -> tuple[str, ...]:
		return tuple(self._attendees.get(session_id, {}).keys())

	def participations(self, session_id: str) -> tuple[Participation, ...]:
		return tuple(self._participations.get(session_id, ()))

	def throttle_allows(self, key: str, *, now: float, max_per_second: int) -> bool:
		bucket = self._throttle.setdefault(key, [])
		cutoff = now - 1.0
		bucket[:] = [ts for ts in bucket if ts >= cutoff]
		if len(bucket) >= max_per_second:
			return False
		bucket.append(now)
		return True

	def same_ip_attendee_count(self, session_id: str, ip_address: str) -> int:
		return len(self._ip_attendees.get((session_id, ip_address), set()))

	def schedule_finalize_close(self, session_id: str, grace_seconds: int) -> None:
		# Record the pending finalize for unit-test control; tests drain it by
		# calling ``EngagementService.finalize_close`` explicitly (no timer).
		self.pending_finalizes.append((session_id, grace_seconds))


# ---------------------------------------------------------------------------- #
# The service — domain rules live HERE and only here.
# ---------------------------------------------------------------------------- #
class EngagementService:
	"""The canonical engagement runtime (FLO-9 §4 / §6 / §9).

	Owns: state-machine guarded open/close, signed-ticket join, validated
	participate (window + scope + throttle + nonce idempotency + suspect
	heuristics), and the close path that projects the participation log into
	``AttendanceItem`` rows and routes them through the bulk-reporting service
	(FLO-15) — engagement does NOT re-implement the bulk write or the per-attendee
	event fan-out (DRY).
	"""

	def __init__(
		self,
		gateway: EngagementGateway,
		*,
		bulk_service_factory: BulkServiceFactory | None = None,
		grace_seconds: int = DEFAULT_GRACE_SECONDS,
		throttle_per_second: int = DEFAULT_PARTICIPATION_THROTTLE_PER_SEC,
	) -> None:
		self.gateway = gateway
		self._bulk_service_factory = bulk_service_factory or _default_bulk_service_factory
		self.grace_seconds = grace_seconds
		self.throttle_per_second = throttle_per_second

	# -- lifecycle ----------------------------------------------------------- #
	def create_session(self, session: EngagementSession) -> EngagementSession:
		"""Persist a new session in ``draft`` (FLO-9 §4). Idempotent on session_id."""
		validate_kind(session.kind)
		self.gateway.upsert_session(session)
		return session

	def open_session(self, session_id: str, *, now: float | None = None) -> EngagementSession:
		"""Transition ``draft``/``scheduled`` → ``open`` and emit ``engagement.opened``."""
		session = self._require_session(session_id)
		validate_state_transition(from_status=session.status, to_status=STATUS_OPEN)
		self.gateway.set_session_status(session_id, STATUS_OPEN, now=now)
		opened = self._require_session(session_id)
		emit_event(
			ENGAGEMENT_SESSION_OPENED,
			payload={
				"session": opened.session_id,
				"gathering": opened.gathering,
				"kind": opened.kind,
				"branch": opened.branch,
				"organization": opened.organization,
				**({"group": opened.group} if opened.group else {}),
			},
			scope=opened.scope,
		)
		return opened

	def close_session(self, session_id: str, *, now: float | None = None) -> CloseOutcome:
		"""Transition ``open`` → ``closing`` + schedule deferred finalize (FLO-9 §4).

		Stamps ``close_at``, schedules a deferred finalize after
		:data:`grace_seconds`, and returns a **preview** receipt (the in-window
		attendee count at close time; ``inserted``/``deduplicated`` are zeroed
		until the bulk projection runs at finalize). The session stays in
		``closing`` — a live state — so :meth:`participate` keeps applying the
		window check (``submitted_at ≤ close_at + grace`` → in-window) and a
		reconnecting device that flushes its offline queue within the grace
		window is credited (FLO-198). The deferred finalize
		(:meth:`finalize_close`, the RQ target) transitions ``closing →
		closed``, runs the participation → attendance projection through
		:class:`BulkAttendanceService`, and emits ``flock.engagement.closed``
		once with the post-grace count. The bulk service's idempotency makes a
		second projection of any in-flight rows a no-op.

		.. note::
			**Closing grace dwell-state (FLO-198).** Previously the close
			transition was synchronous (``open → closing → closed`` in one
			call) and the grace window only covered clock skew on in-flight
			rounds — a device reconnecting *after* close was rejected with
			``session_not_live`` before the window check. Holding ``closing``
			for ``grace_seconds`` closes that gap (§8 offline-replay-on-
			reconnect).
		"""
		session = self._require_session(session_id)
		validate_state_transition(from_status=session.status, to_status=STATUS_CLOSING)
		self.gateway.set_session_status(session_id, STATUS_CLOSING, now=now)
		closing = self._require_session(session_id)
		# Schedule the deferred finalize (RQ job in prod; recorded for the
		# in-memory adapter so tests drive it explicitly).
		self.gateway.schedule_finalize_close(session_id, self.grace_seconds)
		return self._preview_outcome(closing)

	def finalize_close(self, session_id: str) -> CloseOutcome:
		"""Deferred finalize: ``closing`` → ``closed`` + project + emit (FLO-198).

		RQ target scheduled by :meth:`close_session` after ``grace_seconds``.
		Projects the post-grace participation log to ``AttendanceItem`` rows
		and routes them through :class:`BulkAttendanceService` (FLO-15), then
		emits ``flock.engagement.closed`` once with the final attendee count.

		Idempotent: if the session is already ``closed`` (RQ retry / replay),
		returns a preview receipt without re-projecting or re-emitting — the
		state-machine guard + the bulk service's ``(branch, gathering, member)``
		index make a second projection a no-op anyway, but the single-emission
		contract for ``flock.engagement.closed`` forbids a re-emit.
		"""
		session = self._require_session(session_id)
		if session.status == STATUS_CLOSED:
			# Already finalized — idempotent no-op (RQ may retry after grace).
			return self._preview_outcome(session)
		validate_state_transition(from_status=session.status, to_status=STATUS_CLOSED)
		self.gateway.set_session_status(session_id, STATUS_CLOSED)
		closed = self._require_session(session_id)

		outcome = self._project_and_record_attendance(closed)

		emit_event(
			ENGAGEMENT_SESSION_CLOSED,
			payload={
				"session": closed.session_id,
				"gathering": closed.gathering,
				"count": outcome.attendee_count,
				"branch": closed.branch,
				"organization": closed.organization,
				**({"group": closed.group} if closed.group else {}),
			},
			scope=closed.scope,
		)
		return outcome

	# -- player flow --------------------------------------------------------- #
	def join(
		self,
		*,
		session_id: str,
		member_id: str | None,
		device_fingerprint: str,
	) -> SessionTicket:
		"""Validate the session is live and issue a signed ticket (FLO-9 §6.4)."""
		session = self._require_session(session_id)
		if session.status not in _LIVE_STATES:
			raise FlockEngagementError(f"session {session_id!r} is not live (status={session.status!r})")
		secret = self.gateway.ticket_secret(session.organization)
		return issue_session_ticket(
			session=session,
			member_id=member_id,
			device_fingerprint=device_fingerprint,
			secret=secret,
		)

	def participate(self, request: ParticipateRequest) -> ParticipationReceipt:
		"""Validate one interaction and record it against the participation log.

		Order (FLO-9 §6): ticket verification → window check → throttle → nonce
		idempotency → suspect heuristics → record. Out-of-window participations
		are still recorded (with ``out_of_scope=True``) but do not credit
		headcount; suspect participations are recorded with the flag and still
		credit. A repeat nonce is a no-op (returns ``accepted=False`` with reason
		``duplicate_nonce``); the caller surfaces it as 200 to keep the offline
		queue flush idempotent.
		"""
		session = self._require_session(request.session_id)
		if session.status not in _LIVE_STATES:
			return ParticipationReceipt(
				accepted=False,
				attendee_key=request.attendee_key,
				reason="session_not_live",
			)
		secret = self.gateway.ticket_secret(session.organization)
		if not verify_ticket(request.ticket, secret=secret):
			return ParticipationReceipt(
				accepted=False,
				attendee_key=request.attendee_key,
				reason="invalid_ticket",
			)
		if request.ticket.attendee_key != request.attendee_key:
			return ParticipationReceipt(
				accepted=False,
				attendee_key=request.attendee_key,
				reason="attendee_key_mismatch",
			)

		submitted_at = float(request.submitted_at if request.submitted_at is not None else time.time())
		in_window, reason = validate_session_window(
			session=session, submitted_at=submitted_at, grace_seconds=self.grace_seconds
		)

		throttle_key = f"{request.attendee_key}:{request.device_fingerprint}"
		if not self.gateway.throttle_allows(
			throttle_key, now=submitted_at, max_per_second=self.throttle_per_second
		):
			return ParticipationReceipt(
				accepted=False,
				attendee_key=request.attendee_key,
				reason="throttled",
			)

		same_ip = (
			self.gateway.same_ip_attendee_count(session.session_id, request.ip_address)
			if request.ip_address
			else 0
		)
		suspect = detect_suspect_pattern(
			reaction_ms=request.reaction_ms,
			same_ip_attendee_count=same_ip,
		)
		flags = status_flags_for(
			in_window=in_window,
			suspect=suspect,
			offline_replay=bool(request.offline_replay),
			out_of_scope_reason=reason,
		)

		engagement_type = engagement_type_for(session.kind)
		score = normalize_score(session.kind, request.score)
		participation = Participation(
			session_id=session.session_id,
			attendee_key=request.attendee_key,
			member_id=request.member_id,
			attendee_display_name=request.attendee_display_name,
			device_fingerprint=request.device_fingerprint,
			role=request.role or DEFAULT_ATTENDEE_ROLE,
			engagement_type=engagement_type,
			engagement_kind=session.kind,
			score=score,
			submitted_at=submitted_at,
			client_submitted_at=request.client_submitted_at,
			branch=session.branch,
			organization=session.organization,
			group=session.group,
			geo_region=request.geo_region,
			nonce=request.nonce,
			ip_address=request.ip_address,
			gathering=session.gathering,
			status_flags=flags,
			feedback=dict(request.feedback),
		)
		recorded = self.gateway.record_participation(participation)
		if not recorded:
			return ParticipationReceipt(
				accepted=False,
				attendee_key=request.attendee_key,
				reason="duplicate_nonce",
				submitted_at=submitted_at,
				status_flags=flags,
			)
		return ParticipationReceipt(
			accepted=True,
			attendee_key=request.attendee_key,
			submitted_at=submitted_at,
			status_flags=flags,
		)

	def bulk_participate(self, requests: Iterable[ParticipateRequest]) -> list[ParticipationReceipt]:
		"""Offline-queue flush: validate + record a batch of interactions (FLO-9 §8).

		Each request is processed independently (one bad nonce does not abort the
		batch). Idempotent on ``(session, attendee_key, nonce)`` — replaying a
		flushed queue yields ``accepted=False, reason=duplicate_nonce`` per row.
		"""
		return [self.participate(req) for req in requests]

	# -- close-path projection ---------------------------------------------- #
	def _in_window_attendees(self, session: EngagementSession) -> dict[str, Participation]:
		"""The first in-window participation per attendee_key (close-path basis).

		Out-of-scope rows (``status_flags.out_of_scope``) are excluded from
		headcount; the first in-window participation for each attendee_key wins
		so the score + feedback of the earliest credit are kept. Shared by the
		preview (:meth:`_preview_outcome`) and the final projection
		(:meth:`_project_and_record_attendance`).
		"""
		participations = self.gateway.participations(session.session_id)
		by_attendee: dict[str, Participation] = {}
		for participation in participations:
			if participation.status_flags.get("out_of_scope"):
				continue
			by_attendee.setdefault(participation.attendee_key, participation)
		return by_attendee

	def _preview_outcome(self, session: EngagementSession) -> CloseOutcome:
		"""Preview receipt at close time — attendee count only, no bulk projection.

		``inserted``/``deduplicated`` are zeroed (they come from the bulk service
		at finalize); ``finalized`` reflects the current session status so a
		preview built on a ``closed`` session (idempotent finalize re-run) reads
		as already-finalized.
		"""
		by_attendee = self._in_window_attendees(session)
		return CloseOutcome(
			session_id=session.session_id,
			gathering=session.gathering,
			attendee_count=len(by_attendee),
			inserted=0,
			deduplicated=0,
			finalized=(session.status == STATUS_CLOSED),
		)

	def _project_and_record_attendance(self, session: EngagementSession) -> CloseOutcome:
		"""Project in-window participations → ``AttendanceItem`` rows (FLO-9 §4/§5).

		One attendance row per attendee_key (the first in-window participation for
		that key wins — keeps the score + feedback of the earliest credit).
		Routes the batch through :class:`BulkAttendanceService` (FLO-15), which
		owns the sharded RQ write, the ``(branch, gathering, member)`` cross-source
		dedup, and the single ``flock.attendance.bulk_recorded`` emission.
		"""
		by_attendee = self._in_window_attendees(session)
		items = [_participation_to_attendance_item(p) for p in by_attendee.values()]
		scope = AttendanceScope(branch=session.branch)
		bulk_service = self._bulk_service_factory(session.organization)
		outcome = bulk_service.submit(items, scope, batch_id=f"engagement:{session.session_id}")
		return CloseOutcome(
			session_id=session.session_id,
			gathering=session.gathering,
			attendee_count=len(items),
			inserted=outcome.inserted,
			deduplicated=outcome.deduplicated,
			finalized=True,
		)

	def _require_session(self, session_id: str) -> EngagementSession:
		session = self.gateway.get_session(session_id)
		if session is None:
			raise FlockEngagementError(f"unknown engagement session: {session_id!r}")
		return session


# ---------------------------------------------------------------------------- #
# Participate request — the transport-invariant payload for one interaction.
# ---------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ParticipateRequest:
	"""One player interaction submitted to ``participate`` (FLO-9 §6)."""

	session_id: str
	ticket: SessionTicket
	attendee_key: str
	member_id: str | None
	attendee_display_name: str
	device_fingerprint: str
	nonce: str
	role: str | None = None
	score: float | None = None
	reaction_ms: float | None = None
	submitted_at: float | None = None
	client_submitted_at: float | None = None
	ip_address: str | None = None
	geo_region: str | None = None
	offline_replay: bool = False
	feedback: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CloseOutcome:
	"""Receipt for a ``close_session`` / ``finalize_close`` call.

	``close_session`` returns a **preview** (``finalized=False``): the in-window
	attendee count at close time, with ``inserted``/``deduplicated`` zeroed (the
	bulk projection runs at finalize). ``finalize_close`` returns the **final**
	receipt (``finalized=True``) with the bulk-service counters.
	"""

	session_id: str
	gathering: str
	attendee_count: int
	inserted: int
	deduplicated: int
	finalized: bool = True


# ---------------------------------------------------------------------------- #
# Bulk-service factory — indirection so the close path can route through the
# canonical BulkAttendanceService in prod and a recording fake in tests, without
# the engagement module importing Frappe at module load.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class BulkServiceFactory(Protocol):
	"""Build a :class:`BulkAttendanceService` for one organization's scope."""

	def __call__(self, organization: str) -> BulkAttendanceService: ...


def _default_bulk_service_factory(organization: str) -> BulkAttendanceService:  # noqa: ARG001
	"""Production default: the canonical Frappe-backed bulk service (FLO-15).

	Lazy import so :mod:`flock_os.engagement` stays import-clean under plain
	pytest. The Frappe adapter wires the real gateway; the unit suite installs a
	recording factory via the service constructor.
	"""
	from flock_os.reporting import FrappeBulkAttendanceGateway

	return BulkAttendanceService(FrappeBulkAttendanceGateway())


# ---------------------------------------------------------------------------- #
# Participation → AttendanceItem projection (FLO-9 §5).
# ---------------------------------------------------------------------------- #
def _participation_to_attendance_item(participation: Participation) -> AttendanceItem:
	"""Project one in-window participation to a bulk-reporting attendance row.

	Stamps the ADR §9 / FLO-11 §5 provenance so the cross-source dedup indexes
	actually fire. ``event = participation.gathering``: the gathering is the
	rollup/grouping axis (design §5: ``gathering`` "replaces the rev-1 ``event``
	field"), so the ``Event Attendance Summary`` counts engagement-sourced rows
	against the gathering, not the (opaque) session id. ``gathering`` / ``member``
	are the ``UNIQUE (branch, gathering, member)`` index keys (ADR §9): a
	manual-roster credit + an engagement credit for the same member at the same
	gathering collapse to one row via upsert. ``engagement_session`` /
	``attendee_key`` are the per-session ``UNIQUE (engagement_session,
	attendee_key)`` backstop (FLO-9 §5).

	Visitors (no ``member_id``) upsert as ``Flock Member(status=Visitor)``; their
	``member`` is ``None`` so the nullable-``member`` composite index leaves them
	to the ``(engagement_session, attendee_key)`` index (ANSI multi-NULL). The
	``attendee_key`` rides ``client_req_id`` so the reporting idempotency index
	``(event, attendee_ref, client_req_id)`` dedupes correctly.
	"""
	attendee_ref = participation.member_id or participation.attendee_key
	return AttendanceItem(
		event=participation.gathering,
		attendee_ref=attendee_ref,
		branch=participation.branch,
		status=DEFAULT_ATTENDANCE_STATUS,
		source=DEFAULT_ENGAGEMENT_SOURCE,
		client_req_id=participation.attendee_key,
		gathering=participation.gathering,
		member=participation.member_id,
		engagement_session=participation.session_id,
		attendee_key=participation.attendee_key,
	)


def validate_kind(kind: str) -> None:
	"""Raise :class:`FlockEngagementError` if ``kind`` is not in the catalog."""
	if kind not in ALL_KINDS:
		raise FlockEngagementError(f"unknown engagement kind {kind!r}; expected one of {sorted(ALL_KINDS)}")


__all__ = [
	"ALL_KINDS",
	"BulkServiceFactory",
	"CloseOutcome",
	"DEFAULT_ATTENDANCE_STATUS",
	"DEFAULT_ATTENDEE_ROLE",
	"DEFAULT_ENGAGEMENT_SOURCE",
	"DEFAULT_GRACE_SECONDS",
	"DEFAULT_PARTICIPATION_THROTTLE_PER_SEC",
	"DEFAULT_TICKET_TTL_SECONDS",
	"ENGAGEMENT_TYPE_GAME",
	"ENGAGEMENT_TYPE_QUESTIONNAIRE",
	"EngagementGateway",
	"EngagementService",
	"EngagementSession",
	"FlockEngagementError",
	"GAME_KINDS",
	"InMemoryEngagementGateway",
	"ParticipateRequest",
	"Participation",
	"ParticipationReceipt",
	"QUESTIONNAIRE_KINDS",
	"STATUS_ARCHIVED",
	"STATUS_CLOSED",
	"STATUS_CLOSING",
	"STATUS_DRAFT",
	"STATUS_OPEN",
	"STATUS_SCHEDULED",
	"SessionTicket",
	"SUSPECT_REACTION_MS",
	"SUSPECT_SAME_IP_ATTENDEES",
	"attendee_key",
	"detect_suspect_pattern",
	"engagement_type_for",
	"issue_session_ticket",
	"normalize_score",
	"sign_ticket",
	"status_flags_for",
	"validate_kind",
	"validate_session_window",
	"validate_state_transition",
	"verify_ticket",
]
