"""
Admin announcement compose-context — the UI scoping layer (FLO-60, FLO-8 §8).

One concern: resolve the **targetable scope** an admin may address from the
compose UI — the branches + groups they are permitted to target — and guard that
a picked target never escapes that set. The fan-out itself is FLO-57's job
(:func:`flock_os.scheduling.publish_announcement`); this module only feeds the
picker and enforces "no cross-subtree leakage in the UI targeting" (FLO-60 DoD).

Layering (ADR-0001 §2 separation of concerns)::

    Portal page ``get_context`` / Desk form   (Frappe transport, FLO-60)
      -> build_compose_context(user, gateway)   <- THIS module, pure half
            |-> targetable_branches(user)         (the no-leakage boundary)
            |-> groups_for_branches(...)          (scoped group picker)
      -> assert_target_in_context(branch, group)  (don't-trust-the-client guard)
      ...UI then calls flock_os.scheduling.preview_audience / publish_announcement
         (FLO-94) — the backend re-validates scope (source of truth).

Reuse contract — FLO-60 DoD *"No cross-subtree leakage in the UI targeting"*:

* The offered branch set is exactly :meth:`ComposeGateway.targetable_branches`
  — for a ``Flock Branch Admin`` that is their materialized User-Permission
  subtree (the spine's :meth:`list_branch_user_permissions`, ADR §6.2). Sibling
  branches are never offered, so a well-behaved picker cannot even surface them.
* :func:`assert_target_in_context` rejects any picked ``branch``/``group`` not in
  the offered context — the client-side complement to the backend
  :func:`flock_os.scheduling.validate_announcement_scope` cross-branch guard.
  Never trust the client for enforcement (FLO-60 role brief); this is defense in
  depth on top of the backend source of truth.
* Role constants + the global/bypass groupings are imported from
  :mod:`flock_os.permissions` (DRY — one role catalog, no drift).

Transport-agnostic + import-clean without a Frappe site: the
:class:`ComposeGateway` port wraps the only Frappe calls; the project-level
pytest gate pins every rule against an in-memory gateway (same hexagonal
discipline as :mod:`flock_os.scheduling` / :mod:`flock_os.notifications`).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from flock_os.permissions import (
	GLOBAL_BRANCH_ROLES,
	ROLE_BRANCH_ADMIN,
	ROLE_GROUP_LEADER,
	ROLE_ORG_ADMIN,
)

# ---------------------------------------------------------------------------- #
# Select option catalogs — the canonical Python source for the compose UI.
#
# These mirror the `Flock Announcement` DocType select options (subject/body/
# category/priority/audience_role) and the `Flock Announcement Channel` child
# table discriminator (FLO-94). The DocType remains the validation authority;
# these lists are the UI's single source so the picker and the schema agree.
# ---------------------------------------------------------------------------- #
CATEGORY_OPTIONS: tuple[str, ...] = ("General", "Urgent", "Event", "Pastoral", "Administrative", "Other")
PRIORITY_OPTIONS: tuple[str, ...] = ("Low", "Normal", "High", "Critical")
AUDIENCE_ROLE_OPTIONS: tuple[str, ...] = ("Everyone", "Leaders Only", "Admins Only", "Members Only")
CHANNEL_OPTIONS: tuple[str, ...] = ("In-App", "Push", "Email", "SMS")

#: Roles that may open the admin compose surface (FLO-8 §6 / DocType permlevel 1).
ADMIN_COMPOSE_ROLES: frozenset[str] = frozenset({ROLE_ORG_ADMIN, ROLE_BRANCH_ADMIN, ROLE_GROUP_LEADER})


class FlockPortalError(ValueError):
	"""Raised when a compose target escapes the admin's offered scope."""


# ---------------------------------------------------------------------------- #
# ComposeGateway port (hexagonal) — the only Frappe-touching surface.
#
# Production: FrappeComposeGateway (lazy Frappe import). Unit tests:
# RecordingComposeGateway (in flock_os.tests.test_portal). Returns plain data so
# the context builder + target guard stay Frappe-agnostic.
# ---------------------------------------------------------------------------- #
@runtime_checkable
class ComposeGateway(Protocol):
	"""Port: the role + tree-membership reads the compose picker needs.

	``targetable_branches`` is the **no-leakage boundary** (FLO-60 DoD): the exact
	branch set the admin may target. For a ``Flock Branch Admin`` it is their
	materialized User-Permission subtree (ADR §6.2); for a global role
	(``Flock Org Admin`` / ``Flock Auditor``) it is every branch in their org.
	"""

	def get_user_roles(self, user: str) -> tuple[str, ...]:
		"""The Frappe roles on ``user`` (the role catalog, ADR §6.4)."""
		...

	def get_user_organization(self, user: str) -> str | None:
		"""The ``Flock Organization`` the user is rooted in (tenant floor)."""
		...

	def targetable_branches(self, user: str) -> tuple[str, ...]:
		"""Branches the admin may address (subtree for scoped roles; all-org for
		global roles). Siblings are never included — the no-leakage guarantee."""
		...

	def branch_organization(self, branch: str) -> str | None:
		"""The ``Flock Branch.organization`` for ``branch`` (ADR §3 tenant floor)."""
		...

	def branch_label(self, branch: str) -> str | None:
		"""A human label for ``branch`` (falls back to the name)."""
		...

	def groups_for_branches(self, branches: tuple[str, ...]) -> tuple[dict[str, Any], ...]:
		"""Groups rooted in ``branches`` — the scoped group picker options.

		Each row: ``{"name", "branch", "label"}``. Confined to the targetable
		branches so the group picker cannot offer a cross-subtree group either.
		"""
		...


