# Flock OS — Fun Attendance facilitator console portal page (FLO-12 / FLO-9 §12).
#
# Frappe ``www`` page served at ``/engage-host``. Resolves the calling
# facilitator's hostable scope via the hexagonal engagement context service
# (``flock_os.engagement``) — the no-cross-subtree-leakage guarantee for the
# console's gathering/branch/group picker. The page only renders the offered
# options; every mutating action (create/open/close/share/override) calls the
# FLO-11 engagement controller REST endpoints, which re-validate scope
# server-side (source of truth).

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cstr

from flock_os.engagement import FlockEngagementError, build_facilitator_context


def get_context(context):
	"""Render the facilitator console for the calling admin (FLO-12).

	Facilitator-only: a user without a facilitator role is redirected to login
	(or shown a 403). The offered branch/group/gathering sets come from
	:func:`build_facilitator_context` and are confined to the facilitator's
	targetable subtree — siblings never appear in the picker.
	"""
	user = frappe.session.user
	try:
		ctx = build_facilitator_context(user=user, gateway=_get_gateway())
	except FlockEngagementError as exc:
		if user == "Guest":
			frappe.respond_as_web_page(
				_("Sign in required"),
				_("Please sign in as a facilitator to run engagement sessions."),
				redirect_to="/login?redirect-to=/engage-host",
			)
		else:
			frappe.respond_as_web_page(
				_("Not permitted"),
				cstr(exc),
				http_status_code=403,
			)
		return context

	context.update(ctx)
	context["title"] = _("Fun Attendance — Facilitator")
	context["no_cache"] = 1
	# Expose scope as JSON for the client picker logic (branch -> groups/gatherings).
	context["groups_by_branch_json"] = frappe.as_json(_groups_by_branch(ctx["groups"]))
	context["gatherings_by_branch_json"] = frappe.as_json(_gatherings_by_branch(ctx["gatherings"]))
	context["kinds_json"] = frappe.as_json(ctx["kinds"])
	context["parity_json"] = frappe.as_json(ctx["parity"])
	context["a11y_defaults_json"] = frappe.as_json(ctx["a11y_defaults"])
	context["endpoints_json"] = frappe.as_json(ctx["endpoints"])
	return context


def _get_gateway():
	"""Resolve the production Frappe engagement gateway (lazy import).

	The Frappe adapter reuses the permission spine's materialized branch
	User-Permissions (ADR §6.2) for scoped facilitator roles. Imported lazily so
	the portal page degrades to the no-bench error path under plain pytest.
	"""
	try:
		from flock_os.engagement_frappe import FrappeFacilitatorGateway

		return FrappeFacilitatorGateway()
	except Exception:  # noqa: BLE001 - no bench under CI; degrade gracefully
		from flock_os.engagement import NullFacilitatorGateway

		return NullFacilitatorGateway()


def _groups_by_branch(groups):
	"""Group the offered groups under their branch for the client filter."""
	out: dict[str, list[dict]] = {}
	for g in groups:
		out.setdefault(g["branch"], []).append({"name": g["name"], "label": g["label"]})
	return out


def _gatherings_by_branch(gatherings):
	"""Group the offered gatherings under their branch for the client filter."""
	out: dict[str, list[dict]] = {}
	for g in gatherings:
		out.setdefault(g["branch"], []).append({"name": g["name"], "label": g["label"]})
	return out
