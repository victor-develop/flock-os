# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt.

from __future__ import annotations

from typing import Any

import frappe
from frappe.model.document import Document

from flock_os import events, permissions, registrations
from flock_os.traversal import get_service as get_traversal_service

# The roster roles that count as an active group member for scope eligibility
# (FLO-7 §5). Mirrors ``permissions.LEADER_ROSTER_ROLES`` plus the plain
# ``Member`` role — every active roster edge puts a member in ``Own Group`` /
# ``Group Subtree`` scope. ``Visitor`` roster rows are excluded so a visitor
# who merely attended one past gathering is not auto-eligible to register.
_SCOPE_MEMBER_ROLES = ("Leader", "Co-Leader", "Member")


class FlockEventRegistration(Document):
	# Flock Event Registration = one person RSVP'd to one one-time event
	# (FLO-7 §3.5). Top-level transaction (NOT a child table) so it scales to
	# ~15,000 per event and supports the queue-backed bulk path (Phase B,
	# [FLO-79]); mirrors Flock Attendance Record's scale posture. 1:N with a
	# Flock Gathering, unique per (gathering, registrant) so a replay/idempotent
	# retry never double-counts (§5 #4 — the composite UNIQUE index is owned by
	# the v0_2 patch).
	#
	# Row-level reads are scoped by the central ``permission_query_conditions``
	# hook (registered in ``flock_os.permissions.SCOPED_DOCTYPES``): the branch
	# axis rides native User Permissions; the group axis narrows a leader to
	# their led subtree + the self-membership ``registrant`` column. The
	# eligibility + capacity invariants live in :mod:`flock_os.registrations`
	# (pure); this controller enforces them on save + drives the scoped
	# ``@frappe.whitelist()`` actions (FLO-7 §8).

	def validate(self):
		self._denormalize_tenant_floor()
		self._denormalize_registrant_name()
		self._validate_branch_group_binding()
		self._validate_gathering_is_one_time()

	# ------------------------------------------------------------------ #
	# Scoping contract (ADR §3 / §4.2) — mirrors Flock Gathering / approval.
	# ------------------------------------------------------------------ #
	def _denormalize_tenant_floor(self):
		if self.organization:
			return
		if self.branch:
			branch_org = frappe.db.get_value("Flock Branch", self.branch, "organization")
			if branch_org:
				self.organization = branch_org

	def _denormalize_registrant_name(self):
		# Denormalize the registrant's full_name for fast list search/filter
		# (§3.5). Read-only field — kept in sync on every save.
		if self.registrant and not self.registrant_name:
			self.registrant_name = frappe.db.get_value("Flock Member", self.registrant, "full_name") or ""

	def _validate_branch_group_binding(self):
		# ADR §4.2: the registration is branch-bound to its gathering. The
		# branch/group must match the gathering's exact scope anchors so the
		# row-level hooks resolve against the same subtree.
		if not self.branch:
			frappe.throw(
				"Flock Event Registration.branch is required (the row-level perm anchor).",
				frappe.ValidationError,
			)
		gathering_branch, gathering_group = frappe.db.get_value(
			"Flock Gathering", self.gathering, ["branch", "group"]
		) or (None, None)
		if gathering_branch and (gathering_branch != self.branch or gathering_group != self.group):
			frappe.throw(
				"A registration's branch/group must match its gathering's branch/group (FLO-7 §3.5).",
				frappe.ValidationError,
			)

	def _validate_gathering_is_one_time(self):
		# Registrations only exist for one-time events (event_category =
		# One-time). A routine gathering has no registration layer (FLO-7 §2).
		category = frappe.db.get_value("Flock Gathering", self.gathering, "event_category")
		if category and category != "One-time":
			frappe.throw(
				"Registration is only available for one-time events (FLO-7 §2).",
				frappe.ValidationError,
			)


