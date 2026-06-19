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

This module is also the **frontend contract** layer for the live engagement UI
(FLO-12 / FLO-9 §12): the engagement-kind catalog, the WCAG 2.1 AA accessibility
defaults, the JS realtime parity contract (so a player lands on the same shard
the server fans out to — ADR §5.1), and the facilitator console scope (the no-
leakage targetable subtree a facilitator may host engagement for). The portal
pages + client JS read these constants via JSON injected from the page, so
Python stays the single source of truth and the browser never hard-codes a
channel/event name. FLO-11's runtime is the superset; FLO-12's a11y/catalog/
facilitator-context additions are folded in here (FLO-207 reconciliation).
"""

from __future__ import annotations

import hashlib
import hmac
import json as _json
import random as _random
import string as _string
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from flock_os import realtime as rt
from flock_os.events import (
	ENGAGEMENT_SESSION_CLOSED,
	ENGAGEMENT_SESSION_OPENED,
)
from flock_os.events import (
	emit as emit_event,
)
from flock_os.permissions import (
	GLOBAL_BRANCH_ROLES,
	ROLE_BRANCH_ADMIN,
	ROLE_GROUP_LEADER,
	ROLE_ORG_ADMIN,
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
# Engagement-kind catalog (FLO-9 §3 / FLO-12) — the single source for the UI's
# kind router + i18n keys. The runtime's frozensets (above) cover validation;
# this catalog carries the richer per-kind metadata the portal page + client
# kind-router need: family, component tag, scoring, timing, the Calm Check-in
# availability, and the i18n key. The backend DocType ``engagement_kind`` select
# options (FLO-11) mirror these discriminators exactly.
# ---------------------------------------------------------------------------- #
FAMILY_GAME = ENGAGEMENT_TYPE_GAME
FAMILY_QUESTIONNAIRE = ENGAGEMENT_TYPE_QUESTIONNAIRE

KIND_TAP_BURST = "tap_burst"
KIND_QUIZ_RACE = "quiz_race"
KIND_REACTION = "reaction"
KIND_BINGO = "bingo"
KIND_TEAM_CHALLENGE = "team_challenge"
KIND_POLL = "poll"
KIND_WORD_CLOUD = "word_cloud"
KIND_QA = "qa"
KIND_PULSE = "pulse"

#: The component tag for each kind in the client kind-router (FLO-9 §12).
COMPONENT_BY_KIND: dict[str, str] = {
	KIND_TAP_BURST: "TapBurst",
	KIND_QUIZ_RACE: "QuizRace",
	KIND_REACTION: "ReactionTap",
	KIND_BINGO: "BingoCard",
	KIND_TEAM_CHALLENGE: "TeamChallenge",
	KIND_POLL: "LivePoll",
	KIND_WORD_CLOUD: "WordCloud",
	KIND_QA: "LiveQA",
	KIND_PULSE: "PulseSurvey",
}


class EngagementKind:
	"""One starter kind from FLO-9 §3 (frozen data; a small class for clarity).

	``calm_checkin`` is True for every timed game (FLO-9 §7 — "every timed game
	has a calm equivalent"); pure questionnaires are untimed so the toggle is a
	no-op there but still offered for consistency.
	"""

	__slots__ = ("kind", "family", "component", "scored", "timed", "calm_checkin", "i18n_key")

	def __init__(
		self,
		*,
		kind: str,
		family: str,
		component: str,
		scored: bool,
		timed: bool,
		calm_checkin: bool,
		i18n_key: str,
	) -> None:
		self.kind = kind
		self.family = family
		self.component = component
		self.scored = scored
		self.timed = timed
		self.calm_checkin = calm_checkin
		self.i18n_key = i18n_key

	def as_dict(self) -> dict[str, Any]:
		return {
			"kind": self.kind,
			"family": self.family,
			"component": self.component,
			"scored": self.scored,
			"timed": self.timed,
			"calm_checkin": self.calm_checkin,
			"i18n_key": self.i18n_key,
		}


#: The 9 starter kinds (FLO-9 §3.1 + §3.2). Attendance trigger for games =
#: "complete >=1 round"; for questionnaires = the per-kind capture column.
ENGAGEMENT_CATALOG: tuple[EngagementKind, ...] = (
	EngagementKind(
		kind=KIND_TAP_BURST,
		family=FAMILY_GAME,
		component="TapBurst",
		scored=True,
		timed=True,
		calm_checkin=True,
		i18n_key="Tap to Check In",
	),
	EngagementKind(
		kind=KIND_QUIZ_RACE,
		family=FAMILY_GAME,
		component="QuizRace",
		scored=True,
		timed=True,
		calm_checkin=True,
		i18n_key="Live Quiz Race",
	),
	EngagementKind(
		kind=KIND_REACTION,
		family=FAMILY_GAME,
		component="ReactionTap",
		scored=True,
		timed=True,
		calm_checkin=True,
		i18n_key="Reaction Tap",
	),
	EngagementKind(
		kind=KIND_BINGO,
		family=FAMILY_GAME,
		component="BingoCard",
		scored=True,
		timed=False,
		calm_checkin=False,
		i18n_key="Team Bingo",
	),
	EngagementKind(
		kind=KIND_TEAM_CHALLENGE,
		family=FAMILY_GAME,
		component="TeamChallenge",
		scored=True,
		timed=False,
		calm_checkin=False,
		i18n_key="Team Challenge",
	),
	EngagementKind(
		kind=KIND_POLL,
		family=FAMILY_QUESTIONNAIRE,
		component="LivePoll",
		scored=False,
		timed=False,
		calm_checkin=False,
		i18n_key="Live Poll",
	),
	EngagementKind(
		kind=KIND_WORD_CLOUD,
		family=FAMILY_QUESTIONNAIRE,
		component="WordCloud",
		scored=False,
		timed=False,
		calm_checkin=False,
		i18n_key="Word Cloud",
	),
	EngagementKind(
		kind=KIND_QA,
		family=FAMILY_QUESTIONNAIRE,
		component="LiveQA",
		scored=False,
		timed=False,
		calm_checkin=False,
		i18n_key="Live Q&A",
	),
	EngagementKind(
		kind=KIND_PULSE,
		family=FAMILY_QUESTIONNAIRE,
		component="PulseSurvey",
		scored=False,
		timed=False,
		calm_checkin=False,
		i18n_key="Pulse Survey",
	),
)

_CATALOG_BY_KIND: dict[str, EngagementKind] = {k.kind: k for k in ENGAGEMENT_CATALOG}


def get_kind(kind: str) -> EngagementKind | None:
	"""Look up a catalog kind by its discriminator (``None`` if unknown)."""
	return _CATALOG_BY_KIND.get(kind)


def catalog_json() -> list[dict[str, Any]]:
	"""The catalog as JSON-serializable rows for portal-page injection."""
	return [k.as_dict() for k in ENGAGEMENT_CATALOG]


def valid_kinds() -> tuple[str, ...]:
	"""The ordered kind discriminators (the ``engagement_kind`` select options)."""
	return tuple(k.kind for k in ENGAGEMENT_CATALOG)


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


# ---------------------------------------------------------------------------- #
# Accessibility config (FLO-9 §7, WCAG 2.1 AA — FLO-12). Defaults the UI
# applies; the player's saved preference overrides per-session.
# ---------------------------------------------------------------------------- #
#: Minimum tap-target side in CSS px (WCAG 2.5.5 / 2.1 AA target size ~48dp).
A11Y_MIN_TARGET_PX = 48

#: localStorage key for the player's accessibility-mode preference (persisted
#: across sessions; the portal page reads it before first paint to avoid a
#: flash of the default motion palette).
A11Y_PREF_KEY = "flock:engage:a11y"

#: Default accessibility profile (all fields overridable by the saved pref).
DEFAULT_A11Y_PROFILE: dict[str, Any] = {
	"enabled": False,
	"reduced_motion": False,
	"high_contrast": False,
	"colorblind_safe": False,
	"captions": False,
	"haptics": True,
}

#: Every timed game offers a Calm Check-in (FLO-9 §7) — attendance credits
#: without scoring reaction time. Pure questionnaires are untimed already.
CALM_CHECKIN_KINDS: frozenset[str] = frozenset(k.kind for k in ENGAGEMENT_CATALOG if k.calm_checkin)


def resolve_a11y_profile(saved: dict[str, Any] | None) -> dict[str, Any]:
	"""Merge a saved a11y preference over the defaults (unknown keys dropped)."""
	merged = dict(DEFAULT_A11Y_PROFILE)
	if saved:
		for key in DEFAULT_A11Y_PROFILE:
			if key in saved:
				merged[key] = bool(saved[key])
	return merged


# ---------------------------------------------------------------------------- #
# JS parity contract (ADR §5.1 / FLO-10 §5.1 — FLO-12). The single source the
# browser replicates so a player's shard room matches the server's fan-out
# target. realtime.shard_for is ``crc32(utf8(ref)) % N``; the browser computes
# the same via the parity helper in engagement-core.js. Kept here (not in
# realtime.py) so the frontend has one import for everything it needs to sync.
# ---------------------------------------------------------------------------- #
def js_parity_contract(shard_count: int = rt.DEFAULT_SHARD_COUNT) -> dict[str, Any]:
	"""The realtime constants the browser must replicate (injected as JSON).

	The shard-assignment algorithm is documented as a string so the JS parity
	test can assert it; the numeric ``shard_count`` is the ``N`` in ``% N``.
	"""
	return {
		"realtime_events": {
			"game_state": rt.RT_GAME_STATE,
			"attendance_presence": rt.RT_ATTENDANCE_PRESENCE,
			"attendance_count": rt.RT_ATTENDANCE_COUNT,
		},
		"channels": {
			"prefix": rt.EVENT_ROOM_PREFIX,
			"broadcast_segment": rt.BROADCAST_SEGMENT,
			"shard_segment": rt.SHARD_SEGMENT,
		},
		"shard_count": shard_count,
		"shard_algorithm": "crc32(utf8(attendee_ref)) >>> 0) % shard_count",
		"broadcast_channel": "flock_os:event:<session_id>:broadcast",
		"shard_channel": "flock_os:event:<session_id>:shard:<n>",
	}


#: REST surface the client calls (design FLO-9 §11; implemented by FLO-11).
#: Method paths so ``frappe.call({method})`` works the same as the announce page.
ENGAGEMENT_ENDPOINTS: dict[str, str] = {
	"create_session": "flock_os.engagement_views.create_session",
	"open_session": "flock_os.engagement_views.open_session",
	"close_session": "flock_os.engagement_views.close_session",
	"join_session": "flock_os.engagement_views.join_session",
	"participate": "flock_os.engagement_views.participate",
	"session_state": "flock_os.engagement_views.session_state",
	"flush_offline": "flock_os.engagement_views.flush_offline_queue",
	"facilitator_context": "flock_os.engagement_views.facilitator_context",
	"review_queue": "flock_os.engagement_views.suspect_review_queue",
	"manual_override": "flock_os.engagement_views.manual_override",
}


# ---------------------------------------------------------------------------- #
# Facilitator console context — mirrors portal.build_compose_context (FLO-60).
# The no-leakage boundary for hosting engagement: a facilitator may only bind a
# session to a gathering inside their targetable subtree.
#
# NOTE (FLO-207 reconciliation): the runtime I/O port above keeps the name
# ``EngagementGateway`` (it is FLO-11's, the superset). This console-scope port
# is a distinct concern (role + tree-membership + gathering reads), so it is
# named ``FacilitatorGateway`` to avoid the pre-merge name clash.
# ---------------------------------------------------------------------------- #
#: Roles that may run the facilitator console (host engagement).
FACILITATOR_ROLES: frozenset[str] = frozenset({ROLE_ORG_ADMIN, ROLE_BRANCH_ADMIN, ROLE_GROUP_LEADER})


@runtime_checkable
class FacilitatorGateway(Protocol):
	"""Port: the role + tree-membership + gathering reads the console needs.

	``facilitator_branches`` is the no-leakage boundary (mirrors
	:meth:`ComposeGateway.targetable_branches`): the exact branch set the
	facilitator may host engagement for. ``gatherings_for_branches`` is confined
	to those branches so the gathering picker cannot surface a cross-subtree
	gathering.
	"""

	def get_user_roles(self, user: str) -> tuple[str, ...]: ...

	def get_user_organization(self, user: str) -> str | None: ...

	def facilitator_branches(self, user: str) -> tuple[str, ...]: ...

	def branch_label(self, branch: str) -> str | None: ...

	def gatherings_for_branches(self, branches: tuple[str, ...]) -> tuple[dict[str, Any], ...]: ...

	def groups_for_branches(self, branches: tuple[str, ...]) -> tuple[dict[str, Any], ...]: ...


class NullFacilitatorGateway:
	"""Empty gateway — yields no targets (default before wiring)."""

	def get_user_roles(self, user: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def get_user_organization(self, user: str) -> str | None:  # noqa: ARG002
		return None

	def facilitator_branches(self, user: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def branch_label(self, branch: str) -> str | None:  # noqa: ARG002
		return None

	def gatherings_for_branches(self, branches: tuple[str, ...]) -> tuple[dict[str, Any], ...]:  # noqa: ARG002
		return ()

	def groups_for_branches(self, branches: tuple[str, ...]) -> tuple[dict[str, Any], ...]:  # noqa: ARG002
		return ()


def _role_set(roles: tuple[str, ...]) -> frozenset[str]:
	return frozenset(roles)


def is_facilitator(roles: tuple[str, ...]) -> bool:
	"""True iff ``roles`` may open the facilitator console."""
	return bool(_role_set(roles) & FACILITATOR_ROLES)


def build_facilitator_context(*, user: str, gateway: FacilitatorGateway) -> dict[str, Any]:
	"""Build the facilitator console picker context (FLO-12 — no-leakage scope).

	Returns the option set the console UI renders: the facilitator's targetable
	branches, the hostable gatherings within them, the group picker, the
	engagement-kind catalog, the a11y defaults, the JS parity contract, and the
	REST endpoints the client calls. The branch set is
	:meth:`facilitator_branches` verbatim — siblings never appear.

	Raises :class:`FlockEngagementError` if ``user`` lacks a facilitator role.
	"""
	roles = gateway.get_user_roles(user)
	if not is_facilitator(roles):
		raise FlockEngagementError(
			f"User {user!r} lacks a facilitator role (needs one of {sorted(FACILITATOR_ROLES)})."
		)

	organization = gateway.get_user_organization(user)
	branches = gateway.facilitator_branches(user)
	gatherings = gateway.gatherings_for_branches(branches)
	groups = gateway.groups_for_branches(branches)

	branch_rows = [{"name": b, "label": gateway.branch_label(b) or b} for b in branches]
	targetable = set(branches)
	gathering_rows = [
		{
			"name": g["name"],
			"branch": g["branch"],
			"label": g.get("label") or g["name"],
		}
		for g in gatherings
		if g["branch"] in targetable
	]
	group_rows = [
		{"name": g["name"], "branch": g["branch"], "label": g.get("label") or g["name"]}
		for g in groups
		if g["branch"] in targetable
	]

	return {
		"user": user,
		"roles": list(roles),
		"organization": organization,
		"branches": branch_rows,
		"gatherings": gathering_rows,
		"groups": group_rows,
		"kinds": catalog_json(),
		"a11y_defaults": DEFAULT_A11Y_PROFILE,
		"parity": js_parity_contract(),
		"endpoints": dict(ENGAGEMENT_ENDPOINTS),
		"is_facilitator": True,
	}


# ---------------------------------------------------------------------------- #
# Facilitator launch + authoring reconciliation (FLO-190).
#
# Pure, frappe-free helpers that bridge the FLO-12 portal/JS contract to the
# FLO-11 runtime transport (engagement_api). The ``engagement_views`` module is
# the thin ``@frappe.whitelist()`` adapter the portal JS actually calls
# (ENGAGEMENT_ENDPOINTS); every branch of real logic lives HERE so it stays
# unit-testable without a bench — the same hexagonal discipline as the rest of
# this module. FLO-190 is "the missing launch surface": template authoring +
# wiring a saved template to a session launch.
# ---------------------------------------------------------------------------- #
#: Length of a Fun Attendance room code (FLO-9 §2 — "6-digit join code").
ROOM_CODE_LEN = 6


def generate_room_code(rng: _random.Random | None = None) -> str:
	"""Return a fresh 6-digit room code (FLO-9 §2).

	``rng`` is injectable so the call is deterministic under test. The code is a
	six-character numeric string so it round-trips the player-side
	``/^\\d{6}$/`` join guard (engagement-core.js).
	"""
	r = rng if rng is not None else _random
	return "".join(r.choices(_string.digits, k=ROOM_CODE_LEN))


def resolve_session_ref(payload: dict[str, Any]) -> dict[str, Any]:
	"""Normalize the client's many spellings of "which session" to one lookup.

	The portal JS (engagement-core.js / engage-host.js) sends ``session``,
	``name``, ``session_id``, or ``room_code`` depending on the call site.
	Returns ``{"session_id": x}`` when a direct id is present, else
	``{"room_code": x}`` for a code join. Raises :class:`FlockEngagementError`
	if no reference at all was supplied — the adapter surfaces this as a 4xx.
	"""
	sid = payload.get("session_id") or payload.get("name") or payload.get("session")
	if sid:
		return {"session_id": str(sid)}
	code = payload.get("room_code")
	if code:
		return {"room_code": str(code)}
	raise FlockEngagementError("A session id or room code is required.")


def pack_session_config(payload: dict[str, Any]) -> dict[str, Any]:
	"""Pack the facilitator's inline session fields into the runtime ``config``.

	The console sends flat fields (rounds, calm_default, …); the runtime stores
	them under ``config`` (FLO-9 §2). Template/config keys the caller already
	nested under ``config`` are preserved verbatim.
	"""
	config = dict(payload.get("config") or {})
	for key in ("rounds", "calm_default", "accessibility_mode_default", "languages"):
		if payload.get(key) is not None:
			config[key] = payload[key]
	return config


def unpack_participate_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
	"""Map a player's kind-specific ``payload`` to ``engagement_api.participate`` fields.

	Attendance credit (the North Star) only needs the call to be *accepted*; the
	score fields drive scored-game leaderboards. The full payload is always
	preserved as ``feedback`` so close-time analytics stay lossless.
	"""
	feedback = dict(payload or {})
	out: dict[str, Any] = {"feedback": feedback}
	if kind == KIND_TAP_BURST and isinstance(feedback.get("hit"), (int, float)):
		out["score"] = float(feedback["hit"])
	return out


#: Doctypes that hold reusable engagement templates (FLO-190 authoring surface).
TEMPLATE_DOCTYPES: dict[str, str] = {
	ENGAGEMENT_TYPE_GAME: "Flock Engagement Game Template",
	ENGAGEMENT_TYPE_QUESTIONNAIRE: "Flock Engagement Questionnaire Template",
}

#: Kinds valid for each template family (mirrors the DocType ``Select`` options).
TEMPLATE_KINDS: dict[str, frozenset[str]] = {
	ENGAGEMENT_TYPE_GAME: GAME_KINDS,
	ENGAGEMENT_TYPE_QUESTIONNAIRE: QUESTIONNAIRE_KINDS,
}

#: Roles permitted to author (create/edit) engagement templates. The DocType
#: perms deny Group Leaders write; the authoring UI mirrors that here so the
#: page can hide the create/edit affordance for leaders (who only launch).
TEMPLATE_AUTHOR_ROLES: frozenset[str] = frozenset({ROLE_ORG_ADMIN, ROLE_BRANCH_ADMIN, "System Manager"})


def template_doctype_for_kind(kind: str) -> str:
	"""Resolve which template DocType a kind belongs to (game vs questionnaire)."""
	if kind in GAME_KINDS:
		return TEMPLATE_DOCTYPES[ENGAGEMENT_TYPE_GAME]
	if kind in QUESTIONNAIRE_KINDS:
		return TEMPLATE_DOCTYPES[ENGAGEMENT_TYPE_QUESTIONNAIRE]
	raise FlockEngagementError(f"Unknown engagement kind: {kind!r}")


def _template_config(template: dict[str, Any]) -> dict[str, Any]:
	"""Parse a template's JSON ``config`` (str or dict) into a plain dict."""
	raw = template.get("config")
	if isinstance(raw, dict):
		return dict(raw)
	if isinstance(raw, str) and raw:
		try:
			parsed = _json.loads(raw)
		except (ValueError, TypeError):
			return {}
		return parsed if isinstance(parsed, dict) else {}
	return {}


def template_to_launch_config(template: dict[str, Any]) -> dict[str, Any]:
	"""Project a saved Template DocType row onto ``create_session`` args.

	Facilitators launch from a saved template (FLO-190): its ``kind`` +
	``config`` become the session's, its ``template_name`` the default title,
	and its a11y default the session's accessibility-mode default.
	"""
	kind = template.get("kind")
	if not kind:
		raise FlockEngagementError("Template has no engagement kind.")
	return {
		"kind": kind,
		"engagement_type": engagement_type_for(kind),
		"title": template.get("template_name") or template.get("title"),
		"config": _template_config(template),
		"accessibility_mode_default": bool(template.get("accessibility_mode_default")),
		"template_name": template.get("name"),
		"template_doctype": template.get("doctype") or template_doctype_for_kind(kind),
	}


def template_summary(row: dict[str, Any], *, doctype: str = "") -> dict[str, Any]:
	"""Project a template row to the lean shape the authoring list view renders."""
	kind = row.get("kind") or ""
	return {
		"name": row.get("name"),
		"doctype": doctype or row.get("doctype") or (template_doctype_for_kind(kind) if kind else ""),
		"template_name": row.get("template_name") or row.get("title") or row.get("name"),
		"kind": kind or None,
		"engagement_type": engagement_type_for(kind) if kind else None,
		"description": row.get("description") or "",
		"is_active": bool(row.get("is_active", True)),
		"reviewed": bool(row.get("reviewed", False)),
		"accessibility_mode_default": bool(row.get("accessibility_mode_default", False)),
	}


#: The portal-view function name each endpoint key routes to (FLO-190).
#:
#: Pure contract so a frappe-free test can pin that every documented endpoint
#: resolves to a real ``@frappe.whitelist()`` view in :mod:`flock_os.engagement_views`
#: (closes the FLO-12 false-green where ENGAGEMENT_ENDPOINTS pointed at a module
#: that did not exist). The test parses ``engagement_views.py`` statically so it
#: needs no bench.
ENGAGEMENT_VIEWS_CONTRACT: dict[str, str] = {
	"create_session": "create_session",
	"open_session": "open_session",
	"close_session": "close_session",
	"join_session": "join_session",
	"participate": "participate",
	"session_state": "session_state",
	"flush_offline": "flush_offline_queue",
	"facilitator_context": "facilitator_context",
	"review_queue": "suspect_review_queue",
	"manual_override": "manual_override",
	"list_templates": "list_engagement_templates",
	"get_template": "get_engagement_template",
}


def assert_host_target_in_context(
	*, branch: str | None, group: str | None, gathering: str | None, context: dict[str, Any]
) -> None:
	"""Guard: the picked host scope must be inside the offered console context.

	The client-side complement to the backend engagement-scope guard (FLO-11):
	never trust the client for enforcement — the backend remains the source of
	truth — but reject a forged target here too so a tampered request never
	reaches session creation.

	Raises :class:`FlockEngagementError` if any picked scope is outside the
	offered set.
	"""
	offered_branches = {b["name"] for b in context.get("branches", ())}
	if not branch:
		raise FlockEngagementError("A host branch is required.")
	if branch not in offered_branches:
		raise FlockEngagementError(
			f"Branch {branch!r} is outside your targetable scope (no cross-subtree leakage)."
		)
	if group:
		offered_groups = {g["name"] for g in context.get("groups", ())}
		if group not in offered_groups:
			raise FlockEngagementError(
				f"Group {group!r} is outside your targetable scope (no cross-subtree leakage)."
			)
	if gathering:
		offered_gatherings = {g["name"] for g in context.get("gatherings", ())}
		if gathering not in offered_gatherings:
			raise FlockEngagementError(
				f"Gathering {gathering!r} is outside your targetable scope (no cross-subtree leakage)."
			)


__all__ = [
	"A11Y_MIN_TARGET_PX",
	"A11Y_PREF_KEY",
	"ALL_KINDS",
	"BulkServiceFactory",
	"CALM_CHECKIN_KINDS",
	"CloseOutcome",
	"COMPONENT_BY_KIND",
	"DEFAULT_A11Y_PROFILE",
	"DEFAULT_ATTENDANCE_STATUS",
	"DEFAULT_ATTENDEE_ROLE",
	"DEFAULT_ENGAGEMENT_SOURCE",
	"DEFAULT_GRACE_SECONDS",
	"DEFAULT_PARTICIPATION_THROTTLE_PER_SEC",
	"DEFAULT_TICKET_TTL_SECONDS",
	"ENGAGEMENT_CATALOG",
	"ENGAGEMENT_ENDPOINTS",
	"ENGAGEMENT_VIEWS_CONTRACT",
	"ENGAGEMENT_TYPE_GAME",
	"ENGAGEMENT_TYPE_QUESTIONNAIRE",
	"EngagementGateway",
	"EngagementKind",
	"EngagementService",
	"EngagementSession",
	"FACILITATOR_ROLES",
	"FAMILY_GAME",
	"FAMILY_QUESTIONNAIRE",
	"FacilitatorGateway",
	"FlockEngagementError",
	"GAME_KINDS",
	"GLOBAL_BRANCH_ROLES",
	"InMemoryEngagementGateway",
	"KIND_BINGO",
	"KIND_POLL",
	"KIND_PULSE",
	"KIND_QA",
	"KIND_QUIZ_RACE",
	"KIND_REACTION",
	"KIND_TAP_BURST",
	"KIND_TEAM_CHALLENGE",
	"KIND_WORD_CLOUD",
	"NullFacilitatorGateway",
	"ParticipateRequest",
	"Participation",
	"ParticipationReceipt",
	"QUESTIONNAIRE_KINDS",
	"ROLE_BRANCH_ADMIN",
	"ROLE_GROUP_LEADER",
	"ROLE_ORG_ADMIN",
	"ROOM_CODE_LEN",
	"STATUS_ARCHIVED",
	"STATUS_CLOSED",
	"STATUS_CLOSING",
	"STATUS_DRAFT",
	"STATUS_OPEN",
	"STATUS_SCHEDULED",
	"SUSPECT_REACTION_MS",
	"SUSPECT_SAME_IP_ATTENDEES",
	"TEMPLATE_AUTHOR_ROLES",
	"TEMPLATE_DOCTYPES",
	"TEMPLATE_KINDS",
	"SessionTicket",
	"assert_host_target_in_context",
	"attendee_key",
	"build_facilitator_context",
	"catalog_json",
	"detect_suspect_pattern",
	"engagement_type_for",
	"generate_room_code",
	"get_kind",
	"is_facilitator",
	"issue_session_ticket",
	"js_parity_contract",
	"normalize_score",
	"pack_session_config",
	"resolve_a11y_profile",
	"resolve_session_ref",
	"sign_ticket",
	"status_flags_for",
	"template_doctype_for_kind",
	"template_summary",
	"template_to_launch_config",
	"unpack_participate_payload",
	"valid_kinds",
	"validate_kind",
	"validate_session_window",
	"validate_state_transition",
	"verify_ticket",
]
