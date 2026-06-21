# Flock OS — Fun Attendance player portal page (FLO-12 / FLO-9 §12).
#
# Frappe ``www`` page served at ``/engage``. A player arrives either via a
# share link (``?session=<id>``), a QR code, or by typing a 6-digit room code.
# The page renders the shell + injects the engagement-kind catalog, the JS
# realtime parity contract, the a11y defaults, and the REST endpoints the
# client calls. Business logic stays server-side: every participation / join /
# offline-flush goes through ``flock_os.engagement_views`` (FLO-11), which
# re-validates scope + the session ticket (source of truth).

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from flock_os import engagement


def get_context(context):
	"""Render the player shell + the catalog/parity/a11y contract for the client.

	Open to any authenticated member or visitor (FLO-9 §2 — visitors are pre-
	member ``Flock Member`` rows). The backend ``join`` endpoint issues the
	signed session ticket; this page only renders the shell.
	"""
	user = frappe.session.user
	session_id = cstr(frappe.form_dict.get("session") or frappe.form_dict.get("s") or "").strip()
	room_code = cstr(frappe.form_dict.get("code") or "").strip()

	if user == "Guest":
		# Visitors may still join a live session, but Frappe needs a session; send
		# them through login with a return-to so they land back on the room.
		frappe.respond_as_web_page(
			_("Join the fun"),
			_("Please sign in (or continue as a guest) to join the live session."),
			redirect_to="/login?redirect-to=/engage" + (_room_qs(session_id, room_code)),
		)
		return context

	context.update(
		{
			"title": _("Fun Attendance"),
			"no_cache": 1,
			# The session id / room code the client auto-joins on load (share link).
			"session_id": session_id,
			"room_code": room_code,
			# Single-source contract injected as JSON (engagement.py is canonical).
			"kinds_json": frappe.as_json(engagement.catalog_json()),
			"parity_json": frappe.as_json(engagement.js_parity_contract()),
			"a11y_defaults_json": frappe.as_json(engagement.DEFAULT_A11Y_PROFILE),
			"endpoints_json": frappe.as_json(engagement.ENGAGEMENT_ENDPOINTS),
			"a11y_pref_key": engagement.A11Y_PREF_KEY,
			"min_target_px": engagement.A11Y_MIN_TARGET_PX,
		}
	)
	return context


def _room_qs(session_id: str, room_code: str) -> str:
	"""Build the ``?session=``/``?code=`` querystring to round-trip after login."""
	parts = []
	if session_id:
		parts.append("session=" + frappe.utils.url_quote(session_id))
	if room_code:
		parts.append("code=" + frappe.utils.url_quote(room_code))
	return ("?" + "&".join(parts)) if parts else ""