# ---------------------------------------------------------------------------- #
# Frappe registration-scope gateway adapter (ADR-0001 §2 hexagonal boundary).
#
# Implements :class:`flock_os.registrations.RegistrationScopeGateway` over
# Frappe reads. The group/branch subtree reads come from the traversal service
# (FLO-19) — DRY, no copied tree logic; the membership reads are the only
# registration-specific surface. Lazy Frappe use only (this whole module is
# Frappe's natural home and coverage-omitted under the gate).
# ---------------------------------------------------------------------------- #
class FrappeRegistrationScopeGateway:
	"""Production adapter over the traversal service + membership reads (FLO-7 §5)."""

	@property
	def _traversal(self):
		return get_traversal_service()

	@property
	def _frappe(self):
		return frappe

	def member_branch(self, member: str) -> str | None:
		if not member:
			return None
		return self._frappe.db.get_value("Flock Member", member, "branch")

	def member_organization(self, member: str) -> str | None:
		branch = self.member_branch(member)
		if not branch:
			return None
		return self._frappe.db.get_value("Flock Branch", branch, "organization")

	def member_groups(self, member: str) -> tuple[str, ...]:
		# Active roster edges (Leader/Co-Leader/Member) — the ``Own Group`` /
		# ``Group Subtree`` eligible set (§5).
		if not member:
			return ()
		rows = self._frappe.get_all(
			"Flock Group Member",
			filters={"member": member, "role": ["in", list(_SCOPE_MEMBER_ROLES)], "status": "Active"},
			pluck="group",
		)
		seen: dict[str, None] = dict.fromkeys(rows)
		return tuple(seen)

	def group_subtree(self, group: str) -> tuple[str, ...]:
		if not group:
			return ()
		return tuple(row["name"] for row in self._traversal.group_subtree(group))

	def branch_subtree(self, branch: str) -> tuple[str, ...]:
		if not branch:
			return ()
		return tuple(row["name"] for row in self._traversal.branch_subtree(branch))

	def gathering_branch(self, gathering: str) -> str | None:
		if not gathering:
			return None
		return self._frappe.db.get_value("Flock Gathering", gathering, "branch")

	def gathering_organization(self, gathering: str) -> str | None:
		if not gathering:
			return None
		return self._frappe.db.get_value("Flock Gathering", gathering, "organization")

	def gathering_group(self, gathering: str) -> str | None:
		if not gathering:
			return None
		return self._frappe.db.get_value("Flock Gathering", gathering, "group")


# ---------------------------------------------------------------------------- #
# Gathering window/counter helpers (FLO-7 §3.1 / §5).
# ---------------------------------------------------------------------------- #
def _registration_window(gathering: str) -> registrations.RegistrationWindow:
	"""Load the gathering's registration gate inputs (§5 #1)."""
	row = (
		frappe.db.get_value(
			"Flock Gathering",
			gathering,
			[
				"approval_status",
				"registration_scope",
				"registration_opens_on",
				"registration_closes_on",
				"capacity",
				"registered_count",
			],
			as_dict=True,
		)
		or {}
	)
	capacity = row.get("capacity")
	return registrations.RegistrationWindow(
		approval_status=row.get("approval_status") or "Not Required",
		scope=row.get("registration_scope") or registrations.SCOPE_NONE,
		opens_on=row.get("registration_opens_on"),
		closes_on=row.get("registration_closes_on"),
		capacity=int(capacity) if capacity not in (None, "") else None,
		registered_count=int(row.get("registered_count") or 0),
	)


def _scope_for_event(gathering: str, branch: str | None, group: str | None) -> dict:
	"""Row-level scope anchors carried on every registration event (FLO-7 §7)."""
	org = frappe.db.get_value("Flock Gathering", gathering, "organization") if gathering else None
	return {"branch": branch, "group": group, "organization": org}


def _resolve_registrant_member() -> str | None:
	"""The session user's linked ``Flock Member`` (the self-registration path)."""
	return frappe.db.get_value("Flock Member", {"linked_user": frappe.session.user}, "name")


def _authoritative_registration_status(gathering: str) -> str:
	"""Lock the gathering row + decide ``Registered`` vs ``Waitlisted`` (§5 #3).

	The capacity race-correctness backstop: ``SELECT ... FOR UPDATE`` locks the
	gathering row for the rest of this request's transaction, so the
	read-decide is race-free. The verdict reuses the pure
	:func:`registrations.capacity_decision` against the authoritative (locked)
	``registered_count`` + ``registration_capacity``. ``Registered`` iff a seat
	remains; ``Waitlisted`` once full (this issue — waitlist **status** is
	Phase A; only **auto-promotion** on cancel is Phase B, [FLO-79]).

	The counter is **not** bumped here — the caller bumps only after the
	registration row lands (insert-first), so a failed unique-constraint insert
	rolls back without ever moving the counter (no phantom seat). The lock
	spans decide→insert→bump; Frappe commits the whole request atomically (or
	rolls it all back on a failure), so the counter never diverges from the rows.
	"""
	row = frappe.db.sql(
		"SELECT registered_count, registration_capacity FROM `tabFlock Gathering` WHERE name = %s FOR UPDATE",
		values=(gathering,),
		as_dict=True,
	)
	if not row:
		frappe.throw(f"Gathering {gathering!r} not found (cannot decide capacity).", frappe.ValidationError)
	current = int(row[0].get("registered_count") or 0)
	cap_val = row[0].get("registration_capacity")
	cap_int = int(cap_val) if cap_val not in (None, "") else None
	decision = registrations.capacity_decision(capacity=cap_int, registered_count=current)
	return decision.status


