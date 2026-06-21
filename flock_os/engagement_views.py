"""
Flock OS — Fun Attendance portal view adapter (FLO-190 / FLO-9 §11, §12).

This is the **portal-facing** transport layer the engagement UI actually calls:
every endpoint string in :data:`flock_os.engagement.ENGAGEMENT_ENDPOINTS`
resolves to a ``@frappe.whitelist()`` function here. FLO-11 shipped the runtime
transport as :mod:`flock_os.engagement_api`; FLO-12's portal JS + the FLO-190
facilitator launch were authored against this ``engagement_views`` surface (the
documented FLO-9 §11 method paths). FLO-190 closes that seam.

Discipline (mirrors :mod:`flock_os.engagement_api`):

* **No domain rules live here.** This module only reconciles the client's
  argument shape (``session``/``name``/``session_id``/``room_code``; the flat
  console fields; the player ``{kind, payload}`` envelope) and delegates to the
  runtime transport (:mod:`flock_os.engagement_api`) + the pure reconciliation
  helpers in :mod:`flock_os.engagement`. The backend re-validates scope, window,
  throttle, and the signed ticket on every mutating call (source of truth).
* **Business logic stays pure + unit-tested.** Every branch of reconciliation
  lives in :mod:`flock_os.engagement` so it is covered by the project gate; the
  wrappers below are intentionally thin (bench-exercised, omitted from the
  coverage ratchet — see ``scripts/qa-gate.sh``).
* **Templates (FLO-190).** Facilitators author Game / Questionnaire Templates
  (admin-scoped) and launch a session from one; the template's ``kind`` +
  ``config`` populate ``create_session``.

Frappe-level integration tests exercise the whitelist path under ``bench
run-tests``; the project gate statically pins the contract in
``test_engagement_views_contract`` (no bench required).
"""

from __future__ import annotations

from typing import Any

import frappe

from flock_os import engagement as eng
from flock_os import engagement_api

# ---------------------------------------------------------------------------- #
# Facilitator lifecycle — create / open / close (FLO-190 launch surface).
# ---------------------------------------------------------------------------- #


@frappe.whitelist()
def create_session(**kwargs: Any) -> dict[str, Any]:
	"""Create a session from inline console config OR a saved template (FLO-190).

	Accepts the FLO-12 console shape (``engagement_kind``/``rounds``/
	``calm_default``) and the FLO-190 template-launch shape (``template_doctype``
	+ ``template_name``). Delegates persistence + scope validation to
	:func:`engagement_api.create_session`; generates and persists the 6-digit
	room code (FLO-9 §2) the share surface + code-join path need.
	"""
	kind = kwargs.get("kind") or kwargs.get("engagement_kind")
	config = eng.pack_session_config(kwargs)
	title = kwargs.get("title")

	template_name = kwargs.get("template_name")
	if template_name and (not kind or not kwargs.get("_from_template")):
		template = _template_doc(kwargs.get("template_doctype"), template_name)
		launch = eng.template_to_launch_config(template)
		kind = kind or launch["kind"]
		# Template defaults first; inline console fields override (client has
		# already folded the template into the inline fields when launching via
		# the picker, so this only fills gaps for a direct template launch).
		merged_config = dict(launch["config"])
		merged_config.update(config)
		config = merged_config
		title = title or launch["title"]

	if not kind:
		frappe.throw("engagement_kind (or a template) is required")

	res = engagement_api.create_session(
		gathering=kwargs.get("gathering"),
		title=title or "",
		engagement_type=eng.engagement_type_for(kind),
		kind=kind,
		branch=kwargs.get("branch"),
		group=kwargs.get("group"),
		organization=kwargs.get("organization"),
		facilitator=kwargs.get("facilitator"),
		config=config,
		geofence=kwargs.get("geofence"),
		grace_seconds=int(kwargs.get("grace_seconds") or eng.DEFAULT_GRACE_SECONDS),
		scheduled_at=kwargs.get("scheduled_at"),
	)

	session_id = res["session_id"]
	room_code = eng.generate_room_code()
	_persist_room_code(session_id, room_code, accessibility=bool(kwargs.get("calm_default")))
	return {
		"name": session_id,
		"session_id": session_id,
		"status": res["status"],
		"room_code": room_code,
		"player_url": f"/engage?session={session_id}",
		"engagement_type": eng.engagement_type_for(kind),
		"kind": kind,
	}


