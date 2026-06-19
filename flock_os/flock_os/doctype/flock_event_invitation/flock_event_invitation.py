# Copyright (c) 2026, Flock OS and contributors
# For license information, please see license.txt

from __future__ import annotations

import secrets

import frappe
from frappe.model.document import Document

from flock_os import events, permissions, registrations

# Invitation lifecycle (FLO-7 §3.6). Kept here (not in fixtures) because the
# controller + the eligibility gate key on the exact strings.
INVITATION_SENT = "Sent"
INVITATION_ACCEPTED = "Accepted"
INVITATION_DECLINED = "Declined"
INVITATION_EXPIRED = "Expired"

# Token byte length for the link-based RSVP (§3.6). 32 bytes → urlsafe base64
# yields ~43 chars; opaque + unguessable, suitable for login-less visitor RSVP.
_INVITE_TOKEN_NBYTES = 32


class FlockEventInvitation(Document):
	# Flock Event Invitation = the master row for an ``Invited Only`` one-time
	# event (FLO-7 §3.6). One row per (gathering, invitee) — a person OR a
	# group-subtree invitation. Carries the scoping contract (branch+group+
	# organization), an opaque ``invite_token`` for link-based login-less RSVP,
	# and an Expires On that the ``Invited Only`` eligibility gate honors.
	#
	# Row-level reads are scoped by the central ``permission_query_conditions``
	# hook (registered in ``flock_os.permissions.SCOPED_DOCTYPES``): the branch
	# axis rides native User Permissions; the group axis narrows a leader to
	# their led subtree + the self-membership ``invitee`` column. The
	# eligibility predicate (a valid, non-expired invitation) lives in
	# :mod:`flock_os.registrations` (pure) + this controller's gateway adapter.

	def validate(self):
		self._denormalize_tenant_floor()
		self._denormalize_invitee_name()
		self._validate_branch_group_binding()
		self._validate_gathering_is_one_time()
		self._ensure_invite_token()

	# ------------------------------------------------------------------ #
	# Scoping contract (ADR §3 / §4.2) — mirrors the registration/gathering.
	# ------------------------------------------------------------------ #
	def _denormalize_tenant_floor(self):
		if self.organization:
			return
		if self.branch:
			branch_org = frappe.db.get_value("Flock Branch", self.branch, "organization")
			if branch_org:
				self.organization = branch_org

	def _denormalize_invitee_name(self):
		# Denormalized invitee full_name for fast list search/filter (§3.6).
		if self.invitee and not self.invitee_name:
			self.invitee_name = frappe.db.get_value("Flock Member", self.invitee, "full_name") or ""

	def _validate_branch_group_binding(self):
		# ADR §4.2: the invitation is branch-bound to its gathering. The
		# branch/group must match the gathering's exact scope anchors so the
		# row-level hooks resolve against the same subtree.
		if not self.branch:
			frappe.throw(
				"Flock Event Invitation.branch is required (the row-level perm anchor).",
				frappe.ValidationError,
			)
		gathering_branch, gathering_group = frappe.db.get_value(
			"Flock Gathering", self.gathering, ["branch", "group"]
		) or (None, None)
		if gathering_branch and gathering_branch != self.branch:
			frappe.throw(
				"An invitation's branch must match its gathering's branch (FLO-7 §3.6).",
				frappe.ValidationError,
			)
		if self.group and gathering_group and self.group != gathering_group:
			frappe.throw(
				"An invitation's group must match its gathering's group (FLO-7 §3.6).",
				frappe.ValidationError,
			)

	def _validate_gathering_is_one_time(self):
		# Invitations only exist for one-time events (FLO-7 §2).
		category = frappe.db.get_value("Flock Gathering", self.gathering, "event_category")
		if category and category != "One-time":
			frappe.throw(
				"Invitations are only available for one-time events (FLO-7 §2).", frappe.ValidationError
			)

	def _ensure_invite_token(self):
		# Generated once on create (opaque, unguessable) for link-based RSVP
		# (§3.6). ``secrets.token_urlsafe`` is the CSPRNG Frappe-grade path.
		if not self.invite_token:
			self.invite_token = secrets.token_urlsafe(_INVITE_TOKEN_NBYTES)


