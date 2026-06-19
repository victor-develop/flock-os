"""
Frappe adapter for the engagement runtime (FLO-11).

The :class:`FrappeEngagementGateway` is the **production adapter** wiring the
pure :class:`flock_os.engagement.EngagementService` to MariaDB + Redis. It is
bench-only code — it cannot run without a Frappe site — so it lives outside
:mod:`flock_os.engagement` (which stays import-clean under plain pytest) and is
omitted from the project-level coverage ratchet alongside the other Frappe-only
surfaces (doctype controllers, hooks, patches).

Architecture (mirrors :class:`flock_os.reporting.FrappeBulkAttendanceGateway`):

* Sessions live on ``tabFlock Engagement Session`` (created by the transport
  layer in :mod:`flock_os.engagement_api`).
* The participation log is **Redis-backed during play** (FLO-9 §9 — no per-
  request DB writes), keyed on ``(session, attendee_key, nonce)`` for the
  idempotency backstop. On close the service projects the log to
  ``AttendanceItem`` rows and routes them through the canonical
  :class:`flock_os.reporting.BulkAttendanceService` (FLO-15), which owns the
  sharded MariaDB write + the single ``flock.attendance.bulk_recorded`` event.
* Hot counters (live headcount) read from Redis sorted sets for the polling-
  fallback state snapshot (§5.1).
* The throttle is a Redis sliding-window (§6.6); the suspect-IP heuristic reads
  the participation log.

All Redis access goes through ``frappe.cache`` / ``frappe.publish_realtime`` —
no bespoke Redis clients (FLO-10 §6, keeps the D3 escape hatch viable).

This module also hosts :class:`FrappeFacilitatorGateway` (FLO-12) — the
production :class:`flock_os.engagement.FacilitatorGateway` adapter that resolves
a facilitator's hostable scope (targetable branches + gatherings + groups) for
the facilitator console picker. It reuses the permission spine's materialized
branch User-Permissions (ADR §6.2) — the same no-leakage boundary as the
announcement compose gateway. (FLO-207 reconciliation: FLO-11's runtime adapter
keeps the name ``FrappeEngagementGateway``; FLO-12's console adapter is the
distinct ``FrappeFacilitatorGateway`` so the two never clash.)
"""

from __future__ import annotations

import datetime as _dt
import json
from typing import Any

from flock_os.engagement import (
	DEFAULT_ATTENDEE_ROLE,
	ENGAGEMENT_TYPE_GAME,
	STATUS_CLOSED,
	STATUS_CLOSING,
	STATUS_OPEN,
	EngagementSession,
	FacilitatorGateway,
	Participation,
)
from flock_os.permissions import GLOBAL_BRANCH_ROLES


