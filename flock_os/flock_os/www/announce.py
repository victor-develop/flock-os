# Flock OS — admin announcement compose portal page (FLO-60 / FLO-8 §8).
#
# Frappe ``www`` page served at ``/announce``. Resolves the calling admin's
# targetable scope via the hexagonal compose-context service (``flock_os.portal``)
# — the no-cross-subtree-leakage guarantee for the UI targeting. The page itself
# only renders the offered options; every mutating action calls the FLO-94 admin
# controller REST endpoints (preview_audience / publish_announcement /
# schedule_announcement), which re-validate scope server-side (source of truth).

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from flock_os.portal import FlockPortalError, build_compose_context, get_gateway


def get_context(context):
	"""Render the compose picker for the calling admin (FLO-60).

	Admin-only: a user without a compose role is redirected to login. The offered
	branch/group sets come from :func:`build_compose_context` and are confined to
	the admin's targetable subtree — siblings never appear in the picker.
	"""
	user = frappe.session.user
	try:
		ctx = build_compose_context(user=user, gateway=get_gateway())
	except FlockPortalError as exc:
		# Not a compose admin → bounce to login with a message (FLO-8 §6).
		if user == "Guest":
			frappe.respond_as_web_page(
				_("Sign in required"),
				_("Please sign in as an admin to compose announcements."),
				redirect_to="/login",
			)
		else:
			frappe.respond_as_web_page(
				_("Not permitted"),
				cstr(exc),
				http_status_code=403,
			)
		return context

	# Channels become the child-table rows the client builds on send.
	context.update(ctx)
	context["title"] = _("Compose Announcement")
	context["no_cache"] = 1
	# Expose the scope tree as JSON for the client filter logic (branch -> groups).
	context["groups_by_branch_json"] = frappe.as_json(_groups_by_branch(ctx["groups"]))
	return context


def _groups_by_branch(groups):
	"""Group the offered groups under their branch for the client filter."""
	out: dict[str, list[dict]] = {}
	for g in groups:
		out.setdefault(g["branch"], []).append({"name": g["name"], "label": g["label"]})
	return out