# ---------------------------------------------------------------------------- #
# Invitation actions (FLO-7 §3.6 / §8) — scoped ``@frappe.whitelist()`` endpoints.
#
# A leader/admin creates invitations (person or group-subtree), the invitee RSVPs
# via the token (login-less for visitors), and the ``Invited Only`` eligibility
# gate honors non-expired ``Sent``/``Accepted`` rows. The canonical event
# publisher emits ``flock.invitation.*`` (§7).
# ---------------------------------------------------------------------------- #
def _resolve_branch_group_for_gathering(gathering: str) -> tuple[str, str]:
	"""Read the gathering's branch + group (the invitation's scope anchors)."""
	row = frappe.db.get_value("Flock Gathering", gathering, ["branch", "group"], as_dict=True) or {}
	return row.get("branch") or "", row.get("group") or ""


def _scope_for_event(gathering: str, branch: str, group: str) -> dict:
	"""Row-level scope anchors carried on every invitation event (FLO-7 §7)."""
	org = frappe.db.get_value("Flock Gathering", gathering, "organization") if gathering else None
	return {"branch": branch, "group": group, "organization": org}


@frappe.whitelist()
def create_invitation(
	gathering: str,
	invitee: str | None = None,
	invitee_group: str | None = None,
	expires_on: str | None = None,
) -> dict:
	"""Create one ``Flock Event Invitation`` for a person or group subtree (§3.6).

	A leader/admin invites a person (``invitee``) or a whole group subtree
	(``invitee_group``) to an ``Invited Only`` one-time event. The opaque
	``invite_token`` is generated on insert for link-based RSVP. Emits
	``flock.invitation.sent``.
	"""
	if not gathering:
		frappe.throw("gathering is required")
	if not invitee and not invitee_group:
		frappe.throw(
			"Either invitee (a person) or invitee_group (a group subtree) is required (FLO-7 §3.6).",
			frappe.ValidationError,
		)
	permissions.assert_branch_scope(
		doc_branch=frappe.db.get_value("Flock Gathering", gathering, "branch"),
		user=frappe.session.user,
		gateway=permissions.get_gateway(),
	)
	branch, group = _resolve_branch_group_for_gathering(gathering)
	doc = frappe.get_doc(
		{
			"doctype": "Flock Event Invitation",
			"organization": frappe.db.get_value("Flock Gathering", gathering, "organization"),
			"branch": branch,
			"group": group or invitee_group or "",
			"gathering": gathering,
			"invitee": invitee or "",
			"invitee_group": invitee_group or "",
			"expires_on": expires_on,
			"status": INVITATION_SENT,
		}
	)
	doc.insert(ignore_permissions=True)
	events.emit(
		events.INVITATION_SENT,
		payload={
			"invitation": doc.name,
			"gathering": gathering,
			"invitee": invitee or "",
			"invitee_group": invitee_group or "",
		},
		scope=_scope_for_event(gathering, branch, group),
	)
	return {"invitation": doc.name, "status": doc.status, "invite_token": doc.invite_token}


@frappe.whitelist()
def rsvp_invitation(invite_token: str, decision: str) -> dict:
	"""RSVP an invitation via its token (link-based, login-less for visitors) (§3.6).

	``decision`` is ``Accepted`` or ``Declined``. Accepting creates/links the
	registration (the ``Invited Only`` scope gate then honors the row). An
	expired invitation cannot be RSVP'd. Emits ``flock.invitation.accepted`` /
	``flock.invitation.declined``.
	"""
	if decision not in (INVITATION_ACCEPTED, INVITATION_DECLINED):
		frappe.throw(
			f"decision must be {INVITATION_ACCEPTED!r} or {INVITATION_DECLINED!r}.", frappe.ValidationError
		)
	doc: FlockEventInvitation = frappe.get_doc("Flock Event Invitation", {"invite_token": invite_token})
	if not doc:
		frappe.throw("Invitation not found for that token.", frappe.ValidationError)
	if registrations.is_invitation_expired(doc.expires_on, now=frappe.utils.now()):
		if doc.status != INVITATION_EXPIRED:
			doc.status = INVITATION_EXPIRED
			doc.save(ignore_permissions=True)
		frappe.throw("This invitation has expired.", frappe.ValidationError)
	if doc.status not in (INVITATION_SENT,):
		frappe.throw(
			f"Invitation is {doc.status!r}; only a Sent invitation can be RSVP'd.",
			frappe.ValidationError,
		)
	doc.status = decision
	doc.save(ignore_permissions=True)
	branch, group = _resolve_branch_group_for_gathering(doc.gathering)
	events.emit(
		events.INVITATION_ACCEPTED if decision == INVITATION_ACCEPTED else events.INVITATION_DECLINED,
		payload={"invitation": doc.name, "gathering": doc.gathering, "invitee": doc.invitee or ""},
		scope=_scope_for_event(doc.gathering, branch, group),
	)
	return {"invitation": doc.name, "status": doc.status}
