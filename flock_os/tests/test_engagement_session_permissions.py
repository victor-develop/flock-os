"""
SEC-A2 — least-privilege DocPerm regression for Flock Engagement Session
([FLO-293](/FLO/issues/FLO-293)).

These pin the **role-level (DocPerm)** layer for engagement sessions at the JSON
layer — the same discipline as the FLO-290 permission-audit suite, scoped to the
SEC-A2 fix from the [FLO-290](/FLO/issues/FLO-290) audit. They run under plain
``pytest`` (no Frappe site / bench): the DocType JSON is parsed directly and the
locked least-privilege contract is asserted, identical harness style to
:mod:`flock_os.tests.test_doctype_schema`.

SEC-A2 context: a plain ``Flock Member`` must NOT be able to spawn an engagement
session — only the four trusted managing roles (Group Leader, Branch Admin, Org
Admin, System Manager) may create. The session document is not submittable, so
``submit``/``cancel``/``amend`` are out of scope; the least-privilege review
confirmed ``Flock Member`` already lacked ``write``/``delete``, so the single
over-grant was ``create`` (now dropped). The row-level *which sessions a member
may read* is enforced by the runtime scoping hook (:mod:`flock_os.permissions`),
covered by :mod:`test_permissions` / :mod:`test_tenant_isolation`; this suite is
the JSON-layer backstop that makes a role with no ``create`` unable to spawn one
via the standard form.

Coverage:
* ``Flock Member`` cannot create an Engagement Session (negative deny) — SEC-A2.
* ``Flock Group Leader`` can create an Engagement Session (positive).
* Only the four trusted managing roles hold ``create``; no unknown role at all.
* ``Flock Member`` is read-only (no create/write/delete/submit), keeps ``read``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DOCTYPE_DIR = Path(__file__).resolve().parent.parent / "flock_os" / "doctype"

#: The four trusted managing roles that may spawn/own engagement sessions (SEC-A2).
CREATE_ROLES: frozenset[str] = frozenset(
	{
		"System Manager",
		"Flock Org Admin",
		"Flock Branch Admin",
		"Flock Group Leader",
	}
)

#: The complete allowed role catalog (mirrors fixtures.FLOCK_ROLES + System Manager).
ALLOWED_ROLES: frozenset[str] = frozenset(
	{
		"Flock Org Admin",
		"Flock Branch Admin",
		"Flock Group Leader",
		"Flock Member",
		"Flock Visitor",
		"Flock Auditor",
		"System Manager",
	}
)

DT = "Flock Engagement Session"


def _slug(name: str) -> str:
	return name.lower().replace(" ", "_")


def _load(name: str) -> dict:
	path = DOCTYPE_DIR / _slug(name) / f"{_slug(name)}.json"
	assert path.exists(), f"Missing DocType JSON for {name}: {path}"
	with path.open() as f:
		return json.load(f)


def _perms(name: str) -> list[dict]:
	"""The ``permissions`` array for ``name`` (role -> capability rows)."""
	return _load(name).get("permissions", [])


def _perm_rows(name: str, *, role: str, permlevel: int = 0) -> list[dict]:
	return [p for p in _perms(name) if p.get("role") == role and p.get("permlevel", 0) == permlevel]


def _has(name: str, role: str, capability: str, *, permlevel: int = 0) -> bool:
	"""True iff ``role`` holds ``capability`` (==1) at ``permlevel`` on ``name``."""
	return any(p.get(capability) == 1 for p in _perm_rows(name, role=role, permlevel=permlevel))


# --------------------------------------------------------------------------- #
# SEC-A2 — Flock Member cannot create; Group Leader can (the core deny/allow).
# --------------------------------------------------------------------------- #


def test_flock_member_cannot_create_engagement_session():
	# Negative deny — the SEC-A2 fix. A plain member must not spawn sessions;
	# only managing roles may. (Previously Flock Member held create=1.)
	assert not _has(DT, "Flock Member", "create"), (
		f"{DT}: Flock Member must not hold create (members do not spawn sessions)."
	)


def test_flock_group_leader_can_create_engagement_session():
	# Positive — the lead actor for sessions may still create. Guards against an
	# over-broad fix that also stripped the leader's create.
	assert _has(DT, "Flock Group Leader", "create"), (
		f"{DT}: Flock Group Leader must hold create (leads engagement sessions)."
	)


# --------------------------------------------------------------------------- #
# Least-privilege scope — only the four trusted managing roles may create,
# and no role outside the allowed catalog holds any capability.
# --------------------------------------------------------------------------- #


def test_only_trusted_roles_can_create_engagement_session():
	creators = {p.get("role") for p in _perms(DT) if p.get("create") == 1}
	assert creators == CREATE_ROLES, (
		f"{DT}: create holders must be exactly the four trusted managing roles, got {creators}."
	)


def test_no_unknown_role_holds_any_capability_on_engagement_session():
	unknown = {p.get("role") for p in _perms(DT)} - ALLOWED_ROLES
	assert not unknown, f"{DT}: grants permissions to unknown roles: {unknown}"


# --------------------------------------------------------------------------- #
# Flock Member is read-only on engagement sessions (no mutation capability),
# while remaining able to read sessions it belongs to (row-scoped at runtime).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cap", ["create", "write", "delete", "submit"])
def test_flock_member_has_no_mutation_capability_on_engagement_session(cap):
	assert not _has(DT, "Flock Member", cap), (
		f"{DT}: Flock Member must not hold {cap} (read-only participant)."
	)


def test_flock_member_can_read_engagement_session():
	# A member remains a participant: they may read sessions they belong to
	# (row-level scoping confines which rows). Read is the only member grant.
	assert _has(DT, "Flock Member", "read")