def _bump_registered_count(gathering: str, delta: int) -> None:
	"""Adjust ``registered_count`` within the caller's held transaction (§5 #3 / §10).

	``+1`` claims a seat (capacity already verified under the ``FOR UPDATE``
	lock by :func:`_authoritative_registration_status`); ``-1`` releases one on
	cancel (clamped at 0). No commit: the caller's request commits atomically —
	or rolls the claim back on a failed insert — so the counter never diverges
	from the registration rows. Reindex-free, single-statement, the 15k path.
	"""
	if delta > 0:
		frappe.db.sql(
			"UPDATE `tabFlock Gathering` SET registered_count = registered_count + %s WHERE name = %s",
			values=(delta, gathering),
		)
	else:
		frappe.db.sql(
			"UPDATE `tabFlock Gathering` "
			"SET registered_count = GREATEST(registered_count + %s, 0) WHERE name = %s",
			values=(delta, gathering),
		)


# ---------------------------------------------------------------------------- #
# Registration actions (FLO-7 §5 / §8) — scoped ``@frappe.whitelist()`` endpoints.
#
# Each enforces the pure eligibility + window + capacity predicates, the row-
# level scope guard, and emits the canonical registration events. The session
# user is the actor for scope + audit.
# ---------------------------------------------------------------------------- #
@frappe.whitelist()
def get_registration_eligibility(gathering: str, member: str | None = None) -> dict[str, Any]:
	"""GET ``?gathering=<name>[&member=<member>]`` → eligible/ineligible + reason (§8).

	Read-only UI hint — no side effects. Resolves the member from the session
	user when omitted. Returns the window verdict + the scope verdict + a
	human-readable reason the frontend can surface before the leader/member
	clicks Register.
	"""
	if not gathering:
		frappe.throw("gathering is required")
	registrant = member or _resolve_registrant_member()
	if not registrant:
		return {
			"gathering": gathering,
			"eligible": False,
			"reason": "No linked Flock Member for this session — pass ?member=<id>.",
		}
	gateway = FrappeRegistrationScopeGateway()
	window = _registration_window(gathering)
	now = frappe.utils.now()
	window_open = registrations.is_registration_window_open(window, now=now)
	in_scope = registrations.is_member_in_scope(
		member=registrant, gathering=gathering, scope=window.scope, gateway=gateway
	)
	full = registrations.is_capacity_full(capacity=window.capacity, registered_count=window.registered_count)
	eligible = window_open and in_scope and not full
	reason = registrations.eligibility_reason(
		member=registrant, gathering=gathering, scope=window.scope, gateway=gateway
	)
	if window_open and in_scope and full:
		reason = "Event is at capacity — you will be waitlisted."
	if not window_open:
		reason = "Registration is not open for this event (not approved or outside the window)."
	return {
		"gathering": gathering,
		"member": registrant,
		"eligible": eligible,
		"window_open": window_open,
		"in_scope": in_scope,
		"at_capacity": full,
		"scope": window.scope,
		"reason": reason,
	}