@frappe.whitelist()
def open_session(**kwargs: Any) -> dict[str, Any]:
	"""Transition a session to ``open``. Accepts the console's ``name`` arg."""
	ref = eng.resolve_session_ref(kwargs)
	res = engagement_api.open_session(session_id=ref["session_id"])
	return _enrich_lifecycle(ref["session_id"], res)


@frappe.whitelist()
def close_session(**kwargs: Any) -> dict[str, Any]:
	"""Transition a session to ``closing`` (grace window). Console ``name`` arg."""
	ref = eng.resolve_session_ref(kwargs)
	res = engagement_api.close_session(session_id=ref["session_id"])
	# Console reads ``res.count`` for the recorded headcount (engage-host.js).
	res = dict(res)
	res["count"] = res.get("attendee_count")
	return _enrich_lifecycle(ref["session_id"], res)


# ---------------------------------------------------------------------------- #
# Player path — join / participate / state / offline flush (FLO-9 §6, §8).
# ---------------------------------------------------------------------------- #


@frappe.whitelist()
def join_session(**kwargs: Any) -> dict[str, Any]:
	"""Issue a signed session ticket. Resolves ``session``/``room_code`` → id."""
	session_id = _resolve_session_id(kwargs)
	member_id = kwargs.get("member_id")
	device_fingerprint = kwargs.get("device_fingerprint")
	return engagement_api.join_session(
		session_id=session_id,
		member_id=member_id,
		device_fingerprint=device_fingerprint,
	)


@frappe.whitelist()
def participate(**kwargs: Any) -> dict[str, Any]:
	"""Record one player interaction from the ``{kind, payload}`` envelope."""
	session_id = _resolve_session_id(kwargs)
	ticket = kwargs.get("ticket") or {}
	attendee_key = kwargs.get("attendee_key") or ticket.get("attendee_key")
	if not attendee_key:
		frappe.throw("A valid session ticket is required to participate.")
	kind = kwargs.get("kind") or (ticket.get("kind")) or ""
	extra = eng.unpack_participate_payload(kind, kwargs.get("payload") or {})
	return engagement_api.participate(
		session_id=session_id,
		ticket=ticket,
		attendee_key=attendee_key,
		nonce=kwargs.get("nonce") or frappe.generate_hash(length=12),
		member_id=kwargs.get("member_id"),
		attendee_display_name=kwargs.get("attendee_display_name") or "",
		device_fingerprint=kwargs.get("device_fingerprint") or "",
		role=kwargs.get("role"),
		score=extra.get("score"),
		reaction_ms=extra.get("reaction_ms"),
		client_submitted_at=kwargs.get("client_submitted_at"),
		geo_region=kwargs.get("geo_region"),
		offline_replay=bool(kwargs.get("offline_replay")),
		feedback=extra.get("feedback"),
	)


@frappe.whitelist()
def flush_offline_queue(**kwargs: Any) -> dict[str, Any]:
	"""Drain the player's offline queue (delegates to the idempotent bulk path)."""
	session_id = _resolve_session_id(kwargs)
	items = kwargs.get("items") or []
	res = engagement_api.bulk_attendance(session_id=session_id, items=items)
	res = dict(res)
	# engagement-core.js reads ``accepted_count`` from the flush receipt.
	res["accepted_count"] = res.get("accepted")
	return res


@frappe.whitelist()
def session_state(**kwargs: Any) -> dict[str, Any]:
	"""Polling-fallback snapshot. Wrapped for the player's ``refreshState`` shape."""
	session_id = _resolve_session_id(kwargs)
	snapshot = engagement_api.session_state(session_id=session_id)
	# The player core reads ``state`` (serverSnapshot) + ``headcount`` +
	# ``status``; the facilitator monitor reads the raw fields too.
	return {
		**snapshot,
		"state": snapshot,
		"headcount": snapshot.get("attendee_count"),
	}


# ---------------------------------------------------------------------------- #
# Facilitator console support (FLO-12 console; read-only / audit here).
# ---------------------------------------------------------------------------- #