class FrappeEngagementGateway:
	"""Production adapter wiring the service to MariaDB + Redis (FLO-9 §9).

	Lazily imports Frappe so this module stays import-clean in CI. See the
	module docstring for the storage split.
	"""

	SESSION_DOCTYPE = "Flock Engagement Session"

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	# -- ticket secret ------------------------------------------------------- #
	def ticket_secret(self, organization: str) -> str:
		"""The per-org HMAC secret (FLO-9 §6.4).

		Reads from ``Flock Organization.engagement_ticket_secret`` when set, else
		falls back to a site-wide secret in ``Flock Settings``. Always non-empty
		in a production site (the install patch seeds it).
		"""
		frappe = self._frappe
		if organization:
			org_secret = frappe.db.get_value("Flock Organization", organization, "engagement_ticket_secret")
			if org_secret:
				return str(org_secret)
		site_secret = frappe.get_cached_value("Flock Settings", "Flock Settings", "engagement_ticket_secret")
		return str(site_secret or "flock-engagement-default-secret")

	# -- session CRUD -------------------------------------------------------- #
	def get_session(self, session_id: str) -> EngagementSession | None:
		frappe = self._frappe
		row = frappe.db.get_value(
			self.SESSION_DOCTYPE,
			session_id,
			[
				"name",
				"gathering",
				"branch",
				"organization",
				"group",
				"kind",
				"status",
				"open_at",
				"close_at",
				"facilitator",
				"config",
				"geofence",
			],
			as_dict=True,
		)
		if not row:
			return None
		return _session_from_row(row)

	def upsert_session(self, session: EngagementSession) -> None:
		# Sessions are inserted by the transport layer (engagement_api) where
		# Frappe naming + validation are available; the runtime re-loads via
		# get_session. Persistence concern stays at the transport boundary.
		self._frappe.logger().debug("FrappeEngagementGateway.upsert_session: %s", session.session_id)

	def set_session_status(self, session_id: str, status: str, *, now: float | None = None) -> None:
		frappe = self._frappe
		values: dict[str, Any] = {"status": status}
		if now is not None:
			ts = _dt.datetime.fromtimestamp(now, tz=_dt.UTC).replace(tzinfo=None)
			if status == STATUS_OPEN:
				values["open_at"] = ts
			elif status in (STATUS_CLOSING, STATUS_CLOSED):
				# ``close_at`` is stamped on the closing transition (FLO-198) so
				# the grace window ``[close_at, close_at + grace]`` is live during
				# the closing dwell-state; the closed transition reuses it.
				values["close_at"] = ts
		frappe.db.set_value(self.SESSION_DOCTYPE, session_id, values, update_modified=False)

	# -- participation log (Redis during play, §9) -------------------------- #
	def _participation_key(self, session_id: str) -> str:
		return f"engagement:participations:{session_id}"

	def _nonce_set_key(self, session_id: str) -> str:
		"""Per-session Redis SET of ``(attendee_key|nonce)`` members (§9 scale).

		O(1) ``SADD``-return-value membership check replaces the O(n) ``zrange`` +
		Python substring scan the idempotency check used to do on every write.
		At 15k attendees × multiple rounds the sorted set reaches tens of
		thousands of members; scanning all of them per participation is O(n²)
		and contradicts the §9 scale claim (FLO-195 P3). The sorted set is kept
		for the ordered log projection; this SET is the nonce membership index.
		"""
		return f"engagement:nonce_seen:{session_id}"

	def record_participation(self, participation: Participation) -> bool:
		"""Append one participation to the Redis log; ``False`` on a duplicate nonce.

		The log is a Redis sorted set keyed by ``submitted_at`` with the nonce
		payload JSON as the member. Idempotency on ``(session, attendee_key,
		nonce)`` is enforced by an O(1) ``SADD`` against a per-session SET —
		``SADD`` returns 1 for a new member (recorded) and 0 for an existing one
		(duplicate, rejected). The sorted set is the ordered log used by the
		close-path projection; the SET is the membership index (FLO-195 P3).
		"""
		frappe = self._frappe
		cache = frappe.cache()
		log_key = self._participation_key(participation.session_id)
		nonce_member = f"{participation.attendee_key}|{participation.nonce}"
		# O(1) membership check via SADD return value (1 = new, 0 = duplicate).
		# Done before the zadd so a duplicate never appends to the ordered log.
		added = cache.sadd(self._nonce_set_key(participation.session_id), nonce_member)
		if added == 0:
			return False
		cache.expire(self._nonce_set_key(participation.session_id), 24 * 60 * 60)
		member = json.dumps(_participation_to_payload(participation), separators=(",", ":"))
		cache.zadd(log_key, {member: participation.submitted_at})
		cache.expire(log_key, 24 * 60 * 60)
		return True

	def has_attendee(self, session_id: str, attendee_key: str) -> bool:
		return any(p.attendee_key == attendee_key for p in self.participations(session_id))

	def attendees(self, session_id: str) -> tuple[str, ...]:
		frappe = self._frappe
		key = self._participation_key(session_id)
		raw_members = frappe.cache().zrange(key, 0, -1) or []
		seen: dict[str, None] = {}
		for raw in raw_members:
			if isinstance(raw, bytes):
				raw = raw.decode("utf-8", errors="ignore")
			payload = _safe_json(raw)
			if not payload:
				continue
			if payload.get("status_flags", {}).get("out_of_scope"):
				continue
			ak = payload.get("attendee_key")
			if ak and ak not in seen:
				seen[ak] = None
		return tuple(seen)

	def participations(self, session_id: str) -> tuple[Participation, ...]:
		frappe = self._frappe
		key = self._participation_key(session_id)
		raw_members = frappe.cache().zrange(key, 0, -1) or []
		out: list[Participation] = []
		for raw in raw_members:
			if isinstance(raw, bytes):
				raw = raw.decode("utf-8", errors="ignore")
			payload = _safe_json(raw)
			if payload:
				out.append(_participation_from_payload(payload))
		return tuple(out)

	# -- throttle + IP heuristic -------------------------------------------- #
	def throttle_allows(self, key: str, *, now: float, max_per_second: int) -> bool:
		"""Redis sliding-window throttle (§6.6). Best-effort: a Redis failure
		degrades to allow (the §6.6 throttle protects the log, not correctness)."""
		frappe = self._frappe
		try:
			cache = frappe.cache()
			bucket_key = f"engagement:throttle:{key}"
			cutoff = now - 1.0
			cache.zremrangebyscore(bucket_key, 0, cutoff)
			count = cache.zcard(bucket_key)
			if count and int(count) >= max_per_second:
				return False
			cache.zadd(bucket_key, {str(now): now})
			cache.expire(bucket_key, 2)
			return True
		except Exception:  # noqa: BLE001 — Redis is best-effort
			return True

	def same_ip_attendee_count(self, session_id: str, ip_address: str) -> int:
		"""Distinct attendees already seen from ``ip_address`` in ``session_id``."""
		if not ip_address:
			return 0
		seen: set[str] = set()
		for p in self.participations(session_id):
			if p.ip_address == ip_address:
				seen.add(p.attendee_key)
		return len(seen)

	# -- deferred finalize (FLO-198) --------------------------------------- #
	def schedule_finalize_close(self, session_id: str, grace_seconds: int) -> None:
		"""Enqueue ``finalize_close`` as an RQ job firing after ``grace_seconds``.

		Mirrors the bulk-attendance retry pattern (FLO-10 §3.3): ``at`` is the
		absolute Unix timestamp to run at; the stock ``short`` queue keeps this
		lightweight projection off the ``long`` bulk-write queue. The job target
		(:func:`flock_os.engagement_api.finalize_close`) is idempotent, so an RQ
		retry after the grace window is a safe no-op (the session is already
		``closed`` → preview receipt, no re-emit).
		"""
		frappe = self._frappe
		import time as _time

		frappe.enqueue(
			"flock_os.engagement_api.finalize_close",
			queue="short",
			session_id=session_id,
			at=_time.time() + int(grace_seconds),
		)