@frappe.whitelist()
def register_for_event(
	gathering: str,
	member: str | None = None,
	registered_via: str = registrations.VIA_SELF,
) -> dict[str, Any]:
	"""Register one member for an approved one-time event (§5 #1–#4).

	Enforces, in order: the window is open (approved + in-window + not-closed
	scope), the member is in the confirmed scope (out-of-scope rejected), and
	the unique (gathering, registrant) constraint (idempotent — a replay returns
	the existing row rather than double-counting). Capacity is then decided
	atomically (§5 #3): a seat is claimed via the conditional ``UPDATE``, else
	the row is waitlisted. Emits ``flock.registration.created`` or
	``flock.registration.waitlisted``.

	``member`` defaults to the session user's linked member (self-registration);
	a leader may pass an explicit member to register on behalf (``registered_via
	= Leader``).
	"""
	if not gathering:
		frappe.throw("gathering is required")
	registrant = member or _resolve_registrant_member()
	if not registrant:
		frappe.throw(
			"No linked Flock Member for this session — pass an explicit member=.",
			frappe.ValidationError,
		)
	if registered_via not in registrations.REGISTRATION_VIA:
		frappe.throw(
			f"registered_via must be one of {registrations.REGISTRATION_VIA}.", frappe.ValidationError
		)

	gateway = FrappeRegistrationScopeGateway()
	window = _registration_window(gathering)
	now = frappe.utils.now()

	# §5 #1: window gate (approved + in-window + not-closed scope).
	if not registrations.is_registration_window_open(window, now=now):
		frappe.throw(
			"Registration is not open for this event (not approved or outside the window).",
			frappe.ValidationError,
			title="Registration closed",
		)
	# §5 #2: scope gate — out-of-scope registration is rejected.
	if not registrations.is_member_in_scope(
		member=registrant, gathering=gathering, scope=window.scope, gateway=gateway
	):
		frappe.throw(
			f"Member {registrant!r} is out of scope ({window.scope!r}) for this event (FLO-7 §5).",
			frappe.PermissionError,
			title="Out of registration scope",
		)

	# Idempotency: a replay returns the existing row (unique constraint backstop).
	existing = frappe.db.get_value(
		"Flock Event Registration",
		{"gathering": gathering, "registrant": registrant},
		["name", "registration_status"],
		as_dict=True,
	)
	if existing:
		return {
			"registration": existing.name,
			"status": existing.registration_status,
			"already_registered": True,
		}

	# §5 #3: capacity decision — race-free via the gathering row lock
	# (``SELECT ... FOR UPDATE`` inside ``_authoritative_registration_status``).
	# The verdict is derived from the authoritative locked count + capacity, not
	# a re-read that cannot tell "I claimed it" from "someone else did" — so a
	# losing claimant lands ``Waitlisted`` and the event never over-admits
	# beyond capacity (the precise 15k invariant the lock exists to protect).
	# The counter is bumped AFTER the row lands (below), not here.
	status = _authoritative_registration_status(gathering)

	registrant_name = frappe.db.get_value("Flock Member", registrant, "full_name") or ""
	branch = gateway.gathering_branch(gathering) or ""
	group = gateway.gathering_group(gathering) or ""
	# Insert-first: the unique ``(gathering, registrant)`` index is the
	# idempotency backstop. A failed insert rolls the whole request tx back
	# (releasing the row lock + undoing any counter work), so the counter is
	# never moved for a row that never landed — no phantom seat (the
	# increment-then-insert ordering the §5 #4 invariant requires).
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Flock Event Registration",
				"organization": gateway.gathering_organization(gathering),
				"branch": branch,
				"group": group,
				"gathering": gathering,
				"registrant": registrant,
				"registrant_name": registrant_name,
				"registration_status": status,
				"registered_at": now,
				"registered_via": registered_via,
			}
		)
		doc.insert(ignore_permissions=True)
	except (frappe.exceptions.DuplicateEntryError, frappe.exceptions.UniqueValidationError):
		# Lost the (gathering, registrant) uniqueness race to a concurrent
		# first-timer: the insert failed, the tx (incl. the locked decision
		# above) rolls back, so no seat was claimed. Return their existing row.
		frappe.db.rollback()
		existing = frappe.db.get_value(
			"Flock Event Registration",
			{"gathering": gathering, "registrant": registrant},
			["name", "registration_status"],
			as_dict=True,
		)
		if existing:
			return {
				"registration": existing.name,
				"status": existing.registration_status,
				"already_registered": True,
			}
		raise

	# Seated registrations bump the counter now that the row has landed. The
	# row lock from ``_authoritative_registration_status`` is still held (same
	# request tx, no commit yet), so decide→insert→bump is atomic. A
	# ``Waitlisted`` row never moves the counter.
	if status == registrations.REGISTRATION_REGISTERED:
		_bump_registered_count(gathering, +1)

	scope = _scope_for_event(gathering, branch, group)
	if status == registrations.REGISTRATION_WAITLISTED:
		events.emit(
			events.REGISTRATION_WAITLISTED,
			payload={"gathering": gathering, "member": registrant, "registration": doc.name},
			scope=scope,
		)
	else:
		events.emit(
			events.REGISTRATION_CREATED,
			payload={
				"gathering": gathering,
				"member": registrant,
				"registration": doc.name,
				"status": status,
			},
			scope=scope,
		)
	return {"registration": doc.name, "status": status, "already_registered": False}