@frappe.whitelist()
def facilitator_context(**kwargs: Any) -> dict[str, Any]:
	"""Return the no-leakage console picker context for the calling user."""
	from flock_os.engagement_frappe import FrappeFacilitatorGateway

	user = kwargs.get("user") or frappe.session.user
	return eng.build_facilitator_context(user=user, gateway=FrappeFacilitatorGateway())


@frappe.whitelist()
def suspect_review_queue(**kwargs: Any) -> dict[str, Any]:
	"""Read-only list of flagged participations for facilitator review (§6.7).

	Anti-abuse is non-blocking: flagged rows are *still counted* until a
	revocation lands. The actual keep/revoke mutation is owned by the bulk
	attendance path (out of scope for FLO-190); this view only surfaces the
	review queue so the console can render it.

	The payload is **capped** server-side at :data:`flock_os.engagement.
	REVIEW_QUEUE_MAX` (P2-5): under adversarial flag rates at 15k concurrent
	attendees the flagged set can grow large, and the host console must never
	render or transit an unbounded array. ``total`` carries the full flagged
	count so the console can show "showing N of M".
	"""
	ref = eng.resolve_session_ref(kwargs)
	session_id = ref["session_id"]
	try:
		participations = engagement_api.get_service().gateway.participations(session_id)
	except Exception:  # noqa: BLE001 — runtime/Redis is best-effort from the view
		participations = ()
	payload = eng.review_queue_items(participations)
	return {"session_id": session_id, **payload}


@frappe.whitelist()
def manual_override(**kwargs: Any) -> dict[str, Any]:
	"""Audit-log a facilitator keep/revoke decision (FLO-9 §6.7).

	The decision is recorded on the session's ``config`` audit trail
	(``overrides``) so it survives review. Applying it to the captured
	attendance set is owned by the bulk-reporting path (FLO-15), which FLO-190
	defers to; this endpoint never silently mutates attendance.
	"""
	ref = eng.resolve_session_ref(kwargs)
	session_id = ref["session_id"]
	action = kwargs.get("action")
	if action not in ("keep", "revoke"):
		frappe.throw("action must be 'keep' or 'revoke'")
	reason = kwargs.get("reason") or "Facilitator review"
	override = {
		"attendee_key": kwargs.get("attendee_key"),
		"action": action,
		"reason": reason,
		"by": frappe.session.user,
	}
	_append_session_override(session_id, override)
	return {"session_id": session_id, "recorded": True, "override": override}


# ---------------------------------------------------------------------------- #
# Template authoring (FLO-190) — list / get against the config DocTypes.
# Create / edit happen via the Frappe Desk Form (admin-scoped); these endpoints
# feed the portal authoring list + the launch-time template picker.
# ---------------------------------------------------------------------------- #


@frappe.whitelist()
def list_engagement_templates(**kwargs: Any) -> dict[str, Any]:
	"""List active templates for the caller's org, grouped by family.

	``engagement_type`` (``game``/``questionnaire``) filters to one family; omit
	for both. Only ``is_active`` rows are returned to the launch picker.
	"""
	organization = kwargs.get("organization")
	engagement_type = kwargs.get("engagement_type")
	families = (
		(engagement_type,) if engagement_type in eng.TEMPLATE_DOCTYPES else tuple(eng.TEMPLATE_DOCTYPES)
	)
	out: dict[str, list[dict[str, Any]]] = {}
	for family in families:
		doctype = eng.TEMPLATE_DOCTYPES[family]
		rows = _template_rows(doctype, organization, only_active=True)
		out[family] = [eng.template_summary(r, doctype=doctype) for r in rows]
	return {"organization": organization, "templates": out}


@frappe.whitelist()
def get_engagement_template(**kwargs: Any) -> dict[str, Any]:
	"""Return one template's launch config (doctype + name, or by kind).

	The FLO-190 launch picker calls this to fold a saved template's ``kind`` +
	``config`` + a11y default into the console's inline session fields.
	"""
	doctype = kwargs.get("doctype")
	name = kwargs.get("name")
	if not doctype and kwargs.get("kind"):
		doctype = eng.template_doctype_for_kind(str(kwargs.get("kind")))
	if not doctype or not name:
		frappe.throw("doctype (or kind) and name are required")
	row = _template_doc(doctype, name)
	summary = eng.template_summary(row, doctype=doctype)
	launch = eng.template_to_launch_config(row)
	# Merge so the picker gets both the lean list fields + the launch config
	# (rounds / calm_default / kind-specific params) it needs to populate.
	return {**summary, **launch, "template_doctype": doctype}