class NullComposeGateway:
	"""Empty gateway — yields no targets (default before wiring)."""

	def get_user_roles(self, user: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def get_user_organization(self, user: str) -> str | None:  # noqa: ARG002
		return None

	def targetable_branches(self, user: str) -> tuple[str, ...]:  # noqa: ARG002
		return ()

	def branch_organization(self, branch: str) -> str | None:  # noqa: ARG002
		return None

	def branch_label(self, branch: str) -> str | None:  # noqa: ARG002
		return None

	def groups_for_branches(self, branches: tuple[str, ...]) -> tuple[dict[str, Any], ...]:  # noqa: ARG002
		return ()


# ---------------------------------------------------------------------------- #
# Pure compose-context builder — assembles the picker options from gateway data.
#
# No I/O. The offered branch/group sets are exactly what the gateway returned, so
# the context is, by construction, confined to the admin's targetable scope.
# ---------------------------------------------------------------------------- #
def _role_set(roles: tuple[str, ...]) -> frozenset[str]:
	return frozenset(roles)


def is_compose_admin(roles: tuple[str, ...]) -> bool:
	"""True iff ``roles`` may open the admin compose surface (FLO-8 §6)."""
	return bool(_role_set(roles) & ADMIN_COMPOSE_ROLES)


def build_compose_context(*, user: str, gateway: ComposeGateway) -> dict[str, Any]:
	"""Build the admin compose picker context (FLO-60 — the no-leakage scope set).

	Returns the option set the portal/Desk UI renders: the admin's targetable
	branches + the groups within them + the select catalogs + the REST endpoints
	the client calls. The branch set is :meth:`targetable_branches` verbatim —
	siblings never appear, so the picker cannot surface a cross-subtree target.

	Raises :class:`FlockPortalError` if ``user`` lacks a compose-admin role.
	"""
	roles = gateway.get_user_roles(user)
	if not is_compose_admin(roles):
		raise FlockPortalError(
			f"User {user!r} lacks an announcement-compose role (needs one of {sorted(ADMIN_COMPOSE_ROLES)})."
		)

	organization = gateway.get_user_organization(user)
	branches = gateway.targetable_branches(user)
	groups = gateway.groups_for_branches(branches)

	branch_rows = [
		{"name": b, "label": gateway.branch_label(b) or b, "organization": gateway.branch_organization(b)}
		for b in branches
	]
	# Group picker is confined to the targetable branches (no cross-subtree group).
	targetable = set(branches)
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
		"groups": group_rows,
		"categories": list(CATEGORY_OPTIONS),
		"priorities": list(PRIORITY_OPTIONS),
		"audience_roles": list(AUDIENCE_ROLE_OPTIONS),
		"channels": list(CHANNEL_OPTIONS),
		# REST surface the client calls (FLO-94 admin controller — FLO-60 wires it).
		"endpoints": {
			"preview_audience": "flock_os.scheduling.preview_audience",
			"publish_announcement": "flock_os.scheduling.publish_announcement",
			"schedule_announcement": "flock_os.scheduling.schedule_announcement",
			"insert": "frappe.client.insert",
		},
		"is_admin": True,
	}


def assert_target_in_context(*, branch: str | None, group: str | None, context: dict[str, Any]) -> None:
	"""Guard: the picked scope must be inside the offered compose context.

	The client-side complement to the backend
	:func:`flock_os.scheduling.validate_announcement_scope` cross-branch guard
	(FLO-60 DoD: "no cross-subtree leakage in the UI targeting"). Never trust the
	client for enforcement — the backend remains the source of truth — but reject
	a forged target here too, so a tampered request never reaches fan-out.

	Raises :class:`FlockPortalError` if ``branch``/``group`` is absent from the
	context's offered branch/group sets.
	"""
	offered_branches = {b["name"] for b in context.get("branches", ())}
	if not branch:
		raise FlockPortalError("A target branch is required.")
	if branch not in offered_branches:
		raise FlockPortalError(
			f"Branch {branch!r} is outside your targetable scope (no cross-subtree leakage)."
		)
	if group:
		offered_groups = {g["name"] for g in context.get("groups", ())}
		if group not in offered_groups:
			raise FlockPortalError(
				f"Group {group!r} is outside your targetable scope (no cross-subtree leakage)."
			)