@frappe.whitelist()
def cancel_registration(registration_id: str) -> dict[str, Any]:
	"""Cancel a registration → ``Cancelled`` (§5). Releases the seat counter.

	The registrant, their group leader, or a branch/org admin may cancel. A
	``Cancelled`` row frees a capacity seat (``registered_count - 1``); the
	Phase B waitlist auto-promotion ([FLO-79]) is intentionally NOT wired here —
	this issue owns only the MVP cancel. Emits ``flock.registration.cancelled``.
	"""
	if not registration_id:
		frappe.throw("registration_id is required")
	doc: FlockEventRegistration = frappe.get_doc("Flock Event Registration", registration_id)
	registrations.validate_registration_transition(
		from_status=doc.registration_status, to_status=registrations.REGISTRATION_CANCELLED
	)
	_assert_may_act_on_registration(doc)
	was_seated = doc.registration_status == registrations.REGISTRATION_REGISTERED
	doc.registration_status = registrations.REGISTRATION_CANCELLED
	doc.save(ignore_permissions=True)
	if was_seated:
		# Release the seat in the same transaction as the status save (the
		# counter + the row stay consistent). The waitlist auto-promotion is
		# Phase B ([FLO-79]); this issue owns only the seat release.
		_bump_registered_count(doc.gathering, -1)
	events.emit(
		events.REGISTRATION_CANCELLED,
		payload={"gathering": doc.gathering, "member": doc.registrant, "registration": doc.name},
		scope=_scope_for_event(doc.gathering, doc.branch, doc.group),
	)
	return {"registration": doc.name, "status": doc.registration_status}


@frappe.whitelist()
def check_in_registration(registration_id: str, attendance_ref: str | None = None) -> dict[str, Any]:
	"""Bridge a registration to a FLO-6 attendance row → ``Checked-in`` (§5 #5).

	The registrant attends (a FLO-6 ``Flock Attendance Record`` row, or a FLO-9
	game/questionnaire capture that records one). This marks the registration
	``Checked-in``, links the attendance row, and increments the gathering's
	``checked_in_count``. Emits ``flock.registration.checked_in``. Only a leader
	/ branch admin / org admin may check a registrant in (the registrant
	self-checks-in via the FLO-9 fun-attendance path, which calls this).
	"""
	if not registration_id:
		frappe.throw("registration_id is required")
	doc: FlockEventRegistration = frappe.get_doc("Flock Event Registration", registration_id)
	registrations.validate_registration_transition(
		from_status=doc.registration_status, to_status=registrations.REGISTRATION_CHECKED_IN
	)
	_assert_may_check_in(doc)
	doc.registration_status = registrations.REGISTRATION_CHECKED_IN
	if attendance_ref:
		doc.checked_in_attendance = attendance_ref
	doc.save(ignore_permissions=True)
	frappe.db.set_value(
		"Flock Gathering", doc.gathering, "checked_in_count", _bump(doc.gathering, "checked_in_count")
	)
	events.emit(
		events.REGISTRATION_CHECKED_IN,
		payload={
			"gathering": doc.gathering,
			"member": doc.registrant,
			"registration": doc.name,
			"attendance": attendance_ref,
		},
		scope=_scope_for_event(doc.gathering, doc.branch, doc.group),
	)
	return {"registration": doc.name, "status": doc.registration_status}


# ---------------------------------------------------------------------------- #
# Scope guards — who may act on a registration row (§6.1).
# ---------------------------------------------------------------------------- #
def _assert_may_act_on_registration(doc: FlockEventRegistration) -> None:
	"""The registrant, their leader, or an admin may cancel (§6.1).

	Self: the session user is the registrant's linked user. Leader/admin: the
	row's branch is in their allowed set (branch axis) or they hold a bypass
	role. Out-of-branch actors are denied — the cross-branch isolation test
	(FLO-7 §11.8) pins this.
	"""
	user = frappe.session.user
	registrant_user = frappe.db.get_value("Flock Member", doc.registrant, "linked_user")
	if registrant_user and registrant_user == user:
		return
	permissions.assert_branch_scope(doc_branch=doc.branch, user=user, gateway=permissions.get_gateway())


def _assert_may_check_in(doc: FlockEventRegistration) -> None:
	"""A leader / branch admin / org admin checks a registrant in (§6.1).

	The registrant does not self-check-in here — the FLO-9 fun-attendance path
	records the attendance row, then a leader confirms via this action. Falls
	back to the branch-axis scope guard so a cross-branch user is denied.
	"""
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if roles & {
		permissions.ROLE_ORG_ADMIN,
		permissions.ROLE_BRANCH_ADMIN,
		permissions.ROLE_GROUP_LEADER,
	}:
		permissions.assert_branch_scope(doc_branch=doc.branch, user=user, gateway=permissions.get_gateway())
		return
	frappe.throw(
		"Only a group leader, branch admin, or org admin may check in a registration (FLO-7 §6.1).",
		frappe.PermissionError,
	)


def _bump(gathering: str, field: str) -> int:
	"""Read + 1 on a gathering counter field (the check-in bridge, §5 #5)."""
	current = int(frappe.db.get_value("Flock Gathering", gathering, field) or 0)
	return current + 1
