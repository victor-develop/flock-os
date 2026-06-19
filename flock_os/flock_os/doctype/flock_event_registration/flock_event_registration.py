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

	def has_valid_invitation(self, *, gathering: str, member: str) -> bool:
		# Phase B ([FLO-79]): ``Invited Only`` eligibility (§5). A valid
		# invitation is a Sent/Accepted, non-expired ``Flock Event Invitation``
		# for (gathering, member) — either addressed directly (invitee=member)
		# or via a group-subtree invitation (invitee_group in the member's
		# active groups' subtree). Declined/Expired rows are out of scope.
		if not gathering or not member:
			return False
		from flock_os.registrations import is_invitation_expired

		now = self._frappe.utils.now()
		# Direct person invitations.
		direct = self._frappe.db.get_value(
			"Flock Event Invitation",
			{
				"gathering": gathering,
				"invitee": member,
				"status": ["in", ["Sent", "Accepted"]],
				"docstatus": ["<", 2],
			},
			["name", "expires_on"],
			as_dict=True,
		)
		if direct and not is_invitation_expired(direct.get("expires_on"), now=now):
			return True
		# Group-subtree invitations: the member belongs to a group whose
		# subtree intersects an invitation's ``invitee_group`` subtree.
		invitee_groups = self._frappe.get_all(
			"Flock Event Invitation",
			filters={
				"gathering": gathering,
				"invitee_group": ["is", "set"],
				"status": ["in", ["Sent", "Accepted"]],
				"docstatus": ["<", 2],
			},
			pluck="invitee_group",
		)
		if not invitee_groups:
			return False
		member_groups = set(self.member_groups(member))
		if not member_groups:
			return False
		for ig in dict.fromkeys(invitee_groups):
			if not ig:
				continue
			invited_subtree = set(self.group_subtree(ig))
			if member_groups & invited_subtree:
				# Confirm at least one such invitation is not expired (the
				# group-subtree row's own expires_on governs the subtree offer).
				exp = self._frappe.db.get_value(
					"Flock Event Invitation",
					{"gathering": gathering, "invitee_group": ig, "status": ["in", ["Sent", "Accepted"]]},
					"expires_on",
				)
				if not is_invitation_expired(exp, now=now):
					return True
		return False


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


