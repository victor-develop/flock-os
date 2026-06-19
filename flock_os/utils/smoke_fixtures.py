"""Idempotent smoke-fixture seeder for the FLO-10 §8 load / WS gate (FLO-112).

Materializes the runtime fixtures that ``load/README.md`` -> "Runtime fixtures"
assumes (tenant org -> branch -> group -> gathering, plus the scoped leader
user) so the §8 WS room-join scope gate resolves ``gathering-smoke`` ->
``branch-smoke`` and ``can_join_event_room`` returns true for the leader. This
is the data half of the WS-broadcast delivery chain ([FLO-53](/FLO/issues/FLO-53)
§8 gate): the ``Flock Gathering`` DocType (FLO-54) already ships in the app, but
without a seeded gathering the resolver raises -> the gate fails closed -> the
smoke records zero broadcasts.

The smoke reuses the site's singleton ``Flock Organization`` (1 per site, FLO-5
§8.1) instead of creating a parallel ``org-smoke`` — that violated the singleton
and made the seeder non-reproducible on a real site (the FLO-114 stale-runbook
bug). ``_resolve_smoke_organization`` attaches branch/group/gathering to the
existing org, creating the singleton only on an empty site.

These are **runtime smoke rows, not canonical catalog fixtures** — intentionally
NOT wired into ``flock_os/patches.txt`` (no ``bench migrate`` seeding), so they
never pollute a production site. Invoke on demand against a bench:

    bench --site <site> execute flock_os.utils.smoke_fixtures.execute
    # or the thin wrapper: scripts/dev/seed-smoke-fixtures.sh

Idempotent: each row is created only when missing, so re-runs are a no-op and
safe across environments. The seed names are pure constants in
:mod:`flock_os.fixtures` so the shape is unit-tested under plain pytest.
"""

from __future__ import annotations

import frappe

from flock_os import fixtures
from flock_os.permissions import ROLE_GROUP_LEADER


def _ensure(dtype: str, name: str, values: dict) -> None:
	"""Insert ``dtype`` / ``name`` from ``values`` iff it does not exist."""
	if frappe.db.exists(dtype, name):
		return
	doc = frappe.get_doc({"doctype": dtype, "name": name, **values})
	doc.insert(ignore_permissions=True)


def _ensure_user_permission(user: str, doctype: str, value: str) -> None:
	"""Idempotently grant ``user`` a single ``User Permission`` (``doctype`` = ``value``)."""
	if frappe.db.exists("User Permission", {"user": user, "allow": doctype, "for_value": value}):
		return
	frappe.get_doc(
		{
			"doctype": "User Permission",
			"user": user,
			"allow": doctype,
			"for_value": value,
			"apply_to_all_doctypes": 1,
		}
	).insert(ignore_permissions=True)


def _ensure_has_role(user: str, role: str) -> None:
	"""Idempotently add ``role`` to ``user`` (Frappe Has Role, owned by the User doc)."""
	if frappe.db.exists("Has Role", {"parent": user, "role": role, "parenttype": "User"}):
		return
	user_doc = frappe.get_doc("User", user)
	user_doc.append("roles", {"role": role})
	user_doc.save(ignore_permissions=True)


def _resolve_smoke_organization() -> str:
	"""Resolve the site's single ``Flock Organization`` (singleton, FLO-5 §8.1).

	The smoke shares the site's real tenant org — a parallel ``org-smoke`` would
	violate the 1-org-per-site invariant (the FLO-114 stale-runbook failure:
	``ValidationError: Only one Flock Organization is allowed per site``).
	Returns the existing org's PK, or — only on a completely empty site — creates
	the singleton (labeled :data:`flock_os.fixtures.FLOCK_SMOKE_ORG_NAME`) and
	returns its PK. Reads/writes bypass the group-axis hook (``db.get_value`` +
	``insert(ignore_permissions=True)``), so this never trips a scoped list query.
	"""
	existing = frappe.db.get_value("Flock Organization", {}, "name")
	if existing:
		return existing
	doc = frappe.get_doc(
		{"doctype": "Flock Organization", "organization_name": fixtures.FLOCK_SMOKE_ORG_NAME}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def execute() -> dict[str, str]:
	"""Create the §8 smoke fixtures if missing. Returns the seeded name map.

	Callable via ``bench execute`` (Frappe resolves ``execute`` by convention) so
	the wrapper script + the runbook both stay one line.
	"""
	# Tenant floor -> branch the leader is scoped to. The org is the site's
	# singleton (reused, not a parallel org-smoke — FLO-114).
	organization = _resolve_smoke_organization()
	_ensure(
		"Flock Branch",
		fixtures.FLOCK_SMOKE_BRANCH,
		{"branch_name": "Smoke Branch", "organization": organization},
	)
	# A branch-bound group the gathering attaches to (Flock Gathering.group is reqd
	# + validate_gathering_branch_binding requires gathering.branch == group.branch).
	_ensure(
		"Flock Group",
		fixtures.FLOCK_SMOKE_GROUP,
		{"group_name": "Smoke Group", "branch": fixtures.FLOCK_SMOKE_BRANCH},
	)
	# The gathering whose broadcast room the smoke joins.
	_ensure(
		"Flock Gathering",
		fixtures.FLOCK_SMOKE_GATHERING,
		{
			"title": fixtures.FLOCK_SMOKE_GATHERING_TITLE,
			"branch": fixtures.FLOCK_SMOKE_BRANCH,
			"group": fixtures.FLOCK_SMOKE_GROUP,
			"starts_on": "2026-01-01 10:00:00",
			"status": "Scheduled",
			"event_category": "Routine",
		},
	)
	# The scoped leader: a Frappe user with a single Flock Branch User Permission =
	# branch-smoke so can_access_branch resolves (load/config.js -> FLOCK_USER).
	if not frappe.db.exists("User", fixtures.FLOCK_SMOKE_USER):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": fixtures.FLOCK_SMOKE_USER,
				"first_name": "Smoke",
				"last_name": "Leader",
				"send_welcome_email": 0,
				"new_password": fixtures.FLOCK_SMOKE_USER_PASSWORD,
			}
		).insert(ignore_permissions=True)
	_ensure_has_role(fixtures.FLOCK_SMOKE_USER, ROLE_GROUP_LEADER)
	_ensure_user_permission(fixtures.FLOCK_SMOKE_USER, "Flock Branch", fixtures.FLOCK_SMOKE_BRANCH)

	frappe.db.commit()
	return {
		"organization": organization,
		"branch": fixtures.FLOCK_SMOKE_BRANCH,
		"group": fixtures.FLOCK_SMOKE_GROUP,
		"gathering": fixtures.FLOCK_SMOKE_GATHERING,
		"user": fixtures.FLOCK_SMOKE_USER,
	}