# ---------------------------------------------------------------------------- #
# Row <-> payload mappers (kept here so the pure service stays Frappe-free).
# ---------------------------------------------------------------------------- #
def _session_from_row(row: Any) -> EngagementSession:
	def _to_epoch(value: Any) -> float | None:
		if not value:
			return None
		if isinstance(value, _dt.datetime):
			return value.timestamp()
		return float(value)

	return EngagementSession(
		session_id=row["name"],
		gathering=row["gathering"],
		branch=row["branch"],
		organization=row["organization"],
		group=row.get("group"),
		kind=row["kind"],
		status=row["status"],
		open_at=_to_epoch(row.get("open_at")),
		close_at=_to_epoch(row.get("close_at")),
		facilitator=row.get("facilitator"),
		config=_safe_json(row.get("config")) or {},
		geofence=_safe_json(row.get("geofence")),
	)


def _participation_to_payload(p: Participation) -> dict[str, Any]:
	return {
		"session_id": p.session_id,
		"attendee_key": p.attendee_key,
		"member": p.member_id,
		"attendee_display_name": p.attendee_display_name,
		"device_fingerprint": p.device_fingerprint,
		"role": p.role,
		"engagement_type": p.engagement_type,
		"engagement_kind": p.engagement_kind,
		"score": p.score,
		"submitted_at": p.submitted_at,
		"client_submitted_at": p.client_submitted_at,
		"branch": p.branch,
		"organization": p.organization,
		"group": p.group,
		"geo_region": p.geo_region,
		"nonce": p.nonce,
		"ip_address": p.ip_address,
		"gathering": p.gathering,
		"status_flags": p.status_flags,
		"feedback": p.feedback,
	}


