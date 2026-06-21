# Flock OS — Fun Attendance template authoring portal page (FLO-190).
#
# Frappe ``www`` page served at ``/engage-templates``. Lists the org's reusable
# Game / Questionnaire Templates, offers admins the create/edit affordance, and
# lets any facilitator launch a session from one (deep-links into ``/engage-host``
# with the template pre-selected so its kind + config drive ``create_session``).
#
# The page only renders server-resolved scope (org + role) + the template rows;
# every mutating launch action still goes through the FLO-11 controller
# (``flock_os.engagement_views.*``), which re-validates scope server-side.
# Authoring create/edit route to the Frappe Desk Form (the DocType perms deny
# Group Leaders write, so the portal honours the same boundary here).

from __future__ import annotations

import frappe
from frappe import _

from flock_os.engagement import (
	TEMPLATE_AUTHOR_ROLES,
	TEMPLATE_DOCTYPES,
	FlockEngagementError,
	build_facilitator_context,
	template_summary,
)


def get_context(context):
	"""Render the template library for the calling facilitator (FLO-190).

	Facilitator-only view: a user without a facilitator role is redirected to
	login (or shown a 403). Authors (Org/Branch Admin + System Manager) additionally
	see the create/edit affordances; Group Leaders see a read-only launch list.
	"""
	user = frappe.session.user
	try:
		ctx = build_facilitator_context(user=user, gateway=_get_gateway())
	except FlockEngagementError as exc:
		if user == "Guest":
			frappe.respond_as_web_page(
				_("Sign in required"),
				_("Please sign in as a facilitator to manage engagement templates."),
				redirect_to="/login?redirect-to=/engage-templates",
			)
		else:
			frappe.respond_as_web_page(
				_("Not permitted"),
				cstr(exc),
				http_status_code=403,
			)
		return context

	organization = ctx.get("organization")
	roles = set(ctx.get("roles") or ())
	can_author = bool(roles & TEMPLATE_AUTHOR_ROLES)

	context.update(ctx)
	context["title"] = _("Fun Attendance — Templates")
	context["no_cache"] = 1
	context["can_author"] = can_author
	context["organization"] = organization
	context["templates_by_family"] = {
		family: [template_summary(row, doctype=doctype) for row in _template_rows(doctype, organization)]
		for family, doctype in TEMPLATE_DOCTYPES.items()
	}
	# Desk routes for full CRUD (admin-scoped via DocType perms).
	context["desk_routes"] = {
		family: f"/app/{doctype.lower().replace(' ', '-')}" for family, doctype in TEMPLATE_DOCTYPES.items()
	}
	context["endpoints_json"] = frappe.as_json(ctx["endpoints"])
	return context


def _get_gateway():
	"""Resolve the production Frappe facilitator gateway (lazy import)."""
	try:
		from flock_os.engagement_frappe import FrappeFacilitatorGateway

		return FrappeFacilitatorGateway()
	except Exception:  # noqa: BLE001 - no bench under CI; degrade gracefully
		from flock_os.engagement import NullFacilitatorGateway

		return NullFacilitatorGateway()


def _template_rows(doctype, organization):
	"""Read template rows for the org (active first; authors see inactive too)."""
	try:
		return frappe.get_all(
			doctype,
			filters={"organization": organization} if organization else {},
			fields=[
				"name",
				"template_name",
				"kind",
				"description",
				"is_active",
				"reviewed",
				"accessibility_mode_default",
				"organization",
			],
			order_by="is_active desc, modified desc",
			limit_page_length=200,
		)
	except Exception:  # noqa: BLE001 — degrade to empty before doctype is synced
		return []


def cstr(value):
	"""Local cstr to avoid an extra import in the render path."""
	return value if isinstance(value, str) else str(value)