# ---------------------------------------------------------------------------- #
# Frappe-bound helpers (bench-only; thin I/O around the pure helpers).
# ---------------------------------------------------------------------------- #


def _resolve_session_id(kwargs: dict[str, Any]) -> str:
	"""Resolve the client's session ref to a concrete session id (room-code lookup)."""
	ref = eng.resolve_session_ref(kwargs)
	if "session_id" in ref:
		return ref["session_id"]
	# room_code join: look the code up on the session DocType.
	code = ref["room_code"]
	session_id = frappe.db.get_value("Flock Engagement Session", {"room_code": code}, "name")
	if not session_id:
		frappe.throw(f"No session found for room code {code!r}", frappe.DoesNotExistError)
	return session_id


def _persist_room_code(session_id: str, room_code: str, *, accessibility: bool = False) -> None:
	"""Persist the generated room code (+ a11y default) on the session doc."""
	try:
		values: dict[str, Any] = {"room_code": room_code}
		if accessibility:
			values["accessibility_mode_default"] = 1
		frappe.db.set_value("Flock Engagement Session", session_id, values, update_modified=False)
	except Exception:  # noqa: BLE001 — DocType metadata is best-effort from the view
		frappe.log_error(f"flock_os.engagement_views persist room_code failed: {session_id}")


def _append_session_override(session_id: str, override: dict[str, Any]) -> None:
	"""Append a facilitator override to the session config audit trail."""
	try:
		raw = frappe.db.get_value("Flock Engagement Session", session_id, "config") or "{}"
		config = frappe.parse_json(raw) if isinstance(raw, str) else dict(raw or {})
		overrides = list(config.get("overrides") or [])
		overrides.append(override)
		config["overrides"] = overrides
		frappe.db.set_value(
			"Flock Engagement Session",
			session_id,
			{"config": frappe.as_json(config)},
			update_modified=False,
		)
	except Exception:  # noqa: BLE001 — audit trail is best-effort
		frappe.log_error(f"flock_os.engagement_views manual_override append failed: {session_id}")


def _enrich_lifecycle(session_id: str, res: dict[str, Any]) -> dict[str, Any]:
	"""Add the ``name``/``room_code`` fields the console reads post-lifecycle."""
	out = dict(res)
	out.setdefault("name", session_id)
	out.setdefault("session_id", session_id)
	if not out.get("room_code"):
		try:
			out["room_code"] = frappe.db.get_value("Flock Engagement Session", session_id, "room_code")
		except Exception:  # noqa: BLE001
			out["room_code"] = None
	return out


def _template_rows(doctype: str, organization: str | None, *, only_active: bool) -> list[dict[str, Any]]:
	"""Read template rows for the org-scoped authoring list / launch picker."""
	filters: dict[str, Any] = {}
	if organization:
		filters["organization"] = organization
	if only_active:
		filters["is_active"] = 1
	try:
		return frappe.get_all(
			doctype,
			filters=filters,
			fields=[
				"name",
				"template_name",
				"kind",
				"description",
				"is_active",
				"reviewed",
				"accessibility_mode_default",
				"organization",
				"config",
			],
			order_by="modified desc",
			limit_page_length=200,
		)
	except Exception:  # noqa: BLE001 — degrade to empty before templates exist
		return []


def _template_doc(doctype: str | None, name: str | None) -> dict[str, Any]:
	"""Load one template row as a dict (for template_to_launch_config)."""
	if not doctype or not name:
		frappe.throw("A template doctype + name is required.")
	try:
		doc = frappe.get_doc(doctype, name)
	except frappe.DoesNotExistError:
		frappe.throw(f"Template {name!r} not found", frappe.DoesNotExistError)
	row = doc.as_dict()
	row["doctype"] = doctype
	return row