# ---------------------------------------------------------------------------- #
# Frappe adapter (lazy import — this module stays import-clean under pytest).
# ---------------------------------------------------------------------------- #
class FrappeComposeGateway:
	"""Production adapter: role + tree-membership reads over ``frappe``.

	``targetable_branches`` reuses the permission spine's materialized branch
	User-Permissions (ADR §6.2) for scoped roles, and falls back to every branch
	in the user's organization for global roles (Org Admin / Auditor). The group
	read is branch-filtered so the group picker is scoped to the same subtree.
	"""

	@property
	def _frappe(self):  # type: ignore[no-untyped-def]
		import frappe

		return frappe

	def _user_roles(self, frappe, user: str) -> tuple[str, ...]:  # type: ignore[no-untyped-def]
		# frappe.get_roles returns the inherited + explicit role set.
		return tuple(frappe.get_roles(user) if user else [])

	def get_user_roles(self, user: str) -> tuple[str, ...]:
		return self._user_roles(self._frappe, user)

	def get_user_organization(self, user: str) -> str | None:
		frappe = self._frappe
		if not user:
			return None
		# The member link carries the user's org floor; admins without a member
		# fall back to the single-org case (most Flock OS tenants are single-org).
		member = frappe.db.get_value("Flock Member", {"linked_user": user}, "organization")
		if member:
			return member
		orgs = frappe.get_all("Flock Organization", pluck="name", limit=1)
		return orgs[0] if orgs else None

	def targetable_branches(self, user: str) -> tuple[str, ...]:
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

	def branch_organization(self, branch: str) -> str | None:
		frappe = self._frappe
		return frappe.db.get_value("Flock Branch", branch, "organization") if branch else None

	def branch_label(self, branch: str) -> str | None:
		frappe = self._frappe
		# Flock Branch uses `branch_name` for the display label when present.
		return frappe.db.get_value("Flock Branch", branch, "branch_name") or branch if branch else None

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


# ---------------------------------------------------------------------------- #
# Module-level gateway accessor (lazy; production wires FrappeComposeGateway).
# ---------------------------------------------------------------------------- #
_gateway: ComposeGateway | None = None


def get_gateway() -> ComposeGateway:
	"""Process-wide compose gateway (lazily built, singleton per process)."""
	global _gateway
	if _gateway is None:
		_gateway = FrappeComposeGateway()
	return _gateway


def install_gateway(gateway: ComposeGateway) -> ComposeGateway:
	"""Install a custom gateway (production wiring / tests) and return it."""
	global _gateway
	_gateway = gateway
	return _gateway


# ---------------------------------------------------------------------------- #
# Portal page wiring — the ``get_context`` entry the www page calls.
#
# ``_whitelist`` mirrors :mod:`flock_os.scheduling`: under plain pytest (no
# bench) Frappe is absent, so the decorator is a no-op and the function stays
# callable for unit tests of the transport layer.
# ---------------------------------------------------------------------------- #
def _whitelist():
	try:
		import frappe

		return frappe.whitelist()
	except Exception:  # noqa: BLE001 - no bench under CI; the deco is a no-op

		def _identity(fn):  # type: ignore[no-untyped-def]
			return fn

		return _identity


@_whitelist()
def get_compose_context() -> dict[str, Any]:
	"""GET -> the admin announcement compose picker context (FLO-60 portal page).

	Resolves the calling user's targetable scope (no cross-subtree leakage) and
	returns the option set + REST endpoints the page renders. Raises
	:class:`FlockPortalError` if the user lacks a compose-admin role.
	"""
	import frappe

	return build_compose_context(user=frappe.session.user, gateway=get_gateway())


__all__ = [
	"ADMIN_COMPOSE_ROLES",
	"AUDIENCE_ROLE_OPTIONS",
	"CATEGORY_OPTIONS",
	"CHANNEL_OPTIONS",
	"ComposeGateway",
	"FlockPortalError",
	"FrappeComposeGateway",
	"NullComposeGateway",
	"PRIORITY_OPTIONS",
	"assert_target_in_context",
	"build_compose_context",
	"get_compose_context",
	"get_gateway",
	"install_gateway",
	"is_compose_admin",
]