def _scope_for_event(
	gathering: str, branch: str | None, group: str | None, *, organization: str | None = None
) -> dict:
	"""Row-level scope anchors carried on every registration event (FLO-7 §7)."""
	if organization is None:
		organization = (
			frappe.db.get_value("Flock Gathering", gathering, "organization") if gathering else None
		)
	return {"branch": branch, "group": group, "organization": organization}


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
def cancel_registration(registration_id: str) -> dict:
	"""Cancel a registration → ``Cancelled`` (§5). Releases the seat counter and
	atomically promotes the oldest waitlister (§5 #6, Phase B/[FLO-79]).

	The registrant, their group leader, or a branch/org admin may cancel. A
	``Cancelled`` ``Registered`` row frees a capacity seat
	(``registered_count - 1``); if a ``Waitlisted`` row exists, the oldest is
	promoted to ``Registered`` (the seat is re-claimed in the same transaction
	so the counter stays consistent with the rows). Emits
	``flock.registration.cancelled`` and, on a promotion,
	``flock.registration.promoted``.
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
	promoted = None
	if was_seated:
		# Release the seat in the same transaction as the status save (the
		# counter + the row stay consistent).
		_bump_registered_count(doc.gathering, -1)
		# §5 #6 waitlist auto-promotion (Phase B/[FLO-79]): atomically promote
		# the oldest Waitlisted row → Registered, re-claiming the freed seat.
		promoted = _promote_oldest_waitlisted(doc.gathering)
	events.emit(
		events.REGISTRATION_CANCELLED,
		payload={"gathering": doc.gathering, "member": doc.registrant, "registration": doc.name},
		scope=_scope_for_event(doc.gathering, doc.branch, doc.group),
	)
	if promoted:
		events.emit(
			events.REGISTRATION_PROMOTED,
			payload={
				"gathering": doc.gathering,
				"member": promoted["registrant"],
				"registration": promoted["name"],
				"promoted_from": registration_id,
			},
			scope=_scope_for_event(doc.gathering, doc.branch, doc.group),
		)
	return {"registration": doc.name, "status": doc.registration_status, "promoted": promoted}


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
# Waitlist auto-promotion (FLO-7 §5 #6, Phase B/[FLO-79]).
#
# Atomic single-statement promotion of the oldest Waitlisted row → Registered,
# re-claiming the seat freed by a cancellation in the same transaction. The
# selection rule (oldest-first FIFO) is pure in :func:`registrations.select_waitlist_promotion_candidate`;
# this helper owns the race-free atomic UPDATE + counter bump.
# ---------------------------------------------------------------------------- #
def _promote_oldest_waitlisted(gathering: str) -> dict | None:
	"""Atomically promote the oldest ``Waitlisted`` row → ``Registered`` (§5 #6).

	The seat was just freed by the caller's cancellation (the counter already
	decremented), so this re-claims it: a single conditional ``UPDATE`` flips
	the oldest Waitlisted row to Registered (ordered by ``registered_at`` +
	name for deterministic replay), then bumps the counter back. Returns the
	promoted row's identity (for the event payload) or ``None`` if the waitlist
	was empty (no promotion — the seat simply stays free).

	Race-correctness: the conditional ``UPDATE ... ORDER BY ... LIMIT 1`` is one
	statement, so concurrent cancellations each promote a distinct row (or none
	if the waitlist drained). The counter bump shares the caller's transaction.
	"""
	# Read the oldest waitlisted candidate (the pure selector keys the ordering;
	# the read-then-update is safe because the unique promotion is guarded by
	# the ``registration_status`` predicate in the UPDATE itself).
	rows = frappe.db.get_all(
		"Flock Event Registration",
		filters={
			"gathering": gathering,
			"registration_status": registrations.REGISTRATION_WAITLISTED,
		},
		fields=["name", "registrant", "registered_at"],
		order_by="registered_at asc, name asc",
		limit_page_length=1,
	)
	candidates = [
		registrations.WaitlistCandidate(
			name=r["name"],
			gathering=gathering,
			registrant=r["registrant"],
			registered_at=str(r["registered_at"]),
		)
		for r in rows
	]
	choice = registrations.select_waitlist_promotion_candidate(candidates)
	if choice is None:
		return None
	# Conditional UPDATE: only flips if the row is still Waitlisted (guards a
	# concurrent promotion of the same row). One statement = atomic.
	updated = frappe.db.sql(
		"UPDATE `tabFlock Event Registration` "
		"SET registration_status = %s WHERE name = %s AND registration_status = %s",
		values=(
			registrations.REGISTRATION_REGISTERED,
			choice.name,
			registrations.REGISTRATION_WAITLISTED,
		),
	)
	# MariaDB affected rows — if 0, a concurrent job beat us; no promotion.
	affected = frappe.db.row_count() if hasattr(frappe.db, "row_count") else (updated or 0)
	if not affected:
		return None
	_bump_registered_count(gathering, +1)
	return {"name": choice.name, "registrant": choice.registrant}


# ---------------------------------------------------------------------------- #
# Bulk registration (FLO-7 §5, Phase B/[FLO-79]) — the 15k path.
#
# ``register_bulk`` validates scope once (the leader's batch is pre-vetted),
# chunks the member list, and enqueues per-batch inserts on the Frappe/RQ queue.
# The background job runs idempotent inserts (the unique ``(gathering,
# registrant)`` index backstops replays). Mirrors the FLO-6 §5 attendance bulk
# posture: queue-backed, batched, atomic-per-batch.
# ---------------------------------------------------------------------------- #
@frappe.whitelist()
def register_bulk(
	gathering: str,
	members: list[str] | str,
	*,
	batch_size: int = registrations.DEFAULT_BULK_BATCH_SIZE,
) -> dict[str, Any]:
	"""Queue-backed bulk registration for a 15k-scale one-time event (§5).

	Validates the window once (approved + in-window + not-closed scope) and the
	session user's branch authority, then chunks ``members`` and enqueues one
	``flock_os.flock_os.doctype.flock_event_registration.process_bulk_batch``
	background job per batch (Frappe/RQ). Per-row scope is the leader's
	responsibility (the leader pre-vets the roster); the unique constraint
	prevents double-counting on replay. Emits ``flock.registration.bulk_queued``
	with the job/batch totals; per-batch completion emits
	``flock.registration.bulk_completed`` when the last batch lands.
	"""
	if not gathering:
		frappe.throw("gathering is required")
	if isinstance(members, str):
		# Frappe whitelisted lists arrive JSON-encoded; accept the raw string too.
		import json

		members = json.loads(members) if members.startswith("[") else [members]
	if not members:
		frappe.throw("members list is required", frappe.ValidationError)

	# Window gate (approved + in-window). Scope is NOT re-checked per member —
	# the leader pre-vets the roster; the unique index backstops idempotency.
	window = _registration_window(gathering)
	if not registrations.is_registration_window_open(window, now=frappe.utils.now()):
		frappe.throw(
			"Registration is not open for this event (not approved or outside the window).",
			frappe.ValidationError,
			title="Registration closed",
		)
	permissions.assert_branch_scope(
		doc_branch=frappe.db.get_value("Flock Gathering", gathering, "branch"),
		user=frappe.session.user,
		gateway=permissions.get_gateway(),
	)

	batches = registrations.chunk_members(members, batch_size=batch_size)
	branch = frappe.db.get_value("Flock Gathering", gathering, "branch") or ""
	group = frappe.db.get_value("Flock Gathering", gathering, "group") or ""
	org = frappe.db.get_value("Flock Gathering", gathering, "organization")
	total_unique = sum(len(b) for b in batches)
	job_id = f"bulk-{gathering}-{frappe.utils.random_string(8)}"

	for idx, batch in enumerate(batches):
		frappe.enqueue(
			"flock_os.flock_os.doctype.flock_event_registration.process_bulk_batch",
			queue="long",
			job_name=f"{job_id}-batch-{idx}",
			gathering=gathering,
			members=batch,
			registered_via=registrations.VIA_BULK,
			bulk_job_id=job_id,
			batch_index=idx,
			total_batches=len(batches),
		)
	events.emit(
		events.REGISTRATION_BULK_QUEUED,
		payload={
			"gathering": gathering,
			"bulk_job_id": job_id,
			"total_members": total_unique,
			"batches": len(batches),
		},
		scope=_scope_for_event(gathering, branch, group, organization=org),
	)
	return {
		"gathering": gathering,
		"bulk_job_id": job_id,
		"total_members": total_unique,
		"batches": len(batches),
		"status": "queued",
	}


def process_bulk_batch(
	*,
	gathering: str,
	members: list[str],
	registered_via: str,
	bulk_job_id: str,
	batch_index: int,
	total_batches: int,
) -> dict[str, Any]:
	"""Background job: idempotently insert one bulk-registration batch (§5).

	Runs on the RQ ``long`` queue. Per member: skip if a row already exists
	(unique constraint backstop), else decide capacity atomically + insert. The
	unique ``(gathering, registrant)`` index makes this safe under at-least-once
	delivery (a replayed batch is a no-op for already-inserted rows). Emits
	``flock.registration.created``/``waitlisted`` per row, and
	``flock.registration.bulk_completed`` when the last batch finishes.
	"""
	now = frappe.utils.now()
	gateway = FrappeRegistrationScopeGateway()
	branch = gateway.gathering_branch(gathering) or ""
	group = gateway.gathering_group(gathering) or ""
	org = gateway.gathering_organization(gathering)
	registrant_name_cache: dict[str, str] = {}
	created = waitlisted = skipped = 0
	for member in members:
		if not member:
			continue
		existing = frappe.db.get_value(
			"Flock Event Registration",
			{"gathering": gathering, "registrant": member},
			"name",
		)
		if existing:
			skipped += 1
			continue
		status = _authoritative_registration_status(gathering)
		name = registrant_name_cache.get(member)
		if not name:
			name = frappe.db.get_value("Flock Member", member, "full_name") or ""
			registrant_name_cache[member] = name
		try:
			doc = frappe.get_doc(
				{
					"doctype": "Flock Event Registration",
					"organization": org,
					"branch": branch,
					"group": group,
					"gathering": gathering,
					"registrant": member,
					"registrant_name": name,
					"registration_status": status,
					"registered_at": now,
					"registered_via": registered_via,
				}
			)
			doc.insert(ignore_permissions=True)
		except (frappe.exceptions.DuplicateEntryError, frappe.exceptions.UniqueValidationError):
			frappe.db.rollback()
			skipped += 1
			continue
		if status == registrations.REGISTRATION_REGISTERED:
			_bump_registered_count(gathering, +1)
			created += 1
			events.emit(
				events.REGISTRATION_CREATED,
				payload={
					"gathering": gathering,
					"member": member,
					"registration": doc.name,
					"status": status,
				},
				scope=_scope_for_event(gathering, branch, group),
			)
		else:
			waitlisted += 1
			events.emit(
				events.REGISTRATION_WAITLISTED,
				payload={"gathering": gathering, "member": member, "registration": doc.name},
				scope=_scope_for_event(gathering, branch, group),
			)
	# Last batch signals completion so the dashboard/leader know the 15k ingest
	# landed (best-effort; the counter is authoritative regardless).
	if batch_index + 1 >= total_batches:
		events.emit(
			events.REGISTRATION_BULK_COMPLETED,
			payload={"gathering": gathering, "bulk_job_id": bulk_job_id, "batch": batch_index},
			scope=_scope_for_event(gathering, branch, group, organization=org),
		)
	return {"created": created, "waitlisted": waitlisted, "skipped": skipped}


# ---------------------------------------------------------------------------- #
# Dashboards (FLO-7 §10, Phase B/[FLO-79]) — counter-only reads.
#
# Leader/branch-admin views read the atomic ``registered_count`` /
# ``checked_in_count`` counters on the gathering row, never ``COUNT(*)`` over
# 15k registration rows on the fly (§10). The waitlist depth is the one
# exception that needs a counted query — it is small (capped by overflow) and
# not on the gathering row.
# ---------------------------------------------------------------------------- #
@frappe.whitelist()
def get_registration_dashboard(gathering: str) -> dict[str, Any]:
	"""Counter-based registration summary for leader/branch-admin views (§10).

	Reads the gathering's atomic counters (``registered_count`` /
	``checked_in_count``) directly — never a ``COUNT(*)`` over the 15k-row
	registration table. The waitlist depth is a counted query (small, capped by
	overflow; not denormalized). Scoped by the branch-axis guard.
	"""
	if not gathering:
		frappe.throw("gathering is required")
	permissions.assert_branch_scope(
		doc_branch=frappe.db.get_value("Flock Gathering", gathering, "branch"),
		user=frappe.session.user,
		gateway=permissions.get_gateway(),
	)
	row = (
		frappe.db.get_value(
			"Flock Gathering",
			gathering,
			[
				"registered_count",
				"checked_in_count",
				"capacity",
				"registration_scope",
				"approval_status",
				"registration_opens_on",
				"registration_closes_on",
			],
			as_dict=True,
		)
		or {}
	)
	# Waitlist depth: the only counted query (small + not on the gathering row).
	waitlisted = frappe.db.count(
		"Flock Event Registration",
		filters={"gathering": gathering, "registration_status": registrations.REGISTRATION_WAITLISTED},
	)
	capacity = row.get("capacity")
	cap_int = int(capacity) if capacity not in (None, "") else None
	registered = int(row.get("registered_count") or 0)
	checked_in = int(row.get("checked_in_count") or 0)
	return {
		"gathering": gathering,
		"registered_count": registered,
		"checked_in_count": checked_in,
		"waitlisted_count": int(waitlisted or 0),
		"capacity": cap_int,
		"seats_remaining": None if cap_int is None else max(cap_int - registered, 0),
		"registration_scope": row.get("registration_scope") or registrations.SCOPE_NONE,
		"approval_status": row.get("approval_status") or "Not Required",
		"registration_opens_on": row.get("registration_opens_on"),
		"registration_closes_on": row.get("registration_closes_on"),
	}


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