def _participation_from_payload(payload: dict[str, Any]) -> Participation:
	return Participation(
		session_id=payload.get("session_id") or "",
		attendee_key=payload.get("attendee_key") or "",
		member_id=payload.get("member"),
		attendee_display_name=payload.get("attendee_display_name") or "",
		device_fingerprint=payload.get("device_fingerprint") or "",
		role=payload.get("role") or DEFAULT_ATTENDEE_ROLE,
		engagement_type=payload.get("engagement_type") or ENGAGEMENT_TYPE_GAME,
		engagement_kind=payload.get("engagement_kind") or "tap_burst",
		score=payload.get("score"),
		submitted_at=float(payload.get("submitted_at") or 0.0),
		client_submitted_at=payload.get("client_submitted_at"),
		branch=payload.get("branch") or "",
		organization=payload.get("organization") or "",
		group=payload.get("group"),
		geo_region=payload.get("geo_region"),
		nonce=payload.get("nonce") or "",
		ip_address=payload.get("ip_address"),
		gathering=payload.get("gathering") or "",
		status_flags=payload.get("status_flags") or {},
		feedback=payload.get("feedback") or {},
	)


def _safe_json(value: Any) -> Any:
	if not value:
		return None
	if isinstance(value, (dict, list)):
		return value
	try:
		return json.loads(value)
	except Exception:  # noqa: BLE001
		return None


# ---------------------------------------------------------------------------- #
# Facilitator console adapter (FLO-12) — resolves a facilitator's hostable scope
# over ``frappe``. The no-leakage boundary mirrors FrappeComposeGateway.
# ---------------------------------------------------------------------------- #
class FrappeFacilitatorGateway(FacilitatorGateway):
	"""Production adapter: role + tree-membership + gathering reads over ``frappe``."""

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def _user_roles(self, frappe, user: str) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
		return tuple(frappe.get_roles(user) if user else [])

	def get_user_roles(self, user: str) -> tuple[str, ...]:
		return self._user_roles(self._frappe, user)

	def get_user_organization(self, user: str) -> str | None:
		frappe = self._frappe
		if not user:
			return None
		member = frappe.db.get_value("Flock Member", {"linked_user": user}, "organization")
		if member:
			return member
		orgs = frappe.get_all("Flock Organization", pluck="name", limit=1)
		return orgs[0] if orgs else None

	def facilitator_branches(self, user: str) -> tuple[str, ...]:
		frappe = self._frappe
		if not user:
			return ()
		roles = self._user_roles(frappe, user)
		# Global roles see every branch in their org (ADR §6.2 global axis).
		if frozenset(roles) & GLOBAL_BRANCH_ROLES:
			org = self.get_user_organization(user)
			if not org:
				return ()
			return tuple(
				frappe.get_all("Flock Branch", filters={"organization": org}, pluck="name", order_by="name")
			)
		# Scoped roles: the materialized User-Permission subtree (ADR §6.2).
		return tuple(
			frappe.get_all(
				"User Permission", filters={"user": user, "doctype": "Flock Branch"}, pluck="doc.name"
			)
		)

	def branch_label(self, branch: str) -> str | None:
		frappe = self._frappe
		if not branch:
			return None
		return frappe.db.get_value("Flock Branch", branch, "branch_name") or branch

	def gatherings_for_branches(self, branches: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
		frappe = self._frappe
		if not branches:
			return ()
		# Hostable gatherings: not cancelled, ordered by most recent start.
		rows = frappe.get_all(
			"Flock Gathering",
			filters={"branch": ["in", list(branches)], "status": ["!=", "Cancelled"]},
			fields=["name", "branch", "title", "starts_on"],
			order_by="starts_on desc, title",
			limit=200,
		)
		return tuple(
			{"name": r["name"], "branch": r["branch"], "label": r.get("title") or r["name"]} for r in rows
		)

	def groups_for_branches(self, branches: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
		frappe = self._frappe
		if not branches:
			return ()
		rows = frappe.get_all(
			"Flock Group",
			filters={"branch": ["in", list(branches)]},
			fields=["name", "branch", "group_name"],
			order_by="branch, group_name",
		)
		return tuple(
			{"name": r["name"], "branch": r["branch"], "label": r.get("group_name") or r["name"]}
			for r in rows
		)


__all__ = ["FrappeEngagementGateway", "FrappeFacilitatorGateway"]
